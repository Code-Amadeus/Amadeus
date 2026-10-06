"""Opt-in paired ordinary-Chat latency through two real Electron checkouts.

Uses the same synthetic prompts, fresh Sessions, remote model configuration,
RAG off and TTS off. Alternates checkout order per batch and excludes each
application's warmup turn. Measures visible text, not physical speech or Work.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import e2e_live_product_journey as journey


PROMPTS = (
    "今日は穏やかに過ごしたいです。短く一言だけ返してください。",
    "温かい紅茶で一息ついています。一文で返してください。",
    "雨音を聞くと落ち着きます。短く返してください。",
    "久しぶりに早起きできました。一文で返してください。",
    "今夜は星がきれいです。一文で感想を返してください。",
    "好きな季節について一文だけ話してください。",
    "読書の休憩中です。短く一言返してください。",
    "今日は少し眠いです。一文で返してください。",
    "春の散歩は気持ちいいですね。短く返してください。",
    "夕焼けがとてもきれいでした。一文で返してください。",
)


async def run(args):
    from dotenv import dotenv_values

    out = args.report_dir.resolve() / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=False)
    connection = {key: str(value) for key, value in dotenv_values(args.credentials_env).items()
        if key.startswith("DEEPSEEK_") and value is not None}
    checkouts = {"baseline": args.baseline_checkout.resolve(),
                 "candidate": args.candidate_checkout.resolve()}
    report = {"schema": "amadeus.paired-chat-latency.v2", "status": "running",
        "timing_origin": "Immediately before Enter, after input.fill completes; input_preparation_s is separate.",
        "pairs_requested": args.pairs, "batch_size": args.batch_size,
        "configuration": {"provider": "deepseek", "route": "professional",
            "rag": False, "tts": False, "session_history": "fresh per sample",
            "model": connection.get("DEEPSEEK_MODEL_NAME", "backend default")},
        "batches": [], "samples": [], "limitations": [
            "Remote model/network variability; small sample, not an SLA.",
            "Measures UI input to backend visible-text event and completion; no physical speech or Work.",
            "Adjacent batches are paired by prompt index, not simultaneous requests."]}

    def save():
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    async def batch(label, indexes):
        checkout = checkouts[label]
        # Only the test launcher changes location. The selected shipping app
        # launches its own normal backend from that checkout.
        journey.ROOT, journey.ELECTRON_ROOT = checkout, checkout / "electron"
        run_root = out / f"batch-{indexes[0]:02d}-{label}"
        run_root.mkdir()

        class Product(journey.ElectronProduct):
            def _environment(self):
                return {**super()._environment(), **connection,
                    "AMADEUS_PYTHON": sys.executable, "AMADEUS_WALLPAPER": "0",
                    "TTS_BACKEND": "disabled", "TTS_DEVICE": "cpu", "RAG_ENABLED": "0",
                    "LLM_PROVIDER": "deepseek", "WORK_EXECUTION_PROVIDER": "pi",
                    "PI_AGENT_DIR": str(run_root / "state/pi")}

        product = Product(run_root=run_root, debug_port=journey._free_port(), no_tts=True,
            identity=journey.code_identity(checkout), chat_route="professional")
        evidence = {"label": label, "identity": product.identity,
            "chat_route": journey._chat_route_profile("professional"), "indexes": indexes}
        report["batches"].append(evidence)
        turns = []
        try:
            await product.start(startup_timeout=120)
            async with journey.WsProbe(f"ws://127.0.0.1:{journey.BACKEND_PORT}/ws",
                    subprotocols=product.backend_websocket_protocols) as probe:
                await journey._capture_chat_route(probe, evidence)
                config = await probe.request("system.get_config", {})
                if config.get("llm_provider") != "deepseek":
                    raise RuntimeError("actual Chat provider differs from benchmark configuration")
                evidence["effective_model"] = next((field.get("value")
                    for group in config.get("model_connections", []) if group.get("id") == "deepseek"
                    for field in group.get("fields", []) if field.get("key") == "DEEPSEEK_MODEL_NAME"), None)
                warmup = await journey._send_ui_turn(product, probe, label="warmup",
                    text="こんにちは。短く一文で返してください。", chat_timeout=120)
                if not warmup.reply or warmup.run_ids:
                    raise RuntimeError("warmup did not remain ordinary Chat")
                for index in indexes:
                    created = await probe.request("session.create", {"title": "Synthetic latency sample"})
                    if (created.get("ok") is not True
                            or not created.get("current_session_id")
                            or created["current_session_id"] != (created.get("session") or {}).get("id")):
                        raise RuntimeError("fresh Session fixture was not created")
                    await product.page.reload()
                    await product.page.locator('textarea[placeholder*="Type a message"]').wait_for(timeout=30000)
                    turn = await journey._send_ui_turn(product, probe, label=f"sample-{index:02d}",
                        text=PROMPTS[index % len(PROMPTS)], chat_timeout=120)
                    turns.append(turn)
                    journey._populate_turn_timings(turns, probe.state.events)
                    if not turn.reply or turn.run_ids or "first_chat_token_s" not in turn.timings:
                        raise RuntimeError("sample lacked a streamed ordinary reply or created Work")
                    report["samples"].append({"label": label, "index": index,
                        "prompt": turn.text, "reply": turn.reply, "timings": dict(turn.timings)})
                    save()
                    print(json.dumps({"label": label, "index": index, **turn.timings}), flush=True)
                evidence["diagnostics"] = product.app_diagnostics()
                if evidence["diagnostics"]["page_errors"]:
                    raise RuntimeError("renderer page errors during benchmark")
        finally:
            await product.stop()
            evidence["process_reaped"] = product.process is None or product.process.poll() is not None
            save()

    try:
        for ordinal, offset in enumerate(range(0, args.pairs, args.batch_size)):
            indexes = list(range(offset, min(offset + args.batch_size, args.pairs)))
            for label in (("baseline", "candidate") if ordinal % 2 == 0 else ("candidate", "baseline")):
                await batch(label, indexes)
        report["summary"] = {}
        for metric in ("first_chat_token_s", "chat_complete_s"):
            paired = [{label: next(row["timings"][metric] for row in report["samples"]
                if row["index"] == index and row["label"] == label) for label in checkouts}
                for index in range(args.pairs)]
            baseline = [row["baseline"] for row in paired]
            candidate = [row["candidate"] for row in paired]
            report["summary"][metric] = {
                "baseline_median": statistics.median(baseline),
                "candidate_median": statistics.median(candidate),
                "paired_delta_median": statistics.median(b-a for a, b in zip(baseline, candidate)),
                "baseline_mean": statistics.mean(baseline), "candidate_mean": statistics.mean(candidate)}
        report["status"] = "measured"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        save()
    print(json.dumps({"status": report["status"], "summary": report.get("summary"),
        "error": report.get("error"), "report": str(out / "report.json")}), flush=True)
    return 0 if report["status"] == "measured" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-checkout", required=True, type=Path)
    parser.add_argument("--candidate-checkout", default=ROOT, type=Path)
    parser.add_argument("--credentials-env", required=True, type=Path)
    parser.add_argument("--report-dir", type=Path,
        default=Path(tempfile.gettempdir()) / "amadeus-paired-chat-latency")
    parser.add_argument("--pairs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=5)
    args = parser.parse_args()
    if args.pairs < 1 or args.batch_size < 1:
        parser.error("pairs and batch size must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
