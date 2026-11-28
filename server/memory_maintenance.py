"""Explicit normal-Host composition of native receipt and SDK maintenance.

There is one Session receiver and one borrowed MemoryStore. This owner stores
only SDK scheduling checkpoints, wakes from completed receipts or SDK timing,
and runs at most one native executor unit on its own serialized thread.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from hashlib import sha256
from itertools import islice
import json
import logging
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

from server.memory_formation import create_executor

logger = logging.getLogger(__name__)


async def bind_maintenance(host, *, enabled=False, access=None,
                           warm_provider=None, cold_provider=None, clock=None,
                           window_id=None, config=None, budget=None,
                           schedule_path=None, context=None, observer=None,
                           resource_ready=None, reformation_enabled=False,
                           warm_adoption_selector=None, cold_adoption_selector=None,
                           automatic_cold_context=False, cold_generation_enabled=True):
    """Mount after normal Host startup, using explicit trusted execution inputs.

    A disabled call is pure. Enabled callers supply scope, providers, clock and
    an existing SDK budget window ID; ``budget`` may explicitly configure it.
    ``context(lane)`` (or a lane-key mapping) nominates bounded, full SourceInput
    pins for optional prior context. No model result grants execution/adoption.
    """
    if type(enabled) is not bool:
        raise ValueError("invalid_maintenance_opt_in")
    if type(automatic_cold_context) is not bool:
        raise ValueError("invalid_cold_context_opt_in")
    if type(cold_generation_enabled) is not bool:
        raise ValueError("invalid_cold_generation_opt_in")
    if type(reformation_enabled) is not bool:
        raise ValueError("invalid_reformation_opt_in")
    if not enabled:
        return None
    if (host._state != "ready" or host._closing or host._store is None
            or host._sources is None or host._source_lookup is None
            or host._sources.host is not host
            or host._maintenance is not None):
        raise ValueError("ready_shared_maintenance_owner_required")
    api = host._api
    if access is not None and (not isinstance(access, api.AccessScope) or access.user_id != host.config.user_id):
        raise ValueError("trusted_maintenance_scope_required")
    if not callable(clock) or not isinstance(window_id, str) or not window_id:
        raise ValueError("maintenance_clock_and_window_required")
    if warm_provider is None or not getattr(warm_provider, "pipeline_version", None):
        raise ValueError("maintenance_warm_provider_required")
    if reformation_enabled and not callable(getattr(warm_provider, "prepare_reformation", None)):
        raise ValueError("maintenance_reformation_provider_required")
    if cold_provider is not None and not getattr(cold_provider, "pipeline_version", None):
        raise ValueError("invalid_maintenance_cold_provider")
    if cold_provider is not None and cold_provider.pipeline_version == warm_provider.pipeline_version:
        raise ValueError("distinct_maintenance_pipeline_versions_required")
    if any(callback is not None and not callable(callback)
           for callback in (warm_adoption_selector, cold_adoption_selector)):
        raise ValueError("invalid_maintenance_adoption_selector")
    if cold_adoption_selector is not None and cold_provider is None:
        raise ValueError("cold_adoption_provider_required")
    config = api.RuntimeConfig(enabled=True) if config is None else config
    if not isinstance(config, api.RuntimeConfig):
        raise ValueError("invalid_maintenance_config")
    if context is not None and not callable(context) and not hasattr(context, "get"):
        raise ValueError("invalid_maintenance_context")
    if resource_ready is not None and not callable(resource_ready):
        raise ValueError("maintenance_resource_probe_required")
    memory_path = Path(host.config.db_path).resolve()
    schedule_path = (memory_path.with_name(memory_path.stem + ".maintenance.sqlite3")
                     if schedule_path is None else Path(schedule_path).resolve())
    if schedule_path == memory_path or schedule_path.parent != memory_path.parent:
        raise ValueError("local_maintenance_schedule_required")
    binding = MaintenanceBinding(host, access, warm_provider, cold_provider,
        clock, window_id, config, schedule_path, context, observer, resource_ready,
        reformation_enabled, warm_adoption_selector=warm_adoption_selector,
        cold_adoption_selector=cold_adoption_selector,
        automatic_cold_context=automatic_cold_context and context is None,
        cold_generation_enabled=cold_generation_enabled)
    host._maintenance = binding
    # Subscribe before mounting. A receipt during construction remains a wake
    # and the startup snapshot is always captured on the same Host loop.
    binding._unsubscribe = host._sources.subscribe_receipts(binding._notify_receipt)
    binding._mount_task = binding.loop.create_task(binding._mount(budget))
    try:
        await asyncio.shield(binding._mount_task)
        return binding
    except asyncio.CancelledError:
        # The Host, rather than a cancelled requester, still owns the mount.
        raise
    except Exception:
        await binding.close()
        raise


class MaintenanceBinding:
    def __init__(self, host, access, warm_provider, cold_provider, clock,
                 window_id, config, schedule_path, context, observer, resource_ready=None,
                 reformation_enabled=False, *, warm_adoption_selector=None,
                 cold_adoption_selector=None, automatic_cold_context=False, cold_generation_enabled=True):
        self.host, self.delivery, self.api = host, host._sources, host._api
        self.access, self.clock, self.window_id = access, clock, window_id
        self.warm_provider, self.cold_provider = warm_provider, cold_provider
        self.config, self.schedule_path, self.context = config, schedule_path, context
        self.observer = observer
        self.resource_ready = resource_ready
        self.reformation_enabled = reformation_enabled
        self.warm_adoption_selector = warm_adoption_selector
        self.cold_adoption_selector = cold_adoption_selector
        self.automatic_cold_context = automatic_cold_context
        self.cold_generation_enabled = cold_generation_enabled
        identity = json.dumps(asdict(access) if access is not None else {"user_id": host.config.user_id},
                              sort_keys=True, separators=(",", ":"))
        self.runtime_id = "native-session-maintenance-v1:" + sha256(identity.encode()).hexdigest()
        self.loop = asyncio.get_running_loop()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="native-maintenance")
        self._wake = asyncio.Event()
        self._foreground_busy = self._closed = False
        self._task = self._mount_task = self._close_task = None
        self._unsubscribe = None
        self.connection = self.runtime = None
        self.lanes = ()
        self._thread = None
        self._state, self._error, self._next_wake = "starting", None, None
        self._ticks = self._notifications = self._receipts = 0
        self._last = None
        self._rolling_budget = None
        self._budget_end_ms = None

    def _emit(self, event, **values):
        # Diagnostics are presentation, never an authority or execution gate.
        if self.observer is not None:
            try:
                self.observer(event, **values)
            except Exception:
                logger.warning("[Memory] maintenance diagnostic observer failed")

    def _ready(self):
        return (not (self._foreground_busy or self._closed or self.host._closing)
                and (self.resource_ready is None or self.resource_ready() is True))

    def _authorized(self):
        # Closing stops dispatch through ready, while issued calls may settle
        # under the native version/permission guard with the Store still alive.
        return (self.host.config.enabled and self.host._store is not None
                and self.host._sources is self.delivery and self.host._policy.ready)

    def _notify(self, reason):
        if not self._closed and not self.host._closing:
            self._notifications += 1
            self._wake.set()
            self._emit("maintenance_notification", reason=reason)

    def _notify_receipt(self):
        """No source body, pin, permission or durable ACK crosses this notice."""
        self._receipts += 1
        self._notify("received")

    def set_foreground_busy(self, busy):
        if type(busy) is not bool:
            raise ValueError("invalid_foreground_busy")
        previous, self._foreground_busy = self._foreground_busy, busy
        if previous and not busy:
            self.host._schedule_index()
            self._notify("foreground_idle")

    def resume(self):
        """Trusted notification after a fake clock advance or repaired fault."""
        self._notify("trusted_recovery")

    async def _run_owned(self, function):
        # Cancellation must not orphan a provider or its SQLite completion.
        return await asyncio.shield(self.loop.run_in_executor(self._pool, function))

    async def _mount(self, budget):
        if callable(budget):
            self._rolling_budget, budget = budget, None

        def create():
            self._thread = threading.get_ident()
            self.connection = sqlite3.connect(self.schedule_path, isolation_level=None)
            if budget is not None:
                self.api.Maintenance(self.host._store).configure_budget(budget)
            self._set_budget_window()
        try:
            await self._run_owned(create)
            if not self._closed and not self.host._closing:
                self._task = self.loop.create_task(self._drive())
                self._notify("startup")
        except Exception as error:
            self._fail(error)
            raise

    def _check_thread(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError("maintenance_schedule_thread_mismatch")

    def _set_budget_window(self):
        if self._rolling_budget is not None:
            window = self._rolling_budget(self.clock())
            if self._budget_end_ms != window.end_ms:
                self.api.Maintenance(self.host._store).configure_budget(window)
                self.window_id, self._budget_end_ms = window.window_id, window.end_ms

    def _lane(self, access, stream, kind, provider):
        selector = self.warm_adoption_selector if kind == "warm" else self.cold_adoption_selector
        authorized = self._authorized if kind == "warm" or self.cold_generation_enabled else lambda: False
        executor = create_executor(self.delivery, access, provider,
            authorized=authorized, clock=self.clock, ready=self._ready, kind=kind, selector=selector,
            automatic_cold_context=kind=="cold" and self.automatic_cold_context)
        max_sources = getattr(provider, "max_sources", 100)
        return self.api.RuntimeLane(kind + ":" + stream, access, stream,
            provider.pipeline_version, executor, context=self._context,
            max_events=max_sources, max_sources=max_sources,
            materialize_raw=False, failure_policy="hold")

    def _refresh(self, streams):
        self._check_thread()
        previous = []
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='eidema_runtime'").fetchone():
            row = self.connection.execute("SELECT binding FROM eidema_runtime WHERE runtime_id=?",
                                          (self.runtime_id,)).fetchone()
            previous = [] if row is None else json.loads(row[0])
        bound_keys = {bound[0] for bound in previous}
        offered = []
        for access, stream in streams:
            # Multi-scope desktop maintenance still runs each SDK lane with
            # the exact scope granted by this Host's Session receipt.
            if access.user_id != self.host.config.user_id or (self.access is not None and access != self.access):
                continue
            offered.append(self._lane(access, stream, "warm", self.warm_provider))
            # Existing checkpoints keep their identity when generation is paused.
            if self.cold_provider is not None and (self.cold_generation_enabled or "cold:" + stream in bound_keys):
                offered.append(self._lane(access, stream, "cold", self.cold_provider))
        by_key = {lane.key: lane for lane in offered}
        if not bound_keys.issubset(by_key):
            raise self.api.MemoryConflict("known_source_generation_missing")
        ordered = [by_key.pop(bound[0]) for bound in previous]
        lanes = tuple(ordered) + tuple(lane for lane in offered if lane.key in by_key)
        if tuple((lane.key, lane.pipeline_version) for lane in lanes) == tuple(
                (lane.key, lane.pipeline_version) for lane in self.lanes):
            return
        if not lanes:
            return
        runtime_class = self.api.MaintenanceRuntime
        if self.resource_ready is not None:
            # The SDK checks resources before it computes coalescing/cadence.
            # Desktop owners have no shared idle wake. Let the original SDK
            # reach its own due/budget verdict before the executor checks the
            # actual resource. No body read or provider call precedes that
            # executor check (or the final native dispatch check).
            sdk_runtime = runtime_class

            class DesktopMaintenanceRuntime(sdk_runtime):
                def __init__(self, *args, **options):
                    super().__init__(*args, **options)
                    self._index_waits = set()

                @staticmethod
                def _gate(lane):
                    authority = SimpleNamespace(executor=SimpleNamespace(
                        authorized=lane.executor.authorized, ready=None))
                    return sdk_runtime._gate(authority)

                def _work_metadata(self, lane, state, *, before_gate=False):
                    result = super()._work_metadata(lane, state, before_gate=before_gate)
                    if self.store._embedder is not None:
                        self._index_waits.clear()
                    resolved, active, waiting = result
                    # Keep the original pre-authority metadata/gate ordering.
                    # After authority, only a task already proven index-only
                    # may yield its priority to ordinary SDK formation policy.
                    if (not before_gate and resolved
                            and (lane.key, resolved[0].task_id) in self._index_waits):
                        return (), active, waiting
                    return result

                def _postwork(self, lane, state, task, delivery, progress):
                    report = super()._postwork(lane, state, task, delivery, progress)
                    step = report.step
                    key = (lane.key, task.task_id)
                    if (self.store._embedder is None and step is not None
                            and step.stage in ("done", "abandoned")
                            and not step.remaining and step.post_pending
                            and key not in self._index_waits):
                        # Semantic/projection work settled; only unavailable
                        # embedding remains. Preserve the durable checkpoint.
                        # Reconsider once, then await sources/SDK deadlines or
                        # the Host's existing embedder-attachment notification.
                        self._index_waits.add(key)
                        return replace(report, wake_at_ms=self.clock())
                    return report

            runtime_class = DesktopMaintenanceRuntime
        runtime = runtime_class(self.connection, self.host._store, lanes,
            runtime_id=self.runtime_id, sync=self._sync, clock=self.clock, config=self.config)
        # SDK initialization verifies exact prior policy/identity bindings and
        # permits only extension. Publish callbacks only after it succeeds.
        runtime.initialize()
        # Only a successfully validated native binding may set this existing
        # SDK policy. Reapply False on reopen, so a prior opt-in cannot leak into
        # a default binding. Cold pipelines and unrelated streams are untouched.
        for lane in lanes:
            if lane.executor._work_class == "warm":
                runtime.maintenance.configure_reformation(lane.access, lane.stream_id,
                    lane.pipeline_version, enabled=self.reformation_enabled)
        self.lanes, self.runtime = lanes, runtime

    def _sync(self, lane, *, limit):
        # The normal SourceDelivery already appended these changes. Do not read
        # Session files, copy text, append changes, or fabricate another cursor.
        _received, committed = self.delivery.accepted_receipt(lane.access, lane.stream_id)
        progress = self.api.Maintenance(self.host._store).progress(
            lane.access, lane.stream_id, lane.pipeline_version)
        return self.api.SourceDelivery(lane.stream_id, committed, progress, 0,
                                       committed == progress.received_position)

    def _context(self, lane):
        if self.context is None:
            return None
        maintenance = self.api.Maintenance(self.host._store)
        progress = maintenance.progress(lane.access, lane.stream_id, lane.pipeline_version)
        reserved = min(lane.max_events, lane.max_sources,
                       progress.received_position - progress.processed_position)
        capacity = max(0, lane.max_sources - reserved)
        if not capacity:
            return None
        offered = (self.context(lane) if callable(self.context)
                   else self.context.get(lane.key))
        if callable(offered):
            offered = offered()
        # Keep the bounded proposal intact. The SDK validates all its pins
        # together with the incremental plan before reservation.
        return None if offered is None else tuple(islice(offered, capacity))

    def _fail(self, error):
        self._state = "failed"
        self._error = {"type": type(error).__name__, "message": str(error)}
        self._emit("maintenance_failure", error=self._error)

    async def _drive(self):
        while not self._closed and not self.host._closing:
            await self._wake.wait()
            self._wake.clear()
            budget_wake = None
            while not self._closed and not self.host._closing:
                # The first complete inventory is essential on reopen: a
                # partial source prefix cannot redefine a durable SDK binding.
                if not self.delivery.receipt_inventory_ready:
                    self._state, self._next_wake = "waiting_receipts", None
                    break
                streams = self.delivery.accepted_streams()
                self._state, self._next_wake = "running", None
                try:
                    def turn():
                        self._refresh(streams)
                        if self.runtime is None:
                            return self.api.RuntimeReport("idle", code="no_events")
                        self._set_budget_window()
                        return self.runtime.tick(window_id=self.window_id)
                    report = await self._run_owned(turn)
                    self._ticks += 1
                    self._last = report
                    fault = report.source_fault
                    self._error = ({"type": "SourceSyncFault", "lane": fault[0], "message": fault[1]}
                                   if fault else None)
                    self._emit("maintenance_tick", report=asdict(report))
                except Exception as error:
                    self._fail(error)
                    break
                if self._closed or self.host._closing:
                    break
                if (report.unit == "postprocessing" and report.step is not None
                        and report.step.index_followup == "busy"):
                    self._state = "waiting_index"
                    settled = self.host._store.index_settlement()
                    self._emit("maintenance_index_wait", already_settled=settled is None)
                    if settled is not None:
                        await asyncio.shield(asyncio.gather(
                            asyncio.wrap_future(settled), return_exceptions=True))
                    await asyncio.sleep(0)
                    continue
                if (self.resource_ready is not None and report.step is not None
                        and report.step.code == "resource_busy"):
                    self._state, self._next_wake = "waiting_resource", None
                    self._emit("maintenance_resource_wait")
                    try:
                        # Source receipts have their own owner. While the
                        # foreground is busy, poll only its in-memory probe.
                        while not self._closed and not self.host._closing:
                            if self._wake.is_set():
                                self._wake.clear()
                                self.runtime.request_scan()
                            if self._ready():
                                break
                            try:
                                await asyncio.wait_for(self._wake.wait(), .05)
                            except TimeoutError:
                                pass
                    except Exception as error:
                        self._fail(error)
                        break
                    continue
                if self._wake.is_set():
                    self._wake.clear()
                    # In particular, SDK _synchronize re-reads progress after
                    # our receipt. An ACK advancing between those reads yields
                    # runtime_delivery_mismatch. Its completed-receipt notice
                    # recovers that sweep; no polling or provider lock is used.
                    if self.runtime is not None:
                        self.runtime.request_scan()
                    continue
                if any(code in ("budget_exhausted", "outside_budget_window")
                       for _, _, code in report.observations):
                    budget_wake = self._budget_end_ms
                if report.scan_remaining:
                    await asyncio.sleep(0)
                    continue
                self._next_wake = min(
                    (wake for wake in (report.wake_at_ms, budget_wake) if wake is not None),
                    default=None)
                budget_wake = None
                if self._next_wake is None:
                    self._state = "waiting_event"
                    break
                try:
                    delay = max(0., (self._next_wake - self.clock()) / 1000)
                except Exception as error:
                    self._fail(error)
                    break
                if delay == 0:
                    await asyncio.sleep(0)
                    continue
                self._state = "waiting_time"
                self._emit("maintenance_wait_until", wake_at_ms=self._next_wake)
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except TimeoutError:
                    self._emit("maintenance_timer_due", wake_at_ms=self._next_wake)
                if self._wake.is_set() and self.runtime is not None:
                    self.runtime.request_scan()
                self._wake.clear()

    def status(self):
        """Mechanical metadata only; no generated/source bodies are fetched."""
        return dict(state=self._state, error=self._error, ticks=self._ticks,
            notifications=self._notifications, receipts=self._receipts,
            next_wake_at_ms=self._next_wake,
            last_report=asdict(self._last) if self._last is not None else None)

    def stop(self):
        """Stop new dispatch immediately; close later joins issued settlement."""
        self._closed = True
        self._wake.set()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    async def close(self):
        if self._close_task is None:
            self.stop()
            async def finish():
                try:
                    if self._mount_task is not None:
                        await asyncio.gather(self._mount_task, return_exceptions=True)
                    if self._task is not None:
                        await asyncio.gather(self._task, return_exceptions=True)
                    if self.connection is not None:
                        await self._run_owned(self.connection.close)
                finally:
                    self._pool.shutdown(wait=True)
                    self._state, self._next_wake = "closed", None
                    if self.host._maintenance is self:
                        self.host._maintenance = None
                    self._emit("maintenance_closed", store_still_open=self.host._store is not None)
            self._close_task = self.loop.create_task(finish())
        await asyncio.shield(self._close_task)
