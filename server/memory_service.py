"""Thin Host authority/projection adapter for the independently installed Eidema.

SQLite memories, operations, exclusions and vector validity belong to Eidema.
This owner keeps only noncontent exclusion coordinates and bounded live work.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
import hashlib
import importlib
import json
import logging
import sqlite3
import time
from typing import Callable

from config.memory import MemoryConfig, MEMORY_FINAL_VALIDITY_TIMEOUT_S
from core import session_manager as sm
from core import session_source_journal as journal
from core.chat_history_projection import MEMORY_TEXT_PROJECTION_VERSION, exclude_source_spans
from server.memory_source_policy import SourcePolicyProjection, SourcePolicyUnavailable, SourcePolicyBindingMismatch
from server.memory_context import estimate_reply_tokens, pack_reference
from server.reference_deadline import await_until, deadline_now

logger = logging.getLogger(__name__)
SOURCE_NAMESPACE = journal.V1
MAX_MEMORY_TEXT_CHARS = 32768
MAX_MEMORY_CONDITION_CHARS = 4096  # Eidema's published condition input cap.
MEMORY_CANDIDATE_LIMIT = 16
# Count-only fragmentation ceiling for one already text-bounded entry. Smaller
# nonpreemptive passage units must not shrink the accepted text/condition limit.
# A 64-token unit fits at least 13 four-byte Unicode scalars even at the
# byte-level prefix/EOS bound; 36,865 legal text+condition scalars need <=2,836.
# Use Eidema's bounded public ceiling without reducing either source input cap.
MEMORY_INDEX_CHUNK_LIMIT = 4096
MEMORY_CANDIDATE_CHAR_BUDGET = MEMORY_CANDIDATE_LIMIT * (MAX_MEMORY_TEXT_CHARS + MAX_MEMORY_CONDITION_CHARS)


def _message_key(session_id, row):
    return session_id, row.get("incarnation"), row.get("message_id")


class MemoryRequestError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _SingleFlight:
    """One running job, zero queued jobs, even when its awaiter times out."""
    def __init__(self, name):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._future = None
        self._priority_waiter = False

    async def run(self, function, *, timeout=5, on_completed=None, on_submitted=None, mutation=False, priority=False):
        deadline = None if timeout is None else deadline_now() + timeout
        if priority and self._future is not None:
            if self._priority_waiter:
                raise MemoryRequestError("busy")
            self._priority_waiter = True
            try:
                # One short validity reader may wait. New panel jobs cannot
                # repeatedly overtake it, and no executor backlog is created.
                async def released():
                    while self._future is not None:
                        await asyncio.sleep(.001)
                await await_until(released(), deadline)
            except TimeoutError:
                raise MemoryRequestError("busy") from None
            finally:
                self._priority_waiter = False
        if (self._future is not None or (self._priority_waiter and not priority)
                or (deadline is not None and deadline_now() >= deadline)):
            raise MemoryRequestError("busy")
        future = self._executor.submit(function)
        self._future = future
        if on_submitted is not None:
            on_submitted()
        wrapped = asyncio.wrap_future(future)
        def completed(task):
            try:
                if not task.cancelled() and task.exception() is None:
                    if on_completed is not None:
                        on_completed(task.result())
            finally:
                # Publication is part of this lane's ownership, even if the
                # requester timed out. Only the Host loop may release it after
                # installing source facts; a thread-done bit is insufficient.
                if self._future is future:
                    self._future = None
        wrapped.add_done_callback(completed)
        try:
            return await await_until(asyncio.shield(wrapped), deadline)
        except TimeoutError:
            raise MemoryRequestError("outcome_unknown" if mutation else "busy") from None

    def close(self):
        self._executor.shutdown(wait=False, cancel_futures=True)


@dataclass(frozen=True)
class MemoryTurnReference:
    """Timely candidates; deadline records retrieval provenance only."""
    context: dict
    result: object
    deadline: float | None = None


@dataclass(frozen=True)
class MemoryLookupEvidence:
    """Exactly the bounded entries shown to the result-expression model."""
    context: dict
    candidates: tuple
    payload: dict
    read_set: object | None = None
    question_time: dict | None = None
    pack: object | None = None


class HostMemoryService:
    def __init__(self, config: MemoryConfig, *, project_for_session: Callable[[str], str] = lambda sid:"",
                 api=None, embedder_factory=None, reply_token_count=None, reply_token_count_method=None,
                 formation_for_session: Callable[[str], bool] = lambda sid: False):
        self.config = config
        self.project_for_session = project_for_session
        if not callable(formation_for_session):
            raise ValueError("formation_for_session_callback_required")
        self.formation_for_session = formation_for_session
        self._api = api
        self._store = None
        self._embedder_factory = embedder_factory
        self._reply_token_count = reply_token_count
        self._reply_token_count_method = reply_token_count_method
        if reply_token_count is not None and not reply_token_count_method:
            raise ValueError("reply_token_count_method_required")
        self._db = _SingleFlight("eidema-db")
        self._query = _SingleFlight("eidema-query")
        self._model = _SingleFlight("eidema-model")
        self._startup = None
        self._sources = None
        self._maintenance = None
        self._source_lookup = None
        self._index_task = None
        self._index_requested = False
        self._closing = False
        self._close_task = None
        # A destructive mutation and the start of protected answer publication
        # have one linear order. Generation never holds this short boundary.
        self._publication_lock = asyncio.Lock()
        self.output_speech = None
        self._state = "starting" if config.enabled else "disabled"
        self._semantic_state = "warming" if config.semantic_enabled and config.enabled else "disabled"
        self._semantic_reason = self._semantic_state
        self._automatic_threshold = config.semantic_threshold
        self._threshold_calibrated = False
        self._exclusions = {}
        self._retirements = {}
        self._pending_sources = {}
        self._derived_sources = {}
        # With an existing store, absence of loaded exclusion facts cannot be
        # interpreted as permission to send its historical sources to a model.
        self._policy = SourcePolicyProjection(config.db_path, config.user_id)
        self._projection_value = None
        self._exclusions_ready = self._policy.ready
        self._source_policy_reason = "ready" if self._policy.ready else "starting"

    def start(self):
        if self._startup is None:
            self._startup = asyncio.create_task(self._open())

    async def bind_maintenance(self, **options):
        """Explicit trusted opt-in; normal startup installs no maintenance."""
        from server.memory_maintenance import bind_maintenance
        return await bind_maintenance(self, **options)

    async def _open(self):
        if not self.config.enabled and not self.config.db_path.exists() and not self._policy.path.exists():
            return
        try:
            def bootstrap():
                try:
                    self._policy.load()
                except SourcePolicyBindingMismatch:
                    raise
                except (OSError, ValueError, sqlite3.Error):
                    self._source_policy_reason = "sidecar_rebuild_required"
                    logger.warning("[Memory] source projection requires recovery")
                if self.config.db_path.exists():
                    try:
                        self._policy.bootstrap_v1()
                    except SourcePolicyBindingMismatch:
                        raise
                    except (OSError, ValueError, sqlite3.Error) as exc:
                        if not self._policy.ready:
                            self._source_policy_reason = ("store_rollback" if str(exc) == "source_policy_store_rollback"
                                else "core_unreadable")
                        logger.warning("[Memory] source policy bootstrap unavailable")
            await self._db.run(bootstrap, timeout=None)
            self._sync_projection()
            if self._policy.path.exists() and not self.config.db_path.exists():
                self._source_policy_reason = "core_missing"
                raise OSError("memory_store_missing")
            self._api = self._api or importlib.import_module("eidema")
            def opened():
                # Core search remains rank-only by default. Automatic recall
                # supplies its per-call relevance floor before core quotas;
                # final Host validity/admission is still checked at use.
                store = self._api.MemoryStore(self.config.db_path, max_text_chars=MAX_MEMORY_TEXT_CHARS)
                access = self._api.AccessScope(self.config.user_id, "startup")
                # The singular core owner excludes every earlier writer. Its
                # atomic snapshot now contains every completed mutation; an
                # uncommitted prepare has no effect to preserve in core.
                try:
                    snapshot = store.source_policy(access)
                    if snapshot.change_seq < self._policy.value["change_seq"]:
                        raise ValueError("source_policy_store_rollback")
                    self._policy.reconcile(snapshot, clear=[
                        (item["operation_id"], item["item_id"]) for item in self._policy.value["pending"]])
                except BaseException:
                    store.close()
                    raise
                return store
            def installed(result):
                self._store = result
                self._sync_projection()
                if self._exclusions_ready:
                    self._source_policy_reason = "ready"
                self._state = "ready" if self.config.enabled else "disabled"
                if self.config.enabled and self.config.semantic_enabled and not self._closing:
                    self._index_task = asyncio.create_task(self._prewarm())
            await self._db.run(opened, timeout=None, on_completed=installed)
            if self.config.enabled and self.config.source_runtime_enabled and not self._closing:
                from server.memory_source_delivery import SourceDelivery
                from server.memory_source_lookup import SourceLookup
                try:
                    self._sources = SourceDelivery(self)
                    self._source_lookup = SourceLookup(self, self._sources)
                except (AttributeError, ValueError) as exc:
                    logger.warning("[Memory] source delivery unavailable (%s)", type(exc).__name__)
        except Exception as exc:
            if isinstance(exc, SourcePolicyBindingMismatch):
                self._source_policy_reason = "identity_mismatch"
            elif str(exc) == "source_policy_store_rollback":
                self._source_policy_reason = "store_rollback"
            self._state = "unavailable"
            self._semantic_state = "unavailable"
            self._semantic_reason = "dependencies_missing" if isinstance(exc, ImportError) else "store_unavailable"
            logger.warning("[Memory] startup unavailable (%s)", type(exc).__name__)

    async def _prewarm(self):
        try:
            def warm():
                if self._embedder_factory is not None:
                    return self._embedder_factory()
                from core.local_embeddings import create_memory_embedder
                return create_memory_embedder(self.config.model, backend=self.config.model_backend,
                    version=self.config.model_version)
            # Warmup has its own single owner. CRUD/FTS use the independent
            # SQLite adapter lane and continue while model loading runs.
            embedder = await self._model.run(warm, timeout=300)
            if self._closing:
                return
            def attached(_result):
                self._semantic_state, self._semantic_reason = "ready", "ready"
                if self._automatic_threshold is None:
                    self._automatic_threshold = getattr(embedder, "automatic_threshold", None)
                    self._threshold_calibrated = bool(getattr(embedder, "threshold_calibrated", False))
                if self._maintenance is not None:
                    self._maintenance.resume()
            while not self._closing and self._semantic_state != "ready":
                try:
                    await self._db.run(lambda:self._store.configure_embedder(embedder), on_completed=attached)
                except MemoryRequestError as exc:
                    if exc.code != "busy":
                        raise
                    # Keep this exact loaded resource. Foreground DB ownership
                    # can delay attachment, never turn it into model_missing.
                    await asyncio.sleep(.02)
            if self._closing:
                return
            await self._index_pending()
        except Exception as exc:
            self._semantic_state = "unavailable"
            from config.memory_jina import MemoryModelError
            self._semantic_reason = ("dependencies_missing" if isinstance(exc, ImportError)
                else exc.code if isinstance(exc, MemoryModelError) else "model_missing")
            logger.warning("[Memory] semantic warmup unavailable (%s)", type(exc).__name__)

    def _schedule_index(self):
        self._index_requested = True
        if (self.config.semantic_enabled and self._semantic_state == "ready"
                and (self._index_task is None or self._index_task.done()) and not self._closing):
            self._index_task = asyncio.create_task(self._index_pending())

    async def _index_pending(self):
        while not self._closing:
            self._index_requested = False
            try:
                outcome = await self._model.run(lambda:self._store.run_index_batch(limit=1, max_chunks=MEMORY_INDEX_CHUNK_LIMIT),
                    timeout=300)
                if not outcome.busy and outcome.attempted == 0 and self._sources is not None:
                    outcome = await self._model.run(lambda:self._sources.retrieval.run_index_batch(
                        limit=1, max_chunks=MEMORY_INDEX_CHUNK_LIMIT), timeout=300)
                if outcome.busy:
                    await asyncio.sleep(.05)
                    continue
                if outcome.attempted == 0:
                    if self._index_requested:
                        continue
                    return
                # Yield between finite batches; writes invalidate in-flight work
                # in Eidema before any late vector can be committed.
                await asyncio.sleep(0)
            except Exception as exc:
                logger.warning("[Memory] indexing unavailable (%s)", type(exc).__name__)
                self._semantic_reason = "embedding_failed"
                return

    def context(self, session_id: str, *, token: str | None = None):
        if not session_id or session_id != sm.get_current_session_id():
            raise MemoryRequestError("stale_session")
        from llm import character_prompts

        # Current Session identity was validated and installed at activation;
        # hot recall must not synchronously parse the entire Session file again.
        character_id = sm.conversation_history.character_id
        if character_id != character_prompts.active_character_id():
            raise MemoryRequestError("stale_session")
        project_id = self.project_for_session(session_id) or None
        identity = [self.config.user_id, session_id, character_id, project_id,
            sm.get_session_selection_revision()]
        incarnation = sm.conversation_history.source_incarnation()
        if incarnation is not None:
            identity.append(incarnation)
        stamp = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        if token is not None and token != stamp:
            raise MemoryRequestError("context_changed")
        return {"session_id":session_id, "character_id":character_id,
            "project_id":project_id, "context_token":stamp,
            **({"incarnation": incarnation} if incarnation is not None else {})}

    def _access(self, context):
        return self._api.AccessScope(self.config.user_id, context["character_id"], context["project_id"])

    def _available(self):
        if not self.config.enabled:
            raise MemoryRequestError("disabled")
        if self._store is None:
            raise MemoryRequestError("busy" if self._state == "starting" else "unavailable")

    def _scope(self, context, kind):
        if kind not in {"character", "shared", "project"} or (kind == "project" and not context["project_id"]):
            raise MemoryRequestError("invalid_request")
        return self._api.MemoryScope(kind, self.config.user_id,
            context["character_id"] if kind != "shared" else None,
            context["project_id"] if kind == "project" else None)

    async def _source(self, context, value, text):
        if value is None:
            return None
        self._sync_projection()
        if not self._exclusions_ready:
            raise MemoryRequestError("conflict")
        if not isinstance(value, dict) or value.get("session_id") != context["session_id"]:
            raise MemoryRequestError("invalid_request")
        if value.get("incarnation") != context.get("incarnation"):
            raise MemoryRequestError("conflict")
        start, end, revision = value.get("start_cp"), value.get("end_cp"), value.get("revision")
        if (any(type(number) is not int for number in (start, end, revision))
                or value.get("projection_version") != MEMORY_TEXT_PROJECTION_VERSION):
            raise MemoryRequestError("invalid_request")
        # Resolve the exact committed source from the Session owner's snapshot;
        # no file parsing or migration runs on this conversation loop.
        try:
            source = await asyncio.to_thread(sm.read_committed_session_source,
                context["session_id"], value["message_id"], revision=revision)
        except (KeyError, ValueError, LookupError, OSError):
            raise MemoryRequestError("conflict") from None
        incarnation = source.get("incarnation")
        if incarnation != context.get("incarnation"):
            raise MemoryRequestError("conflict")
        if not 0 <= start < end <= len(source["text"]):
            raise MemoryRequestError("invalid_request")
        self._sync_projection()
        if not self._exclusions_ready:
            raise MemoryRequestError("conflict")
        key = _message_key(context["session_id"], source)
        known = self._exclusions.get(key, []) + self._pending_sources.get(key, [])
        if any(span["revision"] != revision
                or span["projection_version"] != source["projection_version"]
                or max(start, span.get("start_cp") or 0) < min(end,
                    span["end_cp"] if span.get("end_cp") is not None else len(source["text"])) for span in known):
            raise MemoryRequestError("conflict")
        coordinate = journal.coordinate(context["session_id"], source["message_id"], revision, incarnation)
        return self._api.SourceSpan(**{**coordinate, "start_cp": start, "end_cp": end})

    def _index_source_spans(self, sources):
        indexed = {}
        for span, reason in sources:
            if span.namespace not in (journal.V1, journal.V2):
                continue
            try:
                sid, mid, incarnation, projection = journal.decode_record_key(span.namespace, span.record_key)
                indexed.setdefault((sid, incarnation, mid), []).append({"revision":span.revision,
                    "projection_version":projection, "start_cp":span.start_cp, "end_cp":span.end_cp,
                    **({"reason":reason} if reason is not None else {})})
            except (TypeError, ValueError):
                raise ValueError("invalid_source_coordinates") from None
        return indexed

    def _install_source_facts(self, exclusions, retirements):
        try:
            excluded = self._index_source_spans((source, None) for source in exclusions)
            retired = self._index_source_spans((value.source, value.reason) for value in retirements)
        except ValueError:
            self._exclusions_ready = False
            self._source_policy_reason = "invalid_coordinates"
            return
        self._exclusions, self._retirements = excluded, retired
        self._exclusions_ready = True

    def _sync_projection(self):
        value = self._policy.value
        if self._projection_value is value:
            # Only successfully parsed values enter this cache. Readiness may
            # change independently when an empty legacy store is recognised.
            self._exclusions_ready = self._policy.ready
            return
        from types import SimpleNamespace
        excluded = [SimpleNamespace(**source) for source in value["exclusions"]]
        retired = [SimpleNamespace(source=SimpleNamespace(**item["source"]), reason=item["reason"])
            for item in value["retirements"]]
        self._install_source_facts(excluded, retired)
        if not self._exclusions_ready:
            return
        try:
            pending = self._index_source_spans((SimpleNamespace(**source), None)
                for item in value["pending"] for source in item["sources"])
        except ValueError:
            self._exclusions_ready = False
            self._source_policy_reason = "invalid_coordinates"
            return
        self._pending_sources = pending
        try:
            self._derived_sources = self._index_source_spans((SimpleNamespace(**source), None)
                for source in value["derived_sources"])
        except ValueError:
            self._exclusions_ready = False
            self._source_policy_reason = "invalid_coordinates"
            return
        self._exclusions_ready = self._policy.ready
        self._projection_value = value

    def project_history(self, session_id, rows):
        self._sync_projection()
        if not self._exclusions_ready:
            raise SourcePolicyUnavailable("memory_source_policy_recovery_required")
        result = []
        for row in rows:
            key = _message_key(session_id, row)
            spans = (self._retirements.get(key, []) + self._exclusions.get(key, [])
                + self._pending_sources.get(key, []))
            # Known generated answers carry whole-output dependencies. A later
            # partial source mask cannot safely turn their remainder into an
            # independent assertion. Ordinary sources retain partial masking.
            if key in self._derived_sources and (spans or not self.config.enabled or self._store is None):
                continue
            projected = exclude_source_spans(row, spans)
            if projected is not None:
                result.append(projected)
        return result

    @staticmethod
    def _source_dto(source):
        if source is None or source["namespace"] not in (journal.V1, journal.V2):
            return None
        sid, mid, incarnation, projection = journal.decode_record_key(source["namespace"], source["record_key"])
        return {"session_id":sid, "message_id":mid, "projection_version":projection,
            "revision":source["revision"], "start_cp":source["start_cp"], "end_cp":source["end_cp"],
            **({"incarnation": incarnation} if incarnation is not None else {})}

    def _memory_dto(self, memory):
        result = asdict(memory)
        result["scope"].pop("user_id", None)
        result["source"] = self._source_dto(result["source"])
        result["sources"] = [result["source"]] if result["source"] is not None else []
        return result

    async def status(self, context):
        self._sync_projection()
        raw = {}
        if self._store is not None:
            try:
                raw = asdict(await self._db.run(lambda:self._store.status(self._access(context))))
            except MemoryRequestError:
                pass
        semantic = self._semantic_state
        if semantic == "ready":
            semantic = raw.get("semantic", "partial")
        state = self._state
        if state == "ready" and semantic != "ready":
            state = "degraded"
        reason = self._semantic_reason
        if semantic == "partial":
            reason = "embedding_failed" if raw.get("failed_memories", 0) else "index_pending"
        return {**raw, "state":state, "semantic_state":semantic, "semantic_reason":reason,
            "cleanup_state":("pending" if raw["cleanup_pending"] else "complete")
                if "cleanup_pending" in raw else "unknown"
                    if self.config.db_path.exists() or self._policy.path.exists() else "complete",
            "semantic_threshold":self.config.semantic_threshold,
            "automatic_admission":("ready" if self._automatic_threshold is not None and self._semantic_state == "ready"
                else "gate_required" if self._semantic_state == "ready" else "semantic_unavailable"),
            "automatic_threshold":self._automatic_threshold,
            "source_policy_state":"ready" if self._exclusions_ready else "recovery_required",
            "source_policy_reason":self._source_policy_reason,
            **({"source_delivery":self._sources.state if self._sources is not None else "unavailable"}
                if self.config.enabled and self.config.source_runtime_enabled else {}),
            "threshold_calibrated":self._threshold_calibrated, "history_exclusions_ready":self._exclusions_ready}

    async def request(self, method, params):
        context = self.context(params.get("session_id", ""),
            token=None if method == "memory.status" else params.get("context_token", ""))
        if method == "memory.status":
            status = await self.status(context)
            self.context(context["session_id"], token=context["context_token"])
            return {"ok":True, "context":context, "enabled":self.config.enabled, "status":status,
                "scopes":[{"kind":kind, "label":kind, "available":kind != "project" or bool(context["project_id"])}
                    for kind in ("character", "shared", "project")]}
        self._available()
        access = self._access(context)
        if method == "memory.sources":
            limit = params.get("limit", 50)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise MemoryRequestError("invalid_request")
            rows = sm.committed_session_rows(context["session_id"], limit=limit)
            from core.chat_history_projection import project_memory_source
            sources = await asyncio.to_thread(lambda:tuple(
                project_memory_source(context["session_id"], row, incarnation=context.get("incarnation"))
                for row in rows))
            self.context(context["session_id"], token=context["context_token"])
            return {"ok":True, "context":context, "sources":list(sources)}
        if method == "memory.list":
            kind = params.get("scope", "character")
            self._scope(context, kind)
            limit = params.get("limit", 100)
            cursor = params.get("cursor")
            if (type(limit) is not int or not 1 <= limit <= 100 or (cursor is not None
                    and (not isinstance(cursor, list) or len(cursor) != 2
                        or type(cursor[0]) is not int or not 0 <= cursor[0] <= 2**63-1
                        or not isinstance(cursor[1], str) or not 0 < len(cursor[1]) <= 200))):
                raise MemoryRequestError("invalid_request")
            result = await self._db.run(lambda:self._store.list_memories(access, limit=limit,
                scope_kind=kind, before=cursor))
            response = {"items":[self._memory_dto(item) for item in result],
                "next_cursor":[result[-1].updated_at_ms, result[-1].memory_id] if len(result) == limit else None}
        elif method == "memory.get":
            detail = await self._db.run(lambda:self._store.detail(access, params.get("memory_id", "")))
            response = {"memory":self._memory_dto(detail.memory),
                "history":[self._memory_dto(item) for item in detail.history]}
        elif method == "memory.forget_preview":
            preview = await self._db.run(lambda:self._store.preview_forget(access,
                params.get("memory_id", ""), params.get("expected_version")))
            response = {"preview":{"targets":[asdict(target) for target in preview.targets],
                "memories":[self._memory_dto(item) for item in preview.memories],
                "source_exclusions":[self._source_dto(asdict(source)) for source in preview.source_exclusions]}}
        elif method in {"memory.remember", "memory.correct", "memory.forget"}:
            operation_id, item_id = params.get("operation_id"), params.get("item_id")
            if any(not isinstance(value, str) or not value or len(value) > 200 for value in (operation_id, item_id)):
                raise MemoryRequestError("invalid_request")
            text = params.get("text")
            source = None
            if method != "memory.forget":
                if not isinstance(text, str) or not text.strip() or len(text) > MAX_MEMORY_TEXT_CHARS:
                    raise MemoryRequestError("invalid_request")
                source = await self._source(context, params.get("source"), text)
                self.context(context["session_id"], token=context["context_token"])
            scope = self._scope(context, params.get("scope", "character")) if method == "memory.remember" else None
            now = time.time_ns() // 1_000_000
            def mutation():
                common = {"operation_id":operation_id, "item_id":item_id, "now_ms":now}
                destructive = method in {"memory.correct", "memory.forget"}
                targets = params.get("confirmed_targets")
                if targets is not None:
                    if (not isinstance(targets, list) or len(targets) > 100
                            or any(not isinstance(item, dict) or set(item) != {"memory_id", "row_version"}
                                for item in targets)):
                        raise MemoryRequestError("invalid_request")
                    targets = [self._api.ForgetTarget(item["memory_id"], item["row_version"]) for item in targets]
                if destructive:
                    preview = self._store.prepare_mutation(access, params.get("memory_id", ""),
                        params.get("expected_version"), operation_id=operation_id, item_id=item_id,
                        kind="correct" if method == "memory.correct" else "forget", confirmed_targets=targets)
                    self._policy.prepare(context, operation_id, item_id, preview.source_spans, preview.memory_ids)
                try:
                    if method == "memory.remember":
                        operation = self._store.remember(access, scope, text=text, source=source, **common)
                    elif method == "memory.correct":
                        operation = self._store.correct(access, params.get("memory_id", ""), params.get("expected_version"),
                            text=text, source=source, mode=params.get("mode", "change"), **common)
                    else:
                        operation = self._store.forget(access, params.get("memory_id", ""), params.get("expected_version"),
                            confirmed_targets=targets, **common)
                except BaseException:
                    if destructive:
                        try:
                            self._policy.reconcile(self._store.source_policy(access), clear=[(operation_id, item_id)])
                            self._source_policy_reason = "ready"
                        except Exception as exc:
                            # Keep the exact prepared mask and the primary
                            # rejection. Recovery cannot replace its outcome.
                            self._source_policy_reason = "publication_pending"
                            logger.warning("[Memory] source publication pending (%s)", type(exc).__name__)
                    raise
                try:
                    self._policy.reconcile(self._store.source_policy(access),
                        clear=[(operation_id, item_id)] if destructive else ())
                    self._source_policy_reason = "ready"
                except Exception as exc:
                    # Core's receipt remains truthful. Prepared coordinates
                    # still protect history until replay/restart publication.
                    self._source_policy_reason = "publication_pending"
                    logger.warning("[Memory] source publication pending (%s)", type(exc).__name__)
                return operation
            def committed(result):
                self._sync_projection()
                self._schedule_index()
                if (method in {"memory.correct", "memory.forget"} and self._maintenance is not None
                        and self._maintenance.reformation_enabled):
                    # A privacy mutation can capture SDK repair work without
                    # appending a Session source receipt. Wake the same owner
                    # after commit; privacy completion never waits for formation.
                    self._maintenance.resume()
            async with self._publication_lock:
                operation = await self._db.run(mutation, on_completed=committed, mutation=True)
            response = {"operation":asdict(operation),
                "source_policy_pending":self._source_policy_reason == "publication_pending"}
        else:
            raise MemoryRequestError("invalid_request")
        if method not in {"memory.remember", "memory.correct", "memory.forget"}:
            self.context(context["session_id"], token=context["context_token"])
        return {"ok":True, "context":context, **response}

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - deadline_now()
        if remaining <= 0:
            raise MemoryRequestError("busy")
        return remaining

    async def recall_for_session(self, session_id, query, turn_id="", *, deadline=None):
        # Automatic recall has no consumer until a relevance gate is configured.
        # Indexing, explicit core search and independent source filtering still run.
        if not self.config.enabled or self._automatic_threshold is None:
            return None
        self._available()
        deadline = deadline if deadline is not None else deadline_now() + self.config.recall_timeout_s
        context = self.context(session_id)
        # Current request plus up to two eligible prior USER utterances only.
        # Source masks run before these strings become retrieval input.
        history = sm.conversation_history.model_recent()
        prior = [row["content"] for row in history if row["role"] == "user"
            and (not turn_id or row.get("turn_id") != turn_id)][-2:]
        result = await self._query.run(lambda:self._store.recall(self._access(context),
            query, recent_context=tuple(reversed(prior)), now_ms=time.time_ns() // 1_000_000,
            minimum_semantic_score=self._automatic_threshold,
            limit=MEMORY_CANDIDATE_LIMIT, max_chars=MEMORY_CANDIDATE_CHAR_BUDGET), timeout=self._remaining(deadline))
        self._remaining(deadline)
        return MemoryTurnReference(context, result, deadline)

    async def validate_reference(self, reference, *, deadline=None):
        if not isinstance(reference, MemoryTurnReference):
            return None
        # Retrieval may already have completed before an independent role RAG
        # wait. Final validity has one short window at use, never another recall.
        final_deadline = deadline_now() + MEMORY_FINAL_VALIDITY_TIMEOUT_S
        deadline = min(deadline, final_deadline) if deadline is not None else final_deadline
        context = self.context(reference.context["session_id"], token=reference.context["context_token"])
        result = await self._db.run(lambda:self._store.revalidate(self._access(context),
            reference.result.candidates, now_ms=time.time_ns() // 1_000_000,
            limit=MEMORY_CANDIDATE_LIMIT, max_chars=MEMORY_CANDIDATE_CHAR_BUDGET),
            timeout=self._remaining(deadline), priority=True)
        self.context(context["session_id"], token=context["context_token"])
        self._sync_projection()
        if not self._exclusions_ready:
            raise SourcePolicyUnavailable("memory_source_policy_recovery_required")
        pending = {mid for item in self._policy.value["pending"] for mid in item["memory_ids"]}
        threshold = self._automatic_threshold
        valid = [candidate for candidate in result.candidates if candidate.memory_id not in pending
            and threshold is not None and candidate.semantic_score is not None
            and candidate.semantic_score >= threshold]
        admitted, budget, budget_omitted = pack_reference(valid,
            token_count=self._reply_token_count, count_method=self._reply_token_count_method,
            byte_limit=self.config.context_bytes)
        omitted = len(result.candidates) - len(valid)
        coverage = asdict(reference.result.coverage)
        coverage["budget_omitted"] += result.coverage.budget_omitted + budget_omitted
        coverage["automatic_omitted"] = omitted
        coverage["context_budget"] = budget
        self._remaining(deadline)
        return {**admitted, "coverage":coverage}

    async def lookup_for_session(self, session_id, query, *, input_id="", turn_id="", question="",
                                 prepare_choice=False):
        """Explicit search in the Host-bound scope of a previously accepted turn."""
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise MemoryRequestError("invalid_request")
        self._available()
        context = self.context(session_id)
        deadline = deadline_now() + self.config.explicit_lookup_timeout_s
        if self._source_lookup is not None:
            current, clock = self._lookup_question(context, input_id, turn_id, question)
            self._lookup_context(context, ())
            result = await self._query.run(lambda:self._source_lookup.rank(
                self._access(context), query.strip(), current=current, question_time=clock,
                **({"prepare_choice": True} if prepare_choice else {})),
                timeout=self._remaining(deadline))
            self._remaining(deadline)
            return MemoryTurnReference(context, result, deadline)
        result = await self._query.run(lambda:self._store.recall(self._access(context),
            query.strip(), ranking="semantic_first", now_ms=time.time_ns() // 1_000_000,
            limit=MEMORY_CANDIDATE_LIMIT, max_chars=MEMORY_CANDIDATE_CHAR_BUDGET),
            timeout=self._remaining(deadline))
        self._remaining(deadline)
        return MemoryTurnReference(context, result, deadline)

    def _lookup_question(self, context, input_id, turn_id, question):
        unknown = {"status":"unknown"}
        if not input_id or not turn_id:
            return None, unknown
        try:
            row = sm.read_committed_session_source(context["session_id"], input_id, revision=1)
        except (LookupError, ValueError):
            return None, unknown
        if (row["role"], row["turn_id"], row.get("incarnation")) != (
                "user", turn_id, context.get("incarnation")):
            return None, unknown
        source = self._api.SourceSpan(**journal.coordinate(
            context["session_id"], input_id, row["revision"], context.get("incarnation")))
        if row["text"] != question:
            return source, unknown
        clock = self._api.format_recorded_time(row["created_at_ms"])
        if clock.status != "known":
            return source, unknown
        return source, {"status":"known", "utc":clock.utc,
                        "utc_offset_minutes":row.get("utc_offset_minutes")}

    def history_policy_token(self):
        # Retain the value object in the turn guard; a recycled Python id must
        # not make two different published policy snapshots appear identical.
        return (self._policy.value, self._exclusions_ready, self.config.enabled, self._store is not None)

    def _derived_history_sources(self, rows):
        from core.chat_history_projection import project_role_history
        self._sync_projection()
        for row in rows[-2 * sm.PROMPT_HISTORY_ROUNDS:]:
            source = row.get("_source")
            if (source is not None and _message_key(source.get("session_id"), source)
                    in self._derived_sources and project_role_history((row,))):
                yield source

    def history_is_derived(self, rows):
        return next(self._derived_history_sources(rows), None) is not None

    async def history_dependencies(self, session_id, rows):
        sources = tuple(self._derived_history_sources(rows))
        if not sources:
            # Ordinary history stays usable without core availability. Only a
            # known derived output requires the durable reference owner.
            return None, rows
        def independent_rows():
            return [row for row in rows if not (row.get("_source") is not None
                and _message_key(row["_source"].get("session_id"), row["_source"]) in self._derived_sources)]
        context = self.context(session_id)
        spans = [self._api.SourceSpan(**journal.coordinate(source["session_id"], source["message_id"],
            source["revision"], source.get("incarnation"))) for source in sources]
        deadline = deadline_now() + self.config.explicit_validity_timeout_s
        try:
            self._available()
            if self._source_lookup is not None:
                allowed, signatures = await self._db.run(lambda:self._current_history_outputs(context, spans),
                    timeout=self._remaining(deadline), priority=True)
                self._sources.require_current(signatures)
                by_key = {(s.namespace, s.record_key, s.revision) for s, _ in allowed}
                rejected = {_message_key(item["session_id"], item) for item, span in zip(sources, spans)
                            if (span.namespace, span.record_key, span.revision) not in by_key}
                references = tuple(dict.fromkeys(ref for _, refs in allowed for ref in refs))
                self._lookup_context(context, references)
                kept = [row for row in rows if row.get("_source") is None or
                        _message_key(row["_source"]["session_id"], row["_source"]) not in rejected]
                reads = self._api.OutputReadSet(outputs=tuple(span for span, _ in allowed))
                evidence = MemoryLookupEvidence(context, references, {}, read_set=reads) if allowed else None
                return evidence, kept
            references = await self._db.run(lambda:self._store.recall_use_dependencies(self._access(context),
                spans, now_ms=time.time_ns() // 1_000_000), timeout=self._remaining(deadline), priority=True)
            self._lookup_context(context, references)
            if not references:
                raise MemoryRequestError("conflict")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Before the first model call, retaining independent history is
            # sufficient: an unreadable/busy relation never admits an
            # unprotected derived answer and need not block ordinary dialogue.
            self.context(session_id, token=context["context_token"])
            return None, independent_rows()
        return MemoryLookupEvidence(context, references, {}), rows

    def _current_history_outputs(self, context, spans):
        access = self._access(context)
        allowed = self._store._filter_recall_use_read_sets(
            access, spans, now_ms=time.time_ns() // 1_000_000)
        view = self._sources.view(access)
        valid, signatures = [], {}
        checked = {}

        def current(output):
            if output not in checked:
                try:
                    sid, _ = view.output_state(output)
                    path, signature, *_ = view.files[sid]
                    checked[output] = (path, signature)
                except (FileNotFoundError, MemoryRequestError):
                    checked[output] = None
            return checked[output]

        for output, reads in allowed:
            if all(current(parent) is not None for parent in reads.outputs):
                valid.append((output, reads.memories))
                signatures.update(checked[parent] for parent in reads.outputs)
        return valid, tuple(signatures.items())

    def _lookup_context(self, context, candidates):
        self.context(context["session_id"], token=context["context_token"])
        self._sync_projection()
        if not self._exclusions_ready:
            raise SourcePolicyUnavailable("memory_source_policy_recovery_required")
        pending = {mid for item in self._policy.value["pending"] for mid in item["memory_ids"]}
        if any(candidate.memory_id in pending for candidate in candidates):
            raise MemoryRequestError("conflict")

    async def validate_lookup(self, reference, *, original_choice=None):
        if not isinstance(reference, MemoryTurnReference):
            raise MemoryRequestError("invalid_request")
        context = reference.context
        if self._source_lookup is not None:
            from server.memory_source_lookup import RankedLookup
            if isinstance(reference.result, RankedLookup):
                return await self._validate_source_lookup(context, reference.result, original_choice=original_choice)
        self._lookup_context(context, reference.result.candidates)
        deadline = deadline_now() + self.config.explicit_validity_timeout_s
        result = await self._db.run(lambda:self._store.revalidate(self._access(context),
            reference.result.candidates, now_ms=time.time_ns() // 1_000_000,
            limit=MEMORY_CANDIDATE_LIMIT, max_chars=MEMORY_CANDIDATE_CHAR_BUDGET),
            timeout=self._remaining(deadline), priority=True)
        self._lookup_context(context, reference.result.candidates)
        expected = {(item.memory_id, item.row_version) for item in reference.result.candidates}
        if {(item.memory_id, item.row_version) for item in result.candidates} != expected:
            raise MemoryRequestError("conflict")
        selected = []
        admitted, budget, omitted = pack_reference(result.candidates,
            token_count=self._reply_token_count, count_method=self._reply_token_count_method,
            token_limit=1024, byte_limit=8192, item_limit=8, selected=selected)
        coverage = asdict(reference.result.coverage)
        coverage["budget_omitted"] += result.coverage.budget_omitted + omitted
        coverage["context_budget"] = budget
        self._remaining(deadline)
        return MemoryLookupEvidence(context, tuple(selected), {**admitted, "coverage":coverage})

    async def _validate_source_lookup(self, context, ranked, *, original_choice=None):
        self._lookup_context(context, ())
        deadline = deadline_now() + self.config.explicit_validity_timeout_s
        counter = self._reply_token_count or estimate_reply_tokens
        method = self._reply_token_count_method or "ascii_3_nonascii_2_nonbmp_4_estimate"
        choices = []
        options = {"_original_choice":(original_choice[0], choices)} if original_choice is not None else {}
        reads, signatures, payload, package = await self._db.run(lambda:self._source_lookup.assemble(
            self._access(context), ranked, counter=counter, method=method, **options),
            timeout=self._remaining(deadline), priority=True)
        self._lookup_context(context, reads.memories)
        self._sources.require_current(signatures)
        self._remaining(deadline)
        evidence = MemoryLookupEvidence(context, reads.memories, payload, reads, ranked.question_time, package)
        if not choices:
            return evidence
        choice, = choices
        try:
            self._sources.require_current(choice.signatures)
        except self._source_lookup.stale as error:
            raise MemoryRequestError("conflict") from error
        # Selection never holds the DB lane or the publication boundary.
        proposal = await original_choice[1](list(choice.messages), json_output=True)
        self._lookup_context(context, evidence.candidates)
        from server.memory_original_choice import apply

        try:
            selected, checked = await self._db.run(lambda:apply(self._source_lookup,
                self._access(context), evidence, choice, proposal, counter, method),
                timeout=self.config.explicit_validity_timeout_s, priority=True)
            self._sources.require_current(checked)
        except self._source_lookup.stale as error:
            if isinstance(error, MemoryRequestError) and error.code == "busy":
                raise
            raise MemoryRequestError("conflict") from error
        self._lookup_context(context, selected.candidates)
        return selected

    async def supplement_lookup(self, evidence, action, *, user_requested=False):
        if self._source_lookup is None or evidence.pack is None:
            raise MemoryRequestError("invalid_request")
        context = evidence.context
        self._lookup_context(context, evidence.candidates)
        access = self._access(context)
        deadline = deadline_now() + self.config.explicit_lookup_timeout_s
        ranked = ()
        if "query" in action:
            ranked = await self._query.run(lambda:self._source_lookup.search_originals(access, action["query"]),
                timeout=self._remaining(deadline))
        counter = self._reply_token_count or estimate_reply_tokens
        method = self._reply_token_count_method or "ascii_3_nonascii_2_nonbmp_4_estimate"
        try:
            updated, signatures = await self._db.run(lambda:self._source_lookup.supplement(access,
                evidence.pack, evidence.read_set, action, ranked, payload=evidence.payload,
                counter=counter, method=method, user_requested=user_requested),
                priority=True, timeout=self._remaining(deadline))
        except (FileNotFoundError, self._api.MemoryConflict, self._api.MemoryNotFound,
                self._api.MemoryValidationError):
            raise MemoryRequestError("conflict") from None
        self._lookup_context(context, evidence.candidates)
        self._sources.require_current(signatures)
        self._remaining(deadline)
        if updated is None:
            return None
        reads, _, payload, package = updated
        return replace(evidence, candidates=reads.memories, payload=payload, read_set=reads, pack=package)

    @asynccontextmanager
    async def lookup_delivery(self, evidence, *, message_id, revision=1):
        """Register exact output provenance before its first publication.

        The caller holds this context through publication start. Later mutations
        mask this whole answer from model history; already displayed text cannot
        be retracted by a source policy.
        """
        if (not isinstance(evidence, MemoryLookupEvidence) or not message_id
                or type(revision) is not int or revision < 1):
            raise MemoryRequestError("invalid_request")
        await self._publication_lock.acquire()
        released = False
        def publication_started():
            nonlocal released
            if not released:
                released = True
                self._publication_lock.release()
        try:
            context = evidence.context
            self._lookup_context(context, evidence.candidates)
            source = self._api.SourceSpan(**journal.coordinate(context["session_id"], message_id,
                revision, context.get("incarnation")))
            deadline = deadline_now() + self.config.explicit_validity_timeout_s
            def register():
                access, now_ms = self._access(context), time.time_ns() // 1_000_000
                watches = []
                try:
                    if evidence.read_set is None:
                        self._store.register_recall_use(access, evidence.candidates, source=source, now_ms=now_ms)
                        signatures = ()
                    else:
                        purpose = "ordinary" if evidence.pack is None else "lookup"
                        reads = self._source_lookup.capture(access, evidence.read_set, purpose=purpose)
                        signatures = self._source_lookup.require_reads(
                            access, reads, self._sources.view(access), purpose=purpose,
                            record_watches=watches.extend if self.output_speech is not None else None)
                        self._store.register_output_use(
                            access, reads, source=source, now_ms=now_ms, purpose=purpose)
                except (FileNotFoundError, self._api.MemoryConflict, self._api.MemoryNotFound,
                        self._api.MemoryValidationError):
                    raise MemoryRequestError("conflict") from None
                self._policy.register_derived(source)
                return signatures, tuple(watches)
            reads = evidence.read_set
            registered = bool(evidence.candidates or (reads and
                (reads.sources or reads.memories or reads.outputs or reads.candidates)))
            if registered:
                signatures, watches = await self._db.run(
                    register, timeout=self._remaining(deadline), priority=True, mutation=True)
            speech_scope = (self.output_speech.publication(
                context["session_id"], message_id, revision, source, watches=watches)
                if registered and self.output_speech is not None else nullcontext())
            with speech_scope:
                if registered and signatures:
                    # Install the output's watcher before this last file-version check.
                    self._sources.require_current(signatures)
                self._lookup_context(context, evidence.candidates)
                self._remaining(deadline)
                yield publication_started
        finally:
            publication_started()

    async def wait_closed(self):
        """Process-owner join; the UI-facing close keeps its existing bound."""
        if self._close_task is None:
            await self.close()
        if self._close_task is not None:
            await asyncio.shield(self._close_task)

    async def close(self):
        if self._close_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._close_task), .5)
            except TimeoutError:
                pass
            return
        self._closing = True
        async def finish():
            # Keep completion owners alive until already running work settles;
            # cancellation of a UI awaiter never orphans a SQLite owner/commit.
            if self._startup is not None:
                await asyncio.gather(self._startup, return_exceptions=True)
            if self._maintenance is not None:
                await self._maintenance.close()
            if self._sources is not None:
                await self._sources.close()
            if self._index_task is not None and not self._index_task.done():
                self._index_task.cancel()
                await asyncio.gather(self._index_task, return_exceptions=True)
            for lane in (self._db, self._query, self._model):
                if lane._future is not None:
                    await asyncio.gather(asyncio.wrap_future(lane._future), return_exceptions=True)
            if self._store is not None:
                await self._db.run(self._store.close, timeout=None)
            for lane in (self._db, self._query, self._model):
                lane.close()
        task = self._close_task = asyncio.create_task(finish())
        try:
            await asyncio.wait_for(asyncio.shield(task), .5)
        except TimeoutError:
            # The process owner completes cleanup after the synchronous job;
            # caller shutdown is bounded and submits no replacement jobs.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
