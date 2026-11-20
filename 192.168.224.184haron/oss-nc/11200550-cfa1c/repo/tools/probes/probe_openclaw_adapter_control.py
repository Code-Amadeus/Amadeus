r"""Exercise the production OpenClaw adapter against an isolated real Session.

The probe uses the same write-scoped Gateway transport as Amadeus, visits only
``https://example.com``, steers by exact run id, and archives only the Session
whose opaque id was returned by this adapter invocation.

Run::

    .venv\Scripts\python.exe -X utf8 \
        tools/probes/probe_openclaw_adapter_control.py --live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.adapters.openclaw import OpenClawAdapter
from agent_host.provider_types import (
    ProviderEvent,
    ProviderRunRequest,
    ProviderSteerRequest,
)
from config.settings import OPENCLAW_BASE_URL, OPENCLAW_TOKEN
from openclaw.gateway_client import OpenClawGatewayClient


async def run_live_probe() -> dict[str, Any]:
    adapter = OpenClawAdapter()
    events: list[dict[str, Any]] = []

    async def emit(event: ProviderEvent) -> None:
        events.append(event.to_dict())

    adapter_run_id = "openclaw_adapter_probe"
    run_task = asyncio.create_task(
        adapter.run(
            ProviderRunRequest(
                provider="openclaw",
                task=(
                    "This is an isolated Amadeus adapter test. Use the OpenClaw-managed "
                    "browser to open https://example.com and inspect its title. Then run one "
                    "harmless wait of about 30 seconds before replying ADAPTER_OLD_FINISHED. "
                    "Do not visit another URL, write files, send messages, or perform any "
                    "other external action."
                ),
                metadata={
                    "timeout": 90.0,
                    "work": {"work_item_id": "amadeus-probe-work-item"},
                },
            ),
            adapter_run_id,
            emit,
        ),
        name="openclaw-adapter-live-probe",
    )

    session_key = ""
    result: dict[str, Any] = {
        "transport": "gateway_session",
        "write_scoped": True,
        "steer": {},
        "result": {},
        "completed_followup": {},
        "events": {},
        "cleanup": {},
    }
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            control = adapter._controls.get(adapter_run_id)
            if control is not None:
                session_key = control.session.session_id
                if control.native_ready.is_set():
                    break
            if run_task.done():
                raise RuntimeError("initial adapter turn ended before the steer boundary")
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("adapter did not expose a native run id in time")

        # Give the browser invocation a short opportunity to establish page
        # state. The 30-second harmless wait keeps the run active and makes the
        # exact cancellation fence observable.
        await asyncio.sleep(4.0)
        steer = await adapter.steer(
            adapter_run_id,
            ProviderSteerRequest(
                task=(
                    "Stop the wait. Reuse the existing example.com browser tab without opening "
                    "another tab. Inspect it and reply with ADAPTER_STEER_OK, its URL, and title. "
                    "Do nothing else."
                ),
                revision=1,
            ),
        )
        result["steer"] = dict(steer)
        provider_result = await asyncio.wait_for(run_task, timeout=100.0)
        result["result"] = provider_result.to_dict()
        followup_events: list[dict[str, Any]] = []

        async def emit_followup(event: ProviderEvent) -> None:
            followup_events.append(event.to_dict())

        followup_result = await asyncio.wait_for(
            adapter.run(
                ProviderRunRequest(
                    provider="openclaw",
                    task=(
                        "Continue from the exact existing browser state. Without opening a new "
                        "tab, inspect the current page and reply with ADAPTER_FOLLOWUP_OK, its "
                        "URL, and title. Do nothing else."
                    ),
                    metadata={
                        "timeout": 90.0,
                        "work": {"work_item_id": "amadeus-probe-work-item"},
                    },
                    session=provider_result.session,
                ),
                "openclaw_adapter_probe_followup",
                emit_followup,
            ),
            timeout=100.0,
        )
        result["completed_followup"] = followup_result.to_dict()
        tool_events = [event for event in events if event.get("type") == "tool.call"]
        followup_tool_events = [
            event for event in followup_events if event.get("type") == "tool.call"
        ]
        result["events"] = {
            "count": len(events),
            "tool_count": len(tool_events),
            "semantic_progress_count": sum(
                1 for event in events if event.get("type") == "semantic.progress"
            ),
            "followup_count": len(followup_events),
            "followup_tool_count": len(followup_tool_events),
        }
        native_run_ids = list(provider_result.metadata.get("native_run_ids") or [])
        followup_run_ids = list(
            followup_result.metadata.get("native_run_ids") or []
        )
        checks = {
            "steer_accepted": steer.get("accepted") is True,
            "confirmed_abort_boundary": (
                steer.get("safe_boundary") == "confirmed_abort_then_same_session"
            ),
            "old_run_identity_confirmed": bool(native_run_ids)
            and steer.get("native_run_id") == native_run_ids[0],
            "two_distinct_native_runs": len(native_run_ids) == 2
            and len(set(native_run_ids)) == 2,
            "steered_run_completed": provider_result.status == "done",
            "typed_session_returned": provider_result.session is not None,
            "same_session_after_steer": provider_result.session is not None
            and provider_result.session.session_id == session_key,
            "old_terminal_suppressed": (
                "ADAPTER_OLD_FINISHED" not in provider_result.result
            ),
            "steered_page_fact_returned": (
                "example.com" in provider_result.result.lower()
            ),
            "browser_tool_observed": bool(tool_events),
            "completed_followup_done": followup_result.status == "done",
            "completed_followup_same_session": (
                followup_result.session == provider_result.session
            ),
            "completed_followup_attached": (
                followup_result.metadata.get("session_attached") is True
            ),
            "completed_followup_has_one_native_run": len(followup_run_ids) == 1,
            "completed_followup_page_fact_returned": (
                "example.com" in followup_result.result.lower()
            ),
            "completed_followup_browser_tool_observed": bool(
                followup_tool_events
            ),
        }
        result["acceptance_checks"] = checks
        # Exact marker echo is useful diagnostic evidence, but it is not a
        # control-plane fact: a provider may make a one-character spelling
        # error while exact abort, successor identity, Session attachment and
        # observed browser work all remain valid.
        result["model_echo_diagnostics"] = {
            "steer_marker_exact": "ADAPTER_STEER_OK" in provider_result.result,
            "followup_marker_exact": (
                "ADAPTER_FOLLOWUP_OK" in followup_result.result
            ),
        }
        result["accepted"] = all(checks.values())
    finally:
        if not run_task.done():
            try:
                await adapter.cancel(adapter_run_id)
            except Exception:
                pass
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):
                pass
        if session_key:
            async with OpenClawGatewayClient(
                base_url=OPENCLAW_BASE_URL,
                token=OPENCLAW_TOKEN,
                scopes=("operator.read", "operator.write", "operator.admin"),
            ) as cleanup:
                if "amadeus-" not in session_key:
                    raise RuntimeError(
                        f"refusing to delete a non-Amadeus Session: {session_key}"
                    )
                payload = await cleanup.request(
                    "sessions.delete",
                    {"key": session_key, "deleteTranscript": True},
                )
                result["cleanup"] = (
                    dict(payload) if isinstance(payload, dict) else {"payload": payload}
                )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"live": False, "message": "Pass --live to run."}, indent=2))
        return 0
    result = asyncio.run(run_live_probe())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("accepted") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
