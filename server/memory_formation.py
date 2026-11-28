"""Explicit SDK formation over current native Session authority.

This adapter supplies native source authority to the existing SDK executor.
The SDK owns task planning, attempts, accounting, compilation and completion.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from core import session_source_journal as journal
from eidema.candidates import Candidates
from eidema.comparisons import ColdReflectionExecutor
from eidema.executor import ExecutorLimits, FormationExecutor, StepReport, _Stop
from eidema.maintenance import Maintenance
from eidema.models import MemoryConflict, MemoryNotFound, MemoryValidationError
from server.memory_service import MemoryRequestError


class _SessionAuthority:
    """One actual task read-set, checked again before its authority is used."""

    def __init__(self, delivery, access, task, limits):
        self.delivery, self.access, self.task, self.limits = delivery, access, task, limits
        self.view = delivery.view(access, purpose="formation")
        self.contents = {}

    def read(self, pin, **limits):
        if pin not in self.task.inputs:
            raise MemoryRequestError("conflict")
        content = self.view.read(pin, **limits)
        self.contents[pin] = content
        return content

    def _stage(self):
        # Reopen native files outside both the install lock and SQLite writes.
        # Comparing the body also rejects an out-of-owner edit that reused a
        # revision; the normal Session owner already forbids that mutation.
        view = self.delivery.view(self.access, purpose="formation")
        for pin in self.task.inputs:
            if pin not in self.contents or view.read(pin,
                    max_chars=self.limits.source_chars,
                    max_bytes=self.limits.source_bytes) != self.contents[pin]:
                raise MemoryRequestError("conflict")
        signatures = dict(view.signatures(self.task.inputs))
        # Existing registered-output ownership includes known committed and
        # retired ancestry, pending privacy changes, and native parent versions.
        # purpose=None checks that ancestry's availability/scope, and grants no
        # lookup, ordinary, or formation permission to the actual task inputs.
        policy = self.delivery.host._policy
        policy_value = policy.value
        lookup = self.delivery.host._source_lookup
        if lookup is None:
            raise MemoryRequestError("conflict")
        ancestry = self.delivery.api.OutputReadSet(
            outputs=tuple(pin.source for pin in self.task.inputs))
        for path, signature in lookup.require_reads(self.access, ancestry,
                self.delivery.view(self.access), purpose=None):
            if path in signatures and signatures[path] != signature:
                raise MemoryRequestError("conflict")
            signatures[path] = signature
        sessions = tuple(dict.fromkeys(journal.decode(pin.source)[0] for pin in self.task.inputs))
        return tuple(signatures.items()), sessions, policy_value

    @contextmanager
    def guard(self, access):
        if access != self.access:
            raise _Stop("denied", "formation_scope_mismatch")
        native_errors = (OSError, ValueError, MemoryRequestError, MemoryConflict,
                         MemoryNotFound, MemoryValidationError)
        try:
            signatures, sessions, policy_value = self._stage()
        except native_errors:
            raise _Stop("stale", "source_snapshot_changed") from None
        with journal.commit_lock:
            # Only stat/current Host decisions and the original SDK
            # dispatch/commit run here. Provider waits, file reads and
            # quote compilation all remain outside this boundary.
            try:
                self.delivery.require_current(signatures)
                policy = self.delivery.host._policy
                if (not policy.ready or policy.value is not policy_value
                        or not all(self.delivery.formation_allowed(sid) for sid in sessions)):
                    raise MemoryRequestError("conflict")
            except native_errors:
                raise _Stop("stale", "source_snapshot_changed") from None
            # Preserve the SDK's own dispatch/budget/completion errors.
            yield


class _SessionMaintenance(Maintenance):
    def __init__(self, store, owner):
        super().__init__(store)
        self.owner = owner

    def validate_dispatch(self, access, attempt_id, **options):
        with self.owner._native_authority.guard(access):
            # Native staging may outlast the SDK's preceding resource check.
            # Admit at this short dispatch boundary, after reopening sources.
            # Issued calls can still settle after close through commit guards.
            self.owner._allowed()
            self.owner._resource()
            return super().validate_dispatch(access, attempt_id, **options)

    def complete_no_candidates(self, access, attempt_id, **options):
        with self.owner._native_authority.guard(access):
            return super().complete_no_candidates(access, attempt_id, **options)


class _SessionCandidates(Candidates):
    def __init__(self, store, owner):
        super().__init__(store)
        self.owner = owner

    def _commit_guard(self, access):
        # The SDK calls this seam AFTER canonical quote compilation.
        return self.owner._native_authority.guard(access)

    @contextmanager
    def _publish_guard(self, access, candidate_id):
        if access != self.owner._native_access:
            raise _Stop("denied", "formation_scope_mismatch")
        candidate = self.detail(access, candidate_id)
        authority = self.owner._native_authority
        if authority is None or authority.task.task_id != candidate.task_id:
            task = self.owner.maintenance.task(access, candidate.task_id)
            limits = self.owner.limits
            authority = _SessionAuthority(self.owner._source_delivery, access, task, limits)
            # A resumed attempt still owns the originally recorded revisions.
            # Restore only those inputs; the model is not called again.
            try:
                for pin in task.inputs:
                    authority.read(pin, max_chars=limits.source_chars, max_bytes=limits.source_bytes)
            except (OSError, ValueError, MemoryRequestError, MemoryConflict,
                    MemoryNotFound, MemoryValidationError):
                raise _Stop("stale", "source_snapshot_changed") from None
        with authority.guard(access):
            self.owner._allowed()
            self.owner._resource()
            yield


class _SessionExecutor:
    def _step(self, access, *args, **options):
        # SDK step acquired the shared formation lock before entering here.
        # A rejected concurrent step never owns (or clears) this read set.
        try:
            return super()._step(access, *args, **options)
        finally:
            self._native_authority = None

    def _prepare_source_inputs(self, access, task, **planning):
        if (self._automatic_cold_context and not task.attempts and task.reason == "source_delta"
                and "context" not in task.input_roles):
            try:
                context = _cold_context(self, access, task, planning["max_sources"])
                if context:
                    task = self.maintenance.attach_context(access, task.task_id, context,
                                                           max_sources=planning["max_sources"])
            except MemoryConflict as exc:
                if str(exc) == "context_snapshot_stale":
                    raise _Stop("stale", "context_snapshot_stale") from None
                raise
        self._native_authority = _SessionAuthority(
            self._source_delivery, access, task, self.limits)
        return task, self._native_authority.read

    def step(self, access, *args, **options):
        if access != self._native_access:
            return StepReport("denied", "formation_scope_mismatch")
        return super().step(access, *args, **options)

    def resume(self, access, task_id, **options):
        if access != self._native_access:
            return StepReport("denied", "formation_scope_mismatch", task_id=task_id)
        return super().resume(access, task_id, **options)


class _SessionWarmExecutor(_SessionExecutor, FormationExecutor):
    pass


class _SessionColdExecutor(_SessionExecutor, ColdReflectionExecutor):
    pass


def _cold_context(owner, access, task, max_sources):
    capacity = min(8, max_sources - len(task.inputs))
    clocks = [p.recorded_at_ms for p in task.inputs]
    if capacity <= 0 or not clocks or any(ms is None for ms in clocks):
        return ()
    # Only prior recorded sources, with exact authorities rechecked by attach.
    cutoff = min(clocks)
    # Apply the current Host decision before the SDK caps its historical pool.
    pool = owner.maintenance.formation_context_pool(
        access, task, cutoff, allowed_stream_ids=owner._source_delivery.formation_streams())
    recent = tuple(pool[:min(4, capacity)])
    if not pool or len(recent) == capacity or owner.store._embedder is None:
        return recent
    view = owner._source_delivery.view(access, purpose="formation")
    parts = []
    remaining = 8192
    for pin in task.inputs:
        owner._resource()
        content = view.read(pin, max_chars=owner.limits.source_chars, max_bytes=owner.limits.source_bytes)
        parts.append(content.text[:remaining])
        remaining -= len(parts[-1])
        if remaining <= 0:
            break
    owner._resource()
    recalled = owner.retrieval._lookup_formation_sources(access, "\n".join(parts), pool)
    allowed = {pin.source: pin for pin in pool}
    chosen = {pin.source: pin for pin in recent}
    for item in recalled.items:
        if len(chosen) == capacity:
            break
        pin = allowed.get(item.source)
        if (pin is not None and pin.state_version == item.reference.target_version
                and item.semantic_score is not None):
            chosen[pin.source] = pin
    return tuple(chosen.values())


def create_executor(source_delivery, access, provider, *, authorized, clock,
                    ready=None, kind="warm", limits=ExecutorLimits(), selector=None,
                    automatic_cold_context=False):
    """Bind a normal native delivery to one explicit warm/cold SDK executor.

    The Host supplies execution authorization and a clock independently of its
    per-Session formation grant. Only a trusted selector can propose adoption.
    """
    if kind not in ("warm", "cold"):
        raise ValueError("invalid_formation_kind")
    executor_type = _SessionWarmExecutor if kind == "warm" else _SessionColdExecutor
    executor = executor_type(source_delivery.host._store, SimpleNamespace(read=None),
        provider, authorized=authorized, clock=clock, ready=ready, limits=limits, selector=selector)
    executor._source_delivery = source_delivery
    executor._native_access = access
    executor._native_authority = None
    executor._automatic_cold_context = automatic_cold_context
    executor.maintenance = _SessionMaintenance(executor.store, executor)
    executor.candidates = _SessionCandidates(executor.store, executor)
    return executor
