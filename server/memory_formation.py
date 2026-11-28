"""Explicit SDK formation over current native Session authority.

This adapter supplies native source authority to the existing SDK executor.
The SDK owns task planning, attempts, accounting, compilation and completion.
"""
from __future__ import annotations

from eidema.comparisons import ColdReflectionExecutor
from eidema.executor import ExecutorLimits, FormationExecutor


def _cold_context(owner, delivery, access, task, max_sources):
    capacity = min(8, max_sources - len(task.inputs))
    clocks = [p.recorded_at_ms for p in task.inputs]
    if capacity <= 0 or not clocks or any(ms is None for ms in clocks):
        return ()
    # Only prior recorded sources, with exact authorities rechecked by attach.
    cutoff = min(clocks)
    # Apply the current Host decision before the SDK caps its historical pool.
    pool = owner.maintenance.formation_context_pool(
        access, task, cutoff, allowed_stream_ids=delivery.formation_streams())
    recent = tuple(pool[:min(4, capacity)])
    if not pool or len(recent) == capacity or owner.store.embedder_info is None:
        return recent
    view = delivery.view(access, purpose="formation")
    parts = []
    remaining = 8192
    for pin in task.inputs:
        owner.require_resources()
        content = view.read(pin, max_chars=owner.limits.source_chars, max_bytes=owner.limits.source_bytes)
        parts.append(content.text[:remaining])
        remaining -= len(parts[-1])
        if remaining <= 0:
            break
    owner.require_resources()
    recalled = owner.retrieval.lookup_formation_sources(access, "\n".join(parts), pool)
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


def create_executor(source_port, access, provider, *, authorized, clock,
                    ready=None, kind="warm", limits=ExecutorLimits(), selector=None,
                    automatic_cold_context=False):
    """Bind a normal native delivery to one explicit warm/cold SDK executor.

    The Host supplies execution authorization and a clock independently of its
    per-Session formation grant. Only a trusted selector can propose adoption.
    """
    if kind not in ("warm", "cold"):
        raise ValueError("invalid_formation_kind")

    source_delivery = source_port.delivery

    def prepare(scope, task, max_sources):
        if (automatic_cold_context and not task.attempts and task.reason == "source_delta"
                and "context" not in task.input_roles):
            context = _cold_context(executor, source_delivery, scope, task, max_sources)
            if context:
                task = executor.maintenance.attach_context(scope, task.task_id, context, max_sources=max_sources)
        return task, source_delivery.view(scope, purpose="formation").read

    def guard(scope, task, contents):
        return source_port.guard_formation(scope, task, contents, limits)

    executor_type = FormationExecutor if kind == "warm" else ColdReflectionExecutor
    executor = executor_type(source_delivery.host._store, None, provider,
        authorized=authorized, clock=clock, ready=ready, limits=limits, selector=selector,
        access_scope=access, prepare_inputs=prepare, source_guard=guard)
    return executor
