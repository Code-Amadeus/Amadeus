"""Compare the actual default Chat HTTP request from two shipping checkouts.

The local endpoint records only synthetic request bodies and supplies a fixed
role reply. This verifies assembly/transport equivalence, not real-model quality.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import e2e_live_product_journey as journey


def normalize_request(body):
    """Remove only the two random identities in the current user-event frame."""
    copied = json.loads(json.dumps(body))
    for message in copied.get("messages", []):
        if message.get("role") != "user":
            continue
        content = message["content"]
        offset = content.index("{")
        frame = json.loads(content[offset:])
        if frame.get("source_kind") == "user":
            for key in ("input_id", "turn_id"):
                if key in frame["current"]:
                    frame["current"][key] = "<current-turn>"
        message["content"] = {"language_wrapper": content[:offset], "frame": frame}
    return copied


async def run(args):
    out = args.report_dir.resolve() / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=False)
    report = {"schema": "amadeus.default-chat-request-comparison.v1", "status": "running",
        "configuration": {"provider": "deepseek", "route": "professional", "rag": False, "tts": False},
        "normalization": "Only current.input_id and current.turn_id; all other request data compared.",
        "cases": [], "limitations": ["Synthetic HTTP model response; not model quality or latency evidence."]}
    requests = []

    async def reply(request):
        requests.append(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for text in ("[EMO normal]", "こんにちは。", "穏やかな一日ね。"):
            await response.write(("data: " + json.dumps({"choices": [{"index": 0,
                "delta": {"content": text}, "finish_reason": None}]}) + "\n\n").encode())
        await response.write(b'data: [DONE]\n\n')
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", reply)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        for label, checkout in (("baseline", args.baseline_checkout.resolve()),
                                ("candidate", args.candidate_checkout.resolve())):
            journey.ROOT, journey.ELECTRON_ROOT = checkout, checkout / "electron"
            run_root = out / label
            run_root.mkdir()

            class Product(journey.ElectronProduct):
                def _environment(self):
                    return {**super()._environment(), "AMADEUS_PYTHON": sys.executable,
                        "AMADEUS_WALLPAPER": "0", "TTS_BACKEND": "disabled", "TTS_DEVICE": "cpu",
                        "RAG_ENABLED": "0", "LLM_PROVIDER": "deepseek", "WORK_EXECUTION_PROVIDER": "pi",
                        "DEEPSEEK_BASE_URL": f"http://127.0.0.1:{port}/v1",
                        "DEEPSEEK_API_KEY": "synthetic-local-fixture", "DEEPSEEK_MODEL_NAME": "contract-model",
                        "PI_AGENT_DIR": str(run_root / "state/pi")}

            product = Product(run_root=run_root, debug_port=journey._free_port(), no_tts=True,
                identity=journey.code_identity(checkout), chat_route="professional")
            case = {"label": label, "identity": product.identity,
                "chat_route": journey._chat_route_profile("professional")}
            report["cases"].append(case)
            start = len(requests)
            try:
                await product.start(startup_timeout=120)
                async with journey.WsProbe(f"ws://127.0.0.1:{journey.BACKEND_PORT}/ws",
                        subprotocols=product.backend_websocket_protocols) as probe:
                    await journey._capture_chat_route(probe, case)
                    turn = await journey._send_ui_turn(product, probe, label="ordinary",
                        text="今日は穏やかに過ごしたいです。短く一言だけ返してください。", chat_timeout=30)
                    case["reply"] = turn.reply
                    case["no_work"] = not turn.run_ids
                    case["requests"] = requests[start:]
                    case["diagnostics"] = product.app_diagnostics()
            finally:
                await product.stop()
                case["process_reaped"] = product.process is None or product.process.poll() is not None
        baseline, candidate = report["cases"]
        report["checks"] = {
            "one_request_each": all(len(case["requests"]) == 1 for case in report["cases"]),
            "no_work": all(case["no_work"] for case in report["cases"]),
            "same_visible_reply": baseline["reply"] == candidate["reply"] and bool(candidate["reply"]),
            "same_complete_request": [normalize_request(body) for body in baseline["requests"]]
                == [normalize_request(body) for body in candidate["requests"]],
            "no_page_errors": not any(case["diagnostics"]["page_errors"] for case in report["cases"]),
            "processes_reaped": all(case["process_reaped"] for case in report["cases"]),
        }
        report["status"] = "passed" if all(report["checks"].values()) else "failed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        await runner.cleanup()
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": report.get("checks"),
        "error": report.get("error"), "report": str(out / "report.json")}), flush=True)
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-checkout", required=True, type=Path)
    parser.add_argument("--candidate-checkout", type=Path, default=ROOT)
    parser.add_argument("--report-dir", type=Path,
        default=Path(tempfile.gettempdir()) / "amadeus-chat-request-comparison")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
