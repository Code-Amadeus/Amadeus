r"""Probe real OpenClaw session continuity, steering, and cancellation safely.

The probe creates only isolated ``amadeus-probe-*`` sessions, performs no file
writes, visits only ``https://example.com``, and deletes its own sessions on
exit.  It measures upstream capability; it does not change Amadeus manifests.

Run with the CUDA 12.4 environment used by the product::

    .venv\Scripts\python.exe -X utf8 \
        tools/probes/probe_openclaw_session_control.py --live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import OPENCLAW_BASE_URL, OPENCLAW_TOKEN
from openclaw.client import ask_openclaw_stream
from openclaw.gateway_client import OpenClawGatewayClient, OpenClawGatewayError


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content", "delta"):
                    text = item.get(key)
                    if isinstance(text, str):
                        parts.append(text)
                        break
        return "\n".join(parts)
    if isinstance(value, dict):
        for key in ("text", "content", "delta"):
            text = _content_text(value.get(key))
            if text:
                return text
    return ""


def _history_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    rows: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        text = _content_text(message.get("content") or message.get("message") or message)
        if text:
            rows.append(f"{role}: {text}")
    return "\n".join(rows)


def _assistant_history_text(payload: Any) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return ""
    rows: list[str] = []
    for message in payload["messages"]:
        if not isinstance(message, dict) or str(message.get("role") or "") != "assistant":
            continue
        text = _content_text(message.get("content") or message.get("message") or message)
        if text:
            rows.append(text)
    return "\n".join(rows)


def _target_ids(value: Any) -> tuple[str, ...]:
    encoded = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return tuple(sorted(set(re.findall(r'"targetId"\s*:\s*"([A-Za-z0-9_-]+)"', encoded))))


async def _wait_run(
    client: OpenClawGatewayClient,
    run_id: str,
    *,
    timeout_s: float = 90.0,
) -> dict[str, Any]:
    payload = await client.request(
        "agent.wait",
        {"runId": run_id, "timeoutMs": int(timeout_s * 1000)},
        timeout=timeout_s + 5.0,
    )
    return dict(payload) if isinstance(payload, dict) else {"payload": payload}


async def _wait_session_idle(
    client: OpenClawGatewayClient,
    key: str,
    *,
    timeout_s: float = 20.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        listing = await client.request("sessions.list", {"limit": 100}, timeout=10.0)
        sessions = listing.get("sessions") if isinstance(listing, dict) else []
        for row in sessions if isinstance(sessions, list) else []:
            if isinstance(row, dict) and str(row.get("key") or "") == key:
                last = dict(row)
                if str(row.get("status") or "").lower() not in {"running", "queued"}:
                    return last
                break
        await asyncio.sleep(0.25)
    return last


async def _wait_for_browser_fact(
    client: OpenClawGatewayClient,
    run_id: str,
    *,
    timeout_s: float = 15.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_tool: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            frame = await client.next_event(timeout=min(1.0, deadline - time.monotonic()))
        except OpenClawGatewayError as exc:
            if exc.code == "EVENT_TIMEOUT":
                continue
            raise
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        if str(frame.get("event") or "") != "agent" or str(payload.get("runId") or "") != run_id:
            continue
        if str(payload.get("stream") or "") != "tool":
            continue
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        last_tool = dict(data)
        encoded = json.dumps(data, ensure_ascii=False).lower()
        if "browser" in encoded and (
            "example.com" in encoded
            or str(data.get("phase") or "").lower() in {"result", "end"}
        ):
            return last_tool
    return last_tool


async def _delete_probe_session(
    client: OpenClawGatewayClient,
    key: str,
) -> dict[str, Any]:
    if "amadeus-probe-" not in key:
        raise RuntimeError(f"refusing to delete a non-probe session: {key}")
    payload = await client.request(
        "sessions.delete",
        {"key": key, "deleteTranscript": True},
        timeout=15.0,
    )
    return dict(payload) if isinstance(payload, dict) else {"payload": payload}


async def run_live_probe(*, keep_sessions: bool = False) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:10]
    continuity_key = f"agent:main:dashboard:amadeus-probe-continuity-{suffix}"
    composite_key = f"agent:main:dashboard:amadeus-probe-composite-{suffix}"
    abort_key = f"agent:main:dashboard:amadeus-probe-abort-{suffix}"
    http_key = f"agent:main:openai:amadeus-probe-http-{suffix}"
    created_keys: list[str] = []
    result: dict[str, Any] = {
        "probe_id": suffix,
        "gateway": {},
        "active_steer": {},
        "completed_followup": {},
        "late_steer_fence": {},
        "safe_composite_steer": {},
        "confirmed_abort": {},
        "http_session_control": {},
        "cleanup": [],
    }

    async with OpenClawGatewayClient(
        base_url=OPENCLAW_BASE_URL,
        token=OPENCLAW_TOKEN,
        request_timeout=20.0,
        # OpenClaw 2026.3.14 does not list sessions.steer in its write-scope
        # table, so the native-method probe needs admin.  Production control
        # uses exact sessions.abort + sessions.send and keeps write scope.
        scopes=("operator.read", "operator.write", "operator.admin"),
    ) as client:
        result["gateway"] = {
            "protocol": client.hello.get("protocol"),
            "version": (
                client.hello.get("server", {}).get("version")
                if isinstance(client.hello.get("server"), dict)
                else ""
            ),
            "methods": sorted(
                method
                for method in client.advertised_methods
                if method in {
                    "sessions.create",
                    "sessions.send",
                    "sessions.steer",
                    "sessions.abort",
                    "sessions.delete",
                    "agent.wait",
                    "chat.history",
                }
            ),
        }
        try:
            initial = await client.request(
                "sessions.create",
                {
                    "key": continuity_key,
                    "agentId": "main",
                    "label": f"amadeus-probe-{suffix}",
                    "message": (
                        "This is an isolated Amadeus control-plane test. Use the OpenClaw-managed "
                        "browser to open https://example.com and inspect its title. Then run one "
                        "harmless wait of about 25 seconds before replying ORIGINAL_FINISHED. "
                        "Do not visit another URL, change files, send messages, or perform any other "
                        "external action."
                    ),
                },
                timeout=20.0,
            )
            created_keys.append(continuity_key)
            initial = dict(initial) if isinstance(initial, dict) else {}
            initial_run = str(initial.get("runId") or "")
            if not initial_run:
                raise RuntimeError(f"sessions.create returned no runId: {initial}")
            browser_fact = await _wait_for_browser_fact(client, initial_run)

            steered = await client.request(
                "sessions.steer",
                {
                    "key": continuity_key,
                    "message": (
                        "Stop the wait and replace the remaining plan. Reuse the existing example.com "
                        "browser tab without opening another tab. Inspect it again and reply with "
                        "STEER_OK, the current URL, and the page title. Do nothing else."
                    ),
                    "idempotencyKey": f"amadeus-probe-steer-{suffix}",
                    "timeoutMs": 45_000,
                },
                timeout=20.0,
            )
            steered = dict(steered) if isinstance(steered, dict) else {}
            steer_run = str(steered.get("runId") or "")
            steer_wait = await _wait_run(client, steer_run) if steer_run else {}
            history = await client.request(
                "chat.history",
                {"sessionKey": continuity_key, "limit": 40},
                timeout=15.0,
            )
            transcript = _history_text(history)
            assistant_transcript = _assistant_history_text(history)
            initial_target_ids = _target_ids(browser_fact)
            active_history_target_ids = _target_ids(transcript)
            result["active_steer"] = {
                "initial_run_id": initial_run,
                "steer_run_id": steer_run,
                "interrupted_active_run": steered.get("interruptedActiveRun") is True,
                "browser_fact_observed_before_steer": bool(browser_fact),
                "terminal": steer_wait.get("status"),
                "steer_marker_observed": "STEER_OK" in assistant_transcript,
                "old_terminal_marker_absent": "ORIGINAL_FINISHED" not in assistant_transcript,
                "initial_target_ids": list(initial_target_ids),
                "history_target_ids": list(active_history_target_ids),
            }

            followup = await client.request(
                "sessions.send",
                {
                    "key": continuity_key,
                    "message": (
                        "Without opening a new tab, inspect the browser state left by the previous "
                        "turn. Reply with FOLLOWUP_OK, whether the example.com tab still exists, its "
                        "URL, and title. Do nothing else."
                    ),
                    "idempotencyKey": f"amadeus-probe-followup-{suffix}",
                    "timeoutMs": 45_000,
                },
                timeout=20.0,
            )
            followup = dict(followup) if isinstance(followup, dict) else {}
            followup_run = str(followup.get("runId") or "")
            followup_wait = await _wait_run(client, followup_run) if followup_run else {}
            history = await client.request(
                "chat.history",
                {"sessionKey": continuity_key, "limit": 60},
                timeout=15.0,
            )
            transcript = _history_text(history)
            assistant_transcript = _assistant_history_text(history)
            followup_target_ids = _target_ids(transcript)
            result["completed_followup"] = {
                "run_id": followup_run,
                "terminal": followup_wait.get("status"),
                "followup_marker_observed": "FOLLOWUP_OK" in assistant_transcript,
                "page_reference_observed": "example.com" in transcript.lower(),
                "same_browser_target_observed": bool(
                    set(active_history_target_ids) & set(followup_target_ids)
                ),
            }

            idle = await _wait_session_idle(client, continuity_key)

            # This is intentionally sent only after the prior run is terminal.
            # On a fenced implementation it must reject.  OpenClaw 2026.3.14
            # instead starts a successor, which is the race Amadeus must not
            # expose as an exact-run steering contract.
            late = await client.request(
                "sessions.steer",
                {
                    "key": continuity_key,
                    "message": "Reply exactly LATE_STEER_STARTED. Do not use tools.",
                    "idempotencyKey": f"amadeus-probe-late-{suffix}",
                    "timeoutMs": 30_000,
                },
                timeout=20.0,
            )
            late = dict(late) if isinstance(late, dict) else {}
            late_run = str(late.get("runId") or "")
            late_wait = await _wait_run(client, late_run, timeout_s=60.0) if late_run else {}
            result["late_steer_fence"] = {
                "rejected": not bool(late_run),
                "started_successor": bool(late_run),
                "interrupted_active_run": late.get("interruptedActiveRun") is True,
                "precondition_session_status": str(idle.get("status") or ""),
                "terminal": late_wait.get("status"),
                "production_exact_run_safe": False if late_run else True,
            }

            composite_created = await client.request(
                "sessions.create",
                {
                    "key": composite_key,
                    "agentId": "main",
                    "label": f"amadeus-probe-composite-{suffix}",
                    "message": (
                        "This is an isolated steering test. Run one harmless command that waits "
                        "for about 30 seconds, then reply COMPOSITE_OLD_FINISHED. Do not browse, "
                        "write files, or perform any external action."
                    ),
                },
                timeout=20.0,
            )
            created_keys.append(composite_key)
            composite_created = (
                dict(composite_created) if isinstance(composite_created, dict) else {}
            )
            composite_old_run = str(composite_created.get("runId") or "")
            await asyncio.sleep(2.0)
            composite_abort = await client.request(
                "sessions.abort",
                {"key": composite_key, "runId": composite_old_run},
                timeout=20.0,
            )
            composite_abort = dict(composite_abort) if isinstance(composite_abort, dict) else {}
            composite_send = await client.request(
                "sessions.send",
                {
                    "key": composite_key,
                    "message": "Replace the old plan and reply exactly COMPOSITE_STEER_OK. Do not use tools.",
                    "idempotencyKey": f"amadeus-probe-composite-new-{suffix}",
                    "timeoutMs": 30_000,
                },
                timeout=20.0,
            )
            composite_send = dict(composite_send) if isinstance(composite_send, dict) else {}
            composite_new_run = str(composite_send.get("runId") or "")
            composite_wait = (
                await _wait_run(client, composite_new_run, timeout_s=60.0)
                if composite_new_run
                else {}
            )
            composite_history = await client.request(
                "chat.history",
                {"sessionKey": composite_key, "limit": 30},
                timeout=15.0,
            )
            composite_assistant = _assistant_history_text(composite_history)
            result["safe_composite_steer"] = {
                "old_run_id": composite_old_run,
                "new_run_id": composite_new_run,
                "abort_matched_old_run": (
                    str(composite_abort.get("abortedRunId") or "") == composite_old_run
                ),
                "terminal": composite_wait.get("status"),
                "new_marker_observed": "COMPOSITE_STEER_OK" in composite_assistant,
                "old_terminal_marker_absent": (
                    "COMPOSITE_OLD_FINISHED" not in composite_assistant
                ),
                "write_scope_methods": ["sessions.abort", "sessions.send"],
            }

            abort_created = await client.request(
                "sessions.create",
                {
                    "key": abort_key,
                    "agentId": "main",
                    "label": f"amadeus-probe-abort-{suffix}",
                    "message": (
                        "This is an isolated cancellation test. Run one harmless command that waits "
                        "for about 30 seconds, then reply ABORT_FAILED. Do not browse, write files, or "
                        "perform any external action."
                    ),
                },
                timeout=20.0,
            )
            created_keys.append(abort_key)
            abort_created = dict(abort_created) if isinstance(abort_created, dict) else {}
            abort_run = str(abort_created.get("runId") or "")
            await asyncio.sleep(2.0)
            abort_payload = await client.request(
                "sessions.abort",
                {"key": abort_key, "runId": abort_run},
                timeout=20.0,
            )
            abort_payload = dict(abort_payload) if isinstance(abort_payload, dict) else {}
            abort_wait = await _wait_run(client, abort_run, timeout_s=30.0) if abort_run else {}
            abort_history = await client.request(
                "chat.history",
                {"sessionKey": abort_key, "limit": 20},
                timeout=15.0,
            )
            abort_transcript = _assistant_history_text(abort_history)
            result["confirmed_abort"] = {
                "run_id": abort_run,
                "aborted_run_id": str(abort_payload.get("abortedRunId") or ""),
                "status": abort_payload.get("status"),
                "terminal": abort_wait.get("status"),
                "post_abort_marker_absent": "ABORT_FAILED" not in abort_transcript,
                "confirmed": (
                    bool(abort_run)
                    and str(abort_payload.get("abortedRunId") or "") == abort_run
                    and abort_payload.get("status") == "aborted"
                ),
            }

            # Negative regression guard: the OpenAI-compatible SSE endpoint
            # can reuse a Session key, but its request lifecycle is not owned
            # by Gateway chat.abort in this local release. Mixing HTTP work
            # with Gateway cancellation must therefore remain rejected even
            # though a completed follow-up appears to remember the page.
            created_keys.append(http_key)
            http_started = asyncio.Event()
            http_native: dict[str, str] = {}

            def http_run_started(run_id: str) -> None:
                http_native["old"] = str(run_id or "")
                http_started.set()

            old_http_task = asyncio.create_task(
                ask_openclaw_stream(
                    (
                        "This is an isolated Amadeus HTTP session-control test. Use the "
                        "OpenClaw-managed browser to open https://example.com and inspect its "
                        "title. Then run one harmless wait of about 25 seconds before replying "
                        "HTTP_OLD_FINISHED. Do not visit another URL, write files, send messages, "
                        "or perform any other external action."
                    ),
                    run_started_callback=http_run_started,
                    timeout=75.0,
                    session_key=http_key,
                ),
                name=f"openclaw-probe-http-old:{suffix}",
            )
            await asyncio.wait_for(http_started.wait(), timeout=20.0)
            old_http_run = http_native.get("old", "")
            http_browser_fact = await _wait_for_browser_fact(
                client,
                old_http_run,
                timeout_s=20.0,
            )
            http_abort = await client.request(
                "sessions.abort",
                {"key": http_key, "runId": old_http_run},
                timeout=20.0,
            )
            http_abort = dict(http_abort) if isinstance(http_abort, dict) else {}
            try:
                await asyncio.wait_for(old_http_task, timeout=8.0)
            except asyncio.TimeoutError:
                old_http_task.cancel()
                try:
                    await old_http_task
                except asyncio.CancelledError:
                    pass

            def http_followup_started(run_id: str) -> None:
                http_native["new"] = str(run_id or "")

            http_followup = await ask_openclaw_stream(
                (
                    "Continue in this exact session. Without opening a new tab, inspect the "
                    "browser state left by the previous instruction. Reply with HTTP_SESSION_OK, "
                    "whether the example.com tab still exists, its URL, and title. Do nothing else."
                ),
                run_started_callback=http_followup_started,
                timeout=75.0,
                session_key=http_key,
            )
            http_history = await client.request(
                "chat.history",
                {"sessionKey": http_key, "limit": 40},
                timeout=15.0,
            )
            http_transcript = _history_text(http_history)
            http_target_ids = _target_ids(http_transcript)
            result["http_session_control"] = {
                "old_run_id": old_http_run,
                "new_run_id": http_native.get("new", ""),
                "abort_matched_old_run": (
                    str(http_abort.get("abortedRunId") or "") == old_http_run
                ),
                "browser_fact_observed_before_abort": bool(http_browser_fact),
                "followup_marker_observed": "HTTP_SESSION_OK" in http_followup,
                "page_reference_observed": "example.com" in http_followup.lower(),
                "same_browser_target_observed": bool(
                    set(_target_ids(http_browser_fact)) & set(http_target_ids)
                ),
                "old_terminal_marker_absent": (
                    "HTTP_OLD_FINISHED" not in _assistant_history_text(http_history)
                ),
                "production_transport_candidate": False,
            }
        finally:
            if not keep_sessions:
                for key in reversed(created_keys):
                    try:
                        result["cleanup"].append(await _delete_probe_session(client, key))
                    except Exception as exc:
                        result["cleanup"].append({"key": key, "error": str(exc)})

    result["production_manifest_ready"] = bool(
        result["safe_composite_steer"].get("abort_matched_old_run")
        and result["safe_composite_steer"].get("new_marker_observed")
        and result["safe_composite_steer"].get("old_terminal_marker_absent")
        and result["completed_followup"].get("followup_marker_observed")
        and result["confirmed_abort"].get("confirmed")
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="create isolated sessions and call real OpenClaw tools",
    )
    parser.add_argument(
        "--keep-sessions",
        action="store_true",
        help="retain only the probe-created sessions for manual inspection",
    )
    args = parser.parse_args()
    if not args.live:
        print(
            json.dumps(
                {
                    "live": False,
                    "gateway": OPENCLAW_BASE_URL,
                    "message": "Pass --live to run isolated session-control probes.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    result = asyncio.run(run_live_probe(keep_sessions=args.keep_sessions))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    required = (
        result["safe_composite_steer"].get("abort_matched_old_run")
        and result["safe_composite_steer"].get("new_marker_observed")
        and result["completed_followup"].get("followup_marker_observed")
        and result["confirmed_abort"].get("confirmed")
    )
    return 0 if required else 1


if __name__ == "__main__":
    raise SystemExit(main())
