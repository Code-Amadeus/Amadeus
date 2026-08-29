"""Exercise a real AUIP-authored game in headless Chromium.

Unlike the transport simulation, this journey runs the shipped HTML, Web SDK,
game mechanics, restricted ``/auip/ws`` endpoint, host action path, and
experience capsule together.  Chromium is intentionally an explicit L3 tool
dependency rather than a requirement of the default unit-test matrix.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    from playwright.async_api import Page

import uvicorn
from fastapi import FastAPI, WebSocket

from server.auip_app_connection import AuipAppConnectionManager
from server.auip_contract import AuipProtocolError
from server.auip_control_decision import AuipControlDecisionResolver
from server.auip_engagement import AuipEngagementCoordinator
from server.auip_participant_llm import decide_with_auip_participant
from server.auip_role_authorizer_llm import authorize_with_main_role
from server.auip_narration import AuipNarrationAdapter, AuipNarrationProfile
from server.auip_narration_llm import (
    decide_with_auip_observer,
    narrate_with_auip_llm,
    present_with_auip_llm,
)
from server.auip_runtime import AuipRuntime
from server.event_bus import bus
from server.handlers.auip_handler import AuipHandler
from server.protocol import Method
from tools.auip_natural_control_driver import (
    EmptyAuipLaunchCatalog,
    NaturalAuipControlDriver,
    query_auip_control_model,
)


ENTRY = ROOT / "examples" / "auip-gomoku" / "index.html"
MANIFEST = ENTRY.with_name("auip.manifest.json")
CONVERSATION_ID = "conversation-auip-gomoku"


@dataclass
class _Artifact:
    artifact_id: str
    work_item_id: str
    attempt_id: str
    kind: str
    title: str
    path: str
    status: str
    sha256: str


@dataclass
class _WorkItem:
    workspace_path: str


@dataclass
class _Attempt:
    attempt_id: str
    metadata: dict[str, Any]


class _ArtifactSource:
    def __init__(self) -> None:
        self.artifact = _Artifact(
            artifact_id="artifact-auip-gomoku",
            work_item_id="work-auip-gomoku",
            attempt_id="attempt-auip-gomoku",
            kind="business.file",
            title="Gomoku Nine",
            path=str(ENTRY),
            status="registered",
            sha256=hashlib.sha256(ENTRY.read_bytes()).hexdigest(),
        )
        self.manifest_artifact = _Artifact(
            artifact_id="artifact-auip-gomoku-manifest",
            work_item_id="work-auip-gomoku",
            attempt_id="attempt-auip-gomoku",
            kind="business.file",
            title="AUIP manifest",
            path=str(MANIFEST),
            status="registered",
            sha256=hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        )
        self.item = _WorkItem(workspace_path=str(ROOT))
        self.attempt = _Attempt(attempt_id="attempt-auip-gomoku", metadata={})

    def get_artifact(self, artifact_id: str) -> Any:
        return next(
            (
                item
                for item in (self.artifact, self.manifest_artifact)
                if artifact_id == item.artifact_id
            ),
            None,
        )

    def get_work_item(self, work_item_id: str) -> Any:
        return self.item if work_item_id == self.artifact.work_item_id else None

    def get_attempt(self, attempt_id: str) -> Any:
        return self.attempt if attempt_id == self.attempt.attempt_id else None

    def list_artifacts(self, work_item_id: str, *, attempt_id: str = "") -> list[Any]:
        if work_item_id != self.artifact.work_item_id:
            return []
        return [self.artifact, self.manifest_artifact]


def _listening_socket() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    return listener


async def _wait_for(
    predicate: Any,
    *,
    description: str,
    timeout_s: float = 5.0,
) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {description}")


async def _user_then_automatic_participant_move(
    page: Page,
    runtime: AuipRuntime,
    app_session_id: str,
    next_move: dict[str, int],
    *,
    user_x: int,
    user_y: int,
    participant_x: int,
    participant_y: int,
    starting_revision: int,
    exact_participant_move: bool = True,
) -> dict[str, Any]:
    """Prove collaborate reacts to the app-declared opportunity, not a test step."""

    if exact_participant_move:
        next_move.update({"x": participant_x, "y": participant_y})
    await page.locator(
        f'.cell[data-x="{user_x}"][data-y="{user_y}"]'
    ).click()
    resolved = await _wait_for(
        lambda: runtime.get(app_session_id)
        if int(runtime.get(app_session_id).get("revision") or 0)
        == starting_revision + 2
        else None,
        description=f"automatic participant move revision {starting_revision + 2}",
    )
    latest = resolved["latest_verified_self_action"]
    assert latest["type"] == "game.place_stone"
    if exact_participant_move:
        assert latest["payload"] == {"x": participant_x, "y": participant_y}
    else:
        payload = latest["payload"]
        x = payload.get("x")
        y = payload.get("y")
        assert isinstance(x, int) and 0 <= x < 9
        assert isinstance(y, int) and 0 <= y < 9
        assert (x, y) != (user_x, user_y)
        state = await page.evaluate("window.__auipGomoku.snapshot()")
        assert state["board"]["rows"][y][x] == "W"
    return latest


async def run_journey(
    *,
    routed_model: str = "",
    real_participant: bool = False,
    real_role_authorizer: bool = False,
    real_narration: bool = False,
) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    if not ENTRY.is_file():
        raise AssertionError(f"Missing AUIP Gomoku entry: {ENTRY}")

    runtime = AuipRuntime()
    manager = AuipAppConnectionManager(runtime)
    next_move: dict[str, int] = {}
    participant_calls: list[dict[str, Any]] = []
    routed_controls: list[dict[str, str]] = []
    role_authorization_calls: list[dict[str, Any]] = []
    real_role_authorization_calls: list[dict[str, Any]] = []
    real_role_authorization_results: list[dict[str, Any]] = []
    narration_deliveries: list[dict[str, Any]] = []
    narration_requests: list[dict[str, Any]] = []
    real_participant_enabled = False
    lifecycle_conclusion_requested = False

    async def deterministic_participant(context: dict[str, Any]) -> dict[str, Any]:
        state = context.get("state") if isinstance(context.get("state"), dict) else {}
        if str(state.get("lifecycle") or "") == "round_finished":
            if lifecycle_conclusion_requested:
                return {
                    "action": "act",
                    "type": "game.finish_experience",
                    "payload": {},
                    "private_note": "conclude only after the app-owned round result",
                }
            return {
                "action": "wait",
                "reason": "the round is over and no post-round choice was requested",
            }
        bindings = (
            state.get("roleBindings")
            if isinstance(state.get("roleBindings"), dict)
            else {}
        )
        if (
            int(state.get("moveCount") or 0) == 0
            and str(state.get("turn") or "") != str(bindings.get("participant") or "")
            and "先手" in str(context.get("global_conversation_context") or "")
        ):
            if "game.take_first_move" in context.get("available_actions", {}):
                return {
                    "action": "act",
                    "type": "game.take_first_move",
                    "payload": {
                        "x": int(next_move.get("x", 4)),
                        "y": int(next_move.get("y", 4)),
                    },
                    "private_note": "take the requested opening side and stone atomically",
                }
            if "game.configure_participants" not in context.get(
                "available_actions", {}
            ):
                return {
                    "action": "blocked",
                    "reason": "the app exposes no legal first-move action",
                }
            return {
                "action": "act",
                "type": "game.configure_participants",
                "payload": {"participantSide": str(state.get("turn") or "black")},
                "private_note": "bind the protocol participant to the requested opening side",
            }
        return {
            "action": "act",
            "type": "game.place_stone",
            "payload": dict(next_move),
            "private_note": "bounded deterministic acceptance move",
        }

    async def participant(context: dict[str, Any]) -> dict[str, Any]:
        participant_calls.append(context)
        if real_participant_enabled:
            return await decide_with_auip_participant(context)
        return await deterministic_participant(context)

    async def role_authorizer(context: dict[str, Any]) -> dict[str, Any]:
        role_authorization_calls.append(context)
        # The real gate owns explicit same-turn role/user consensus in this
        # journey. Later deterministic fixture moves exist only to produce a
        # stable revision/terminal sequence; asking a real quality gate to
        # approve deliberately scripted weak moves conflates strategy evals
        # with protocol truth. The separate strategy probe owns that evidence.
        global_context = str(context.get("global_conversation_context") or "")
        has_current_role_response = False
        try:
            decoded = json.loads(global_context)
            has_current_role_response = bool(
                isinstance(decoded, dict)
                and str(decoded.get("current_role_response") or "").strip()
            )
        except (TypeError, ValueError):
            pass
        if real_role_authorizer and has_current_role_response:
            real_role_authorization_calls.append(context)
            result = await authorize_with_main_role(context)
            real_role_authorization_results.append(dict(result))
            return result
        return {"decision": "approve", "reason": "deterministic E2E policy"}

    engagement = AuipEngagementCoordinator(
        app_runtime=runtime,
        controller=participant,
        role_authorizer=role_authorizer,
        controller_id="e2e-gomoku-participant",
    )
    engagement_callback = engagement.on_update
    bus.on(Method.AUIP_UPDATED, engagement_callback)
    narration: AuipNarrationAdapter | None = None
    narration_callback = None
    if real_narration:
        async def narration_sink(payload: dict[str, Any]) -> dict[str, Any]:
            narration_deliveries.append(dict(payload))
            return {"status": "queued", "sentence_id": payload.get("line_id")}

        async def traced_presenter(payload: dict[str, Any]) -> dict[str, Any] | None:
            narration_requests.append(dict(payload))
            return await present_with_auip_llm(payload)

        narration = AuipNarrationAdapter(
            runtime=runtime,
            observer=decide_with_auip_observer,
            narrator=narrate_with_auip_llm,
            presenter=traced_presenter,
            presentation_mode="structured",
            sink=narration_sink,
            profile=AuipNarrationProfile(
                normal_beat_stride=3,
                max_silent_self_actions=2,
            ),
            recent_chat=lambda _conversation: [],
            display_language=lambda: "japanese",
        )
        narration_callback = narration.enqueue_update
        bus.on(Method.AUIP_UPDATED, narration_callback)
    listener = _listening_socket()
    port = int(listener.getsockname()[1])
    endpoint = f"ws://127.0.0.1:{port}/auip/ws"
    app = FastAPI()

    @app.websocket("/auip/ws")
    async def auip_endpoint(websocket: WebSocket) -> None:
        await manager.handle_connection(websocket)

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await _wait_for(
            lambda: server.started or (server_task.done() and "exited"),
            description="AUIP WebSocket server startup",
        )
        if server_task.done() or not server.started:
            raise RuntimeError("AUIP Gomoku server exited during startup")

        source = _ArtifactSource()
        host = AuipHandler(
            runtime,
            artifacts=source,
            current_session_id=lambda: CONVERSATION_ID,
            app_websocket_url=endpoint,
            engagement=engagement,
        )
        prepared = await host.handle(
            Method.AUIP_ATTACH_PREPARE,
            {"artifact_id": source.artifact.artifact_id},
        )
        assert prepared and prepared.get("ok") is True

        console_errors: list[str] = []
        page_errors: list[str] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                standalone = await browser.new_page()
                standalone.on(
                    "console",
                    lambda message: console_errors.append(message.text)
                    if message.type == "error"
                    else None,
                )
                standalone.on("pageerror", lambda error: page_errors.append(str(error)))
                await standalone.add_init_script("window.WebSocket = undefined")
                await standalone.goto(ENTRY.as_uri(), wait_until="load")
                await standalone.wait_for_function("window.__auipGomoku !== undefined")
                assert await standalone.evaluate("window.__auipGomoku.isAttached()") is False
                standalone_winning_moves = (
                    (0, 0),
                    (0, 1),
                    (1, 0),
                    (1, 1),
                    (2, 0),
                    (2, 1),
                    (3, 0),
                    (3, 1),
                    (4, 0),
                )
                await standalone.locator('.cell[data-x="0"][data-y="0"]').click()
                standalone_state = await standalone.evaluate("window.__auipGomoku.snapshot()")
                assert standalone_state["board"]["rows"][0][0] == "B"
                assert standalone_state["moveCount"] == 1
                for x, y in standalone_winning_moves[1:]:
                    await standalone.locator(
                        f'.cell[data-x="{x}"][data-y="{y}"]'
                    ).click()
                standalone_round = await standalone.evaluate(
                    "window.__auipGomoku.snapshot()"
                )
                assert standalone_round["lifecycle"] == "round_finished"
                await standalone.locator("#reset").click()
                standalone_restarted = await standalone.evaluate(
                    "window.__auipGomoku.snapshot()"
                )
                assert standalone_restarted["lifecycle"] == "playing"
                assert standalone_restarted["moveCount"] == 0
                for x, y in standalone_winning_moves:
                    await standalone.locator(
                        f'.cell[data-x="{x}"][data-y="{y}"]'
                    ).click()
                assert await standalone.locator("#finish").is_enabled()
                await standalone.locator("#finish").click()
                standalone_concluded = await standalone.evaluate(
                    "window.__auipGomoku.snapshot()"
                )
                assert standalone_concluded["lifecycle"] == "concluded"
                await standalone.close()

                attached = await browser.new_page()
                attached.on(
                    "console",
                    lambda message: console_errors.append(message.text)
                    if message.type == "error"
                    else None,
                )
                attached.on("pageerror", lambda error: page_errors.append(str(error)))
                await attached.goto(str(prepared["launch_url"]), wait_until="load")
                await attached.wait_for_function("window.__auipGomoku.isAttached() === true")

                def ready_projection() -> dict[str, Any] | None:
                    candidate = runtime.focused_projection(CONVERSATION_ID)
                    if not candidate or int(candidate.get("revision") or 0) != 1:
                        return None
                    events = candidate.get("recent_key_events")
                    if not isinstance(events, list) or not events:
                        return None
                    return candidate if events[-1].get("type") == "game.ready" else None

                projection = await _wait_for(
                    ready_projection,
                    description="focused Gomoku AppSession with ready event",
                )
                app_session_id = str(projection["app_session_id"])
                assert projection["revision"] == 1
                assert projection["state"]["winner"] == "none"
                assert projection["state"]["roleBindings"] == {
                    "user": "black",
                    "participant": "white",
                }
                assert projection["recent_key_events"][-1]["type"] == "game.ready"

                route_history: list[dict[str, str]] = []
                natural_step: dict[str, Any] = {}
                negotiated_step: dict[str, Any] = {}
                lifecycle_step: dict[str, Any] = {}
                if routed_model:
                    resolver = AuipControlDecisionResolver(
                        query=lambda messages: query_auip_control_model(
                            messages, model=routed_model
                        ),
                        app_runtime=runtime,
                        launch_catalog=EmptyAuipLaunchCatalog(),
                        has_active_work=None,
                    )
                    control_driver = NaturalAuipControlDriver(
                        conversation_id=CONVERSATION_ID,
                        resolver=resolver,
                        route=host.route_control,
                        history=route_history,
                    )
                    for utterance, expected in (
                        ("我先自己下，你在旁边看着就好。", "observe"),
                        ("别光看着了，我们轮流来吧。", "collaborate"),
                    ):
                        decision = await control_driver.turn(
                            utterance,
                            turn_id=f"j7-{expected}",
                        )
                        assert decision.status == "ok", decision
                        assert decision.action == expected, decision
                        assert runtime.get(app_session_id)["engagement_mode"] == expected
                        routed_controls.append(
                            {
                                "utterance": utterance,
                                "decided": decision.action,
                                "expected": expected,
                            }
                        )

                    # Polite question grammar still proposes a current app
                    # outcome. The visible role may settle a different choice;
                    # here it leaves the existing binding unchanged and gives
                    # the user the move, so no application action may occur.
                    role_consensus: dict[str, Any] = {}
                    if real_role_authorizer:
                        consensus_before = runtime.get(app_session_id)
                        participant_before_consensus = len(participant_calls)
                        alternative_response = (
                            "初手はあなたに任せるわ。盤面を見てから応じたいから、"
                            "私は後手でいく。"
                        )
                        consensus_decision = await control_driver.turn(
                            "你可以先手吗？还是不太行？",
                            turn_id="j7-role-consensus",
                            role_response=alternative_response,
                        )
                        await engagement.wait_for_idle(app_session_id)
                        consensus_after = runtime.get(app_session_id)
                        assert consensus_decision.action == "step"
                        assert int(consensus_after["revision"]) == int(
                            consensus_before["revision"]
                        )
                        assert consensus_after.get("pending_action") is None, {
                            "authorization_results": real_role_authorization_results,
                            "last_authorization_input": (
                                real_role_authorization_calls[-1]
                                if real_role_authorization_calls
                                else {}
                            ),
                        }
                        role_consensus = {
                            "decided": consensus_decision.action,
                            "role_response": alternative_response,
                            "action_suppressed": True,
                            "participant_was_consulted": (
                                len(participant_calls) > participant_before_consensus
                            ),
                        }

                    # This is the seam the earlier assembly suite skipped:
                    # natural language must become one source-local step before
                    # the Participant or app action path can do any work.
                    next_move.update({"x": 4, "y": 4})
                    before_step = runtime.get(app_session_id)
                    step_utterance = "这局你能下先手吗"
                    step_decision = await control_driver.turn(
                        step_utterance,
                        turn_id="j7-natural-step",
                    )
                    assert step_decision.status == "ok", step_decision
                    assert step_decision.action == "step", step_decision
                    await engagement.wait_for_idle(app_session_id)
                    stepped = await _wait_for(
                        lambda: runtime.get(app_session_id)
                        if int(runtime.get(app_session_id).get("revision") or 0)
                        == int(before_step.get("revision") or 0) + 1
                        and isinstance(
                            runtime.get(app_session_id).get(
                                "latest_verified_self_action"
                            ),
                            dict,
                        )
                        else None,
                        description="natural-language step accepted receipt",
                    )
                    verified = stepped["latest_verified_self_action"]
                    assert verified["type"] == "game.take_first_move"
                    assert verified["payload"] == {"x": 4, "y": 4}
                    assert stepped["state"]["roleBindings"] == {
                        "user": "white",
                        "participant": "black",
                    }
                    assert await attached.locator(
                        '.cell[data-x="4"][data-y="4"]'
                    ).get_attribute("class") == "cell black last"
                    context_after_step = runtime.render_main_chat_context(
                        CONVERSATION_ID,
                        include_control_contract=False,
                    )
                    assert "latest_verified_self_action" in context_after_step
                    assert "game.take_first_move" in context_after_step

                    # A question about the accepted action is read-only on the
                    # AUIP axis and must not silently schedule another move.
                    revision_after_step = int(stepped["revision"])
                    participant_calls_after_step = len(participant_calls)
                    query_utterance = "你已经落子了吗？"
                    query_decision = await control_driver.turn(
                        query_utterance,
                        turn_id="j7-step-status",
                    )
                    assert query_decision.status == "ok", query_decision
                    assert query_decision.action == "none", query_decision
                    await asyncio.sleep(0.1)
                    after_query = runtime.get(app_session_id)
                    assert int(after_query["revision"]) == revision_after_step
                    assert len(participant_calls) == participant_calls_after_step
                    natural_step = {
                        "utterance": step_utterance,
                        "decided": step_decision.action,
                        "receipt_type": verified["type"],
                        "resulting_revision": verified["resulting_revision"],
                        "participant_role": stepped["state"]["roleBindings"][
                            "participant"
                        ],
                        "status_utterance": query_utterance,
                        "status_decided": query_decision.action,
                        "status_preserved_revision": True,
                    }
                    routed_controls.extend(
                        (
                            {
                                "utterance": step_utterance,
                                "decided": step_decision.action,
                                "expected": "step",
                            },
                            {
                                "utterance": query_utterance,
                                "decided": query_decision.action,
                                "expected": "none",
                            },
                        )
                    )

                    # Put the app in observe mode for one accepted user move,
                    # leaving a participant-owned turn without autonomously
                    # scheduling it. The next Chat turn then carries an exact
                    # user/role agreement through the production inline step
                    # refinement into one immutable Participant proposal.
                    observed = await host.handle(
                        Method.AUIP_MODE_SET,
                        {"app_session_id": app_session_id, "mode": "observe"},
                    )
                    assert observed and observed["engagement_mode"] == "observe"
                    before_user_move = int(runtime.get(app_session_id)["revision"])
                    await attached.locator('.cell[data-x="8"][data-y="8"]').click()
                    ready_for_negotiation = await _wait_for(
                        lambda: runtime.get(app_session_id)
                        if int(runtime.get(app_session_id)["revision"])
                        == before_user_move + 1
                        and runtime.get(app_session_id)["state"]["turn"] == "black"
                        else None,
                        description="negotiated participant turn",
                    )
                    participant_before_negotiation = len(participant_calls)
                    next_move.update({"x": 5, "y": 4})
                    agreed_instruction = "Place one stone at x=5, y=4."
                    negotiated_utterance = (
                        "下一手就走坐标 x=5、y=4 吧，我们按这个来。"
                    )
                    negotiated_decision = await control_driver.turn(
                        negotiated_utterance,
                        turn_id="j7-negotiated-step",
                        role_response=(
                            "ええ、x=5、y=4 に置くわ。"
                            f'[AUIP action="step" instruction="{agreed_instruction}"]'
                        ),
                    )
                    assert negotiated_decision.status == "ok", negotiated_decision
                    assert negotiated_decision.action == "step", negotiated_decision
                    await engagement.wait_for_idle(app_session_id)
                    negotiated = await _wait_for(
                        lambda: runtime.get(app_session_id)
                        if int(runtime.get(app_session_id)["revision"])
                        == int(ready_for_negotiation["revision"]) + 1
                        and (
                            runtime.get(app_session_id).get(
                                "latest_verified_self_action"
                            )
                            or {}
                        ).get("payload")
                        == {"x": 5, "y": 4}
                        else None,
                        description="negotiated step accepted receipt",
                    )
                    assert len(participant_calls) == participant_before_negotiation + 1
                    participant_context = participant_calls[-1]
                    negotiated_context = json.loads(
                        str(
                            participant_context.get(
                                "global_conversation_context"
                            )
                            or "{}"
                        )
                    )
                    negotiated_receipt = negotiated["latest_verified_self_action"]
                    assert negotiated_context.get("instruction") == agreed_instruction
                    assert negotiated_context.get("current_role_response") == (
                        "ええ、x=5、y=4 に置くわ。"
                    )
                    assert negotiated_receipt["type"] == "game.place_stone"
                    assert negotiated_receipt["payload"] == {"x": 5, "y": 4}
                    assert await attached.locator(
                        '.cell[data-x="5"][data-y="4"]'
                    ).get_attribute("class") == "cell black last"
                    negotiated_step = {
                        "utterance": negotiated_utterance,
                        "decided": negotiated_decision.action,
                        "agreed_instruction": agreed_instruction,
                        "participant_instruction": negotiated_context.get(
                            "instruction"
                        ),
                        "receipt_type": negotiated_receipt["type"],
                        "receipt_payload": negotiated_receipt["payload"],
                        "resulting_revision": negotiated_receipt[
                            "resulting_revision"
                        ],
                    }
                else:
                    stance = await host.handle(
                        Method.AUIP_MODE_SET,
                        {"app_session_id": app_session_id, "mode": "collaborate"},
                    )
                    assert stance and stance["engagement_mode"] == "collaborate"

                playing_actions = runtime.participant_context(app_session_id)[
                    "available_actions"
                ]
                assert "game.resign" in playing_actions
                assert "game.restart_round" not in playing_actions
                assert "game.finish_experience" not in playing_actions
                try:
                    runtime.invoke_action(
                        app_session_id=app_session_id,
                        actor="kurisu",
                        type="game.finish_experience",
                        payload={},
                        expected_revision=int(runtime.get(app_session_id)["revision"]),
                    )
                except AuipProtocolError as exc:
                    assert exc.code == "action_not_available"
                else:
                    raise AssertionError(
                        "playing lifecycle must reject a post-round action before dispatch"
                    )

                real_participant_receipt: dict[str, Any] = {}
                if real_participant:
                    # Strategy is intentionally not deterministic. Give the
                    # configured Participant one real application-declared
                    # turn and assert only the protocol contract: one legal
                    # typed move, an accepted receipt, and the matching board
                    # state. The deterministic terminal fixture starts from a
                    # reset board afterward, so a model's valid choice cannot
                    # be mistaken for a journey regression.
                    if int((await attached.evaluate(
                        "window.__auipGomoku.snapshot().moveCount"
                    ))) > 0:
                        before_reset = int(runtime.get(app_session_id)["revision"])
                        await attached.locator("#reset").click()
                        await _wait_for(
                            lambda: runtime.get(app_session_id)
                            if int(runtime.get(app_session_id)["revision"])
                            == before_reset + 1
                            and int(runtime.get(app_session_id)["state"]["moveCount"])
                            == 0
                            else None,
                            description="pre-canary Gomoku reset",
                        )
                    real_participant_enabled = True
                    canary_revision = int(runtime.get(app_session_id)["revision"])
                    real_participant_receipt = (
                        await _user_then_automatic_participant_move(
                            attached,
                            runtime,
                            app_session_id,
                            next_move,
                            user_x=0,
                            user_y=0,
                            participant_x=-1,
                            participant_y=-1,
                            starting_revision=canary_revision,
                            exact_participant_move=False,
                        )
                    )
                    real_participant_enabled = False
                    before_reset = int(runtime.get(app_session_id)["revision"])
                    await attached.locator("#reset").click()
                    await _wait_for(
                        lambda: runtime.get(app_session_id)
                        if int(runtime.get(app_session_id)["revision"])
                        == before_reset + 1
                        and int(runtime.get(app_session_id)["state"]["moveCount"])
                        == 0
                        else None,
                        description="post-canary Gomoku reset",
                    )

                # Each accepted user move publishes a manifest-declared
                # participant opportunity. Collaborate mode must schedule the
                # next typed action without an explicit AUIP_STEP from this
                # harness. White, played by Kurisu, wins on the last receipt.
                remaining_moves = (
                    (
                        ((0, 0), (0, 4)),
                        ((1, 0), (1, 4)),
                        ((2, 0), (2, 4)),
                        ((3, 0), (3, 4)),
                    )
                    if routed_model and not real_participant
                    else (
                        ((0, 0), (0, 1)),
                        ((1, 0), (1, 1)),
                        ((2, 0), (2, 1)),
                        ((3, 0), (3, 1)),
                        ((8, 8), (4, 1)),
                    )
                )
                fixture_start_revision = int(runtime.get(app_session_id)["revision"])
                for user_move, participant_move in remaining_moves:
                    await _user_then_automatic_participant_move(
                        attached,
                        runtime,
                        app_session_id,
                        next_move,
                        user_x=user_move[0],
                        user_y=user_move[1],
                        participant_x=participant_move[0],
                        participant_y=participant_move[1],
                        starting_revision=int(runtime.get(app_session_id)["revision"]),
                    )
                    if narration is not None and runtime.get(app_session_id)["status"] == "active":
                        # A headless fixture can otherwise finish before a real
                        # Observer/Narrator call gets a delivery window.  Waiting
                        # here models the natural pause between user turns and
                        # proves intermediate commentary independently from the
                        # mandatory terminal report.
                        await asyncio.sleep(0.05)
                        await narration.wait_for_idle()

                intermediate_deliveries = [
                    item
                    for item in narration_deliveries
                    if item.get("source") == "auip_narrator"
                    and item.get("terminal") is not True
                ]
                if narration is not None:
                    assert intermediate_deliveries, (
                        "real AUIP narration delivered no intermediate comment "
                        "before the terminal turn"
                    )

                expected_round_revision = fixture_start_revision + 2 * len(
                    remaining_moves
                )
                expected_winner = (
                    "black" if routed_model and not real_participant else "white"
                )
                # The live runtime keeps every receipt in its bounded deque,
                # while the closed Chat capsule intentionally retains the last
                # four verified actions only.
                expected_verified_actions = 4
                round_result = await _wait_for(
                    lambda: (
                        snapshot
                        if snapshot.get("status") == "active"
                        and snapshot.get("pending_action") is None
                        and (snapshot.get("state") or {}).get("lifecycle")
                        == "round_finished"
                        and isinstance(
                            snapshot.get("latest_verified_self_action"), dict
                        )
                        and int(
                            snapshot["latest_verified_self_action"].get(
                                "resulting_revision"
                            )
                            or 0
                        )
                        == expected_round_revision
                        else None
                    )
                    if (snapshot := runtime.get(app_session_id))
                    else None,
                    description="nonterminal Gomoku round result",
                )
                assert round_result["revision"] == expected_round_revision
                assert round_result["state"]["winner"] == expected_winner
                assert round_result.get("experience_capsule") is None
                post_round_actions = runtime.participant_context(app_session_id)[
                    "available_actions"
                ]
                assert "game.resign" not in post_round_actions
                assert "game.restart_round" in post_round_actions
                assert "game.finish_experience" in post_round_actions
                assert await attached.locator("#status").inner_text() == (
                    f"{expected_winner.title()} wins"
                )
                await engagement.wait_for_idle(app_session_id)
                settled_round = runtime.get(app_session_id)
                assert settled_round["operator_status"] == "idle"
                assert not settled_round.get("operator_error")

                lifecycle_conclusion_requested = True
                if routed_model:
                    lifecycle_utterance = "这局到这吧，结束这个系列。"
                    lifecycle_decision = await control_driver.turn(
                        lifecycle_utterance,
                        turn_id="j7-lifecycle-conclude",
                    )
                    assert lifecycle_decision.status == "ok", lifecycle_decision
                    assert lifecycle_decision.action == "step", lifecycle_decision
                    lifecycle_step = {
                        "utterance": lifecycle_utterance,
                        "decided": lifecycle_decision.action,
                        "expected": "step",
                    }
                    routed_controls.append(dict(lifecycle_step))
                else:
                    scheduled = engagement.request_step(
                        app_session_id=app_session_id,
                        instruction=(
                            "Conclude this app-owned match series at the current "
                            "post-round choice."
                        ),
                        expected_revision=expected_round_revision,
                    )
                    assert scheduled.get("scheduled") is True
                await engagement.wait_for_idle(app_session_id)
                expected_terminal_revision = expected_round_revision + 1
                terminal = await _wait_for(
                    lambda: (
                        snapshot
                        if snapshot.get("status") == "completed"
                        and snapshot.get("pending_action") is None
                        and (
                            snapshot.get("latest_verified_self_action") or {}
                        ).get("type")
                        == "game.finish_experience"
                        else None
                    )
                    if (snapshot := runtime.get(app_session_id))
                    else None,
                    description="Gomoku experience terminal event",
                )
                assert terminal["revision"] == expected_terminal_revision
                assert terminal["state"]["winner"] == expected_winner
                terminal_capsule = terminal["experience_capsule"]
                assert terminal_capsule["terminal"]["type"] == "game.experience_finished"
                assert (
                    terminal_capsule["terminal"]["revision"]
                    == expected_terminal_revision
                )
                assert (
                    len(terminal_capsule["verified_self_actions"])
                    == expected_verified_actions
                )
                assert await attached.locator("#status").inner_text() == "Series finished"
                if narration is not None:
                    await asyncio.sleep(0.05)
                    await narration.wait_for_idle()
                    terminal_deliveries = [
                        item
                        for item in narration_deliveries
                        if item.get("terminal") is True
                    ]
                    assert terminal_deliveries, (
                        "real AUIP narration delivered no terminal summary"
                    )

                closed = await attached.evaluate(
                    "window.__auipGomoku.close('journey_complete')"
                )
                assert closed["status"] == "closed"
                await attached.close()
            finally:
                await browser.close()

        snapshot = runtime.get(app_session_id)
        capsule = snapshot["experience_capsule"]
        context = runtime.render_main_chat_context(CONVERSATION_ID)
        assert snapshot["status"] == "closed"
        assert capsule["close_reason"] == "journey_complete"
        assert capsule["terminal"]["type"] == "game.experience_finished"
        expected_verified_actions = 4
        assert len(capsule["verified_self_actions"]) == expected_verified_actions
        assert capsule["verified_self_actions"][-1]["type"] == "game.finish_experience"
        assert capsule["verified_self_actions"][-2]["payload"] == (
            {"x": 3, "y": 4}
            if routed_model and not real_participant
            else {"x": 4, "y": 1}
        )
        assert "Recent AUIP branch capsule" in context
        assert '"type":"game.finish_experience"' in context
        assert '"type":"game.place_stone"' in context
        assert "attachTicket" not in context
        assert "board" not in context
        assert not console_errors, console_errors
        assert not page_errors, page_errors
        narration_trace: list[dict[str, Any]] = []
        if real_narration:
            facts_by_request = {
                str(item.get("request_id") or ""): item
                for item in narration_requests
                if str(item.get("request_id") or "")
            }
            for item in narration_deliveries:
                request_id = str(item.get("line_id") or "")
                request = facts_by_request.get(request_id, {})
                narration_trace.append(
                    {
                        "request_id": request_id,
                        "terminal": bool(item.get("terminal")),
                        "source": str(item.get("source") or ""),
                        "display_text": str(item.get("display_text") or ""),
                        "facts": list(request.get("facts") or []),
                        "voice_text_matches": (
                            item.get("voice_text_ja") == item.get("display_text")
                        ),
                    }
                )
            assert narration_trace
            scene_trace = [
                item for item in narration_trace if item["source"] == "auip_narrator"
            ]
            assert any(not item["terminal"] for item in scene_trace)
            terminal_trace = [item for item in scene_trace if item["terminal"]]
            assert terminal_trace
            assert all(item["display_text"] for item in narration_trace)
            assert all(item["voice_text_matches"] for item in narration_trace)
            terminal_facts = terminal_trace[-1]["facts"]
            assert terminal_facts
            terminal_fact = next(
                item for item in terminal_facts if item.get("terminal") is True
            )
            assert terminal_fact["outcome"]["winner_side"] == snapshot["state"][
                "winner"
            ]
            delivered_texts = [item["display_text"] for item in narration_trace]
            assert capsule["delivered_narration"] == delivered_texts
            assert all(text in context for text in delivered_texts)
        shared_state_chars = len(
            json.dumps(snapshot["state"], ensure_ascii=False, separators=(",", ":"))
        )
        assert shared_state_chars <= 1024, shared_state_chars
        return {
            "ok": True,
            "browser": "chromium-headless",
            "transport": "real_websocket",
            "app_session_id": app_session_id,
            "standalone_move_count": standalone_state["moveCount"],
            "attached_final_revision": snapshot["revision"],
            "winner": snapshot["state"]["winner"],
            "retained_kurisu_actions": len(capsule["verified_self_actions"]),
            "participant_decisions": len(participant_calls),
            "participant_transport": (
                "real_model_canary+deterministic_terminal"
                if real_participant
                else "deterministic"
            ),
            "real_participant_action": (
                dict(real_participant_receipt.get("payload") or {})
                if real_participant
                else {}
            ),
            "role_authorizer_transport": (
                "real_explicit_turns+deterministic_fixture"
                if real_role_authorizer
                else "deterministic"
            ),
            "real_role_authorization_count": len(real_role_authorization_calls),
            "role_consensus": role_consensus if routed_model else {},
            "narration_delivery_count": len(narration_deliveries),
            "narration_trace": narration_trace,
            "terminal_event": capsule["terminal"]["type"],
            "close_reason": capsule["close_reason"],
            "routed_controls": routed_controls,
            "natural_step": natural_step,
            "negotiated_step": negotiated_step,
            "lifecycle_step": lifecycle_step,
            "identity_preserved": True,
            "context_bounded": True,
            "shared_state_chars": shared_state_chars,
            "console_errors": console_errors,
            "page_errors": page_errors,
        }
    finally:
        if narration_callback is not None:
            bus.off(Method.AUIP_UPDATED, narration_callback)
        if narration is not None:
            await narration.close()
        bus.off(Method.AUIP_UPDATED, engagement_callback)
        await engagement.close()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5.0)
        listener.close()


async def run_routed_mode_walk(*, model: str = "deepseek-v4-flash") -> dict[str, Any]:
    """Drive observe/collaborate/delegate/leave on one live AppSession through
    the real source-local control decision, not direct AUIP_MODE_SET.

    This joins the two halves that were previously separate: a genuinely live
    in-memory AppSession (real Chromium + /auip/ws) and codex's model-driven
    ``AuipControlDecisionResolver``, which reads the live ``focused_projection``.
    Each natural utterance must resolve to the expected effective AUIP action.
    Redundant entry vocabulary may be normalized from the Host-owned active
    identity, but it must never create a second AppSession.
    """

    from playwright.async_api import async_playwright
    runtime = AuipRuntime()
    manager = AuipAppConnectionManager(runtime)
    next_move: dict[str, int] = {"x": 7, "y": 7}
    participant_calls: list[dict[str, Any]] = []

    async def participant(context: dict[str, Any]) -> dict[str, Any]:
        participant_calls.append(context)
        return {
            "action": "act",
            "type": "game.place_stone",
            "payload": dict(next_move),
            "private_note": "bounded deterministic routed mode-walk move",
        }

    engagement = AuipEngagementCoordinator(
        app_runtime=runtime,
        controller=participant,
        role_authorizer=lambda _context: {
            "decision": "approve",
            "reason": "deterministic E2E policy",
        },
        controller_id="e2e-gomoku-routed",
    )
    listener = _listening_socket()
    port = int(listener.getsockname()[1])
    endpoint = f"ws://127.0.0.1:{port}/auip/ws"
    app = FastAPI()

    @app.websocket("/auip/ws")
    async def auip_endpoint(websocket: WebSocket) -> None:
        await manager.handle_connection(websocket)

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await _wait_for(
            lambda: server.started or (server_task.done() and "exited"),
            description="AUIP routed-walk server startup",
        )
        if server_task.done() or not server.started:
            raise RuntimeError("AUIP routed-walk server exited during startup")

        source = _ArtifactSource()
        host = AuipHandler(
            runtime,
            artifacts=source,
            current_session_id=lambda: CONVERSATION_ID,
            app_websocket_url=endpoint,
            engagement=engagement,
        )
        prepared = await host.handle(
            Method.AUIP_ATTACH_PREPARE,
            {"artifact_id": source.artifact.artifact_id},
        )
        assert prepared and prepared.get("ok") is True

        resolver = AuipControlDecisionResolver(
            query=lambda messages: query_auip_control_model(messages, model=model),
            app_runtime=runtime,
            launch_catalog=EmptyAuipLaunchCatalog(),
            has_active_work=None,
        )
        utterances = [
            ("我先自己下，你在旁边看着就好。", "observe"),
            ("我们轮流来吧。", "collaborate"),
            ("接下来这局你自己玩到结束吧。", "delegate"),
            ("我是说这个游戏先不玩了，退出这次体验吧。", "leave"),
        ]
        history: list[dict[str, str]] = []
        routed: list[dict[str, Any]] = []

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                attached = await browser.new_page()
                await attached.goto(str(prepared["launch_url"]), wait_until="load")
                await attached.wait_for_function(
                    "window.__auipGomoku.isAttached() === true"
                )

                def ready_projection() -> dict[str, Any] | None:
                    candidate = runtime.focused_projection(CONVERSATION_ID)
                    if not candidate or int(candidate.get("revision") or 0) != 1:
                        return None
                    events = candidate.get("recent_key_events")
                    if not isinstance(events, list) or not events:
                        return None
                    return candidate if events[-1].get("type") == "game.ready" else None

                projection = await _wait_for(
                    ready_projection,
                    description="focused Gomoku AppSession with ready event",
                )
                app_session_id = str(projection["app_session_id"])
                assert not participant_calls

                for utterance, expected in utterances:
                    pending = resolver.capture(
                        session_id=CONVERSATION_ID,
                        user_text=utterance,
                        prior_messages=history,
                    )
                    assert pending is not None, (
                        f"an active AppSession must keep AUIP in scope for: {utterance}"
                    )
                    decision = await pending
                    assert decision.status == "ok", (
                        f"{utterance!r}: status={decision.status} "
                        f"reason={decision.reason} raw={decision.raw_reply!r}"
                    )
                    assert decision.action == expected, (
                        f"{utterance!r}: routed {decision.action!r}, "
                        f"expected {expected!r}; raw={decision.raw_reply!r}"
                    )
                    assert decision.action not in {"launch", "engage"}, (
                        "an active-session decision must not remain a relaunch"
                    )
                    if decision.action in {"observe", "collaborate", "delegate"}:
                        calls_before_mode = len(participant_calls)
                        applied = await host.handle(
                            Method.AUIP_MODE_SET,
                            {"app_session_id": app_session_id, "mode": decision.action},
                        )
                        assert applied and applied["engagement_mode"] == decision.action
                        if decision.action == "delegate":
                            await engagement.wait_for_idle(app_session_id)
                            assert len(participant_calls) > calls_before_mode, (
                                "delegate must schedule an autonomous Participant decision"
                            )
                        else:
                            await asyncio.sleep(0.05)
                            assert len(participant_calls) == calls_before_mode, (
                                "observe/collaborate must not schedule an autonomous "
                                "Participant decision"
                            )
                        focused = runtime.focused_projection(CONVERSATION_ID)
                        assert str(focused["app_session_id"]) == app_session_id
                        assert runtime.get(app_session_id)["status"] == "active"
                    elif decision.action == "leave":
                        left = await engagement.leave(
                            app_session_id=app_session_id, reason="user_left"
                        )
                        assert left["status"] == "closed"
                    routed.append(
                        {
                            "utterance": utterance,
                            "decided": decision.action,
                            "expected": expected,
                        }
                    )
                    history.append({"role": "user", "content": utterance})
                    history.append({"role": "assistant", "content": "はい。"})

                await attached.close()
            finally:
                await browser.close()

        closed = runtime.get(app_session_id)
        assert closed["status"] == "closed"
        return {
            "ok": True,
            "browser": "chromium-headless",
            "transport": "real_websocket",
            "model": model,
            "app_session_id": app_session_id,
            "routed": routed,
            "all_routed_correctly": all(
                item["decided"] == item["expected"] for item in routed
            ),
            "participant_decisions": len(participant_calls),
            "identity_preserved": True,
            "final_status": closed["status"],
        }
    finally:
        await engagement.close()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5.0)
        listener.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument(
        "--routed",
        action="store_true",
        help="also run the model-driven routed mode walk (real model call)",
    )
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument(
        "--real-participant",
        action="store_true",
        help="use the configured AUIP Participant model for real app actions",
    )
    parser.add_argument(
        "--real-narration",
        action="store_true",
        help="run the production structured AUIP presentation model",
    )
    args = parser.parse_args()
    if args.routed:
        report: dict[str, Any] = {
            "routed_mode_walk": asyncio.run(run_routed_mode_walk(model=args.model)),
            "journey": asyncio.run(
                run_journey(
                    real_participant=args.real_participant,
                    real_narration=args.real_narration,
                )
            ),
        }
    else:
        # Preserve the original CLI/report shape for existing callers. The
        # model-routed evidence is explicit because it spends a real model call.
        report = asyncio.run(
            run_journey(
                real_participant=args.real_participant,
                real_narration=args.real_narration,
            )
        )
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
