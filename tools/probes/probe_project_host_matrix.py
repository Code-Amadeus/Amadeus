r"""Real-model, real-host matrix for conversational project routing.

Unlike the older project probes, this does not stop after asking whether the
model emitted a plausible label.  Every raw reply crosses the shipping inline
tag parser, ChatRuntime grounding, ``record_actions``, ``_handle_delegate``,
workspace routing, the Work Ledger, and the work projection.  Provider
execution alone is stubbed: the probe records the prepared request and marks
its attempt complete without modifying repository files.

Usage::

    .venv\Scripts\python.exe -X utf8 tools/probes/probe_project_host_matrix.py [rounds]

Exit codes: 0 all semantic checks passed; 1 at least one semantic failure;
2 infrastructure prevented one or more model turns from running.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SESSION_ID = "probe-project-host-matrix"
_FILE_RE = re.compile(r"(?i)(?<![\w.-])([\w.-]+\.[a-z0-9]{1,12})(?![\w.-])")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _short(text: str, limit: int = 150) -> str:
    return " ".join(str(text or "").split())[:limit]


def _p95(values: list[float] | list[int]):
    """Nearest-rank P95 for small, auditable probe batches."""

    ordered = sorted(values)
    if not ordered:
        raise ValueError("p95 requires at least one value")
    return ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)]


def _control_signature(
    control: dict[str, Any],
    references: list[str] | tuple[str, ...] = (),
) -> tuple[str, str]:
    """Compare only operation kind and Host-owned entity identity."""

    tokens = tuple(sorted(str(token) for token in references if str(token)))
    if tokens:
        entity = "|".join(tokens)
    else:
        workspace_ref = str(control.get("workspace_ref") or "").strip()
        project_id = str(control.get("project_id") or "").strip()
        entity = (
            f"work_item:{workspace_ref}"
            if workspace_ref
            else f"project:{project_id}"
            if project_id
            else ""
        )
    return str(control.get("intent") or "").strip().lower(), entity


def _a_signatures(fact: "TurnFact") -> tuple[tuple[str, str], ...]:
    return tuple(_control_signature(dict(control)) for control in fact.attrs)


def _b_signatures(fact: "TurnFact") -> tuple[tuple[str, str], ...]:
    operations = fact.compound_record.get("operations") or []
    return tuple(
        _control_signature(
            dict(operation.get("control") or {}),
            list(operation.get("references") or []),
        )
        for operation in operations
        if isinstance(operation, dict)
    )


def _ask(messages: list[dict[str, str]], *, model: str, temperature: float) -> str:
    import llm.client as client

    if client.llm_client is None:
        client.llm_client = client.init_llm_client()
    response = client.llm_client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=700,
        stream=False,
        timeout=45,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return str(response.choices[0].message.content or "")


@dataclass
class RunFact:
    work_item_id: str
    operation_id: str
    attempt_id: str
    project_id: str
    workspace_path: str
    task: str
    intent: str
    focus_applied: bool
    focus_guard: str
    routing_source: str
    related_work_item_id: str
    is_scratch: bool
    files: tuple[str, ...] = ()


@dataclass
class TurnFact:
    name: str
    utterance: str
    reply: str
    model_attrs: list[dict[str, Any]]
    attrs: list[dict[str, Any]]
    new_work_item_ids: list[str]
    new_operation_ids: list[str]
    runs: list[RunFact]
    destination_label: str
    session_project_id: str
    latency_s: float
    shadow_outcome: str = ""
    shadow_reason: str = ""
    shadow_reply: str = ""
    shadow_candidate_failure_reply: str = ""
    shadow_latency_ms: int = 0
    shadow_raw: list[dict[str, Any]] = field(default_factory=list)
    shadow_canonical: list[dict[str, Any]] = field(default_factory=list)
    shadow_raw_references: list[list[str] | None] = field(default_factory=list)
    shadow_canonical_references: list[list[str] | None] = field(default_factory=list)
    compound_status: str = ""
    compound_record: dict[str, Any] = field(default_factory=dict)
    compound_latency_ms: int = 0
    reported_work_item_ids: list[str] = field(default_factory=list)


@dataclass
class Check:
    name: str
    errors: list[str] = field(default_factory=list)
    cascade_from: tuple[str, ...] = ()

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            self.errors.append(message)

    @property
    def ok(self) -> bool:
        return not self.errors


class HostMatrix:
    def __init__(
        self,
        root: Path,
        *,
        model: str,
        temperature: float,
        control_shadow: bool = False,
        control_authority_preview: bool = False,
        compound_control_shadow: bool = False,
        compound_control_authority_preview: bool = False,
    ) -> None:
        from agent_host.work_ledger_store import WorkLedgerStore
        from server.work_ledger_coordinator import WorkLedgerCoordinator

        self.root = root
        self.model = model
        self.temperature = temperature
        self.db_path = root / "ledger.sqlite3"
        self.main_path = root / "amadeus"
        self.chess_path = root / "国际象棋游戏"
        self.main_path.mkdir(parents=True)
        self.chess_path.mkdir(parents=True)
        self.store = WorkLedgerStore(self.db_path)
        self.main_project = self.store.create_or_get_project(
            self.main_path,
            name="amadeus",
        )
        self.chess_project = self.store.create_or_get_project(
            self.chess_path,
            name="国际象棋游戏",
        )
        self.coordinator = WorkLedgerCoordinator(self.store)
        self.coordinator.configure()
        self.messages: list[dict[str, str]] = []
        self.runs: list[RunFact] = []
        self.checks: list[Check] = []
        self.latencies: list[float] = []
        self.infrastructure_errors: list[str] = []
        self.host_corrections: list[str] = []
        self.route_note_item_id = ""
        self.shadow_evidence: list[Any] = []
        self.compound_evidence: list[Any] = []
        self.compound_ab_rows: list[dict[str, Any]] = []
        self.reported_work_item_ids: list[str] = []
        self.compound_control_authority_preview = bool(
            compound_control_authority_preview
        )
        self.control_authority_preview = bool(
            control_authority_preview
            or self.compound_control_authority_preview
        )
        self.compound_control_shadow = bool(
            compound_control_shadow
            or self.compound_control_authority_preview
        )
        self._control_shadow_enabled = bool(
            control_shadow
            or self.control_authority_preview
            or self.compound_control_shadow
        )
        self.control_shadow = None
        self._configure_control_shadow()
        from server.attention_request import attention_requests

        attention_requests.reset_for_tests()

    def _configure_control_shadow(self) -> None:
        """Bind the observer to the currently open host coordinator."""

        if not self._control_shadow_enabled:
            self.control_shadow = None
            return
        from server.control_shadow import RuntimeControlDecisionShadow

        async def query(messages: list[dict[str, str]]) -> str:
            from llm.client import remote_llm_messages_query

            return await asyncio.to_thread(
                remote_llm_messages_query,
                messages,
                model=self.model,
                temperature=0.0,
            )

        self.control_shadow = RuntimeControlDecisionShadow(
            coordinator=self.coordinator,
            query=query,
            sink=self.shadow_evidence.append,
            compound_shadow=self.compound_control_shadow,
            compound_sink=self.compound_evidence.append,
            project_limit=200,
        )

    def close(self) -> None:
        from server.attention_request import attention_requests

        attention_requests.reset_for_tests()
        self.coordinator.close()

    def restart_host(self) -> None:
        from agent_host.work_ledger_store import WorkLedgerStore
        from server.work_ledger_coordinator import WorkLedgerCoordinator

        self.coordinator.close()
        self.store = WorkLedgerStore(self.db_path)
        self.coordinator = WorkLedgerCoordinator(self.store)
        self.coordinator.configure()
        self._configure_control_shadow()

    def system_prompt(self) -> str:
        from llm.prompts import finalize_system_prompt_language, get_system_prompt
        from server.work_context import augment_system_prompt_with_active_provider_context

        return finalize_system_prompt_language(
            augment_system_prompt_with_active_provider_context(
                get_system_prompt("with_delegate"),
                session_id=SESSION_ID,
            )
        )

    async def provider_start(self, request) -> SimpleNamespace:
        """Prepare and settle one real ledger attempt without touching files."""

        from core.chat_runtime import _explicit_file_references
        from server.scratch_workspace import is_scratch_path

        original_task = str(request.task or "")
        prepared = self.coordinator.prepare_request(request)
        binding = dict(prepared.metadata.get("work") or {})
        work_item_id = str(binding.get("work_item_id") or "")
        attempt_id = str(binding.get("attempt_id") or "")
        item = self.store.get_work_item(work_item_id)
        if item is None:
            raise RuntimeError("prepared provider request has no WorkItem")

        references = sorted(
            {
                *(_explicit_file_references(original_task) or set()),
                *(match.group(1) for match in _FILE_RE.finditer(original_task)),
            }
        )
        for reference in references:
            self.store.register_artifact(
                work_item_id,
                kind="business.file",
                title=reference,
                attempt_id=attempt_id,
                path=Path(item.workspace_path) / reference,
                metadata={"probe_stub": True},
            )

        self.store.update_attempt(
            attempt_id,
            execution_status="succeeded",
            result="provider execution stubbed by host matrix",
        )
        self.store.release_writer_lease(attempt_id)
        metadata = dict(prepared.metadata or {})
        self.runs.append(
            RunFact(
                work_item_id=work_item_id,
                operation_id=str(binding.get("operation_id") or ""),
                attempt_id=attempt_id,
                project_id=str(binding.get("project_id") or ""),
                workspace_path=str(binding.get("workspace_path") or prepared.cwd or ""),
                task=original_task,
                intent=str(metadata.get("intent") or "execute"),
                focus_applied=metadata.get("focus_applied") is True,
                focus_guard=str(metadata.get("focus_guard") or ""),
                routing_source=str(metadata.get("workspace_routing_source") or ""),
                related_work_item_id=str(metadata.get("related_work_item_id") or ""),
                is_scratch=is_scratch_path(item.workspace_path),
                files=tuple(references),
            )
        )
        return SimpleNamespace(
            task_handle=None,
            result="provider execution stubbed by host matrix",
            error="",
            metadata={"result_type": "ok"},
        )

    async def turn(self, name: str, utterance: str) -> TurnFact | None:
        from core.chat_runtime import ChatRuntime, _TurnState
        from llm.stream_parser import StreamTagParser
        from server.task_lookup import pre_turn_resolve, set_turn_resolution
        from server import host_action_dispatcher as action_dispatcher

        # Mirror ChatRuntime's ordering: resolve the current utterance against
        # the Work Ledger before either the role prompt or ControlDecision
        # snapshots dynamic host context. Without this, the matrix asks both
        # models to infer a WorkItem reference that production has already
        # settled, and mislabels an instrumentation omission as a controller
        # regression.
        set_turn_resolution(None)
        await pre_turn_resolve(SESSION_ID, utterance)
        if not self.messages:
            self.messages.append({"role": "system", "content": self.system_prompt()})
        else:
            # Shipping prompts are rebuilt every turn; the roster and project
            # candidates are current facts, not a snapshot from turn one.
            self.messages[0]["content"] = self.system_prompt()
        prior_history = [dict(message) for message in self.messages[1:]]
        self.messages.append({"role": "user", "content": utterance})
        started = time.monotonic()
        try:
            reply = await asyncio.to_thread(
                _ask,
                list(self.messages),
                model=self.model,
                temperature=self.temperature,
            )
        except Exception as exc:
            self.infrastructure_errors.append(f"{name}: {exc}")
            print(f"  INFRA {name:22s} {_short(str(exc), 180)}")
            return None
        latency = time.monotonic() - started
        self.latencies.append(latency)
        self.messages.append({"role": "assistant", "content": reply})

        items_before = self.store.list_work_items(limit=2000)
        before_items = {item.work_item_id for item in items_before}
        before_operations = {
            operation.operation_id
            for item in items_before
            for operation in self.store.list_operations(item.work_item_id)
        }
        before_runs = len(self.runs)
        before_reports = len(self.reported_work_item_ids)
        parser = StreamTagParser()
        _cleaned, actions = parser.process_chunk(reply)
        raw_delegates = [action for action in actions if action.get("type") == "DELEGATE"]
        model_attrs = [dict(action.get("attrs") or {}) for action in raw_delegates]
        proposal_actions = [
            {
                "type": action.get("type"),
                "attrs": dict(action.get("attrs") or {}),
                "raw": action.get("raw"),
            }
            for action in raw_delegates
        ]
        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=self.control_shadow,
            control_proposal_authority=self.control_authority_preview,
            compound_control_authority=(
                self.compound_control_authority_preview
            ),
        )
        turn_state = _TurnState(
            gui_callback=None,
            turn_id=name,
            question=utterance,
            session_id=SESSION_ID,
            control_prior_messages=prior_history,
        )
        turn_state.history_response = str(_cleaned or "")
        delegates = raw_delegates
        for action in raw_delegates:
            ChatRuntime._annotate_delegate_source(action, utterance)
            ChatRuntime._ground_present_provider_delegate(
                action,
                utterance,
                session_id=SESSION_ID,
            )
            ChatRuntime._annotate_report_lookup(
                action,
                SimpleNamespace(question=utterance, session_id=SESSION_ID),
            )

        if raw_delegates:
            runtime._record_delegate_proposals(
                turn_state,
                raw_delegates,
                transport="inline_tag",
                proposal_actions=proposal_actions,
            )
        if self.control_authority_preview:
            await runtime._wait_for_control_authority(turn_state)
            delegates = list(turn_state.control_effective_actions)
            self.messages[-1]["content"] = turn_state.history_response
        pending = tuple(action_dispatcher.dispatch_tasks)
        if pending:
            await asyncio.gather(*pending)
        # The production authority task waits only for A.  This probe also
        # waits for the independent B shadow receipt so every sealed proposal
        # has paired evidence before the disposable turn advances.
        shadow_pending = tuple(runtime._control_proposal_observer_tasks)
        if shadow_pending:
            await asyncio.gather(*shadow_pending)
        await asyncio.sleep(0)

        final_attrs = [dict(action.get("attrs") or {}) for action in delegates]
        for attrs in final_attrs:
            if str(attrs.get("_host_source_user_text") or "") != utterance:
                raise RuntimeError(
                    f"{name}: host matrix lost the source utterance before dispatch"
                )
        for index, (declared, grounded) in enumerate(zip(model_attrs, final_attrs)):
            changed = {
                key: (declared.get(key), grounded.get(key))
                for key in ("intent", "workspace_ref", "amend_inferred")
                if declared.get(key) != grounded.get(key)
            }
            if changed:
                self.host_corrections.append(f"{name}[{index}]={changed}")

        # Shipping closes work through a later WorkObserver assistant entry,
        # and deterministic ledger reports are also appended to conversation
        # history.  The matrix has no UI/TTS observer, so mirror only that
        # bounded semantic fact; otherwise a long run feeds the model promises
        # without ever feeding it outcomes and ceases to represent production.
        completed_runs = list(self.runs[before_runs:])
        for run in completed_runs:
            artifacts = ", ".join(run.files) if run.files else "no named artifact"
            self.messages.append(
                {
                    "role": "assistant",
                    "content": (
                        "[WORK_OBSERVER]\n"
                        f"実行結果は成功。成果物: {artifacts}。"
                        "この試行は作業台帳に記録済み。"
                    ),
                }
            )
        for attrs in final_attrs:
            if str(attrs.get("intent") or "").strip().lower() != "report":
                continue
            marker = (
                "PROJECT_STATUS"
                if str(attrs.get("subject") or "work_item").strip().lower() == "project"
                else "TASK_STATUS"
            )
            self.messages.append(
                {
                    "role": "assistant",
                    "content": f"[{marker}]\nホストが永続作業台帳の事実から回答済み。",
                }
            )

        items_after = self.store.list_work_items(limit=2000)
        after_items = {item.work_item_id for item in items_after}
        after_operations = {
            operation.operation_id
            for item in items_after
            for operation in self.store.list_operations(item.work_item_id)
        }
        snapshot = self.coordinator.snapshot()
        shadow = next(
            (
                item
                for item in reversed(self.shadow_evidence)
                if str(getattr(item, "turn_id", "")) == name
            ),
            None,
        )
        compound = next(
            (
                item
                for item in reversed(self.compound_evidence)
                if str(getattr(item, "turn_id", "")) == name
            ),
            None,
        )
        compound_record = (
            dict(compound.as_log_record())
            if compound is not None
            else {}
        )
        fact = TurnFact(
            name=name,
            utterance=utterance,
            reply=reply,
            model_attrs=model_attrs,
            attrs=final_attrs,
            new_work_item_ids=sorted(after_items - before_items),
            new_operation_ids=sorted(after_operations - before_operations),
            runs=list(self.runs[before_runs:]),
            destination_label=str(snapshot.get("destinationLabel") or ""),
            session_project_id=self.coordinator.session_project(SESSION_ID),
            latency_s=latency,
            shadow_outcome=str(getattr(shadow, "outcome", "")),
            shadow_reason=str(getattr(shadow, "reason", "")),
            shadow_reply=str(getattr(shadow, "decision_reply", "")),
            shadow_candidate_failure_reply=str(
                getattr(shadow, "candidate_failure_reply", "")
            ),
            shadow_latency_ms=int(getattr(shadow, "latency_ms", 0) or 0),
            shadow_raw=[dict(item) for item in (getattr(shadow, "raw_controls", ()) or ())],
            shadow_canonical=[
                dict(item) for item in (getattr(shadow, "canonical_controls", ()) or ())
            ],
            shadow_raw_references=[
                list(tokens) if tokens is not None else None
                for tokens in (getattr(shadow, "raw_references", ()) or ())
            ],
            shadow_canonical_references=[
                list(tokens) if tokens is not None else None
                for tokens in (getattr(shadow, "canonical_references", ()) or ())
            ],
            compound_status=str(getattr(compound, "status", "")),
            compound_record=compound_record,
            compound_latency_ms=int(getattr(compound, "latency_ms", 0) or 0),
            reported_work_item_ids=list(
                self.reported_work_item_ids[before_reports:]
            ),
        )
        return fact

    async def checked_turn(
        self,
        name: str,
        utterance: str,
        validate: Callable[[Check, TurnFact], None],
        *,
        depends_on: tuple[str, ...] = (),
    ) -> TurnFact | None:
        fact = await self.turn(name, utterance)
        if fact is None:
            return None
        check = Check(name)
        validate(check, fact)
        prior = {item.name: item for item in self.checks}
        check.cascade_from = tuple(
            dependency
            for dependency in depends_on
            if dependency in prior and not prior[dependency].ok
        )
        self.checks.append(check)
        status = "PASS" if check.ok else "CASC" if check.cascade_from else "FAIL"
        route = (
            "scratch"
            if fact.runs and fact.runs[-1].is_scratch
            else fact.runs[-1].routing_source if fact.runs else "-"
        )
        print(
            f"  {status:4s} {name:22s} tags={len(fact.attrs)} "
            f"work=+{len(fact.new_work_item_ids)} dest={fact.destination_label or 'Drafts'} "
            f"route={route} control={fact.shadow_outcome or '-'} "
            f"compound={fact.compound_status or '-'}"
            f"/{fact.compound_record.get('operationCount', '-')} {fact.latency_s:.2f}s"
        )
        if fact.shadow_outcome == "diverge":
            print(
                f"       control-decision: {fact.shadow_raw} -> {fact.shadow_canonical}"
            )
            print(
                "       reference-set: "
                f"{fact.shadow_raw_references} -> "
                f"{fact.shadow_canonical_references}"
            )
        elif fact.shadow_outcome not in {"", "agree"}:
            print(
                f"       control-decision: {fact.shadow_outcome} "
                f"reason={_short(fact.shadow_reason, 240)} "
                f"reply={_short(fact.shadow_reply, 180)}"
            )
            if fact.shadow_candidate_failure_reply:
                print(
                    "       candidate-failure-reply: "
                    f"{_short(fact.shadow_candidate_failure_reply, 180)}"
                )
        grounding_visible = any(
            any(
                declared.get(key) != grounded.get(key)
                for key in ("intent", "workspace_ref", "amend_inferred")
            )
            for declared, grounded in zip(fact.model_attrs, fact.attrs)
        )
        if grounding_visible:
            declared = [str(item.get("intent") or "") for item in fact.model_attrs]
            grounded = [str(item.get("intent") or "") for item in fact.attrs]
            print(f"       host-grounded: intent {declared} -> {grounded}")
        if not check.ok:
            if check.cascade_from:
                print(f"       - dependent on failed step(s): {', '.join(check.cascade_from)}")
            for error in check.errors:
                print(f"       - {error}")
            print(f"       reply: {_short(fact.reply)}")
        return fact

    async def run(self, *, long_horizon: bool = False) -> None:
        main_id = self.main_project.project_id
        chess_id = self.chess_project.project_id

        def pure_main(check: Check, fact: TurnFact) -> None:
            attrs = fact.attrs[0] if len(fact.attrs) == 1 else {}
            check.require(len(fact.attrs) == 1, "expected one focus tag")
            check.require(str(attrs.get("intent") or "") == "focus", "tag was not focus")
            check.require(not str(attrs.get("task") or "").strip(), "pure focus carried a task")
            check.require(str(attrs.get("project_id") or "") == main_id, "focus named the wrong project")
            check.require(not fact.new_work_item_ids, "pure focus created a WorkItem")
            check.require(fact.session_project_id == main_id, "host session focus did not become amadeus")
            check.require(fact.destination_label == "amadeus", "projection did not show amadeus")

        await self.checked_turn(
            "pure-switch-main",
            "切换到 amadeus 项目。",
            pure_main,
        )

        def clear_focus(check: Check, fact: TurnFact) -> None:
            attrs = fact.attrs[0] if len(fact.attrs) == 1 else {}
            check.require(len(fact.attrs) == 1, "expected one taskless focus tag")
            check.require(str(attrs.get("intent") or "") == "focus", "clear was not focus")
            check.require(not str(attrs.get("project_id") or "").strip(), "clear incorrectly named a project")
            check.require(not str(attrs.get("task") or "").strip(), "clear incorrectly started work")
            check.require(not fact.new_work_item_ids, "clear created a WorkItem")
            check.require(fact.session_project_id == "", "host session focus was not cleared")
            check.require(fact.destination_label == "", "projection did not return to Drafts")

        await self.checked_turn(
            "return-to-drafts",
            "回到草稿，接下来不在项目里做。",
            clear_focus,
        )

        def compound(check: Check, fact: TurnFact) -> None:
            attrs = fact.attrs[0] if len(fact.attrs) == 1 else {}
            check.require(len(fact.attrs) == 1, "compound request did not use exactly one tag")
            check.require(str(attrs.get("intent") or "") == "execute", "compound tag did not preserve execute")
            check.require(str(attrs.get("focus") or "") == "set", "compound tag did not declare focus=set")
            check.require(str(attrs.get("project_id") or "") == main_id, "compound route named the wrong project")
            check.require(bool(str(attrs.get("task") or "").strip()), "compound tag swallowed the work")
            check.require(len(fact.new_work_item_ids) == 1, "compound request did not create exactly one WorkItem")
            run = fact.runs[0] if len(fact.runs) == 1 else None
            check.require(run is not None, "compound request never reached provider preparation")
            if run is not None:
                check.require(run.project_id == main_id, "compound work routed outside amadeus")
                check.require(run.intent == "execute", "ledger intent was not consumed to execute")
                check.require(run.focus_applied, "ledger lost focus_applied=true")
                check.require("focus-route.txt" in run.files, "compound work lost its sentinel file")
            check.require(fact.session_project_id == main_id, "compound request did not retain amadeus focus")

        await self.checked_turn(
            "switch-and-work",
            "切到 amadeus，并新建 focus-route.txt，写入 focus route。",
            compound,
        )

        def one_off(check: Check, fact: TurnFact) -> None:
            attrs = fact.attrs[0] if len(fact.attrs) == 1 else {}
            check.require(len(fact.attrs) == 1, "one-off request did not emit one work tag")
            check.require(_truthy(attrs.get("one_off")), "model did not declare one_off=true")
            check.require(len(fact.new_work_item_ids) == 1, "one-off did not create exactly one WorkItem")
            run = fact.runs[0] if len(fact.runs) == 1 else None
            check.require(run is not None, "one-off never reached provider preparation")
            if run is not None:
                check.require(run.is_scratch, "one-off did not route through scratch")
                check.require(run.project_id != main_id, "one-off polluted amadeus")
            check.require(fact.session_project_id == main_id, "one-off changed the conversation project")

        one_off_fact = await self.checked_turn(
            "one-off-inside-main",
            "另外做个一次性的国际象棋游戏，不要放在任何项目里。",
            one_off,
            depends_on=("switch-and-work",),
        )
        recent_draft_work_item_id = (
            one_off_fact.new_work_item_ids[0]
            if one_off_fact is not None and len(one_off_fact.new_work_item_ids) == 1
            else ""
        )

        def ambiguous_chess_reference(check: Check, fact: TurnFact) -> None:
            from server.attention_request import attention_requests

            attrs = fact.attrs[0] if len(fact.attrs) == 1 else {}
            pending = attention_requests.list_pending(SESSION_ID)
            check.require(
                len(fact.attrs) == 1
                and str(attrs.get("intent") or "") in {"focus", "report"},
                (
                    "ambiguous switch did not preserve one host-adjudicable "
                    f"control proposal: {fact.attrs!r}"
                ),
            )
            check.require(not fact.new_work_item_ids, "ambiguous reference created a WorkItem")
            check.require(not fact.runs, "ambiguous reference reached a Provider")
            check.require(
                fact.session_project_id == main_id,
                "ambiguous reference guessed a persistent project",
            )
            if pending:
                check.require(
                    len(pending) == 1,
                    f"reference published multiple selection cards: {pending!r}",
                )
                kinds = {option.get("entityKind") for option in pending[0].get("options") or []}
                check.require(
                    kinds == {"project", "work_item"},
                    f"selection card lost typed hierarchy: {kinds!r}",
                )
            else:
                context = self.coordinator.store.get_session_work_context(SESSION_ID)
                check.require(
                    bool(
                        recent_draft_work_item_id
                        and context is not None
                        and context.active_work_item_id == recent_draft_work_item_id
                    ),
                    "strong recency neither resumed the latest Draft nor preserved ambiguity",
                )

        ambiguous_fact = await self.checked_turn(
            "ambiguous-chess-reference",
            "切到刚才那个象棋。",
            ambiguous_chess_reference,
            depends_on=("one-off-inside-main",),
        )
        if ambiguous_fact is not None:
            from server.attention_request import attention_requests

            pending = attention_requests.list_pending(SESSION_ID)
            if pending:
                draft_option = next(
                    (
                        option
                        for option in pending[0].get("options") or []
                        if (option.get("metadata") or {}).get("scope") == "session_draft"
                    ),
                    None,
                )
                if draft_option is not None:
                    runs_before_choice = len(self.runs)
                    resolved = await attention_requests.resolve(
                        session_id=SESSION_ID,
                        request_id=str(pending[0].get("id") or ""),
                        option_id=str(draft_option.get("id") or ""),
                    )
                    self.checks[-1].require(
                        resolved.get("ok") is True,
                        f"Slice choice did not resolve: {resolved!r}",
                    )
                    self.checks[-1].require(
                        self.coordinator.session_project(SESSION_ID) == main_id,
                        "choosing a Session Draft changed persistent Project binding",
                    )
                    self.checks[-1].require(
                        len(self.runs) == runs_before_choice,
                        "choosing a taskless Session Draft unexpectedly started a Provider",
                    )
                    self.checks[-1].require(
                        not attention_requests.list_pending(SESSION_ID),
                        "resolved selection card remained pending",
                    )

        def switch_chess(check: Check, fact: TurnFact) -> None:
            attrs = fact.attrs[0] if len(fact.attrs) == 1 else {}
            check.require(len(fact.attrs) == 1, "expected one focus tag for chess")
            check.require(str(attrs.get("intent") or "") == "focus", "chess switch was not focus")
            check.require(str(attrs.get("project_id") or "") == chess_id, "chess switch named the wrong project")
            check.require(not fact.new_work_item_ids, "chess switch created work")
            check.require(fact.session_project_id == chess_id, "host did not focus the chess project")

        await self.checked_turn(
            "switch-to-chess",
            "切换到已注册的“国际象棋游戏”项目，不是刚才的一次性草稿。",
            switch_chess,
        )

        def create_note(check: Check, fact: TurnFact) -> None:
            check.require(len(fact.new_work_item_ids) == 1, "route-note creation did not create one WorkItem")
            run = fact.runs[0] if len(fact.runs) == 1 else None
            check.require(run is not None, "route-note creation never reached provider preparation")
            if run is not None:
                check.require(run.project_id == chess_id, "route-note creation escaped the chess project")
                check.require("route-note.txt" in run.files, "provider stub could not register route-note.txt")
                self.route_note_item_id = run.work_item_id

        await self.checked_turn(
            "create-cross-amend-file",
            "新建 route-note.txt，写入 chess route。",
            create_note,
            depends_on=("switch-to-chess",),
        )
        await self.checked_turn(
            "switch-back-main",
            "切换回 amadeus 项目。",
            pure_main,
        )

        def cross_amend(check: Check, fact: TurnFact) -> None:
            attrs = fact.attrs[0] if len(fact.attrs) == 1 else {}
            check.require(len(fact.attrs) == 1, "cross-project amend did not emit one tag")
            check.require(str(attrs.get("intent") or "") == "amend", "model did not declare amend")
            check.require(not fact.new_work_item_ids, "cross-project amend split the stable WorkItem")
            check.require(len(fact.new_operation_ids) == 1, "cross-project amend did not create one Operation")
            run = fact.runs[0] if len(fact.runs) == 1 else None
            check.require(run is not None, "cross-project amend never reached provider preparation")
            if run is not None:
                check.require(run.project_id == chess_id, "cross-project amend routed to the current main project")
                check.require(run.intent == "amend", "ledger did not record actual amend intent")
                check.require(
                    run.work_item_id == self.route_note_item_id,
                    "host did not append amend to the route-note WorkItem",
                )
                check.require(
                    run.operation_id in fact.new_operation_ids,
                    "provider run lost the new Operation binding",
                )
                if str(attrs.get("focus") or "").strip():
                    check.require(
                        run.focus_guard == "removed" and not run.focus_applied,
                        "host did not remove an unconfirmed persistent focus modifier",
                    )
            check.require(fact.session_project_id == main_id, "one cross-project amend changed session focus")

        await self.checked_turn(
            "cross-project-amend",
            "给象棋项目刚才那个 route-note.txt 加一行 reviewed。",
            cross_amend,
            depends_on=("create-cross-amend-file", "switch-back-main"),
        )

        if long_horizon:
            await self._run_long_horizon(main_id=main_id, chess_id=chess_id)

        # Conversation Project context is durable. A host restart must restore
        # the explicit binding rather than silently dropping the user into Drafts.
        self.restart_host()
        restart = Check("restart-restores-binding")
        snapshot = self.coordinator.snapshot()
        restart.require(
            self.coordinator.session_project(SESSION_ID) == main_id,
            "durable project binding was lost across restart",
        )
        restart.require(
            str(snapshot.get("destinationLabel") or "") == "amadeus",
            "restart projection did not restore amadeus",
        )
        restart_dependency = "long-switch-main" if long_horizon else "cross-project-amend"
        prior = {item.name: item for item in self.checks}
        if (
            not restart.ok
            and restart_dependency in prior
            and not prior[restart_dependency].ok
        ):
            restart.cascade_from = (restart_dependency,)
        self.checks.append(restart)
        restart_status = "PASS" if restart.ok else "CASC" if restart.cascade_from else "FAIL"
        print(
            f"  {restart_status:4s} {'restart-restores-binding':22s} "
            "tags=0 work=+0 dest=amadeus route=-"
        )
        if restart.cascade_from:
            print(f"       - dependent on failed step(s): {', '.join(restart.cascade_from)}")
        for error in restart.errors:
            print(f"       - {error}")

        def post_restart(check: Check, fact: TurnFact) -> None:
            check.require(len(fact.new_work_item_ids) == 1, "post-restart task did not create one WorkItem")
            run = fact.runs[0] if len(fact.runs) == 1 else None
            check.require(run is not None, "post-restart task never reached provider preparation")
            if run is not None:
                check.require(run.is_scratch, "post-restart unrelated work did not fail safe to scratch")
                check.require(run.project_id != main_id, "post-restart unnamed work silently returned to amadeus")
            check.require(
                fact.session_project_id == main_id,
                "post-restart one-off lost the restored project binding",
            )

        await self.checked_turn(
            "post-restart-unrelated",
            "另外做个一次性的番茄钟小工具。",
            post_restart,
            depends_on=("restart-restores-binding",),
        )

    async def _run_long_horizon(self, *, main_id: str, chess_id: str) -> None:
        """Mix Ledger queries, read-only work, Drafts and routing over many turns."""

        def report_project(check: Check, fact: TurnFact) -> None:
            attrs = fact.attrs[0] if len(fact.attrs) == 1 else {}
            check.require(len(fact.attrs) == 1, "project query did not emit one tag")
            check.require(str(attrs.get("intent") or "") == "report", "project query was not report")
            check.require(str(attrs.get("subject") or "") == "project", "project query lacked subject=project")
            check.require(not fact.new_work_item_ids, "project query created a WorkItem")
            check.require(not fact.runs, "project query reached a Provider")

        recent = await self.checked_turn(
            "long-recent-projects",
            "我最近有哪些可以继续的本地项目？只按工作账本汇报。",
            report_project,
        )
        if recent is not None:
            attrs = recent.attrs[0] if len(recent.attrs) == 1 else {}
            self.checks[-1].require(
                not str(attrs.get("project_id") or ""),
                "recent-project list incorrectly selected one project",
            )

        current = await self.checked_turn(
            "long-current-project",
            "Amadeus 项目现在进展怎样？只汇报项目账本里的工作项。",
            report_project,
        )
        if current is not None:
            attrs = current.attrs[0] if len(current.attrs) == 1 else {}
            self.checks[-1].require(
                str(attrs.get("project_id") or "") == main_id,
                "specific project report did not name amadeus",
            )

        def new_readonly_work(check: Check, fact: TurnFact) -> None:
            attrs = fact.attrs[0] if len(fact.attrs) == 1 else {}
            check.require(len(fact.attrs) == 1, "code summary did not emit one tag")
            check.require(str(attrs.get("intent") or "") == "execute", "fresh code summary was not execute")
            check.require(len(fact.new_work_item_ids) == 1, "code summary did not create one WorkItem")
            run = fact.runs[0] if len(fact.runs) == 1 else None
            check.require(run is not None, "code summary did not reach Provider preparation")
            if run is not None:
                check.require(run.project_id == main_id, "code summary escaped the current project")
                check.require(run.intent == "execute", "ledger did not record execute")

        await self.checked_turn(
            "long-readonly-summary",
            "请实际读取当前 Amadeus 项目的代码并总结模块结构，只读，不要修改文件。",
            new_readonly_work,
        )

        def report_work_item(check: Check, fact: TurnFact) -> None:
            attrs = fact.attrs[0] if len(fact.attrs) == 1 else {}
            check.require(len(fact.attrs) == 1, "WorkItem status did not emit one tag")
            check.require(str(attrs.get("intent") or "") == "report", "WorkItem status was not report")
            check.require(
                str(attrs.get("subject") or "work_item") == "work_item",
                "WorkItem status used the wrong subject",
            )
            check.require(not fact.new_work_item_ids, "WorkItem status created work")
            check.require(not fact.runs, "WorkItem status reached a Provider")

        await self.checked_turn(
            "long-summary-status",
            "刚才总结代码的任务完成了吗？只汇报状态。",
            report_work_item,
            depends_on=("long-readonly-summary",),
        )

        def inspect_existing(check: Check, fact: TurnFact) -> None:
            attrs = fact.attrs[0] if len(fact.attrs) == 1 else {}
            check.require(len(fact.attrs) == 1, "existing-file inspection did not emit one tag")
            check.require(str(attrs.get("intent") or "") == "amend", "existing-file inspection was not amend")
            check.require(not fact.new_work_item_ids, "inspection split the stable WorkItem")
            check.require(len(fact.new_operation_ids) == 1, "inspection did not create one Operation")
            run = fact.runs[0] if len(fact.runs) == 1 else None
            check.require(run is not None, "inspection did not reach Provider preparation")
            if run is not None:
                check.require(run.project_id == chess_id, "inspection followed current focus instead of the artifact")
                check.require(
                    run.work_item_id == self.route_note_item_id,
                    "inspection lost the existing route-note WorkItem",
                )
                check.require(
                    run.operation_id in fact.new_operation_ids,
                    "inspection run lost the new Operation binding",
                )
            check.require(fact.session_project_id == main_id, "cross-project inspection changed focus")

        await self.checked_turn(
            "long-cross-project-inspect",
            "实际读取并总结象棋项目的 route-note.txt 内容，不要修改它。",
            inspect_existing,
            depends_on=("cross-project-amend",),
        )

        def ordinary_chat(check: Check, fact: TurnFact) -> None:
            check.require(not fact.attrs, "ordinary chat emitted a DELEGATE")
            check.require(not fact.new_work_item_ids, "ordinary chat created a WorkItem")
            check.require(not fact.runs, "ordinary chat reached a Provider")

        await self.checked_turn(
            "long-ordinary-chat",
            "一般来说，为什么长期项目需要把状态和代码观察分开？",
            ordinary_chat,
        )

        def clear_to_drafts(check: Check, fact: TurnFact) -> None:
            attrs = fact.attrs[0] if len(fact.attrs) == 1 else {}
            check.require(str(attrs.get("intent") or "") == "focus", "Drafts switch was not focus")
            check.require(not str(attrs.get("project_id") or ""), "Drafts switch named a project")
            check.require(not fact.new_work_item_ids, "Drafts switch created work")
            check.require(fact.session_project_id == "", "Drafts switch did not clear focus")

        await self.checked_turn(
            "long-return-drafts",
            "先回到草稿，后面的临时工作不要放进项目。",
            clear_to_drafts,
        )

        draft_turn = await self.checked_turn(
            "long-create-draft",
            "临时做一个一次性的 config-draft.ini，写入 mode=probe。",
            lambda check, fact: (
                check.require(len(fact.new_work_item_ids) == 1, "draft work did not create one WorkItem"),
                check.require(bool(fact.runs and fact.runs[0].is_scratch), "draft work did not use scratch"),
                check.require(fact.session_project_id == "", "draft work rebound the conversation"),
            ),
            depends_on=("long-return-drafts",),
        )
        draft_item_id = (
            draft_turn.runs[0].work_item_id
            if draft_turn is not None and len(draft_turn.runs) == 1
            else ""
        )

        await self.checked_turn(
            "long-draft-status",
            "刚才 config-draft.ini 那个临时任务完成了吗？只汇报状态。",
            report_work_item,
            depends_on=("long-create-draft",),
        )

        await self.checked_turn(
            "long-switch-chess",
            "切回象棋项目。",
            lambda check, fact: (
                check.require(
                    bool(fact.attrs and str(fact.attrs[0].get("intent") or "") == "focus"),
                    "chess switch did not emit focus",
                ),
                check.require(fact.session_project_id == chess_id, "chess switch selected the wrong project"),
                check.require(not fact.new_work_item_ids, "chess switch created work"),
            ),
        )
        chess_report = await self.checked_turn(
            "long-chess-status",
            "这个象棋项目整体进展怎样？只查项目账本。",
            report_project,
            depends_on=("long-switch-chess",),
        )
        if chess_report is not None:
            attrs = chess_report.attrs[0] if len(chess_report.attrs) == 1 else {}
            self.checks[-1].require(
                str(attrs.get("project_id") or "") == chess_id,
                "bound chess report named the wrong project",
            )
        await self.checked_turn(
            "long-switch-main",
            "最后切回 Amadeus 项目。",
            lambda check, fact: (
                check.require(fact.session_project_id == main_id, "final focus did not return to amadeus"),
                check.require(not fact.new_work_item_ids, "final focus created work"),
                check.require(bool(draft_item_id), "long Draft WorkItem was never captured"),
            ),
        )

        if not self.compound_control_shadow:
            return

        async def compound_ab_turn(
            name: str,
            utterance: str,
            expected: tuple[tuple[str, str], ...],
            *,
            depends_on: tuple[str, ...] = (),
        ) -> None:
            def validate(check: Check, fact: TurnFact) -> None:
                a = _a_signatures(fact)
                b = _b_signatures(fact)
                check.require(
                    fact.compound_status == "ok",
                    f"compound B did not return ok: {fact.compound_record!r}",
                )
                check.require(
                    b == expected,
                    f"compound B mismatch: expected={expected!r} actual={b!r}",
                )
                if self.compound_control_authority_preview:
                    check.require(
                        a == expected,
                        (
                            "effective compound authority mismatch: "
                            f"expected={expected!r} actual={a!r}"
                        ),
                    )
                    expected_work = tuple(
                        signature
                        for signature in expected
                        if signature[0] in {"execute", "amend"}
                    )
                    check.require(
                        len(fact.runs) == len(expected_work),
                        (
                            "compound authority did not dispatch every Work "
                            f"operation serially: expected={len(expected_work)} "
                            f"runs={len(fact.runs)}"
                        ),
                    )
                    for signature, run in zip(expected_work, fact.runs):
                        intent, token = signature
                        check.require(
                            run.intent == intent,
                            f"compound run intent drifted: {run.intent!r}",
                        )
                        if token.startswith("work_item:"):
                            check.require(
                                run.work_item_id == token.split(":", 1)[1],
                                (
                                    "compound run lost WorkItem identity: "
                                    f"{run.work_item_id!r}"
                                ),
                            )
                    expected_reports = [
                        token.split(":", 1)[1]
                        for intent, token in expected
                        if intent == "report" and token.startswith("work_item:")
                    ]
                    check.require(
                        fact.reported_work_item_ids == expected_reports,
                        (
                            "compound report ignored canonical WorkItem identity: "
                            f"expected={expected_reports!r} "
                            f"actual={fact.reported_work_item_ids!r}"
                        ),
                    )
                self.compound_ab_rows.append(
                    {
                        "turn": name,
                        "expected": expected,
                        "a": a,
                        "b": b,
                        "a_match": a == expected,
                        "b_match": b == expected,
                        "b_latency_ms": fact.compound_latency_ms,
                        "compound_authority": (
                            self.compound_control_authority_preview
                        ),
                    }
                )

            await self.checked_turn(
                name,
                utterance,
                validate,
                depends_on=depends_on,
            )

        route_token = f"work_item:{self.route_note_item_id}"
        draft_token = f"work_item:{draft_item_id}"
        await compound_ab_turn(
            "long-compound-amend-report",
            (
                "给象棋项目刚才那个 route-note.txt 再加一行 compound reviewed，"
                "然后告诉我刚才 config-draft.ini 临时任务的状态。"
            ),
            (("amend", route_token), ("report", draft_token)),
            depends_on=("cross-project-amend", "long-draft-status"),
        )
        await compound_ab_turn(
            "long-compound-same-subject",
            (
                "把 route-note.txt 的最后一行改成 compound ready，"
                "然后告诉我这个任务现在的状态。"
            ),
            (("amend", route_token), ("report", route_token)),
            depends_on=("long-compound-amend-report",),
        )
        await compound_ab_turn(
            "long-compound-new-report",
            (
                "另外新建一个一次性的 gamma-note.txt，只写 gamma；"
                "然后告诉我 route-note.txt 那个任务的状态。"
            ),
            (("execute", ""), ("report", route_token)),
            depends_on=("long-compound-same-subject",),
        )
        await compound_ab_turn(
            "long-compound-two-amends",
            (
                "把刚才那个 config-draft.ini 的 mode 改成 final，"
                "同时给 route-note.txt 再加一行 verified。"
            ),
            (("amend", draft_token), ("amend", route_token)),
            depends_on=("long-compound-new-report",),
        )


async def async_main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rounds", nargs="?", type=int, default=1)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument(
        "--long",
        action="store_true",
        help="append a long mixed Project/WorkItem/Draft/read-only journey",
    )
    parser.add_argument(
        "--control-shadow",
        action="store_true",
        help="run the real runtime ControlDecision observer without changing dispatch",
    )
    parser.add_argument(
        "--control-authority-preview",
        action="store_true",
        help=(
            "apply canonical actions inside the disposable host matrix only; "
            "production remains shadow-only"
        ),
    )
    parser.add_argument(
        "--compound-control-shadow",
        action="store_true",
        help=(
            "collect the exact-clause B arm beside ControlDecision A; with "
            "--long, append four natural compound turns late in the journey"
        ),
    )
    parser.add_argument(
        "--compound-control-authority-preview",
        action="store_true",
        help=(
            "apply the zero/one=A, multi=B authority policy inside the "
            "disposable host matrix"
        ),
    )
    args = parser.parse_args()

    semantic_failures = 0
    cascade_failures = 0
    infrastructure_failures = 0
    host_groundings = 0
    all_latencies: list[float] = []
    all_shadow_evidence: list[Any] = []
    all_compound_evidence: list[Any] = []
    all_compound_ab_rows: list[dict[str, Any]] = []
    print(
        f"project host matrix: rounds={max(1, args.rounds)} "
        f"model={args.model} temperature={args.temperature}\n"
    )
    for index in range(max(1, args.rounds)):
        with tempfile.TemporaryDirectory(prefix=f"project_host_matrix_{index + 1}_") as temp:
            matrix = HostMatrix(
                Path(temp),
                model=args.model,
                temperature=args.temperature,
                control_shadow=args.control_shadow,
                control_authority_preview=args.control_authority_preview,
                compound_control_shadow=args.compound_control_shadow,
                compound_control_authority_preview=(
                    args.compound_control_authority_preview
                ),
            )
            print(f"round {index + 1}")
            try:
                from agent_host.provider_runtime import runtime as provider_runtime
                from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
                import config.settings as settings
                from server import app as server_app
                from server import host_action_dispatcher
                from server import task_lookup

                class MatrixCodexAdapter:
                    provider_id = "codex"
                    manifest = CODEX_APP_SERVER_MANIFEST

                # The provider execution is intentionally stubbed, but current
                # production routing still requires a real semantic manifest
                # before it will select any provider. Register the same Codex
                # contract bootstrap installs; patch only the execution call.
                provider_runtime.register(MatrixCodexAdapter())
                host_action_dispatcher.configure(
                    delegate_handler=server_app._handle_delegate,
                    expression_sink=None,
                )
                render_status_answer = task_lookup.render_current_status_answer

                def capture_status_answer(row, *render_args, **render_kwargs):
                    matrix.reported_work_item_ids.append(
                        str(row.get("work_item_id") or "")
                    )
                    return render_status_answer(row, *render_args, **render_kwargs)

                with (
                    patch.object(provider_runtime, "start", side_effect=matrix.provider_start),
                    patch("core.session_manager.get_current_session_id", return_value=SESSION_ID),
                    patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
                    patch.object(settings, "DELEGATE_FOCUS_INTENT", True),
                    patch.object(settings, "DELEGATE_AMEND_INTENT", True),
                    patch.object(settings, "TASK_LOOKUP_ENABLED", True),
                    patch.object(settings, "WORK_WORKTREE_ISOLATION", False),
                    patch.object(settings, "WORK_SCRATCH_ROOT", str(Path(temp) / "scratch")),
                    patch.object(
                        task_lookup,
                        "render_current_status_answer",
                        side_effect=capture_status_answer,
                    ),
                    patch(
                        "server.work_ledger_coordinator.cwd_in_project_registry",
                        return_value=True,
                    ),
                ):
                    await matrix.run(long_horizon=args.long)
            finally:
                semantic_failures += sum(
                    not check.ok and not check.cascade_from for check in matrix.checks
                )
                cascade_failures += sum(
                    not check.ok and bool(check.cascade_from) for check in matrix.checks
                )
                infrastructure_failures += len(matrix.infrastructure_errors)
                host_groundings += len(matrix.host_corrections)
                all_latencies.extend(matrix.latencies)
                all_shadow_evidence.extend(matrix.shadow_evidence)
                all_compound_evidence.extend(matrix.compound_evidence)
                all_compound_ab_rows.extend(matrix.compound_ab_rows)
                matrix.close()
            print()

    print("summary")
    print(f"  independent failures   : {semantic_failures}")
    print(f"  dependent cascades     : {cascade_failures}")
    print(f"  infrastructure failures: {infrastructure_failures}")
    print(f"  fact-backed groundings : {host_groundings}")
    if all_latencies:
        ordered = sorted(all_latencies)
        print(f"  model turns completed  : {len(all_latencies)}")
        print(f"  latency median         : {ordered[len(ordered) // 2]:.2f}s")
        print(f"  latency p95            : {_p95(all_latencies):.2f}s")
    if args.control_shadow or args.control_authority_preview:
        outcomes: dict[str, int] = {}
        shadow_latencies: list[int] = []
        protocol_retries = 0
        for evidence in all_shadow_evidence:
            outcome = str(getattr(evidence, "outcome", "") or "unknown")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            shadow_latencies.append(int(getattr(evidence, "latency_ms", 0) or 0))
            protocol_retries += int(
                getattr(evidence, "candidate_protocol_retries", 0) or 0
            )
        print(f"  control outcomes       : {outcomes}")
        print(f"  candidate protocol retries: {protocol_retries}")
        if shadow_latencies:
            ordered_shadow = sorted(shadow_latencies)
            print(
                "  control latency median : "
                f"{ordered_shadow[len(ordered_shadow) // 2]}ms"
            )
            print(f"  control latency p95    : {_p95(shadow_latencies)}ms")
    if args.compound_control_shadow or args.compound_control_authority_preview:
        compound_latencies = [
            int(getattr(evidence, "latency_ms", 0) or 0)
            for evidence in all_compound_evidence
        ]
        compound_statuses: dict[str, int] = {}
        for evidence in all_compound_evidence:
            status = str(getattr(evidence, "status", "") or "unknown")
            compound_statuses[status] = compound_statuses.get(status, 0) + 1
        print(f"  compound receipts      : {len(all_compound_evidence)}")
        print(f"  compound statuses      : {compound_statuses}")
        if compound_latencies:
            ordered_compound = sorted(compound_latencies)
            print(
                "  compound latency median: "
                f"{ordered_compound[len(ordered_compound) // 2]}ms"
            )
            print(f"  compound latency p95   : {_p95(compound_latencies)}ms")
        if all_compound_ab_rows:
            if any(
                bool(row.get("compound_authority"))
                for row in all_compound_ab_rows
            ):
                print(
                    "  compound authority exact: "
                    f"{sum(bool(row['b_match']) for row in all_compound_ab_rows)}"
                    f"/{len(all_compound_ab_rows)}"
                )
            else:
                print(
                    "  compound A exact       : "
                    f"{sum(bool(row['a_match']) for row in all_compound_ab_rows)}"
                    f"/{len(all_compound_ab_rows)}"
                )
                print(
                    "  compound B exact       : "
                    f"{sum(bool(row['b_match']) for row in all_compound_ab_rows)}"
                    f"/{len(all_compound_ab_rows)}"
                )
    if infrastructure_failures:
        return 2
    return 1 if semantic_failures or cascade_failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
