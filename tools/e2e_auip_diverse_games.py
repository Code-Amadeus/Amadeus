"""Run non-alternating and continuous-time AUIP apps in real Chromium."""

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
    from playwright.async_api import Browser, Page

import uvicorn
from fastapi import FastAPI, WebSocket

from server.auip_app_connection import AuipAppConnectionManager
from server.auip_control_decision import AuipControlDecisionResolver
from server.auip_engagement import AuipEngagementCoordinator
from server.auip_runtime import AuipRuntime
from server.event_bus import bus
from server.handlers.auip_handler import AuipHandler
from server.protocol import Method
from tools.auip_natural_control_driver import (
    EmptyAuipLaunchCatalog,
    NaturalAuipControlDriver,
    query_auip_control_model,
)


SAMPLES = {
    "2048": ROOT / "examples" / "auip-2048" / "index.html",
    "reactor": ROOT / "examples" / "auip-reactor" / "index.html",
}


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
        self.artifacts: dict[str, _Artifact] = {}
        self.items: dict[str, _WorkItem] = {}
        self.attempts: dict[str, _Attempt] = {}
        for name, entry in SAMPLES.items():
            artifact_id = f"artifact-auip-{name}"
            work_item_id = f"work-auip-{name}"
            self.artifacts[artifact_id] = _Artifact(
                artifact_id=artifact_id,
                work_item_id=work_item_id,
                attempt_id=f"attempt-auip-{name}",
                kind="business.file",
                title=name,
                path=str(entry),
                status="registered",
                sha256=hashlib.sha256(entry.read_bytes()).hexdigest(),
            )
            manifest = entry.with_name("auip.manifest.json")
            manifest_artifact_id = f"artifact-auip-{name}-manifest"
            self.artifacts[manifest_artifact_id] = _Artifact(
                artifact_id=manifest_artifact_id,
                work_item_id=work_item_id,
                attempt_id=f"attempt-auip-{name}",
                kind="business.file",
                title="AUIP manifest",
                path=str(manifest),
                status="registered",
                sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
            )
            self.items[work_item_id] = _WorkItem(workspace_path=str(ROOT))
            attempt_id = f"attempt-auip-{name}"
            self.attempts[attempt_id] = _Attempt(
                attempt_id=attempt_id,
                metadata={},
            )

    def get_artifact(self, artifact_id: str) -> Any:
        return self.artifacts.get(artifact_id)

    def get_work_item(self, work_item_id: str) -> Any:
        return self.items.get(work_item_id)

    def get_attempt(self, attempt_id: str) -> Any:
        return self.attempts.get(attempt_id)

    def list_artifacts(self, work_item_id: str, *, attempt_id: str = "") -> list[Any]:
        return [
            artifact
            for artifact in self.artifacts.values()
            if artifact.work_item_id == work_item_id
            and (not attempt_id or artifact.attempt_id == attempt_id)
        ]


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
    timeout_s: float = 8.0,
) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {description}")


def _scalar_value(value: Any, metric_id: str) -> float | None:
    if isinstance(value, dict):
        if value.get("kind") == "scalars/v1":
            for metric in value.get("metrics") or []:
                if (
                    isinstance(metric, dict)
                    and str(metric.get("id") or "") == metric_id
                    and isinstance(metric.get("value"), (int, float))
                ):
                    return float(metric["value"])
        for nested in value.values():
            found = _scalar_value(nested, metric_id)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _scalar_value(nested, metric_id)
            if found is not None:
                return found
    return None


async def _ready_projection(
    runtime: AuipRuntime,
    conversation_id: str,
    event_type: str,
) -> dict[str, Any]:
    def ready() -> dict[str, Any] | None:
        projection = runtime.focused_projection(conversation_id)
        events = projection.get("recent_key_events") if projection else None
        if not isinstance(events, list):
            return None
        return projection if any(item.get("type") == event_type for item in events) else None

    return await _wait_for(ready, description=f"{event_type} projection")


async def _invoke(
    host: AuipHandler,
    runtime: AuipRuntime,
    *,
    app_session_id: str,
    action_type: str,
    payload: dict[str, Any],
    expected_revision: int,
) -> dict[str, Any]:
    result = await host.handle(
        Method.AUIP_ACTION_INVOKE,
        {
            "app_session_id": app_session_id,
            "actor": "kurisu",
            "action_type": action_type,
            "payload": payload,
            "expected_revision": expected_revision,
        },
    )
    assert result and result.get("ok") is True, result

    def resolved() -> dict[str, Any] | None:
        snapshot = runtime.get(app_session_id)
        action = snapshot.get("latest_verified_self_action")
        if not isinstance(action, dict):
            return None
        if int(action.get("resulting_revision") or 0) <= expected_revision:
            return None
        return snapshot if snapshot.get("pending_action") is None else None

    return await _wait_for(resolved, description=f"accepted {action_type}")


async def _new_page(browser: Browser, errors: dict[str, list[str]]) -> Page:
    page = await browser.new_page()
    page.on(
        "console",
        lambda message: errors["console"].append(message.text)
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: errors["page"].append(str(error)))
    return page


async def _run_2048(
    browser: Browser,
    host: AuipHandler,
    runtime: AuipRuntime,
    source: _ArtifactSource,
    set_conversation: Any,
    *,
    model: str,
    engagement: AuipEngagementCoordinator,
    next_actions: dict[str, tuple[str, dict[str, Any]]],
    participant_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    conversation_id = "conversation-auip-2048"
    set_conversation(conversation_id)
    errors: dict[str, list[str]] = {"console": [], "page": []}

    standalone = await _new_page(browser, errors)
    await standalone.goto(SAMPLES["2048"].as_uri(), wait_until="load")
    await standalone.wait_for_function("window.__auip2048 !== undefined")
    assert await standalone.evaluate("window.__auip2048.isAttached()") is False
    await standalone.locator('button[data-direction="left"]').click()
    standalone_state = await standalone.evaluate("window.__auip2048.snapshot()")
    assert standalone_state["moveCount"] == 1
    await standalone.close()

    prepared = await host.handle(
        Method.AUIP_ATTACH_PREPARE,
        {"artifact_id": source.artifacts["artifact-auip-2048"].artifact_id},
    )
    assert prepared and prepared.get("ok") is True
    page = await _new_page(browser, errors)
    await page.goto(str(prepared["launch_url"]), wait_until="load")
    await page.wait_for_function("window.__auip2048 && window.__auip2048.isAttached()")
    projection = await _ready_projection(runtime, conversation_id, "game.ready")
    app_session_id = str(projection["app_session_id"])
    stance = await host.handle(
        Method.AUIP_STANCE_SET,
        {"app_session_id": app_session_id, "stance": "participant"},
    )
    assert stance and stance.get("stance") == "participant"

    resolver = AuipControlDecisionResolver(
        query=lambda messages: query_auip_control_model(messages, model=model),
        app_runtime=runtime,
        launch_catalog=EmptyAuipLaunchCatalog(),
        has_active_work=None,
    )
    driver = NaturalAuipControlDriver(
        conversation_id=conversation_id,
        resolver=resolver,
        route=host.route_control,
    )
    natural_direction = await page.evaluate("window.__auip2048.legalDirections()[0]")
    next_actions[app_session_id] = (
        "game.slide",
        {"direction": natural_direction},
    )
    calls_before_step = len(participant_calls)
    natural_step = await driver.turn("你能先滑动一步吗", turn_id="diverse-2048-step")
    assert natural_step.status == "ok" and natural_step.action == "step", natural_step
    await engagement.wait_for_idle(app_session_id)

    def natural_2048_receipt() -> dict[str, Any] | None:
        snapshot = runtime.get(app_session_id)
        latest = snapshot.get("latest_verified_self_action")
        if not isinstance(latest, dict) or latest.get("type") != "game.slide":
            return None
        return (
            snapshot
            if int(snapshot["revision"]) > int(projection["revision"])
            else None
        )

    stepped = await _wait_for(
        natural_2048_receipt,
        description="2048 natural step receipt",
    )
    assert len(participant_calls) == calls_before_step + 1
    assert stepped["latest_verified_self_action"]["payload"] == {
        "direction": natural_direction
    }
    revision_after_step = int(stepped["revision"])
    query = await driver.turn("刚才滑动了吗？", turn_id="diverse-2048-status")
    assert query.status == "ok" and query.action == "none", query
    await asyncio.sleep(0.05)
    assert int(runtime.get(app_session_id)["revision"]) == revision_after_step
    assert len(participant_calls) == calls_before_step + 1

    stale = await host.handle(
        Method.AUIP_ACTION_INVOKE,
        {
            "app_session_id": app_session_id,
            "actor": "kurisu",
            "action_type": "game.slide",
            "payload": {"direction": "left"},
            "expected_revision": int(projection["revision"]) - 1,
        },
    )
    assert stale and stale.get("error") == "stale_action_revision"

    first_direction = await page.evaluate("window.__auip2048.legalDirections()[0]")
    first = await _invoke(
        host,
        runtime,
        app_session_id=app_session_id,
        action_type="game.slide",
        payload={"direction": first_direction},
        expected_revision=int(runtime.get(app_session_id)["revision"]),
    )
    first_revision = int(first["revision"])

    user_direction = await page.evaluate("window.__auip2048.legalDirections()[0]")
    await page.locator(f'button[data-direction="{user_direction}"]').click()
    await _wait_for(
        lambda: runtime.get(app_session_id)
        if int(runtime.get(app_session_id)["revision"]) == first_revision + 1
        else None,
        description="2048 user slide",
    )

    second_direction = await page.evaluate("window.__auip2048.legalDirections()[0]")
    final = await _invoke(
        host,
        runtime,
        app_session_id=app_session_id,
        action_type="game.slide",
        payload={"direction": second_direction},
        expected_revision=int(runtime.get(app_session_id)["revision"]),
    )
    closed = await page.evaluate("window.__auip2048.close('journey_complete')")
    assert closed["status"] == "closed"
    await page.close()

    snapshot = runtime.get(app_session_id)
    capsule = snapshot["experience_capsule"]
    assert snapshot["status"] == "closed"
    assert len(capsule["verified_self_actions"]) == 3
    assert capsule["verified_self_actions"][0]["effects"]["slide"]["label"].startswith("slide ")
    assert "Recent AUIP branch capsule" in runtime.render_main_chat_context(conversation_id)
    assert not errors["console"]
    assert not errors["page"]
    return {
        "standalone_move_count": standalone_state["moveCount"],
        "attached_final_revision": final["revision"],
        "kurisu_actions": len(capsule["verified_self_actions"]),
        "stale_action_error": stale["error"],
        "highest_tile": snapshot["state"]["highestTile"],
        "console_errors": errors["console"],
        "page_errors": errors["page"],
        "natural_step": {
            "decided": natural_step.action,
            "receipt_type": "game.slide",
            "status_decided": query.action,
        },
    }


async def _run_reactor(
    browser: Browser,
    host: AuipHandler,
    runtime: AuipRuntime,
    source: _ArtifactSource,
    set_conversation: Any,
    *,
    model: str,
    engagement: AuipEngagementCoordinator,
    next_actions: dict[str, tuple[str, dict[str, Any]]],
    participant_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    conversation_id = "conversation-auip-reactor"
    set_conversation(conversation_id)
    errors: dict[str, list[str]] = {"console": [], "page": []}

    standalone = await _new_page(browser, errors)
    await standalone.goto(SAMPLES["reactor"].as_uri(), wait_until="load")
    await standalone.wait_for_function("window.__auipReactor !== undefined")
    await standalone.wait_for_function("window.__auipReactor.tickCount() >= 5")
    standalone_state = await standalone.evaluate("window.__auipReactor.snapshot()")
    standalone_ticks = await standalone.evaluate("window.__auipReactor.tickCount()")
    assert await standalone.evaluate("window.__auipReactor.isAttached()") is False
    standalone_heat = _scalar_value(standalone_state, "heat")
    assert standalone_heat is not None and standalone_heat > 56
    await standalone.close()

    prepared = await host.handle(
        Method.AUIP_ATTACH_PREPARE,
        {"artifact_id": source.artifacts["artifact-auip-reactor"].artifact_id},
    )
    assert prepared and prepared.get("ok") is True
    page = await _new_page(browser, errors)
    await page.goto(str(prepared["launch_url"]), wait_until="load")
    await page.wait_for_function("window.__auipReactor && window.__auipReactor.isAttached()")
    projection = await _ready_projection(runtime, conversation_id, "simulation.ready")
    app_session_id = str(projection["app_session_id"])
    stance = await host.handle(
        Method.AUIP_STANCE_SET,
        {"app_session_id": app_session_id, "stance": "participant"},
    )
    assert stance and stance.get("stance") == "participant"

    warning = await _ready_projection(runtime, conversation_id, "reactor.heat_warning")
    warning_heat = _scalar_value(warning["state"], "heat")
    assert warning_heat is not None and warning_heat >= 70

    async def synced_revision() -> int:
        for _ in range(100):
            await page.evaluate("window.__auipReactor.settled()")
            local = int(await page.evaluate("window.__auipReactor.revision()"))
            host_revision = int(runtime.get(app_session_id)["revision"])
            if local == host_revision:
                return local
            await asyncio.sleep(0.02)
        raise AssertionError("Reactor local and host revisions did not converge")

    await synced_revision()
    resolver = AuipControlDecisionResolver(
        query=lambda messages: query_auip_control_model(messages, model=model),
        app_runtime=runtime,
        launch_catalog=EmptyAuipLaunchCatalog(),
        has_active_work=None,
    )
    driver = NaturalAuipControlDriver(
        conversation_id=conversation_id,
        resolver=resolver,
        route=host.route_control,
    )
    next_actions[app_session_id] = ("reactor.set_cooling", {"level": 2})
    calls_before_step = len(participant_calls)
    natural_step = await driver.turn(
        "你能先把冷却调到二档吗", turn_id="diverse-reactor-step"
    )
    assert natural_step.status == "ok" and natural_step.action == "step", natural_step
    await engagement.wait_for_idle(app_session_id)

    def natural_reactor_receipt() -> dict[str, Any] | None:
        snapshot = runtime.get(app_session_id)
        latest = snapshot.get("latest_verified_self_action")
        if not isinstance(latest, dict):
            return None
        if latest.get("type") != "reactor.set_cooling":
            return None
        return snapshot if latest.get("payload") == {"level": 2} else None

    natural_receipt = await _wait_for(
        natural_reactor_receipt,
        description="reactor natural step receipt",
    )
    assert len(participant_calls) == calls_before_step + 1
    query = await driver.turn("冷却调好了吗？", turn_id="diverse-reactor-status")
    assert query.status == "ok" and query.action == "none", query
    await asyncio.sleep(0.05)
    assert len(participant_calls) == calls_before_step + 1
    assert runtime.get(app_session_id)["latest_verified_self_action"] == natural_receipt[
        "latest_verified_self_action"
    ]

    terminal = await _wait_for(
        lambda: runtime.get(app_session_id)
        if runtime.get(app_session_id).get("status") == "completed"
        else None,
        description="reactor stabilization",
        timeout_s=10.0,
    )
    await page.evaluate("window.__auipReactor.settled()")
    ticks = int(await page.evaluate("window.__auipReactor.tickCount()"))
    semantic_events = int(await page.evaluate("window.__auipReactor.semanticEventCount()"))
    assert ticks >= semantic_events * 3
    assert terminal["experience_capsule"]["terminal"]["type"] == "reactor.stabilized"
    assert terminal["state"]["status"] == "stabilized"
    assert len(terminal["experience_capsule"]["verified_self_actions"]) == 1
    await page.close()

    assert not errors["console"]
    assert not errors["page"]
    return {
        "standalone_ticks": standalone_ticks,
        "standalone_heat": standalone_heat,
        "attached_ticks": ticks,
        "semantic_events": semantic_events,
        "final_revision": terminal["revision"],
        "final_heat": _scalar_value(terminal["state"], "heat"),
        "terminal_event": terminal["experience_capsule"]["terminal"]["type"],
        "kurisu_actions": len(terminal["experience_capsule"]["verified_self_actions"]),
        "console_errors": errors["console"],
        "page_errors": errors["page"],
        "natural_step": {
            "decided": natural_step.action,
            "receipt_type": "reactor.set_cooling",
            "status_decided": query.action,
        },
    }


async def run_journey(*, model: str = "deepseek-v4-flash") -> dict[str, Any]:
    from playwright.async_api import async_playwright

    for name, entry in SAMPLES.items():
        if not entry.is_file():
            raise AssertionError(f"Missing AUIP {name} entry: {entry}")

    runtime = AuipRuntime()
    manager = AuipAppConnectionManager(runtime)
    source = _ArtifactSource()
    conversation = {"id": ""}
    next_actions: dict[str, tuple[str, dict[str, Any]]] = {}
    participant_calls: list[dict[str, Any]] = []

    async def participant(context: dict[str, Any]) -> dict[str, Any]:
        participant_calls.append(context)
        app_session_id = str(context.get("app_session_id") or "")
        action_type, payload = next_actions[app_session_id]
        return {
            "action": "act",
            "type": action_type,
            "payload": dict(payload),
            "private_note": "deterministic diverse-game acceptance action",
        }

    engagement = AuipEngagementCoordinator(
        app_runtime=runtime,
        controller=participant,
        role_authorizer=lambda _context: {
            "decision": "approve",
            "reason": "deterministic E2E policy",
        },
        controller_id="e2e-diverse-natural-participant",
    )
    engagement_callback = engagement.on_update
    bus.on(Method.AUIP_UPDATED, engagement_callback)
    listener = _listening_socket()
    port = int(listener.getsockname()[1])
    endpoint = f"ws://127.0.0.1:{port}/auip/ws"
    app = FastAPI()

    @app.websocket("/auip/ws")
    async def auip_endpoint(websocket: WebSocket) -> None:
        await manager.handle_connection(websocket)

    host = AuipHandler(
        runtime,
        artifacts=source,
        current_session_id=lambda: conversation["id"],
        app_websocket_url=endpoint,
        engagement=engagement,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await _wait_for(
            lambda: server.started or (server_task.done() and "exited"),
            description="AUIP diverse-game server startup",
        )
        if server_task.done() or not server.started:
            raise RuntimeError("AUIP diverse-game server exited during startup")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                def set_conversation(value: str) -> None:
                    conversation["id"] = value

                game_2048 = await _run_2048(
                    browser,
                    host,
                    runtime,
                    source,
                    set_conversation,
                    model=model,
                    engagement=engagement,
                    next_actions=next_actions,
                    participant_calls=participant_calls,
                )
                reactor = await _run_reactor(
                    browser,
                    host,
                    runtime,
                    source,
                    set_conversation,
                    model=model,
                    engagement=engagement,
                    next_actions=next_actions,
                    participant_calls=participant_calls,
                )
            finally:
                await browser.close()
        return {
            "ok": True,
            "browser": "chromium-headless",
            "transport": "real_websocket",
            "samples": {"2048": game_2048, "reactor": reactor},
            "model": model,
            "natural_control": True,
        }
    finally:
        bus.off(Method.AUIP_UPDATED, engagement_callback)
        await engagement.close()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5.0)
        listener.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--model", default="deepseek-v4-flash")
    args = parser.parse_args()
    report = asyncio.run(run_journey(model=str(args.model)))
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
