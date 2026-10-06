"""Paired current-owner Hybrid acceptance; frozen context, inert execution.

This is a new controlled acceptance fixture, not a replay or rescore of the
September experiment. Default invocation only publishes the frozen manifest.
Live invocation requires its reviewed hash and uses the shipping message client
and HTTP head. Model interpretation, Host acceptance and effect delivery all
remain in the current Cooperative implementation.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack, aclosing
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name("fixtures") / "cooperative_hybrid_semantics_v1.json"
FROZEN_SHA256 = "5a763aceac87eeb1d7e7726efd43b242bd43c57eb9b5641b3711c7978eef0a77"
_QUERY_KIND: ContextVar[str] = ContextVar("hybrid_probe_query_kind", default="unclassified")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def frozen_text_sha256(path: Path) -> str:
    # These reviewed JSON snapshot hashes were recorded with Windows CRLF.
    # Git uses LF for fixtures; newline conversion must not change their identity.
    raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return hashlib.sha256(raw).hexdigest()


def load_fixture(path: Path = FIXTURE) -> dict:
    raw = path.read_bytes()
    if frozen_text_sha256(path) != FROZEN_SHA256:
        raise ValueError("fixture differs from the reviewed frozen manifest")
    fixture = json.loads(raw)
    if fixture["status"] != "frozen_for_current_acceptance":
        raise ValueError("fixture is not frozen for current acceptance")
    for key in ("historical_source", "persona_source"):
        source = fixture[key]
        if frozen_text_sha256(ROOT / source["path"]) != source["sha256"]:
            raise ValueError(key + " changed after fixture creation")
    assert len(fixture["routing"]) == 30
    assert sum(row["scored"] for row in fixture["routing"]) == 26
    assert len(fixture["persona"]) == 15 and len(fixture["new_persona"]) == 2
    assert len({row["case"] for section in ("routing", "persona", "new_persona")
        for row in fixture[section]}) == 47
    return fixture


def expanded_case(fixture: dict, row: dict, section: str) -> dict:
    profile = fixture["profiles"][row["profile"]]
    tasks = [{"key": key, **deepcopy(fixture["tasks"][key]),
        "status": profile.get("task_status", {}).get(key, fixture["tasks"][key]["status"])}
        for key in profile["tasks"]]
    result = {**deepcopy(row), "section": section, "seed": {
        "session_id": "hybrid-semantic-" + row["case"],
        "project_id": "hybrid-semantic-project",
        "project_name": "Controlled acceptance project",
        "tasks": tasks, "bound_work": profile["bound_work"],
        "task_destinations": {task["key"]: {"project_id": "seed-project-" + task["key"],
            "project_name": task["title"] + " acceptance destination",
            "workspace": "seed-workspaces/" + task["key"]} for task in tasks},
        "history": deepcopy(row.get("history", profile["history"])),
        "clock": 1700000000.0, "rag_enabled": False,
        "provider_adapter": "registered inert codex; no filesystem/browser/task effects"}}
    result["seed_sha256"] = digest(result["seed"])
    return result


def select_cases(fixture: dict, case_ids: list[str] | None = None) -> list[dict]:
    rows = [expanded_case(fixture, row, section)
        for section in ("routing", "persona", "new_persona") for row in fixture[section]]
    if case_ids:
        unknown = set(case_ids) - {row["case"] for row in rows}
        if unknown:
            raise ValueError("unknown cases: " + ", ".join(sorted(unknown)))
        rows = [row for row in rows if row["case"] in case_ids]
    return rows


def preparation_report(fixture: dict, rows: list[dict], *, arms: list[dict] | None = None,
                       repeat: int = 1, max_calls: int = 1500) -> dict:
    arms = arms or fixture["arms"]
    return {"schema": "amadeus.cooperative-hybrid-semantic-report.v1",
        "status": "prepared_not_run", "fixture_sha256": FROZEN_SHA256,
        "instrument_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "current_seed_manifest_sha256": digest([{key: row[key] for key in ("case", "seed", "seed_sha256")} for row in rows]),
        "fixture": deepcopy(fixture), "cases": rows, "arms": arms, "repeat": repeat,
        "scope": fixture["scope"], "acceptance": {
            "routing": "pending real outcomes", "persona": "pending real outcomes and human review"},
        "call_budget": {"user_turn_attempts": len(rows) * len(arms) * repeat,
            "head_attempts_upper_bound": len(rows) * sum(arm["head"] for arm in arms) * repeat,
            "maximum_observed_remote_invocations": max_calls,
            "auxiliary_calls": "Planner, typed-reference and presentation calls are dynamic; all observed.",
            "transport_scope": "Canonical Cooperative-client invocations, shipping Work Narrator SDK invocations and HTTP-head producer attempts; SDK internal retries are not inferred.",
            "cost_bound": "No price estimate. Remote invocations stop at the configured global budget; per-query token/timeout settings are recorded."}}


class QueryObserver:
    """Observe the canonical query port without adding a decision or retry."""

    def __init__(self, transport, *, max_calls: int = 1500):
        self.transport = transport
        self.max_calls = max_calls
        self.calls: list[dict] = []
        self.phase = "seed"
        self.case = ""
        self.arm = ""
        self._lock = threading.Lock()
        self.native_clients = []

    def query(self, messages, **kwargs):
        from llm import client

        with self._lock:
            remote_count = sum(call["kind"] != "head" for call in self.calls)
            if remote_count >= self.max_calls:
                raise RuntimeError("semantic probe remote query budget exhausted")
            if self.phase != "evaluation":
                raise RuntimeError("model query attempted outside evaluation")
            call = {"index": len(self.calls), "case": self.case, "arm": self.arm,
                "phase": self.phase, "kind": _QUERY_KIND.get(),
                "provider": client.LLM_PROVIDER, "messages": deepcopy(messages),
                "parameters": {key: value for key, value in kwargs.items()
                    if key not in {"on_text", "response_observer"}}, "chunks": [], "status": "started"}
            self.calls.append(call)
        prior_observer = kwargs.get("response_observer")

        def observe(response):
            call["response"] = dict(response)
            if prior_observer:
                prior_observer(response)

        kwargs["response_observer"] = observe
        accept = kwargs.get("on_text")
        if accept is not None:
            def text(delta):
                call["chunks"].append(delta)
                return accept(delta)
            kwargs["on_text"] = text
        try:
            result = self.transport(messages, **kwargs)
            call.update(status="completed", result=result)
            return result
        except BaseException as exc:
            # Professional streaming handoff is a deliberate current-owner stop.
            call.update(status="handoff" if type(exc).__name__ == "RoleDelegateReady" else "error",
                error=type(exc).__name__ + ": " + str(exc))
            if hasattr(exc, "raw"):
                call["result"] = exc.raw
            raise

    def head_tokens(self, original):
        async def tokens(messages):
            from llm import hybrid_stream
            call = {"index": len(self.calls), "case": self.case, "arm": self.arm,
                "phase": self.phase, "kind": "head", "messages": deepcopy(messages),
                "endpoint": hybrid_stream.HYBRID_LOCAL_LLM_URL,
                "model": hybrid_stream.HYBRID_LOCAL_LLM_MODEL,
                "parameters": {"stream": True, "temperature": 0.35,
                    "top_p": 0.9, "max_tokens": 80, "cache_prompt": True},
                "chunks": [], "status": "started"}
            self.calls.append(call)
            try:
                async with aclosing(original(messages)) as source:
                    async for chunk in source:
                        call["chunks"].append(chunk)
                        yield chunk
                call["status"] = "completed"
            except GeneratorExit:
                call["status"] = "first_sentence_closed"
                raise
            except BaseException as exc:
                call.update(status="error", error=type(exc).__name__ + ": " + str(exc))
                raise
        return tokens

    def observer_client(self, factory):
        """The shipping Work Narrator has its own SDK port; observe it too."""
        def client(provider):
            native = factory(provider)
            self.native_clients.append(native)
            create = native.chat.completions.create

            def observed(**kwargs):
                with self._lock:
                    if sum(call["kind"] != "head" for call in self.calls) >= self.max_calls:
                        raise RuntimeError("semantic probe remote query budget exhausted")
                    if self.phase != "evaluation":
                        raise RuntimeError("model query attempted outside evaluation")
                    call = {"index": len(self.calls), "case": self.case, "arm": self.arm,
                        "phase": self.phase, "kind": "report_presentation",
                        "transport": "production_work_observer_sdk", "provider": provider,
                        "messages": deepcopy(kwargs["messages"]),
                        "parameters": {key: value for key, value in kwargs.items() if key != "messages"},
                        "status": "started"}
                    self.calls.append(call)
                try:
                    response = create(**kwargs)
                    call.update(status="completed", result=str(response.choices[0].message.content or ""),
                        response={"requested_model": kwargs.get("model"),
                            "response_model": getattr(response, "model", None),
                            "response_id": getattr(response, "id", None)})
                    return response
                except BaseException as exc:
                    call.update(status="error", error=type(exc).__name__ + ": " + str(exc))
                    raise
            native.chat.completions.create = observed
            return native
        return client


class InertAdapter:
    """A registered native Provider that records delivery and holds execution."""

    provider_id = "codex"

    def __init__(self):
        from agent_host.provider_contract import ProviderCapabilities, ProviderManifest

        self.manifest = ProviderManifest(provider_id=self.provider_id, display_name="Inert Codex acceptance adapter",
            capabilities=ProviderCapabilities(task_kinds=("general", "workspace_mutation"),
                workspace_access="write", workspace_ownership="caller", resume="attach",
                cancellation="confirmed", interaction="bidirectional", append_input=True))
        self.events: list[dict] = []
        self.handles: dict[str, Any] = {}
        self.releases: dict[str, asyncio.Event] = {}
        self.started: dict[str, asyncio.Event] = {}
        self.cancelled: set[str] = set()
        self.phase = "seed"

    async def run(self, request, run_id, emit):
        from agent_host.provider_types import ProviderEvent, ProviderRunResult, ProviderSessionHandle

        handle = request.session or ProviderSessionHandle(provider=self.provider_id,
            session_id="inert-native-" + run_id, scope="interaction")
        self.handles[run_id] = handle
        self.releases.setdefault(run_id, asyncio.Event())
        self.events.append({"phase": self.phase, "kind": "start", "run_id": run_id,
            "task": request.task, "cwd": str(request.cwd or ""), "session": handle.to_dict(),
            "requirements": request.requirements.to_dict(), "metadata": deepcopy(request.metadata)})
        await emit(ProviderEvent(provider=self.provider_id, run_id=run_id,
            type="session.opened", session=handle))
        self.started.setdefault(run_id, asyncio.Event()).set()
        await self.releases[run_id].wait()
        return ProviderRunResult(status="cancelled" if run_id in self.cancelled else "done",
            result="Inert fixture execution ended; no requested side effect was performed.", session=handle)

    async def append_input(self, run_id, text):
        from agent_host.provider_types import ProviderInputDelivery

        self.events.append({"phase": self.phase, "kind": "input", "run_id": run_id, "text": text})
        return ProviderInputDelivery(state="delivered")

    async def cancel(self, run_id):
        self.events.append({"phase": self.phase, "kind": "cancel", "run_id": run_id})
        self.cancelled.add(run_id)
        self.releases.setdefault(run_id, asyncio.Event()).set()
        return {"confirmed": True, "cancelled": True, "session": self.handles.get(run_id)}

    def release_all(self):
        for event in self.releases.values():
            event.set()


def _stable_uuid_module(namespace: str):
    ordinal = 0
    def uuid4():
        nonlocal ordinal
        ordinal += 1
        return uuid.uuid5(uuid.NAMESPACE_URL, namespace + ":" + str(ordinal))
    return SimpleNamespace(uuid4=uuid4, uuid5=uuid.uuid5, NAMESPACE_URL=uuid.NAMESPACE_URL)


class CaseHost:
    """Isolated composition of the existing Session/Chat/Work/Runtime owners."""

    def __init__(self, root: Path, row: dict, arm: dict, observer: QueryObserver):
        self.root, self.row, self.arm, self.observer = root, row, arm, observer
        self.patches = ExitStack()
        self.display: list[dict] = []
        self.partial: list[dict] = []
        self.transport_events: list[dict] = []

    async def __aenter__(self):
        try:
            return await self._open()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def _open(self):
        from agent_host.provider_contract import ProviderRequirements
        from agent_host.provider_runtime import ProviderRuntime
        from agent_host.work_ledger_store import WorkLedgerStore
        from config import settings
        from core import session_manager as sm
        from core.chat_runtime import ChatRuntime
        import core.turn_coordinator as tc
        from llm import client, hybrid_stream
        from llm.prompts import get_system_prompt
        from server.attention_request import AttentionRequestCoordinator
        from server.chat_role_delivery import ChatRoleDelivery
        from server.control_ledger import ControlLedgerStore
        from server.cooperative_chat_ingress import CooperativeChatManager
        from server.cooperative_delivery import CooperativeHostDelivery
        from server.event_bus import bus
        from server.handlers.chat_handler import ChatHandler
        from server.handlers.work_ledger_handler import WorkLedgerHandler
        from server.protocol import Method
        from server.work_control import WorkControl
        from server.work_destination_service import WorkDestinationService
        from server.work_effect_executor import WorkEffectExecutor
        from server.work_ledger_coordinator import WorkLedgerCoordinator
        from server.work_planner import RuntimeWorkPlanner
        from server import app, work_observer_llm
        from server.work_observer import WorkObserverCoordinator

        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace = self.root / "project"
        self.workspace.mkdir(exist_ok=True)
        self.scratch = self.root / "scratch"
        self.scratch.mkdir(exist_ok=True)
        self.session_id = self.row["seed"]["session_id"]
        self.database = self.root / (self.arm["name"] + ".sqlite3")
        if self.database.exists():
            raise ValueError("case database already exists; use a fresh experiment directory")
        p = self.patches.enter_context
        p(patch.object(sm, "_SESSION_DIR", str(self.root / (self.arm["name"] + "-sessions"))))
        p(patch.object(sm, "_CURRENT_SESSION_ID", None))
        p(patch.object(sm, "_SESSION_SELECTION_REVISION", 0))
        p(patch.object(sm, "_activation_guard", None))
        p(patch.object(sm, "conversation_history", sm.ConversationHistory()))
        p(patch.object(tc, "coordinator", tc.TurnCoordinator()))
        p(patch.object(settings, "RAG_ENABLED", False))
        p(patch.object(settings, "WORK_WORKTREE_ISOLATION", False))
        p(patch.object(settings, "WORK_PROJECT_ALLOWLIST", str(self.root)))
        p(patch.object(settings, "WORK_SCRATCH_ROOT", str(self.scratch)))
        # Deterministic fixture identities, not a model/decision substitution.
        for module in ("agent_host.provider_runtime", "server.cooperative_provider_loop",
                       "server.cooperative_context_store", "agent_host.work_ledger_types", "server.control_ledger"):
            p(patch(module + ".uuid", _stable_uuid_module(self.row["case"] + ":" + module)))
        sm.create_session(self.session_id)
        self.work = WorkLedgerStore(self.database, clock=lambda: self.row["seed"]["clock"])
        self.project = self.work.create_or_get_project(self.workspace,
            project_id=self.row["seed"]["project_id"], name=self.row["seed"]["project_name"])
        self.ledger = ControlLedgerStore(self.database, clock=lambda: self.row["seed"]["clock"])
        self.control = WorkControl(self.ledger, self.work)
        self.coordinator = WorkLedgerCoordinator(self.work, work_control=self.control,
            current_session_id=lambda: self.session_id, clock=lambda: self.row["seed"]["clock"])
        registry = lambda path: Path(path).resolve().is_relative_to(self.root.resolve())
        p(patch("server.work_ledger_coordinator.cwd_in_project_registry", registry))
        self.destination = WorkDestinationService(self.work, registry_check=registry,
            scratch_root_provider=lambda: self.scratch)
        self.coordinator.destination = self.destination
        self.coordinator.configure()
        self.coordinator.bind_session_context(self.session_id, self.project.project_id)
        self.runtime = ProviderRuntime()
        p(patch("agent_host.provider_runtime.runtime", self.runtime))
        self.adapter = InertAdapter()
        self.runtime.register(self.adapter)
        self.executor = WorkEffectExecutor(self.control, self.runtime, self.coordinator)
        self.handler = ChatHandler()
        self.presenter = ChatRuntime()
        self.attention = AttentionRequestCoordinator()
        self.requirements = ProviderRequirements(task_kind="general", workspace_access="write",
            workspace_ownership="caller", ownership="managed", resume="attach")
        self.persona = get_system_prompt("base")
        self.role_delivery = ChatRoleDelivery()

        async def display(event):
            accepted = await self.role_delivery.publish(event)
            if accepted:
                self.display.append(deepcopy(event))
            return accepted

        async def partial(event):
            accepted = await self.role_delivery.publish_partial(event)
            if accepted:
                self.partial.append(deepcopy(event))
            return accepted

        self.delivery = CooperativeHostDelivery(session_id=self.session_id, display=display,
            partial_display=partial, allows=self.role_delivery.allows, record_display=sm.append_session_message,
            role_stream_factory=lambda cause, **kwargs: self.presenter.begin_role_text_stream(
                turn_id=cause, speech=False, **kwargs))
        self.manager = CooperativeChatManager(self.handler, ledger=self.ledger,
            fence_scope="cooperative:hybrid-semantic", provider="codex", runtime=self.runtime,
            context_requirements={"codex": self.requirements}, allocate=self.allocate,
            query=self.query_role, persona=self.persona, publish_factory=lambda _sid: self.delivery,
            hybrid_head=hybrid_stream.hybrid_local_head if self.arm["head"] else None,
            permission_policy="ask", permission_store=self.work, destination=self.destination,
            attention=self.attention)
        self.input_owner = WorkLedgerHandler(self.coordinator, provider_input=self.runtime.append_input)
        self.control.cooperative_context_resolver = self.manager.resolve_work_recipient
        self.manager.configure_work(self.control, self.executor,
            input_request=self.input_owner.submit_input, report_request=self.report)
        if self.arm["strategy"] == "professional":
            self.manager.work_planner = RuntimeWorkPlanner(coordinator=self.coordinator,
                query=self.query_planner, provider="codex",
                project_limit=settings.CONTROL_DECISION_PROJECT_LIMIT,
                work_item_limit=settings.CONTROL_DECISION_WORK_ITEM_LIMIT,
                candidate_limit=settings.CONTROL_DECISION_EXHAUSTIVE_CANDIDATE_LIMIT)

        def prepare(request, run_id, intake_authority=None):
            if getattr(intake_authority, "kind", "") == "cooperative_provider_effect":
                return self.manager.prepare_runtime_request(request, run_id, intake_authority)
            return self.coordinator.prepare_request(request, run_id, intake_authority)

        self.runtime.set_request_preparer(prepare)
        self.runtime.set_native_session_checkpoint(self.manager.checkpoint_native_session)
        self.manager.install()
        p(patch.object(client, "remote_llm_messages_query", self.observer.query))
        p(patch.object(hybrid_stream, "local_head_tokens",
            self.observer.head_tokens(hybrid_stream.local_head_tokens)))

        async def capture(method, params):
            self.transport_events.append({"method": method, "params": deepcopy(params)})

        self.capture = capture
        self.methods = (Method.CHAT_TOKEN, Method.CHAT_COMPLETE, Method.CHAT_ERROR,
            Method.CHAT_ROLE_MESSAGE, Method.ATTENTION_UPDATED)
        for method in self.methods:
            bus.on(method, capture)
        self.ingress = await self.manager._ingress_for(self.session_id)
        await self.seed()
        self.narrator = WorkObserverCoordinator()
        self.narrator.configure(is_chat_busy=self.handler.is_busy, is_tts_busy=lambda: False,
            get_recent_chat=lambda _sid: deepcopy(sm.conversation_history.snapshot().dialog),
            display_language=app._observer_display_language, observer_llm=app._run_work_observer_llm)
        p(patch.object(app, "work_status_narrator", self.narrator))
        p(patch.object(work_observer_llm, "_client", self.observer.observer_client(work_observer_llm._client)))
        # The Narrator's provider choice is frozen across all paired arms.
        p(patch.object(settings, "WORK_OBSERVER_PROVIDER",
            "openai" if client.LLM_PROVIDER in {"openai", "hybrid3"} else "deepseek"))
        self.seed_snapshot = self.snapshot()
        self.seed_projection = {
            "history": deepcopy(self.row["seed"]["history"]),
            "bound_context_id": self.ingress.loop.bound_context_id,
            "contexts": self.ingress.loop.context_catalog(),
            "current_work": self.manager.work_for_recipient(self.session_id, self.ingress.loop.bound_context_id),
            "active_work": self.manager.active_work_for_recipient(self.session_id, self.ingress.loop.bound_context_id),
            "bindings": deepcopy(self.seed_bindings),
            "work_items": [{key: getattr(item, key) for key in ("work_item_id", "project_id", "title", "goal", "state", "workspace_path")}
                for item in self.work.list_work_items()]}
        self.observer.phase = self.adapter.phase = "evaluation"
        self.observer.case, self.observer.arm = self.row["case"], self.arm["name"]
        return self

    def allocate(self, label, context_id):
        # Host-owned temporary destinations; adapters never write into them.
        target = self.scratch / context_id
        target.mkdir(exist_ok=True)
        return target

    async def query_role(self, messages, *, visual_context=None, on_text=None, json_output=None):
        from config import settings
        from llm import client
        from llm.prompts import finalize_system_prompt_language, wrap_user_message_for_language_lock
        from server.cooperative_delivery import query_role_messages

        role_messages = [dict(message) for message in messages]
        role_messages[0]["content"] = finalize_system_prompt_language(role_messages[0]["content"])
        try:
            frame = json.loads(role_messages[-1]["content"])
        except (TypeError, ValueError):
            frame = None
        source = frame.get("source_kind") if isinstance(frame, dict) else None
        if source == "user":
            role_messages[-1]["content"] = wrap_user_message_for_language_lock(role_messages[-1]["content"])
        kind = "role" if source == "user" else "presentation" if source in {"provider", "host_receipt"} else "reference"
        token = _QUERY_KIND.set(kind)
        try:
            return await query_role_messages(client.remote_llm_messages_query, role_messages,
                json_output=source not in {"provider", "host_receipt"} if json_output is None else json_output,
                on_text=on_text, visual_context=visual_context, temperature=0.0,
                max_tokens=max(1, int(settings.COOPERATIVE_CHAT_QUERY_MAX_TOKENS)),
                timeout=max(1.0, float(settings.COOPERATIVE_CHAT_QUERY_TIMEOUT_S)))
        finally:
            _QUERY_KIND.reset(token)

    async def query_planner(self, messages):
        from config import settings
        from llm import client

        token = _QUERY_KIND.set("planner")
        try:
            return await asyncio.to_thread(client.remote_llm_messages_query, messages,
                json_output=True, temperature=0.0, model=settings.COOPERATIVE_WORK_PLANNER_MODEL or None,
                max_tokens=max(1, int(settings.CONTROL_DECISION_MAX_TOKENS)),
                timeout=max(1.0, float(settings.CONTROL_DECISION_TIMEOUT_S)))
        finally:
            _QUERY_KIND.reset(token)

    async def report(self, source, attrs, *, publish=None):
        from server.app import _answer_report_from_ledger

        token = _QUERY_KIND.set("report_presentation")
        try:
            return await _answer_report_from_ledger(source, attrs, publish=publish)
        finally:
            _QUERY_KIND.reset(token)

    async def seed(self):
        from core import session_manager as sm
        from server.turn_admission import capture_turn_admission
        from agent_host.provider_types import ProviderSessionHandle
        from server.work_control import CurrentTurnSourceSpanV1, WorkCooperativeContextPayloadV6

        self.seed_bindings = {}
        loop = self.ingress.loop
        for ordinal, task in enumerate(self.row["seed"]["tasks"], 1):
            key, source = task["key"], task["goal"]
            destination = self.row["seed"]["task_destinations"][key]
            workspace = self.root / destination["workspace"]
            workspace.mkdir(parents=True, exist_ok=True)
            project = self.work.create_or_get_project(workspace,
                project_id=destination["project_id"], name=destination["project_name"])
            child = loop._create_context(task["title"], "codex", requirements=replace(self.requirements,
                workspace_access="read"), workspace=str(workspace), workspace_route={
                    "status": "resolved", "source": "cooperative_session_project",
                    "projectId": project.project_id, "workItemId": "", "cwd": str(workspace)})
            # Explicit existing native conversation fact, frozen with the seed.
            # Its subsequent accepted Work starts through the genuine Host path.
            child.native_session = ProviderSessionHandle(provider="codex",
                session_id="seed-native-" + self.row["case"] + "-" + key, scope="interaction")
            loop._state.checkpoint(child)
            loop.bind_context(child.child_id)
            admission = capture_turn_admission(utterance_id="seed-" + key, turn_id="seed-" + key,
                session_id=self.session_id, transcript=source, input_source="text", chat_epoch=ordinal,
                pending=False, authority_mode="turn_decision")
            self.control.admit(admission, fence_scope="hybrid-semantic:fixture-seed")
            payload = WorkCooperativeContextPayloadV6(provider="codex", task=source, title=task["title"],
                project_id=project.project_id, session_id=self.session_id,
                utterance_id=admission.utterance_id, turn_id=admission.turn_id,
                source_user_text=source, source_user_context="Explicit frozen current acceptance seed.",
                source_context_scope=admission.dialogue_source_scope,
                source_proof=CurrentTurnSourceSpanV1.capture(admission, source, start=0, end=len(source)),
                requirements=replace(self.requirements, task_kind="workspace_mutation"),
                cooperative_context_id=child.child_id, cooperative_binding_token=loop._binding.token,
                cooperative_context_revision=child.revision)
            effect_id = self.control.seal(admission, payload)["effect_id"]
            dispatch = await self.executor.dispatch(effect_id)
            run_id = dispatch.binding["provider_run_id"]
            await asyncio.wait_for(self.adapter.started.setdefault(run_id, asyncio.Event()).wait(), 5)
            if task["status"] == "succeeded":
                self.adapter.releases[run_id].set()
                await self.executor.finish(dispatch)
            loop.bind_work_item(dispatch.binding["work_item_id"], context_id=child.child_id)
            child.run_id, child.run_status = run_id, "done" if task["status"] == "succeeded" else "running"
            child.native_session = self.adapter.handles[run_id]
            loop._state.checkpoint(child)
            self.manager._work_dispatches[child.child_id] = dispatch
            self.seed_bindings[key] = {**dict(dispatch.binding), "effect_id": effect_id, "context_id": child.child_id}
        bound = self.row["seed"]["bound_work"]
        if bound:
            loop.bind_context(self.seed_bindings[bound]["context_id"])
        else:
            loop.bind_context("")
            self.work.clear_session_active_work_item(self.session_id)
        # Seeding a task does not choose a future new-goal destination.
        self.coordinator.bind_session_context(self.session_id, self.project.project_id)
        sm.conversation_history.dialog = deepcopy(self.row["seed"]["history"])
        sm.save_session(self.session_id)

    def snapshot(self):
        with self.ledger._lock:
            rows = self.ledger._db.execute("SELECT * FROM control_effect_outbox ORDER BY rowid").fetchall()
        effects = [{**dict(row), "payload": json.loads(row["payload_json"]),
            "receipt": self.ledger.get_receipt(row["effect_id"])} for row in rows]
        items = self.work.list_work_items()
        return {"adapter_events": deepcopy(self.adapter.events), "effects": effects,
            "work_items": [asdict(item) for item in items],
            "attempts": [asdict(attempt) for item in items for attempt in self.work.list_attempts(item.work_item_id)],
            "operations": [asdict(operation) for item in items for operation in self.work.list_operations(item.work_item_id)],
            "inputs": [row for item in items for row in self.work.list_provider_inputs(item.work_item_id)],
            "context_catalog": self.ingress.loop.context_catalog(),
            "bound_context_id": self.ingress.loop.bound_context_id,
            "attention": self.attention.list_pending(self.session_id),
            "display": deepcopy(self.display), "partial_display": deepcopy(self.partial),
            "transport_events": deepcopy(self.transport_events), "receipts": deepcopy(self.ingress.receipts),
            "loop_trace": deepcopy(self.ingress.loop.trace)}

    async def submit(self):
        turn = "evaluate-" + self.row["case"]
        self.admission_result = await self.handler.send_text(self.row["source"], session_id=self.session_id,
            turn_id=turn)
        if self.handler._stream_task is not None:
            await self.handler._stream_task
        await self.input_owner.drain_inputs()
        # Drain already scheduled adapter starts/facts; held executions are not awaited.
        for public in self.runtime.list_runs():
            record = self.runtime.get_run(public["run_id"])
            if record.task_handle is not None and record.status in {"queued", "running"}:
                await asyncio.wait_for(self.adapter.started.setdefault(record.run_id, asyncio.Event()).wait(), 5)
        await self.coordinator.drain_provider_facts()
        return self.snapshot()

    async def __aexit__(self, *_exc):
        from server.event_bus import bus

        self.observer.phase = "cleanup"
        if hasattr(self, "adapter"):
            self.adapter.phase = "cleanup"
        try:
            if hasattr(self, "narrator"):
                await self.narrator.close()
            if hasattr(self, "handler"):
                await self.handler.close()
            if hasattr(self, "manager"):
                await self.manager.begin_close()
            if hasattr(self, "adapter"):
                self.adapter.release_all()
            if hasattr(self, "runtime"):
                await asyncio.gather(*(record.task_handle for record in self.runtime._runs.values()
                    if record.task_handle is not None), return_exceptions=True)
                await self.runtime.close()
            if hasattr(self, "manager"):
                await self.manager.finish_close()
            if hasattr(self, "input_owner"):
                await self.input_owner.drain_inputs()
            if hasattr(self, "coordinator"):
                await self.coordinator.drain_provider_facts()
            self.cleanup_events = [deepcopy(event) for event in getattr(self, "adapter", SimpleNamespace(events=[])).events
                if event["phase"] == "cleanup"]
        finally:
            if hasattr(self, "adapter"):
                self.adapter.release_all()
            for native in self.observer.native_clients:
                native.close()
            self.observer.native_clients.clear()
            for method in getattr(self, "methods", ()):
                bus.off(method, self.capture)
            if hasattr(self, "attention"):
                self.attention.reset_for_tests()
            for resource in ("coordinator", "ledger", "work"):
                if hasattr(self, resource):
                    getattr(self, resource).close()
            self.patches.close()


def routing_observation(snapshot: dict) -> dict:
    """Summarize accepted effects, never classify source text or narration."""
    observed = []
    events = [event for event in snapshot["adapter_events"] if event["phase"] == "evaluation"]
    for event in events:
        if event["kind"] == "start":
            observed.append(event["metadata"].get("intent", "execute")
                if event["metadata"].get("source") == "control_work_effect" else "message")
        elif event["kind"] == "cancel":
            observed.append("retract")
        elif event["kind"] == "input":
            # Host transaction records an amendment requirement on the same input.
            inputs = [row for row in snapshot["inputs"] if row.get("provider_run_id") == event["run_id"]
                and row.get("text") == event["text"]]
            observed.append("amend" if any(operation.get("intent") == "amend"
                and operation.get("metadata", {}).get("work_input_id") in {row["input_id"] for row in inputs}
                for operation in snapshot["operations"]) else "message")
    receipts = list(snapshot["receipts"].values())
    if snapshot["attention"] or any("selection_required" in row.get("state", "") for row in receipts):
        observed.append("clarify")
    if any(row.get("state") == "work_reported" for row in receipts):
        observed.append("report")
    unknown = [row for row in receipts if row.get("state") in {"rejected", "error", "unknown"}]
    unique = list(dict.fromkeys(observed))
    if not unique:
        unique = ["refused"] if unknown else ["none"]
    return {"observed": unique, "basis": "pre-teardown registered adapter delivery, Host input requirements and attention receipts",
        "unresolved_receipts": unknown}


async def run_case(root: Path, row: dict, arm: dict, observer: QueryObserver,
                   *, timeout: float = 300.0, on_prepared=None) -> dict:
    call_start = len(observer.calls)
    result = {"case": row["case"], "section": row["section"], "arm": arm["name"],
        "fixture_sha256": FROZEN_SHA256, "seed": deepcopy(row["seed"]), "seed_sha256": row["seed_sha256"],
        "expected": row["expected"], "scored": row["scored"], "source": row["source"],
        "historical_results": deepcopy(row.get("historical_results", [])), "status": "started"}
    host = CaseHost(root, row, arm, observer)
    try:
        async with host:
            result["seed_snapshot"] = host.seed_snapshot
            result["seed_projection"] = host.seed_projection
            result["seed_projection_sha256"] = digest(host.seed_projection)
            result["persona_sha256"] = hashlib.sha256(host.persona.encode()).hexdigest()
            if on_prepared is not None:
                on_prepared(result)
            try:
                result["pre_teardown"] = await asyncio.wait_for(host.submit(), timeout)
            except Exception as exc:
                # Preserve partially accepted effects before teardown even when
                # the transport or an owning callback fails later in the turn.
                result["pre_teardown"] = host.snapshot()
                result["error"] = type(exc).__name__ + ": " + str(exc)
            result["routing"] = routing_observation(result["pre_teardown"])
            expected = row["expected"] if isinstance(row["expected"], list) else [row["expected"]]
            errors = [event for event in result["pre_teardown"]["transport_events"] if event["method"] == "chat.error"]
            result["routing"]["match"] = not errors and "error" not in result and set(expected) == set(result["routing"]["observed"])
            from llm.codex_role_contract import evaluate_role_output
            text = "\n".join(event["text"] for event in result["pre_teardown"]["display"])
            result["persona_diagnostics"] = evaluate_role_output(text, source_prompt=host.persona,
                required_presets=row.get("required_presets_diagnostic", ())).to_dict()
            result["persona_acceptance"] = "pending human review of substance, identity, relevance and repetition"
            result["status"] = "error" if errors or "error" in result else "observed"
            if errors:
                result["error"] = "Chat owner emitted an error; retained in transport_events"
    except Exception as exc:
        result.update(status="error", error=type(exc).__name__ + ": " + str(exc))
    result["cleanup_events"] = getattr(host, "cleanup_events", [])
    result["calls"] = deepcopy(observer.calls[call_start:])
    return result


def summarize(results: list[dict], fixture: dict) -> dict:
    summary = {"historical": deepcopy(fixture["historical_source"]["arms"]), "current": {},
        "acceptance": {"routing": "review observed results", "persona": "pending human review"}}
    for arm in fixture["arms"]:
        rows = [row for row in results if row["arm"] == arm["name"]]
        routing = [row for row in rows if row["section"] == "routing"]
        scored = [row for row in routing if row["scored"]]
        summary["current"][arm["name"]] = {"routing_scored": len(scored),
            "routing_matched": sum(row.get("routing", {}).get("match", False) for row in scored),
            "routing_unscored": [row["case"] for row in routing if not row["scored"]],
            "existing_persona_observed": sum(row["section"] == "persona" and row["status"] == "observed" for row in rows),
            "new_persona_observed": sum(row["section"] == "new_persona" and row["status"] == "observed" for row in rows),
            "errors": [row["case"] for row in rows if row["status"] == "error"],
            "actual_calls_by_kind": dict(Counter(call["kind"] for row in rows for call in row["calls"]))}
    return summary


async def experiment(args, fixture, rows, arms):
    from config import settings
    from llm import client, hybrid_stream

    report = preparation_report(fixture, rows, arms=arms, repeat=args.repeat, max_calls=args.max_calls)
    report["status"] = "running_current_acceptance"
    report["results"] = []
    report["pre_model_manifests"] = []
    # Publish the full reviewed context and selection BEFORE the first query.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_report(args.output, report)
    observer = QueryObserver(client.remote_llm_messages_query, max_calls=args.max_calls)
    prior = client.LLM_PROVIDER
    with ExitStack() as scope:
        scope.enter_context(patch.object(settings, "RAG_ENABLED", False))
        if args.model:
            field = "DEEPSEEK_MODEL_NAME" if args.provider == "hybrid2" else "OPENAI_MODEL_NAME"
            scope.enter_context(patch.object(client, field, args.model))
            scope.enter_context(patch.object(settings, field, args.model))
        if args.head_url:
            scope.enter_context(patch.object(hybrid_stream, "HYBRID_LOCAL_LLM_URL", args.head_url))
        if args.head_model:
            scope.enter_context(patch.object(hybrid_stream, "HYBRID_LOCAL_LLM_MODEL", args.head_model))
        client.configure(llm_provider=args.provider)
        report["configuration"] = {"provider": args.provider,
            "role_model": client.DEEPSEEK_MODEL_NAME if args.provider == "hybrid2" else client.OPENAI_MODEL_NAME,
            "planner_model": settings.COOPERATIVE_WORK_PLANNER_MODEL or "same selected role model",
            "head_model": hybrid_stream.HYBRID_LOCAL_LLM_MODEL, "head_url": hybrid_stream.HYBRID_LOCAL_LLM_URL,
            "rag_enabled": False, "tts": False, "registered_providers": ["inert codex"],
            "role_max_tokens": settings.COOPERATIVE_CHAT_QUERY_MAX_TOKENS,
            "planner_max_tokens": settings.CONTROL_DECISION_MAX_TOKENS,
            "serialized_configuration": True}
        write_report(args.output, report)
        try:
            with tempfile.TemporaryDirectory(prefix="amadeus-hybrid-semantic-") as temporary:
                for repeat in range(args.repeat):
                    for row in rows:
                        root = Path(temporary) / str(repeat) / row["case"]
                        for arm in arms:
                            def prepared(value):
                                previous = [seed for seed in report["pre_model_manifests"]
                                    if seed["case"] == row["case"] and seed["repeat"] == repeat]
                                if previous and (previous[0]["seed_projection_sha256"] != value["seed_projection_sha256"]
                                        or previous[0]["persona_sha256"] != value["persona_sha256"]):
                                    raise RuntimeError("paired arms differ in frozen Host context or persona")
                                report["pre_model_manifests"].append({key: deepcopy(value[key]) for key in (
                                    "case", "arm", "fixture_sha256", "seed", "seed_sha256", "seed_projection",
                                    "seed_projection_sha256", "seed_snapshot", "persona_sha256")} | {"repeat": repeat})
                                write_report(args.output, report)
                            result = await run_case(root, row, arm, observer, timeout=args.case_timeout,
                                on_prepared=prepared)
                            result["repeat"] = repeat
                            report["results"].append(result)
                            report["summary"] = summarize(report["results"], fixture)
                            write_report(args.output, report)
                            print(json.dumps({"case": row["case"], "arm": arm["name"], "status": result["status"],
                                "routing": result.get("routing"), "calls": len(result["calls"])}, ensure_ascii=False), flush=True)
        finally:
            client.configure(llm_provider=prior)
    report["status"] = "observed_requires_acceptance_review"
    write_report(args.output, report)
    return report


def write_report(path: Path, report: dict):
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


async def check_seeds(args, fixture, rows, arms):
    """Exercise fixture ownership only; every model port remains forbidden."""
    def forbidden(*_args, **_kwargs):
        raise AssertionError("model query attempted during seed-only preparation")
    report = preparation_report(fixture, rows, arms=arms, repeat=args.repeat, max_calls=args.max_calls)
    report["pre_model_manifests"] = []
    with tempfile.TemporaryDirectory(prefix="amadeus-hybrid-seeds-") as temporary:
        for row in rows:
            projections = []
            for arm in arms:
                observer = QueryObserver(forbidden)
                async with CaseHost(Path(temporary) / row["case"], row, arm, observer) as host:
                    projection = deepcopy(host.seed_projection)
                    projections.append(projection)
                    report["pre_model_manifests"].append({"case": row["case"], "arm": arm["name"],
                        "seed_sha256": row["seed_sha256"], "seed_projection": projection,
                        "seed_snapshot": host.seed_snapshot, "seed_projection_sha256": digest(projection),
                        "persona_sha256": hashlib.sha256(host.persona.encode()).hexdigest()})
                    assert observer.calls == [], "seed-only preparation attempted a model call"
            if projections[1:] != projections[:-1]:
                raise AssertionError(row["case"] + ": paired Host seed projections differ")
    report["status"] = "prepared_host_seeds_verified_no_models"
    report["actual_model_calls"] = 0
    write_report(args.output, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Make real model/HTTP calls after parent manifest review")
    parser.add_argument("--check-seeds", action="store_true", help="Dry run current Host seed ownership across selected arms; no model calls")
    parser.add_argument("--approved-fixture-sha256")
    parser.add_argument("--output", type=Path, default=ROOT / "build/chat-runtime-p0/hybrid-semantic-preparation.json")
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--arm", action="append", dest="arm_names")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=1500)
    parser.add_argument("--case-timeout", type=float, default=300.0)
    parser.add_argument("--provider", choices=("hybrid2", "hybrid3"), default="hybrid2")
    parser.add_argument("--model")
    parser.add_argument("--head-url")
    parser.add_argument("--head-model")
    args = parser.parse_args(argv)
    if args.repeat < 1 or args.max_calls < 1 or args.case_timeout <= 0:
        parser.error("repeat, max-calls and case-timeout must be positive")
    fixture = load_fixture()
    rows = select_cases(fixture, args.cases)
    arms = [arm for arm in fixture["arms"] if not args.arm_names or arm["name"] in args.arm_names]
    if args.arm_names and set(args.arm_names) - {arm["name"] for arm in arms}:
        parser.error("unknown arm")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not args.live:
        report = (asyncio.run(check_seeds(args, fixture, rows, arms)) if args.check_seeds
            else preparation_report(fixture, rows, arms=arms, repeat=args.repeat, max_calls=args.max_calls))
        write_report(args.output, report)
        print(json.dumps({"status": report["status"], "fixture_sha256": FROZEN_SHA256,
            "call_budget": report["call_budget"], "output": str(args.output)}, ensure_ascii=False))
        return 0
    if args.approved_fixture_sha256 != FROZEN_SHA256:
        parser.error("live calls require the exact reviewed --approved-fixture-sha256")
    report = asyncio.run(experiment(args, fixture, rows, arms))
    return int(any(row["status"] == "error" for row in report["results"]))


if __name__ == "__main__":
    raise SystemExit(main())
