"""Composition boundary for the C1-C8 Host-owned Continuity Runtime."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from core.continuity.clock import RealityClock
from core.continuity.memory_extractor import (
    DeterministicMemoryExtractor,
    MemoryExtractor,
    parse_explicit_memory_directive,
)
from core.continuity.memory_resolver import MemoryResolver
from core.continuity.memory_retriever import MemoryRetriever
from core.continuity.archive_recall import ArchiveRecallResult, SessionArchiveSearcher
from core.continuity.context_renderer import render_continuity_grounding
from core.continuity.embedding_runtime import MemorySemanticIndex
from core.continuity.retrieval_policy import ContinuityRetrievalPolicy
from core.continuity.text_excerpt import (
    DeterministicTextCompressor,
    TextCompressor,
    default_text_compressor,
)
from core.continuity.relationship import RelationshipRuntime
from core.continuity.relationship_policy import RelationshipPolicy
from core.continuity.life_policy import LifePolicy
from core.continuity.life_scheduler import CharacterLifeProfile
from core.continuity.life_runtime import CharacterLifeRuntime
from core.continuity.models import (
    ConsolidationStatus,
    ContinuityGrounding,
    MemoryDirectiveAction,
    TurnEvidence,
)
from core.continuity.store import ContinuityStore
from core.continuity.topic_mute import topic_is_raised_by_query
from core.continuity.turn_ingest import load_turn_evidence, storage_session_id

logger = logging.getLogger(__name__)

_CHAT_USER = "chat.user"
_CHAT_COMPLETE = "chat.complete"
_WORK_UPDATED = "work.updated"


class ContinuityService:
    """Host-owned continuity service.

    C1 owns RealityClock observations; C2-C4 own durable memory/retrieval/
    retention/Work projection; C5-C6 add derived Relationship/Life state; C7
    adds Host controls; C8 adds bounded historical archive fallback. SQLite
    remains Host authority. Normal consolidation remains post-chat; neither
    recall nor relationship state gains Provider/AUIP/Work execution authority.
    """

    def __init__(
        self,
        store: ContinuityStore,
        *,
        clock: RealityClock | None = None,
        memory_extractor: MemoryExtractor | None = None,
        memory_resolver: MemoryResolver | None = None,
        memory_retriever: MemoryRetriever | None = None,
        retrieval_policy: ContinuityRetrievalPolicy | None = None,
        archive_searcher: SessionArchiveSearcher | None = None,
        text_compressor: TextCompressor | None = None,
        relationship_runtime: RelationshipRuntime | None = None,
        relationship_policy: RelationshipPolicy | None = None,
        life_runtime: CharacterLifeRuntime | None = None,
        life_policy: LifePolicy | None = None,
        continuity_enabled: bool = True,
        memory_enabled: bool = True,
        relationship_enabled: bool = True,
        relationship_live_enabled: bool = False,
        life_enabled: bool = False,
        life_live_enabled: bool = False,
        archive_recall_enabled: bool = True,
        session_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock or RealityClock(store)
        self.text_compressor = text_compressor or DeterministicTextCompressor()
        self.memory_extractor = memory_extractor or DeterministicMemoryExtractor(
            text_compressor=self.text_compressor
        )
        self.memory_resolver = memory_resolver or MemoryResolver()
        self.retrieval_policy = retrieval_policy or ContinuityRetrievalPolicy()
        self.memory_retriever = memory_retriever or MemoryRetriever(
            store, policy=self.retrieval_policy
        )
        self.session_dir = Path(session_dir).expanduser() if session_dir is not None else None
        self.archive_searcher = archive_searcher or SessionArchiveSearcher(
            store,
            self.session_dir,
            policy=self.retrieval_policy,
            text_compressor=self.text_compressor,
        )
        self.relationship_policy = relationship_policy or RelationshipPolicy()
        self.relationship_runtime = relationship_runtime or RelationshipRuntime(
            store, policy=self.relationship_policy
        )
        self.life_policy = life_policy or LifePolicy()
        self.life_runtime = life_runtime
        self.continuity_enabled = bool(continuity_enabled)
        self.memory_enabled = bool(memory_enabled)
        self.relationship_enabled = bool(relationship_enabled)
        self.relationship_live_enabled = bool(relationship_live_enabled)
        self.life_enabled = bool(life_enabled)
        self.life_live_enabled = bool(life_live_enabled)
        self.archive_recall_enabled = bool(archive_recall_enabled) and bool(self.retrieval_policy.archive_recall_enabled)
        self._event_bus: Any = None
        self._started = False
        self._closed = False
        self._clock_tail: asyncio.Task[None] | None = None
        self._registration_tail: asyncio.Task[None] | None = None
        self._memory_queue: asyncio.Queue[TurnEvidence | None] = asyncio.Queue()
        self._memory_worker: asyncio.Task[None] | None = None
        self._recovery_task: asyncio.Task[None] | None = None
        self._pending_turns: dict[tuple[str, str], TurnEvidence] = {}
        self._queued_turns: set[tuple[str, str]] = set()
        self._aux_tasks: set[asyncio.Task[Any]] = set()
        self._maintenance_interval_seconds = max(0.0, self.retrieval_policy.retention_maintenance_interval_hours * 3600.0)
        self._maintenance_task: asyncio.Task[None] | None = None
        self._life_rollover_task: asyncio.Task[None] | None = None
        self._retrieval_traces: deque[dict[str, Any]] = deque(maxlen=32)

    @classmethod
    def from_project_root(cls, project_root: str | os.PathLike[str]) -> "ContinuityService":
        root = Path(project_root)
        configured = str(os.environ.get("AMADEUS_CONTINUITY_DB_PATH") or "").strip()
        db_path = (
            Path(configured).expanduser()
            if configured
            else root / "runtime" / "continuity.sqlite3"
        )
        configured_sessions = str(os.environ.get("AMADEUS_SESSION_DIR") or "").strip()
        session_dir = (
            Path(configured_sessions).expanduser()
            if configured_sessions
            else root / "sessions"
        )
        from config import settings

        store = ContinuityStore(db_path)
        policy = ContinuityRetrievalPolicy.load(root / "config" / "continuity_policy.json")
        semantic = (
            MemorySemanticIndex(store, model_id=policy.semantic_model)
            if bool(getattr(settings, "CONTINUITY_SEMANTIC_RECALL_ENABLED", False))
            else None
        )
        retriever = MemoryRetriever(store, policy=policy, semantic_searcher=semantic)
        relationship_policy = RelationshipPolicy.load(root / "config" / "continuity_policy.json")
        life_policy = LifePolicy.load(root / "config" / "continuity_policy.json")
        life_profile = CharacterLifeProfile.load(root / life_policy.profile_relative_path)
        life_runtime = CharacterLifeRuntime(store, profile=life_profile, policy=life_policy)
        return cls(
            store,
            session_dir=session_dir,
            memory_retriever=retriever,
            retrieval_policy=policy,
            text_compressor=default_text_compressor(),
            relationship_policy=relationship_policy,
            life_runtime=life_runtime,
            life_policy=life_policy,
            continuity_enabled=bool(getattr(settings, "CONTINUITY_ENABLED", True)),
            memory_enabled=bool(getattr(settings, "CONTINUITY_MEMORY_ENABLED", True)),
            relationship_enabled=bool(getattr(settings, "CONTINUITY_RELATIONSHIP_ENABLED", True)),
            relationship_live_enabled=bool(getattr(settings, "CONTINUITY_RELATIONSHIP_LIVE_ENABLED", False)),
            life_enabled=bool(getattr(settings, "CONTINUITY_LIFE_ENABLED", True)),
            life_live_enabled=bool(getattr(settings, "CONTINUITY_LIFE_LIVE_ENABLED", False)),
            archive_recall_enabled=bool(getattr(settings, "CONTINUITY_ARCHIVE_RECALL_ENABLED", True)),
        )

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("continuity service is closed")
        if self._started:
            return
        self._started = True
        if not self.continuity_enabled:
            return
        self.clock.mark_application_started()
        if self.life_enabled and self.life_runtime is not None:
            try:
                self.life_runtime.ensure_today(self.clock.current_time())
                self._schedule_life_rollover()
            except Exception:
                logger.exception("continuity Character Life startup ensure failed")
        if self.memory_enabled:
            self._ensure_memory_worker()
            self._schedule_recovery()
            self._schedule_maintenance()

    def bind_event_bus(self, event_bus: Any) -> None:
        if self._closed:
            raise RuntimeError("continuity service is closed")
        if self._event_bus is event_bus:
            return
        if self._event_bus is not None:
            self.unbind_event_bus()
        event_bus.on(_CHAT_USER, self._on_chat_user)
        event_bus.on(_CHAT_COMPLETE, self._on_chat_complete)
        event_bus.on(_WORK_UPDATED, self._on_work_updated)
        self._event_bus = event_bus

    def unbind_event_bus(self) -> None:
        event_bus = self._event_bus
        if event_bus is None:
            return
        event_bus.off(_CHAT_USER, self._on_chat_user)
        event_bus.off(_CHAT_COMPLETE, self._on_chat_complete)
        event_bus.off(_WORK_UPDATED, self._on_work_updated)
        self._event_bus = None


    def _spawn_aux_task(self, coro, *, name: str) -> None:
        if self._closed:
            return
        task = asyncio.create_task(coro, name=name)
        self._aux_tasks.add(task)
        task.add_done_callback(self._aux_tasks.discard)

    @staticmethod
    def _normalized_work_events(payload: dict[str, Any]) -> list[tuple[dict[str, Any], float | None]]:
        """Convert the Host Work Ledger snapshot into per-item lifecycle events."""
        work = payload.get("work") if isinstance(payload.get("work"), dict) else payload
        reason = str(payload.get("reason") or "").lower().strip()
        items = work.get("items") if isinstance(work, dict) else None
        if not isinstance(items, list):
            items = [work] if isinstance(work, dict) else []
        result: list[tuple[dict[str, Any], float | None]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            work_id = str(item.get("workItemId") or item.get("work_item_id") or item.get("id") or "").strip()
            if not work_id:
                continue
            state = str(item.get("state") or "").lower().strip()
            execution = str(item.get("execution") or item.get("status") or "").lower().strip()
            if "reopen" in reason:
                status = "reopened"
            elif state in {"accepted", "archived", "closed"}:
                status = state
            elif execution in {"succeeded", "failed", "cancelled", "canceled", "orphaned"}:
                status = execution
            elif execution in {"queued", "running"}:
                status = execution
            else:
                status = state or execution
            if not status:
                continue
            try:
                observed = float(item.get("updatedAtEpoch"))
            except (TypeError, ValueError):
                observed = None
            material = f"{work_id}\0{observed!r}\0{state}\0{execution}\0{status}".encode("utf-8")
            event_id = hashlib.sha256(material).hexdigest()
            result.append((
                {
                    "event_id": event_id,
                    "work": {"id": work_id, "status": status},
                },
                observed,
            ))
        return result

    async def _on_work_updated(self, _method: str, payload: dict[str, Any]) -> None:
        """Consume Host Work Ledger lifecycle events without owning Work state."""
        if self._closed or not self.continuity_enabled or not self.memory_enabled:
            return
        try:
            for normalized, observed in self._normalized_work_events(payload):
                await asyncio.to_thread(
                    self.store.apply_work_update,
                    normalized,
                    observed_at=observed,
                )
        except Exception:
            logger.exception("continuity work lifecycle projection failed")

    def _schedule_life_rollover(self) -> None:
        if self._closed or not self.life_enabled or self.life_runtime is None:
            return
        if self._life_rollover_task is not None and not self._life_rollover_task.done():
            return
        self._life_rollover_task = asyncio.create_task(
            self._life_rollover_loop(), name="continuity-life-rollover"
        )

    async def _life_rollover_loop(self) -> None:
        from datetime import datetime, time as datetime_time, timedelta

        try:
            while not self._closed and self.life_enabled and self.life_runtime is not None:
                now = self.clock.current_time()
                tomorrow = now.date() + timedelta(days=1)
                target = datetime.combine(tomorrow, datetime_time.min, tzinfo=now.tzinfo)
                delay = max(1.0, (target - now).total_seconds())
                await asyncio.sleep(delay)
                if self._closed:
                    return
                await asyncio.to_thread(self.life_runtime.ensure_today, self.clock.current_time())
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("continuity Character Life midnight rollover failed")

    def _schedule_maintenance(self) -> None:
        if self._closed or self._maintenance_interval_seconds <= 0:
            return
        if self._maintenance_task is not None and not self._maintenance_task.done():
            return
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(),
            name="continuity-maintenance",
        )

    async def _maintenance_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._maintenance_interval_seconds)
                if self._closed:
                    return
                try:
                    await self.run_maintenance(reason="scheduled")
                except Exception:
                    logger.exception("scheduled continuity maintenance failed")
        except asyncio.CancelledError:
            return

    async def run_maintenance(self, *, now: float | None = None, reason: str = "manual") -> dict[str, Any]:
        """Run bounded C4 maintenance off the event-loop thread."""
        if self._closed or not self.continuity_enabled or not self.memory_enabled:
            return {"scanned": 0, "disabled": True}
        return await asyncio.to_thread(
            self.store.run_retention_maintenance,
            now=now,
            half_life_days=self.retrieval_policy.retention_half_life_days,
            cold_after_days=self.retrieval_policy.retention_cold_after_days,
            archive_after_days=self.retrieval_policy.retention_archive_after_days,
            max_hot_memories=self.retrieval_policy.retention_max_hot_memories,
            hot_score_threshold=self.retrieval_policy.retention_hot_score_threshold,
            cold_score_threshold=self.retrieval_policy.retention_cold_score_threshold,
            archive_score_threshold=self.retrieval_policy.retention_archive_score_threshold,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # C1 clock tail
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # C7 Host-owned UI / diagnostics / user-control boundary
    # ------------------------------------------------------------------
    @staticmethod
    def _c7_memory_payload(record) -> dict[str, Any]:
        return {
            "id": record.id,
            "memory_key": record.memory_key,
            "scope": record.scope,
            "kind": record.kind.value,
            "summary": record.summary,
            "pinned": bool(record.pinned),
            "retention_tier": record.retention_tier.value,
            "retention_score": round(float(record.retention_score), 6),
            "priority_class": record.priority_class.value,
            "importance": round(float(record.importance), 6),
            "confidence": round(float(record.confidence), 6),
            "source_type": record.source_type.value,
            "created_at": float(record.created_at),
            "updated_at": float(record.updated_at),
            "last_mentioned_at": float(record.last_mentioned_at),
        }

    def c7_list_memories(
        self,
        *,
        scope: str | None = None,
        tier: str | None = None,
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        """Return bounded user-visible active memory summaries through Host authority."""

        bounded_limit = max(1, min(int(limit), 500))
        if tier:
            from core.continuity.models import RetentionTier

            records = self.store.list_memories_by_tier(
                RetentionTier(str(tier).strip().lower()),
                scope=scope,
            )
        else:
            records = self.store.list_active_memories(scope=scope)
            records.sort(
                key=lambda record: (
                    not record.pinned,
                    -float(record.last_mentioned_at),
                    record.memory_key,
                )
            )
        return [self._c7_memory_payload(record) for record in records[:bounded_limit]]

    def c7_set_memory_pinned(self, memory_id: str, pinned: bool) -> dict[str, Any]:
        record = self.store.set_memory_pinned(memory_id, pinned)
        if record is None:
            raise ValueError("active memory not found")
        return self._c7_memory_payload(record)

    def c7_forget_memory(self, memory_id: str) -> dict[str, Any]:
        records = self.store.get_memories_by_ids((str(memory_id or ""),))
        record = records[0] if records else None
        if record is None:
            raise ValueError("active memory not found")
        deleted = self.store.forget_memory(
            record.memory_key,
            scope=record.scope,
            kind=record.kind,
            reason="c7_user_control",
            relationship_policy=self.relationship_policy,
        )
        return {
            "deleted": int(deleted),
            "memory_id": record.id,
            "memory_key": record.memory_key,
            "scope": record.scope,
        }

    @staticmethod
    def _c7_mute_payload(mute) -> dict[str, Any]:
        return {
            "id": mute.id,
            "scope": mute.scope,
            "topic": mute.topic,
            "created_at": float(mute.created_at),
            "source_session_id": mute.source_session_id,
            "source_turn_id": mute.source_turn_id,
        }

    def c7_list_mutes(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        """Return active user-requested topic mutes (bounded user controls)."""

        return [
            self._c7_mute_payload(mute)
            for mute in self.store.list_topic_mutes(active_only=True, scope=scope)
        ]

    def c7_clear_mute(self, mute_id: str) -> dict[str, Any] | None:
        cleared = self.store.clear_topic_mute(str(mute_id or ""))
        if cleared is None:
            return None
        return self._c7_mute_payload(cleared)

    async def c7_rebuild_indexes(self) -> dict[str, Any]:
        """Rebuild FTS and invalidate semantic cache from SQLite authority."""

        fts_count = await asyncio.to_thread(self.store.rebuild_fts)
        cleared_embeddings = await asyncio.to_thread(self.store.clear_memory_embeddings)
        return {
            "fts_row_count": int(fts_count),
            "cleared_embedding_rows": int(cleared_embeddings),
            "semantic_rebuild": "lazy",
        }

    def c7_schedule_inspector(self, *, local_date: str | None = None) -> dict[str, Any]:
        now = self.clock.current_time()
        character_id = (
            self.life_runtime.character_id
            if self.life_runtime is not None
            else self.life_policy.character_id
        )
        selected_date = str(local_date or now.date().isoformat())
        schedule = self.store.get_life_schedule(character_id, selected_date)
        items = self.store.list_life_schedule_items(schedule.schedule_id) if schedule else []
        return {
            "character_id": character_id,
            "local_date": selected_date,
            "source_class": "simulated_life",
            "schedule_present": schedule is not None,
            "profile_version": schedule.profile_version if schedule else "",
            "items": [
                {
                    "item_id": item.item_id,
                    "ordinal": int(item.ordinal),
                    "category": item.category,
                    "title": item.title,
                    "starts_at": float(item.starts_at),
                    "ends_at": float(item.ends_at),
                    "status": item.status.value,
                    "thread_key": item.thread_key,
                }
                for item in items
            ],
        }

    def c7_status(self, *, scope: str | None = None) -> dict[str, Any]:
        """Return Host diagnostics narrowed to one dialogue when ``scope`` is
        given; ``None`` keeps the store-wide view used before Session
        isolation."""
        now = self.clock.current_time()
        now_ts = float(now.timestamp())
        relationship = (
            self.store.relationship_diagnostics(
                scope=str(scope or "").strip() or "global",
                now=now_ts,
                policy=self.relationship_policy,
            )
            if self.relationship_enabled
            else {"disabled": True}
        )
        life = (
            self.store.life_diagnostics(
                self.life_runtime.character_id,
                local_date=now.date().isoformat(),
            )
            if self.life_enabled and self.life_runtime is not None
            else {"disabled": True}
        )
        return {
            "schema_version": int(self.store.schema_version),
            "continuity_enabled": bool(self.continuity_enabled),
            "memory_enabled": bool(self.memory_enabled),
            "relationship_enabled": bool(self.relationship_enabled),
            "relationship_live_enabled": bool(self.relationship_live_enabled),
            "life_enabled": bool(self.life_enabled),
            "life_live_enabled": bool(self.life_live_enabled),
            "archive_recall_enabled": bool(self.archive_recall_enabled),
            "topic_mute_count": len(self.store.list_topic_mutes(active_only=True, scope=scope)) if self.memory_enabled else 0,
            "memory": self.store.continuity_diagnostics(scope=scope),
            "relationship": relationship,
            "life": life,
        }

    def c8_retrieval_traces(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent content-free C8 retrieval diagnostics."""

        bounded = max(1, min(int(limit), 32))
        traces = list(self._retrieval_traces)
        return [dict(item) for item in reversed(traces[-bounded:])]

    def _enqueue_clock_call(self, operation: Callable[..., Any], **kwargs: Any) -> None:
        if self._closed:
            return
        previous = self._clock_tail

        async def _run() -> None:
            if previous is not None:
                try:
                    await previous
                except Exception:
                    pass
            try:
                await asyncio.to_thread(operation, **kwargs)
            except Exception:
                logger.exception("continuity clock persistence failed")

        self._clock_tail = asyncio.create_task(_run())

    # ------------------------------------------------------------------
    # C2 turn journal / consolidation worker
    # ------------------------------------------------------------------
    def _ensure_memory_worker(self) -> None:
        if self._closed or not self.memory_enabled:
            return
        if self._memory_worker is not None and not self._memory_worker.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._memory_worker = asyncio.create_task(
            self._memory_worker_loop(),
            name="continuity-memory-worker",
        )

    def _schedule_recovery(self) -> None:
        if self._closed or not self.memory_enabled or self.session_dir is None:
            return
        if self._recovery_task is not None and not self._recovery_task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._recovery_task = asyncio.create_task(
            self._recover_registered_turns(),
            name="continuity-memory-recovery",
        )

    def _enqueue_registration(self, evidence: TurnEvidence) -> None:
        if self._closed or not self.memory_enabled:
            return
        previous = self._registration_tail

        async def _run() -> None:
            if previous is not None:
                try:
                    await previous
                except Exception:
                    pass
            try:
                await asyncio.to_thread(self.store.register_turn_observed, evidence)
            except Exception:
                logger.exception(
                    "continuity turn observation persistence failed turn=%s",
                    evidence.turn_id,
                )

        self._registration_tail = asyncio.create_task(_run())

    def _queue_evidence(self, evidence: TurnEvidence) -> None:
        if self._closed or not evidence.turn_id:
            return
        if self.relationship_enabled:
            self._spawn_aux_task(
                self._process_relationship_evidence(evidence),
                name=f"continuity-relationship:{evidence.turn_id}",
            )
        if not self.memory_enabled:
            return
        self._ensure_memory_worker()
        key = (storage_session_id(evidence.session_id), str(evidence.turn_id))
        if key in self._queued_turns:
            return
        self._queued_turns.add(key)
        self._memory_queue.put_nowait(evidence)

    async def _process_relationship_evidence(self, evidence: TurnEvidence) -> None:
        if self._closed or not self.continuity_enabled or not self.relationship_enabled:
            return
        try:
            now = float(self.clock.current_time().timestamp())
            await self.relationship_runtime.process_turn(evidence, as_of=now)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "continuity relationship consolidation failed session=%s turn=%s",
                evidence.session_id,
                evidence.turn_id,
            )

    async def _memory_worker_loop(self) -> None:
        while True:
            evidence = await self._memory_queue.get()
            if evidence is None:
                self._memory_queue.task_done()
                return
            key = (storage_session_id(evidence.session_id), str(evidence.turn_id))
            try:
                state = await asyncio.to_thread(self.store.mark_turn_queued, evidence)
                if state.status is ConsolidationStatus.COMPLETED:
                    continue
                candidates = await self.memory_extractor.extract(evidence)
                await asyncio.to_thread(
                    self.store.apply_memory_candidates,
                    evidence,
                    tuple(candidates),
                    resolver=self.memory_resolver,
                    complete_turn=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "continuity memory consolidation failed session=%s turn=%s",
                    evidence.session_id,
                    evidence.turn_id,
                )
                try:
                    await asyncio.to_thread(
                        self.store.mark_consolidation_failed,
                        evidence.session_id,
                        evidence.turn_id,
                        error=str(exc),
                    )
                except Exception:
                    logger.exception("failed to persist consolidation failure")
            finally:
                self._queued_turns.discard(key)
                self._memory_queue.task_done()

    async def _recover_registered_turns(self) -> None:
        """Recover only turns already registered before a previous crash.

        This intentionally does *not* import unregistered historical Session
        files.  It preserves the execution plan's opt-in migration boundary.
        """

        if self.session_dir is None:
            return
        try:
            states = await asyncio.to_thread(self.store.list_recoverable_turns)
        except Exception:
            logger.exception("continuity recovery journal read failed")
            return
        for state in states:
            if state.session_id == "__unsessioned__":
                continue
            try:
                evidence = await asyncio.to_thread(
                    load_turn_evidence,
                    self.session_dir,
                    session_id=state.session_id,
                    turn_id=state.turn_id,
                )
            except Exception:
                logger.exception(
                    "continuity recovery session read failed session=%s turn=%s",
                    state.session_id,
                    state.turn_id,
                )
                continue
            if evidence is not None:
                self._queue_evidence(evidence)

    async def _load_evidence_after_complete(
        self,
        *,
        session_id: str,
        turn_id: str,
        assistant_text: str,
        assistant_created_at: str,
    ) -> None:
        if self.session_dir is None or not session_id:
            return
        evidence = await asyncio.to_thread(
            load_turn_evidence,
            self.session_dir,
            session_id=session_id,
            turn_id=turn_id,
        )
        if evidence is None:
            return
        if assistant_text and not evidence.assistant_text:
            evidence = replace(
                evidence,
                assistant_text=assistant_text,
                assistant_created_at=assistant_created_at,
            )
        self._queue_evidence(evidence)

    async def _apply_explicit_directive(
        self,
        evidence: TurnEvidence,
    ) -> None:
        directive = parse_explicit_memory_directive(evidence.user_text)
        if directive is None:
            return
        scope = storage_session_id(evidence.session_id)
        # Explicit memory-control commands are intentionally durable before the
        # normal post-completion consolidation path.
        await asyncio.to_thread(self.store.register_turn_observed, evidence)
        if directive.action is MemoryDirectiveAction.REMEMBER:
            if directive.candidate is None:
                return
            await asyncio.to_thread(
                self.store.apply_memory_candidates,
                evidence,
                (directive.candidate,),
                resolver=self.memory_resolver,
                complete_turn=False,
            )
            return

        if directive.action is MemoryDirectiveAction.MUTE:
            topic = str(directive.target_text or "").strip()
            if not topic:
                logger.info(
                    "explicit mute target was ambiguous; no topic muted turn=%s",
                    evidence.turn_id,
                )
                return
            await asyncio.to_thread(
                self.store.add_topic_mute,
                topic,
                scope=scope,
                session_id=evidence.session_id,
                turn_id=evidence.turn_id,
                observed_at=evidence.observed_at,
            )
            return

        memory_key = str(directive.target_key or "")
        if not memory_key and directive.target_text:
            memory_key = await asyncio.to_thread(
                self.store.find_active_memory_key_by_exact_summary,
                directive.target_text,
                scope=scope,
            )
        if not memory_key:
            logger.info(
                "explicit forget target was ambiguous; no durable memory deleted turn=%s",
                evidence.turn_id,
            )
            return
        await asyncio.to_thread(
            self.store.forget_memory,
            memory_key,
            scope=scope,
            kind=directive.target_kind,
            session_id=evidence.session_id,
            turn_id=evidence.turn_id,
            observed_at=evidence.observed_at,
            relationship_policy=self.relationship_policy,
        )

    async def _on_chat_user(self, _method: str, params: dict[str, Any]) -> None:
        if not self.continuity_enabled:
            return
        now = self.clock.current_time()
        self._enqueue_clock_call(
            self.clock.mark_user_turn,
            session_id=str(params.get("session_id") or ""),
            observed_at=now,
        )
        turn_id = str(params.get("turn_id") or "").strip()
        session_id = str(params.get("session_id") or "")
        if self.life_enabled and self.life_runtime is not None and turn_id:
            self._spawn_aux_task(
                asyncio.to_thread(
                    self.life_runtime.interrupt_for_chat,
                    now=now, session_id=session_id, turn_id=turn_id,
                ),
                name=f"continuity-life-interrupt:{turn_id}",
            )
        if (not self.memory_enabled and not self.relationship_enabled) or not turn_id:
            return
        evidence = TurnEvidence(
            session_id=session_id,
            turn_id=turn_id,
            user_text=str(params.get("text") or ""),
            source=str(params.get("source") or ""),
            user_created_at=now.isoformat(timespec="milliseconds"),
            observed_at=float(now.timestamp()),
        )
        key = (storage_session_id(session_id), turn_id)
        self._pending_turns[key] = evidence
        directive = parse_explicit_memory_directive(evidence.user_text) if self.memory_enabled else None
        if directive is not None:
            try:
                await self._apply_explicit_directive(evidence)
            except Exception:
                logger.exception("explicit continuity memory directive failed turn=%s", turn_id)
        elif self.memory_enabled:
            self._enqueue_registration(evidence)

    async def _on_chat_complete(self, _method: str, params: dict[str, Any]) -> None:
        if not self.continuity_enabled:
            return
        now = self.clock.current_time()
        session_id = str(params.get("session_id") or "")
        turn_id = str(params.get("turn_id") or "").strip()
        self._enqueue_clock_call(
            self.clock.mark_assistant_completed,
            session_id=session_id,
            successful=True,
            observed_at=now,
        )
        if self.life_enabled and self.life_runtime is not None and turn_id:
            self._spawn_aux_task(
                asyncio.to_thread(
                    self.life_runtime.resume_after_chat,
                    now=now, session_id=session_id, turn_id=turn_id,
                ),
                name=f"continuity-life-resume:{turn_id}",
            )
        if (not self.memory_enabled and not self.relationship_enabled) or not turn_id:
            return
        key = (storage_session_id(session_id), turn_id)
        pending = self._pending_turns.pop(key, None)
        if pending is not None:
            evidence = replace(
                pending,
                assistant_text=str(params.get("full_text") or ""),
                assistant_created_at=now.isoformat(timespec="milliseconds"),
            )
            self._queue_evidence(evidence)
            return

        # A restart/rebind or unusual direct path may mean the in-memory user
        # event is absent.  Re-read exactly this already-accepted turn from the
        # Session authority instead of inventing user evidence.
        if session_id and self.session_dir is not None:
            self._spawn_aux_task(
                self._load_evidence_after_complete(
                    session_id=session_id,
                    turn_id=turn_id,
                    assistant_text=str(params.get("full_text") or ""),
                    assistant_created_at=now.isoformat(timespec="milliseconds"),
                ),
                name=f"continuity-turn-load:{turn_id}",
            )

    def _mute_topics_for_query(self, query: str, scope: str) -> tuple[tuple[str, ...], int]:
        """Return (topics suppressed this turn, count of topics the user raised).

        A topic the current user turn raises itself is dropped from the
        suppressed list for this turn only, so suppression never makes a
        remembered topic unrecallable.  Reading the mute list must never break
        a chat turn.  Mutes are Session-scoped like the memories they hide.
        """

        if not self.memory_enabled:
            return (), 0
        try:
            mutes = self.store.list_topic_mutes(active_only=True, scope=scope)
        except Exception:
            logger.exception("continuity topic mute read failed; continuing unmuted")
            return (), 0
        if not mutes:
            return (), 0
        topics = tuple(dict.fromkeys(str(mute.topic) for mute in mutes if str(mute.topic).strip()))
        raised = tuple(topic for topic in topics if topic_is_raised_by_query(topic, query))
        suppressed = tuple(topic for topic in topics if topic not in raised)
        return suppressed, len(raised)

    def grounding_for_turn(
        self,
        question: str,
        *,
        session_id: str = "",
        turn_id: str = "",
    ) -> ContinuityGrounding:
        """Build one bounded, read-only C3-C8 Continuity projection.

        C8 preserves the existing fast path. Session archive I/O is reachable
        only for an explicit historical question after fast recall is below the
        configured confidence threshold. All fallback failures fail open.
        """

        if not self.continuity_enabled:
            return ContinuityGrounding()
        started = time.perf_counter()
        snapshot = self.clock.snapshot()
        query = str(question or "")
        scope = storage_session_id(session_id)
        suppressed_topics, mute_exempted = self._mute_topics_for_query(query, scope)
        hits = []
        fast_started = time.perf_counter()
        if self.memory_enabled and query.strip():
            try:
                hits = self.memory_retriever.retrieve(
                    query,
                    scope=scope,
                    now=float(snapshot.now.timestamp()),
                    exclude_turn_id=str(turn_id or ""),
                    suppressed_topics=suppressed_topics,
                )
            except Exception:
                logger.exception("continuity memory retrieval failed; using reality context only")
                hits = []
        fast_ms = (time.perf_counter() - fast_started) * 1000.0

        archive_result = ArchiveRecallResult(reason="not_attempted")
        archive_memory_hits = []
        gate_reason = "disabled"
        archive_attempted = False
        archive_started = time.perf_counter()
        if self.memory_enabled and self.archive_recall_enabled and query.strip():
            try:
                should_fallback, gate_reason = self.archive_searcher.evaluate_gate(query, hits)
                if should_fallback:
                    archive_attempted = True
                    archive_memory_hits = self.memory_retriever.retrieve_archive(
                        query,
                        scope=scope,
                        now=float(snapshot.now.timestamp()),
                        exclude_turn_id=str(turn_id or ""),
                        max_items=min(3, self.retrieval_policy.max_items),
                        suppressed_topics=suppressed_topics,
                    )
                    archive_result = self.archive_searcher.search(
                        query,
                        scope=scope,
                        now=snapshot.now,
                        exclude_turn_id=str(turn_id or ""),
                        suppressed_topics=suppressed_topics,
                    )
                    seen = {hit.memory.id for hit in hits}
                    for hit in archive_memory_hits:
                        if hit.memory.id not in seen:
                            hits.append(hit)
                            seen.add(hit.memory.id)
            except Exception:
                logger.exception("continuity archive recall failed; retaining fast recall only")
                archive_result = ArchiveRecallResult(reason="error")
        archive_ms = (time.perf_counter() - archive_started) * 1000.0 if archive_attempted else 0.0

        relationship_context = ""
        if self.relationship_enabled and self.relationship_live_enabled:
            try:
                relationship_context = self.relationship_runtime.render_projection(
                    now=float(snapshot.now.timestamp()), scope=scope
                )
            except Exception:
                logger.exception("continuity relationship projection failed; omitting relationship context")
                relationship_context = ""
        life_context = ""
        if self.life_enabled and self.life_live_enabled and self.life_runtime is not None:
            try:
                life_context = self.life_runtime.render_projection(now=snapshot.now)
            except Exception:
                logger.exception("continuity Character Life projection failed; omitting life context")
                life_context = ""
        rendered = render_continuity_grounding(
            hits,
            reality=snapshot,
            relationship=relationship_context,
            life=life_context,
            archive_hits=archive_result.hits,
            suppressed_topics=suppressed_topics,
            max_chars=self.retrieval_policy.max_context_chars,
        )
        recall_ids = list(rendered.memory_ids)
        archive_section_used = bool(archive_result.hits and "Historical Session evidence" in rendered.text)
        if archive_section_used:
            for archive_hit in archive_result.hits:
                recall_ids.extend(archive_hit.anchor_memory_ids)
        recall_ids = list(dict.fromkeys(recall_ids))
        if recall_ids:
            try:
                self.store.mark_memories_recalled(
                    recall_ids,
                    recalled_at=float(snapshot.now.timestamp()),
                )
            except Exception:
                logger.exception("continuity recall accounting failed")
        semantic_used = any(
            hit.memory.id in rendered.memory_ids and hit.semantic_score > 0.0
            for hit in hits
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        top_fast_score = max((float(hit.score) for hit in hits if "archive" not in hit.reasons), default=0.0)
        trace = {
            "at": float(snapshot.now.timestamp()),
            "query_fingerprint": hashlib.sha256(query.strip().casefold().encode("utf-8")).hexdigest()[:16],
            "fast_hit_count": sum(1 for hit in hits if "archive" not in hit.reasons),
            "fast_top_score": round(top_fast_score, 4),
            "fast_ms": round(fast_ms, 3),
            "archive_gate": gate_reason,
            "archive_attempted": bool(archive_attempted),
            "archive_memory_hit_count": len(archive_memory_hits),
            "archive_session_hit_count": len(archive_result.hits),
            "archive_session_candidates": int(archive_result.session_candidates),
            "archive_sessions_opened": int(archive_result.sessions_opened),
            "archive_turns_scored": int(archive_result.turns_scored),
            "archive_guard_ready": bool(archive_result.guard_ready),
            "archive_result": archive_result.reason,
            "temporal_filter": archive_result.temporal_kind,
            "archive_ms": round(archive_ms, 3),
            "mute_count": len(suppressed_topics),
            "mute_exempted": int(mute_exempted),
            "total_ms": round(elapsed_ms, 3),
        }
        self._retrieval_traces.append(trace)
        logger.info(
            "[Continuity] grounding memories=%d semantic=%s archive=%s archive_hits=%d elapsed_ms=%.1f",
            len(rendered.memory_ids),
            semantic_used,
            archive_attempted,
            len(archive_result.hits),
            elapsed_ms,
        )
        return ContinuityGrounding(
            text=rendered.text,
            memory_count=len(rendered.memory_ids),
            recalled_memory_ids=rendered.memory_ids,
            semantic_used=semantic_used,
            archive_used=archive_section_used or any("archive" in hit.reasons for hit in hits if hit.memory.id in rendered.memory_ids),
            archive_hit_count=len(archive_result.hits) if archive_section_used else 0,
        )

    async def drain(self) -> None:
        registration = self._registration_tail
        if registration is not None:
            await registration
        recovery = self._recovery_task
        if recovery is not None:
            await recovery
        while self._aux_tasks:
            tasks = tuple(self._aux_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            # done-callback cleanup is scheduled by the event loop; remove the
            # completed snapshot eagerly so drain cannot spin on already-done
            # tasks before those callbacks get another scheduling turn.
            self._aux_tasks.difference_update(task for task in tasks if task.done())
        if self._memory_worker is not None:
            await self._memory_queue.join()
        clock_tail = self._clock_tail
        if clock_tail is not None:
            await clock_tail

    async def aclose(self, *, graceful: bool = True) -> None:
        if self._closed:
            return
        self.unbind_event_bus()
        if self._maintenance_task is not None and not self._maintenance_task.done():
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
        if self._life_rollover_task is not None and not self._life_rollover_task.done():
            self._life_rollover_task.cancel()
            await asyncio.gather(self._life_rollover_task, return_exceptions=True)
        await self.drain()
        if self._memory_worker is not None and not self._memory_worker.done():
            self._memory_queue.put_nowait(None)
            await self._memory_worker
        if graceful and self._started and self.continuity_enabled:
            try:
                await asyncio.to_thread(self.clock.mark_graceful_shutdown)
            except Exception:
                logger.exception("continuity clock failed to record graceful shutdown")
        await asyncio.to_thread(self.store.close)
        self._closed = True
