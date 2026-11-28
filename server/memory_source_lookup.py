"""Adapt Amadeus Session snapshots and privacy checks to the Eidema lookup SDK."""
from contextlib import contextmanager

from core import session_source_journal as journal
from eidema import FormationStopped, MemoryConflict, MemoryNotFound, MemoryValidationError
from server.memory_service import MemoryRequestError


class AmadeusSourcePort:
    def __init__(self, host, delivery):
        self.host, self.delivery = host, delivery

    def view(self, access):
        return self.delivery.view(access)

    def authorized(self):
        return self.host._policy.ready

    def require_fresh(self, snapshot):
        self.delivery.require_current(snapshot)

    def verify_reads(self, access, reads, view, *, purpose="lookup", record_watches=None):
        """Check current Session entries even if replay has yet to reach SQLite."""
        outputs = (*reads.outputs, *(item.source for item in reads.sources))
        resolved = self.host._store.resolve_output_ancestry(access, outputs,
            purpose=purpose, candidates=reads.candidates)
        ancestry = resolved.read_set
        pins = (*reads.sources, *ancestry.sources)
        memories = {ref.memory_id for ref in (*reads.memories, *ancestry.memories)}
        identities = {(span.namespace, span.record_key) for span in (
            *outputs, *ancestry.outputs, *(item.source for item in pins))}
        policy = self.host._policy
        if not policy.ready or any(
                memories.intersection(pending["memory_ids"]) or
                any((source["namespace"], source["record_key"]) in identities
                    for source in pending["sources"])
                for pending in policy.value["pending"]):
            raise MemoryRequestError("conflict")
        states = {output: view.output_state(output) for output in resolved.current_outputs}
        signatures = view.signatures(pins, output_states=states.values())
        self.require_fresh(signatures)
        if record_watches is not None:
            record_watches((pin.source, pin.state_version) for pin in pins)
            record_watches((output, position) for output, (_, position) in states.items())
        return signatures

    def _stage_formation(self, access, task, contents, limits):
        # Reopen native files outside both the install lock and SQLite writes.
        # Comparing the body also rejects an out-of-owner edit that reused a
        # revision; the normal Session owner already forbids that mutation.
        view = self.delivery.view(access, purpose="formation")
        limits = dict(max_chars=limits.source_chars, max_bytes=limits.source_bytes)
        if contents is None:
            original = self.delivery.view(access, purpose="formation")
            contents = {pin: original.read(pin, **limits) for pin in task.inputs}
        for pin in task.inputs:
            if pin not in contents or view.read(pin, **limits) != contents[pin]:
                raise MemoryRequestError("conflict")
        signatures = dict(view.signatures(task.inputs))
        # Existing registered-output ownership includes known committed and
        # retired ancestry, pending privacy changes, and native parent versions.
        # purpose=None checks that ancestry's availability/scope, and grants no
        # lookup, ordinary, or formation permission to the actual task inputs.
        policy = self.host._policy
        policy_value = policy.value
        ancestry = self.delivery.api.OutputReadSet(
            outputs=tuple(pin.source for pin in task.inputs))
        for path, signature in self.verify_reads(access, ancestry,
                self.delivery.view(access), purpose=None):
            if path in signatures and signatures[path] != signature:
                raise MemoryRequestError("conflict")
            signatures[path] = signature
        sessions = tuple(dict.fromkeys(journal.decode(pin.source)[0] for pin in task.inputs))
        return tuple(signatures.items()), sessions, policy_value

    @contextmanager
    def guard_formation(self, access, task, contents, limits):
        """Recheck native authorities outside the lock, then seal the commit."""
        native_errors = (OSError, ValueError, MemoryRequestError, MemoryConflict,
                         MemoryNotFound, MemoryValidationError)
        try:
            signatures, sessions, policy_value = self._stage_formation(access, task, contents, limits)
        except native_errors:
            raise FormationStopped("stale", "source_snapshot_changed") from None
        with journal.commit_lock:
            # Only stat/current Host decisions and the original SDK
            # dispatch/commit run here. Provider waits, file reads and
            # quote compilation all remain outside this boundary.
            try:
                self.require_fresh(signatures)
                policy = self.host._policy
                if (not policy.ready or policy.value is not policy_value
                        or not all(self.delivery.formation_allowed(sid) for sid in sessions)):
                    raise MemoryRequestError("conflict")
            except native_errors:
                raise FormationStopped("stale", "source_snapshot_changed") from None
            # Preserve the SDK's own dispatch/budget/completion errors.
            yield
