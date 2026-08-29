"""Canonical J7 AUIP experience journey.

This is one continuous AppSession, not a roll-up of component probes. It uses
the shipped Gomoku application, real Chromium and WebSocket transport, the
production source-local AUIP decision model, typed Participant actions,
accepted receipts, a terminal event, and the bounded main-Chat capsule.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.e2e_auip_diverse_games import SAMPLES, run_journey as run_diverse_journey
from tools.e2e_auip_gomoku import ENTRY, MANIFEST, run_journey as run_gomoku_journey
from tools.semantic_journey_evidence import build_evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument(
        "--report-dir",
        default=str(ROOT / "runtime" / "e2e_reports" / "semantic_journeys" / "J7"),
    )
    return parser


def _checks(result: dict[str, Any]) -> list[dict[str, Any]]:
    routed = result.get("routed_controls")
    diverse = result.get("diverse_games")
    samples = diverse.get("samples") if isinstance(diverse, dict) else None
    game_2048 = samples.get("2048") if isinstance(samples, dict) else None
    reactor = samples.get("reactor") if isinstance(samples, dict) else None
    narration_trace = result.get("narration_trace")
    narrations = narration_trace if isinstance(narration_trace, list) else []
    intermediate_narrations = [
        item
        for item in narrations
        if isinstance(item, dict)
        and item.get("source") == "auip_narrator"
        and item.get("terminal") is not True
    ]
    terminal_narrations = [
        item
        for item in narrations
        if isinstance(item, dict)
        and item.get("source") == "auip_narrator"
        and item.get("terminal") is True
    ]
    return [
        {
            "name": "real-chromium-websocket-session",
            "ok": result.get("browser") == "chromium-headless"
            and result.get("transport") == "real_websocket",
        },
        {
            "name": "natural-control-routes-on-one-app-session",
            "ok": result.get("identity_preserved") is True
            and isinstance(routed, list)
            and [item.get("decided") for item in routed]
            == ["observe", "collaborate", "step", "none", "step"],
        },
        {
            "name": "natural-step-crosses-participant-app-and-receipt",
            "ok": isinstance(result.get("natural_step"), dict)
            and result["natural_step"].get("decided") == "step"
            and result["natural_step"].get("receipt_type")
            == "game.take_first_move"
            and result["natural_step"].get("participant_role") == "black"
            and result["natural_step"].get("status_decided") == "none"
            and result["natural_step"].get("status_preserved_revision") is True,
        },
        {
            "name": "negotiated-step-binds-agreement-proposal-and-receipt",
            "ok": isinstance(result.get("negotiated_step"), dict)
            and result["negotiated_step"].get("decided") == "step"
            and result["negotiated_step"].get("agreed_instruction")
            == "Place one stone at x=5, y=4."
            and result["negotiated_step"].get("participant_instruction")
            == result["negotiated_step"].get("agreed_instruction")
            and result["negotiated_step"].get("receipt_type")
            == "game.place_stone"
            and result["negotiated_step"].get("receipt_payload")
            == {"x": 5, "y": 4},
        },
        {
            "name": "polite-proposal-respects-visible-role-alternative",
            "ok": result.get("role_authorizer_transport")
            == "real_explicit_turns+deterministic_fixture"
            # The polite proposal opens one candidate, but the role visibly
            # chooses to leave the current binding unchanged. Participant/gate
            # may inspect that choice; no application action may occur.
            and int(result.get("real_role_authorization_count") or 0) >= 1
            and isinstance(result.get("role_consensus"), dict)
            and result["role_consensus"].get("decided") == "step"
            and result["role_consensus"].get("participant_was_consulted") is True
            and result["role_consensus"].get("action_suppressed") is True,
        },
        {
            "name": "real-observer-narrator-delivers-an-intermediate-comment",
            "ok": bool(intermediate_narrations)
            and all(item.get("display_text") for item in intermediate_narrations)
            and all(item.get("voice_text_matches") is True for item in narrations),
        },
        {
            "name": "terminal-summary-is-grounded-delivered-and-retained",
            "ok": bool(terminal_narrations)
            and "verified terminal outcome"
            in str(terminal_narrations[-1].get("fact_brief") or "")
            and f'"winner":"{result.get("winner")}"'
            in str(terminal_narrations[-1].get("fact_brief") or ""),
        },
        {
            "name": "user-and-participant-actions-share-revision-truth",
            "ok": result.get("attached_final_revision") == 13
            and result.get("retained_kurisu_actions") == 4,
        },
        {
            "name": "round-result-remains-active-before-explicit-experience-terminal",
            "ok": result.get("winner") == "black"
            and result.get("terminal_event") == "game.experience_finished"
            and (result.get("lifecycle_step") or {}).get("decided") == "step",
        },
        {
            "name": "terminal-close-is-app-session-scoped",
            "ok": result.get("close_reason") == "journey_complete",
        },
        {
            "name": "main-chat-retains-bounded-capsule",
            "ok": result.get("context_bounded") is True
            and 0 < int(result.get("shared_state_chars") or 0) <= 1024,
        },
        {
            "name": "application-remains-standalone-capable",
            "ok": result.get("standalone_move_count") == 1,
        },
        {
            "name": "browser-has-no-console-or-page-errors",
            "ok": not result.get("console_errors") and not result.get("page_errors"),
        },
        {
            "name": "natural-step-generalizes-beyond-turn-based-games",
            "ok": isinstance(game_2048, dict)
            and isinstance(reactor, dict)
            and game_2048.get("natural_step")
            == {
                "decided": "step",
                "receipt_type": "game.slide",
                "status_decided": "none",
            }
            and reactor.get("natural_step")
            == {
                "decided": "step",
                "receipt_type": "reactor.set_cooling",
                "status_decided": "none",
            }
            and not game_2048.get("console_errors")
            and not game_2048.get("page_errors")
            and not reactor.get("console_errors")
            and not reactor.get("page_errors"),
        },
    ]


async def _run_journey(model: str) -> dict[str, Any]:
    # Keep model clients, WebSocket servers, and EventBus callbacks on one
    # event loop. Re-entering asyncio.run between the two real-model journeys
    # can strand loop-bound client state after the first loop closes.
    gomoku = await run_gomoku_journey(
        routed_model=model,
        real_role_authorizer=True,
        real_narration=True,
    )
    diverse = await run_diverse_journey(model=model)
    return {**gomoku, "diverse_games": diverse}


def main() -> int:
    args = _parser().parse_args()
    started = datetime.now(timezone.utc).isoformat()
    run_id = (
        f"auip_journey_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_"
        f"{uuid4().hex[:6]}"
    )
    report_dir = Path(args.report_dir).resolve()
    isolation_root = report_dir / run_id
    isolation_root.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{run_id}.json"
    result: dict[str, Any] = {}
    error = ""
    error_traceback = ""
    try:
        result = asyncio.run(_run_journey(str(args.model)))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        error_traceback = traceback.format_exc()
    checks = (
        _checks(result)
        if not error
        else [{"name": "journey-runtime-completed", "ok": False, "errors": [error]}]
    )
    status = "passed" if all(item["ok"] for item in checks) else "failed"
    finished = datetime.now(timezone.utc).isoformat()
    evidence = build_evidence(
        root=ROOT,
        journey_id="J7",
        status=status,
        test_level="L3",
        provider="none",
        model=str(args.model),
        report_path=report_path,
        isolation_root=isolation_root,
        checks=checks,
        started_at=started,
        finished_at=finished,
        artifact_hashes={
            "index.html": hashlib.sha256(ENTRY.read_bytes()).hexdigest(),
            "auip.manifest.json": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
            **{
                f"{name}/index.html": hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in SAMPLES.items()
            },
        },
        ledger_ids={"app_session_id": str(result.get("app_session_id") or "")},
        manual_acceptance="pending",
        notes=(
            "AUIP is an AppSession protocol, not a WorkLedger Provider.",
            "Visible Electron card, TTS timing, and simultaneous Work feel remain L4.",
        ),
    )
    report = {
        "schema": "amadeus.auip-semantic-journey.v1",
        "journey_id": "J7",
        "started_at": started,
        "finished_at": finished,
        "status": status,
        "checks": checks,
        "result": result,
        "error": error,
        "error_traceback": error_traceback,
        "semantic_evidence": evidence,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": status,
                "report": str(report_path),
                "failed_checks": [item["name"] for item in checks if not item["ok"]],
                "error": error,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
