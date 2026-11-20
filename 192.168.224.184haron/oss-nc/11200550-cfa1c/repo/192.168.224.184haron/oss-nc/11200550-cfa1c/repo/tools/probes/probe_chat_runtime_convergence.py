"""Opt-in real Electron Chat/Work journey using synthetic, isolated data.

Uses the shipping bootstrap, renderer, WebSocket API and role/Planner models.
Speech and Work are separately opt-in. Does not alter production policy.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.e2e_live_product_journey import (
    BACKEND_PORT, ElectronProduct, WsProbe, _capture_chat_route, _chat_route_profile,
    _event_run_id, _free_port, _populate_turn_timings, _send_ui_turn, code_identity,
    _wait_output_idle,
)


async def run(args):
    run_root = args.report_dir.resolve() / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6])
    run_root.mkdir(parents=True, exist_ok=False)
    workspace = None
    if args.work:
        # Use ordinary Project inheritance. Windows mkdtemp's owner-only ACL
        # would hide files created by the execution sandbox account from Host.
        workspace = Path(tempfile.gettempdir()) / ("amadeus-convergence-project-" + uuid.uuid4().hex)
        workspace.mkdir(exist_ok=False)
    connection = {}
    voice = {}
    if args.credentials_env:
        from dotenv import dotenv_values

        # Read model connections and, only with --voice, the voice profile.
        # Sessions, Work policy, role prompts and route flags remain isolated.
        prefixes = ("DEEPSEEK_", "OPENAI_", "GEMINI_", "AWS_BEDROCK_", "AWS_BEARER_TOKEN_")
        supplied = dotenv_values(args.credentials_env)
        connection = {key: str(value) for key, value in supplied.items()
                      if key.startswith(prefixes) and value is not None}
        if args.voice:
            voice = {key: str(value) for key, value in supplied.items() if value is not None
                and (key.startswith(("TTS_", "EXP_TTS_", "FIRST_SENTENCE_"))
                     or key in {"USE_FIRST_SENTENCE_SPRINT", "ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING"})}
            for key, default in (("TTS_REF_AUDIO_JA", "assets/audio/reference/kurisu_reference.wav"),
                                 ("TTS_REF_AUDIO_EN", "assets/audio/reference/english_recording.wav")):
                path = Path(voice.get(key, default))
                voice[key] = str(path if path.is_absolute() else args.credentials_env.resolve().parent / path)

    class ProbeProduct(ElectronProduct):
        def _environment(self):
            return {**super()._environment(), **connection,
                **({"CODEX_APP_SERVER_PROVIDER_AUTH_ENV_FILE": str(args.credentials_env.resolve())}
                    if args.credentials_env else {}),
                "AMADEUS_PYTHON": sys.executable, "AMADEUS_WALLPAPER": "0",
                **({"TTS_BACKEND": "disabled", "TTS_DEVICE": "cpu"} if not args.voice else voice),
                "RAG_ENABLED": "0",
                "LLM_PROVIDER": args.chat_provider, "WORK_EXECUTION_PROVIDER": "pi",
                **({"WORK_PROJECT_ALLOWLIST": str(workspace)} if workspace else {}),
                "PI_AGENT_DIR": str(run_root / "state/pi")}

    product = ProbeProduct(run_root=run_root, debug_port=_free_port(), no_tts=not args.voice,
        identity=code_identity(ROOT), chat_route=args.chat_route)
    report = {"schema": "amadeus.chat-convergence-journey.v1", "status": "incomplete",
        "identity": product.identity, "chat_route": _chat_route_profile(args.chat_route),
        "chat_provider": args.chat_provider, "requested_default_work_provider": "pi",
        "audio_device_writes": False, "acoustic_loopback": False,
        "speech_requested": args.voice,
        "checks": {}, "turns": [], "limitations": ["Synthetic input; no microphone or acoustic loopback."]}
    turns = []
    try:
        await product.start(startup_timeout=120)
        async with WsProbe(f"ws://127.0.0.1:{BACKEND_PORT}/ws",
                           subprotocols=product.backend_websocket_protocols) as probe:
            await _capture_chat_route(probe, report)
            if args.work:
                # A report may live in a protected managed checkout. Exercise
                # Work in an ordinary isolated project, independently of where
                # its diagnostic output is saved.
                report["workspace"] = str(workspace)
                created = await probe.request("project.create", {
                    "workspace_path": str(workspace), "name": "Convergence synthetic project"})
                if not created.get("projectCreated"):
                    raise RuntimeError("isolated test project was not created")
                # Rehydrate the real renderer after API-driven fixture setup.
                await product.page.reload()
                await product.page.locator('textarea[placeholder*="Type a message"]').wait_for(timeout=30000)
            await product.select_chat_provider(args.chat_provider)
            config = await probe.request("system.get_config", {})
            report["effective_provider"] = config.get("llm_provider")
            report["checks"]["provider_selected"] = config.get("llm_provider") == args.chat_provider

            async def send(label, text):
                turn = await _send_ui_turn(product, probe, label=label, text=text,
                                           chat_timeout=args.timeout)
                turns.append(turn)
                if args.voice:
                    first = await probe.wait_event(lambda event: event.method == "tts.sentence_start",
                        after=turn.event_start, timeout=args.timeout, description="real TTS sentence start")
                    await probe.wait_event(lambda event: event.method == "tts.sentence_end"
                        and event.params.get("sentence_id") == first.params.get("sentence_id"),
                        after=turn.event_start, timeout=args.timeout, description="real TTS sentence end")
                    runtime = await _wait_output_idle(probe, timeout=args.timeout)
                    written = (runtime.get("coordinator") or {}).get("first_audio_write_times", {}).get(turn.turn_id)
                    turn.checks["successful_audio_device_write"] = isinstance(written, (float, int))
                    if written is not None:
                        turn.timings["first_audio_device_write_s"] = round(
                            written - (probe.state.started_at + turn.started_elapsed_s), 3)
                if args.chat_provider.startswith("hybrid"):
                    observed = await probe.request("system.get_config", {})
                    report.setdefault("hybrid_head_observations", []).append({
                        "label": label, **observed.get("hybrid_head", {})})
                report["turns"] = [row.to_dict() for row in turns]
                (run_root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"turn": label, "reply_present": bool(turn.reply),
                                  "provider_runs": len(turn.run_ids)}), flush=True)
                return turn

            hello = await send("ordinary", "こんにちは。今日は穏やかに過ごしたいです。短く一言だけ返してください。")
            report["checks"]["ordinary_reply_no_work"] = bool(hello.reply) and not hello.run_ids
            explain = await send("explain", "ファイルやタスクを作らず、虹ができる理由を二文で説明してください。")
            report["checks"]["explanation_no_work"] = bool(explain.reply) and not explain.run_ids

            if args.interaction:
                from PIL import Image, ImageDraw, ImageFont

                picture = run_root / "synthetic-card.png"
                card = Image.new("RGB", (640, 360), "white")
                draw = ImageDraw.Draw(card)
                draw.ellipse((55, 45, 225, 215), fill="red")
                draw.rectangle((365, 45, 535, 215), fill="blue")
                draw.text((270, 235), "42", fill="black", font=ImageFont.truetype("arial.ttf", 76))
                card.save(picture)
                await product.page.locator('input[type="file"][accept="image/*"]').set_input_files(str(picture))
                await product.page.get_by_text(picture.name, exact=True).wait_for(timeout=15000)
                vision = await send("image", "この画像の形と色、書かれた数字を短く教えてください。実行やファイル作成は不要です。")
                report["checks"]["image_grounding"] = (
                    "42" in vision.reply and any(word in vision.reply.lower() for word in ("赤", "红", "red"))
                    and any(word in vision.reply.lower() for word in ("青", "蓝", "blue")) and not vision.run_ids)

                start = len(probe.state.events)
                field = product.page.locator('textarea[placeholder*="Type a message"]')
                await field.fill("実行やファイル作成はせず、空想の図書館について長い物語を二千字ほど書いてください。")
                await field.press("Enter")
                first = await probe.wait_event(lambda event: event.method == "chat.token"
                    and bool(event.params.get("token")), after=start, timeout=args.timeout,
                    description="visible text before interrupt")
                interrupted_id = first.params["turn_id"]
                aborted = await probe.request("chat.abort", {"turn_id": interrupted_id, "stop_execution": False})
                report["interruption"] = {key: aborted.get(key) for key in ("status", "turn_id", "accumulated_text")}
                report["checks"]["interrupt_acknowledged"] = aborted.get("status") == "aborted"
                after = await send("after_interrupt", "今の長い話はもう不要です。「確認したわ。」とだけ短く返してください。")
                report["checks"]["next_turn_after_interrupt"] = bool(after.reply) and not after.run_ids
                report["checks"]["no_late_completion_after_interrupt"] = not any(
                    event.method == "chat.complete" and event.params.get("turn_id") == interrupted_id
                    for event in probe.state.events[start:])

            if args.work:
                work = await send("single_work", "请在当前项目创建 convergence_result.txt，完整内容为 CONVERGENCE_OK。"
                    "只创建这一个文件，不执行代码或命令，也不要修改其他文件。")
                created = await probe.wait_event(lambda event: event.method == "provider.event"
                    and event.params.get("type") == "run.created",
                    after=work.event_start, timeout=60, description="single Work start")
                run_id = _event_run_id(created)
                report["work_provider"] = created.params.get("provider")
                terminal = await probe.wait_event(lambda event: event.method == "provider.result"
                    and _event_run_id(event) == run_id, after=work.event_start,
                    timeout=args.work_timeout, description="single Work terminal result")
                report["checks"]["work_terminal_success"] = terminal.params.get("status") in {"done", "succeeded", "completed"}
                report["work_terminal"] = {key: terminal.params.get(key) for key in ("status", "error")}
                artifact = workspace / "convergence_result.txt"
                report["checks"]["requested_artifact"] = artifact.is_file() and artifact.read_text(encoding="utf-8").strip() == "CONVERGENCE_OK"
                report["checks"]["only_requested_file"] = sorted(
                    str(path.relative_to(workspace)) for path in workspace.rglob("*") if path.is_file()
                ) == ["convergence_result.txt"]
                status = await send("report", "刚才的任务完成了吗？只汇报已有记录，不要继续或新建任务。")
                report["checks"]["report_no_new_run"] = bool(status.reply) and not status.run_ids
            _populate_turn_timings(turns, probe.state.events)
            report["turns"] = [row.to_dict() for row in turns]
            report["event_methods"] = {name: sum(event.method == name for event in probe.state.events)
                for name in sorted({event.method for event in probe.state.events})}
            if args.voice:
                report["speech_events"] = [{"method": event.method, "elapsed_s": event.elapsed_s,
                    "params": event.params} for event in probe.state.events
                    if event.method in {"tts.sentence_start", "tts.sentence_end", "tts.turn_complete"}]
            report["checks"]["total_provider_runs"] = sum(event.method == "provider.event"
                and event.params.get("type") == "run.created" for event in probe.state.events) == int(args.work)
            tool_names = [str((event.params.get("payload") or {}).get("tool") or "")
                for event in probe.state.events if event.method == "provider.event"
                and event.params.get("type") == "tool.call"]
            report["tool_names"] = sorted(set(tool_names))
            if args.work:
                report["checks"]["no_shell_commands"] = not any(
                    name in {"shell", "bash", "command_execution"} for name in tool_names)
            report["app_surface_diagnostics"] = product.app_diagnostics()
            report["checks"]["no_renderer_page_error"] = not report["app_surface_diagnostics"]["page_errors"]
            if args.voice:
                report["checks"]["audio_device_writes"] = all(
                    row.checks.get("successful_audio_device_write") is True for row in turns)
                report["audio_device_writes"] = report["checks"]["audio_device_writes"]
                report["speech_scope"] = "Nonempty successful device-buffer writes; not acoustic loopback."
            if args.chat_provider.startswith("hybrid"):
                report["checks"]["hybrid_local_presented"] = any(
                    row.get("outcome") == "presented"
                    for row in report.get("hybrid_head_observations", []))
            report["status"] = "passed" if all(report["checks"].values()) else "failed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        await product.stop()
        report["process_reaped"] = product.process is None or product.process.poll() is not None
        (run_root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": report["checks"],
                      "error": report.get("error", ""), "report": str(run_root / "report.json")}), flush=True)
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-route", choices=("professional", "basic"), required=True)
    parser.add_argument("--chat-provider", default="deepseek")
    parser.add_argument("--credentials-env", type=Path)
    parser.add_argument("--report-dir", type=Path,
                        default=Path(tempfile.gettempdir()) / "amadeus-chat-convergence")
    parser.add_argument("--work", action="store_true", help="Create one synthetic file through actual Work")
    parser.add_argument("--interaction", action="store_true", help="Exercise image upload and real Chat abort")
    parser.add_argument("--voice", action="store_true", help="Synthesize and play through the configured audio device")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--work-timeout", type=float, default=240)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
