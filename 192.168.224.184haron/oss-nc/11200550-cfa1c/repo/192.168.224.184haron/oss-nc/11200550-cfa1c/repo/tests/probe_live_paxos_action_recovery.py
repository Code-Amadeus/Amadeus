"""Opt-in live-server acceptance for a human-short Paxos research request."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid

from tools.e2e_real_work_conversation import WsProbe


async def run(port: int) -> dict:
    session_id = f"codex-paxos-{uuid.uuid4().hex[:8]}"
    turn_id = f"paxos-{uuid.uuid4().hex[:8]}"
    async with WsProbe(f"ws://127.0.0.1:{port}/ws") as probe:
        await probe.request(
            "session.create",
            {"session_id": session_id, "title": "Paxos 短句验收"},
        )
        start = time.monotonic()
        await probe.request(
            "chat.send",
            {
                "text": "帮我查一下 Paxos 的经典论文。",
                "provider": "deepseek",
                "session_id": session_id,
                "turn_id": turn_id,
                "source": "codex_live_acceptance",
            },
        )
        complete = await probe.wait_event(
            lambda event: event.method == "chat.complete"
            and event.params.get("turn_id") == turn_id,
            timeout=120.0,
            description="short Paxos role turn",
        )
        created = await probe.wait_event(
            lambda event: event.method == "provider.event"
            and event.params.get("type") == "run.created",
            timeout=90.0,
            description="Provider run from short Paxos request",
        )
        run_id = str(created.params.get("run_id") or "")
        work_item_id = str(
            (created.params.get("metadata") or {}).get("work", {}).get("work_item_id")
            or created.params.get("task_id")
            or ""
        )
        work = await probe.request("work.list", {})
        terminal = None
        try:
            terminal = await probe.wait_event(
                lambda event: event.method == "provider.result"
                and event.params.get("run_id") == run_id,
                timeout=150.0,
                description="Paxos Provider terminal",
            )
        except TimeoutError:
            await probe.request("provider.cancel", {"run_id": run_id})
        event_types = [
            str(event.params.get("type") or "")
            for event in probe.state.events
            if event.method == "provider.event"
            and event.params.get("run_id") == run_id
        ]
        return {
            "ok": bool(run_id and work_item_id),
            "session_id": session_id,
            "turn_id": turn_id,
            "role_elapsed_s": round(complete.elapsed_s, 3),
            "run_created_elapsed_s": round(created.elapsed_s, 3),
            "settlement_elapsed_s": round(time.monotonic() - start, 3),
            "provider": str(created.params.get("provider") or ""),
            "run_id": run_id,
            "work_item_id": work_item_id,
            "work_items_visible": len((work.get("work") or {}).get("items") or []),
            "terminal_status": (
                str(terminal.params.get("status") or "") if terminal is not None else "cancelled_after_timeout"
            ),
            "provider_event_types": event_types,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=17777)
    args = parser.parse_args()
    result = asyncio.run(run(args.port))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
