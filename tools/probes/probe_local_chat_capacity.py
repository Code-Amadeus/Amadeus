"""Offline GGUF token counts and opt-in, isolated llama.cpp capacity probes.

Uses synthetic dialogue only. Never dispatches Work or modifies app settings.
The temporary HTTP server and CLI children are owned and reaped by this probe.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_runtime import ProviderRuntime
from llm.prompts import (
    finalize_system_prompt_language, get_system_prompt, wrap_user_message_for_language_lock,
)
from server.cooperative_provider_loop import CooperativeProviderLoop, PRESENTATION_CONTRACT
from server.work_planner_prompt import get_work_planner_prompt


async def capture_role(professional: bool, history_rows: int) -> list[dict]:
    captured = []

    async def query(messages, **_kwargs):
        captured.extend(dict(row) for row in messages)
        return '{"action":null,"say":"はい。"}'

    loop = CooperativeProviderLoop(
        ProviderRuntime(), query, lambda *_: ROOT, provider="pi",
        context_requirements={"pi": ProviderRequirements()}, persona=get_system_prompt("base"),
        owns_runtime=False, work_proposals_only=professional,
    )
    history = [{"source": "user" if i % 2 == 0 else "kurisu",
                "text": "今日はとても良い天気なので散歩に行きます。"[:20],
                "input_id": f"synthetic-{i}"} for i in range(history_rows)]
    await loop._decide({"source": "user", "input_id": "capacity-probe",
                       "turn_id": "capacity-probe", "text": "こんにちは。"},
                      turn_history=history)
    captured[0]["content"] = finalize_system_prompt_language(captured[0]["content"])
    captured[-1]["content"] = wrap_user_message_for_language_lock(captured[-1]["content"])
    return captured


async def probe_cooperative_turns(base: str, *, timeout: float) -> list[dict]:
    """Run synthetic conversation-only turns through the real decoder/loop."""
    from llm import client
    from server.cooperative_delivery import query_role_messages

    client.configure(llm_provider="local", local_llm_type="llama_server")
    client.LOCAL_LLM_URL = base
    results = []

    async def query(messages, *, on_text=None, json_output=True):
        request = [dict(row) for row in messages]
        request[0]["content"] = finalize_system_prompt_language(request[0]["content"])
        request[-1]["content"] = wrap_user_message_for_language_lock(request[-1]["content"])
        return await query_role_messages(client.remote_llm_messages_query, request,
            model="capacity-probe", max_tokens=384, timeout=timeout,
            json_output=json_output, temperature=0.0, on_text=on_text)

    def no_execution(*_args):
        raise AssertionError("conversation probe must not allocate execution")

    for professional in (False, True):
        loop = CooperativeProviderLoop(ProviderRuntime(), query, no_execution,
            provider="unavailable", context_requirements={}, persona=get_system_prompt("base"),
            publish=lambda _event: True, work_proposals_only=professional)
        try:
            for index, text in enumerate(("こんにちは。", "この会話の合言葉はORCHIDです。覚えてください。",
                                          "さっきの合言葉は何でしたか？", "ありがとう。短く一言返してください。")):
                started = time.monotonic()
                try:
                    receipt = await loop.submit(text, input_id=f"probe-{index}")
                    result = {"state": receipt["state"], "reply": next((row["text"]
                        for row in reversed(loop.history) if row["source"] == "kurisu"), "")}
                except Exception as exc:
                    result = {"error": type(exc).__name__ + ": " + str(exc)}
                results.append({"mode": "professional" if professional else "basic", "turn": index,
                    "seconds": round(time.monotonic() - started, 3), **result})
                print(json.dumps({key: results[-1][key] for key in ("mode", "turn", "seconds")}
                    | {"state": result.get("state", "error")}), flush=True)
        finally:
            await loop.close()
    return results


def hidden_flags() -> dict:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llama-dir", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live-server", action="store_true")
    parser.add_argument("--live-cli", action="store_true")
    parser.add_argument("--cli-input-contract", choices=("memory", "multiline"), default="memory")
    parser.add_argument("--inspect-server-only", action="store_true",
                        help="With --live-server, capture /props and templates without generation")
    parser.add_argument("--verify-message-port", action="store_true",
                        help="Exercise the production message capacity check and HTTP query")
    parser.add_argument("--live-cooperative", action="store_true",
                        help="Run four synthetic pure-conversation turns per strategy after the capacity probe")
    parser.add_argument("--max-tokens", type=int, default=24)
    parser.add_argument("--gpu-layers", type=int, default=0,
                        help="Default CPU only; opt into GPU offload explicitly")
    parser.add_argument("--device", default="", help="Optional llama.cpp device, e.g. CUDA0")
    parser.add_argument("--ctx", type=int, default=4096)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if args.inspect_server_only and not args.live_server:
        parser.error("--inspect-server-only requires --live-server")
    if args.verify_message_port and (not args.live_server or args.inspect_server_only):
        parser.error("--verify-message-port requires --live-server without --inspect-server-only")
    if args.live_cooperative and (not args.live_server or args.inspect_server_only):
        parser.error("--live-cooperative requires --live-server without --inspect-server-only")
    if args.ctx <= 0 or args.timeout <= 0:
        parser.error("--ctx and --timeout must be positive")
    if args.max_tokens <= 0 or args.gpu_layers < 0:
        parser.error("--max-tokens must be positive and --gpu-layers nonnegative")
    args.output.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if os.name == "nt" else ""
    binary = lambda name: str(args.llama_dir / (name + suffix))
    session = requests.Session()
    session.trust_env = False
    roles = {f"{mode}_{rows}": asyncio.run(capture_role(mode == "professional", rows))
             for mode in ("basic", "professional") for rows in (0, 60)}
    texts = {"basic_system": roles["basic_0"][0]["content"],
             "professional_system": roles["professional_0"][0]["content"],
             "planner_system_without_catalog": get_work_planner_prompt(("pi",)),
             "receipt_system": finalize_system_prompt_language(
                 get_system_prompt("base") + "\n\n" + PRESENTATION_CONTRACT),
             "old_with_delegate": get_system_prompt("with_delegate")}
    texts.update({key + "_frame": value[-1]["content"] for key, value in roles.items()})

    def count(text):
        result = subprocess.run([binary("llama-tokenize"), "-m", str(args.model),
                                 "--stdin", "--ids", "--no-bos", "--log-disable"],
                                input=text, capture_output=True, text=True, encoding="utf-8",
                                timeout=30, check=True, **hidden_flags())
        return len(ast.literal_eval(result.stdout.strip()))

    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    report = {"source_revision": revision, "model_file": args.model.name,
              "tokenizer": "GGUF embedded tokenizer",
              "counts_exclude_chat_template": True, "output_reserve_tokens": 900,
              "token_counts": {key: count(value) for key, value in texts.items()},
              "text_sha256": {key: hashlib.sha256(value.encode("utf-8")).hexdigest()
                              for key, value in texts.items()},
              "requested_context": args.ctx}
    report["content_plus_output_budget"] = {
        key: count(value[0]["content"]) + count(value[-1]["content"]) + 900
        for key, value in roles.items()}
    for key, value in roles.items():
        (args.output / (key + ".json")).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    version = subprocess.run([binary("llama-server"), "--version"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", **hidden_flags())
    report["binary_version"] = [line for line in (version.stdout + version.stderr).splitlines()
                                if line.startswith(("version:", "built with"))]
    report_path = args.output / "report.json"

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    print(json.dumps(report, ensure_ascii=False), flush=True)
    common = ["-m", str(args.model), "-c", str(args.ctx), "-ngl", str(args.gpu_layers), "-t", "4"]
    if args.device:
        common.extend(["--device", args.device])
    report["generation_limit"] = args.max_tokens
    report["gpu_layers"] = args.gpu_layers
    report["device"] = args.device
    if args.live_server:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        with (args.output / "server.log").open("w", encoding="utf-8") as log:
            proc = subprocess.Popen([binary("llama-server"), *common, "--parallel", "1",
                                     "--host", "127.0.0.1", "--port", str(port),
                                     "--chat-template-kwargs", '{"enable_thinking":false}'],
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, **hidden_flags())
            try:
                until = time.monotonic() + args.timeout
                while time.monotonic() < until:
                    if proc.poll() is not None:
                        raise RuntimeError(f"temporary server exited: {proc.returncode}")
                    try:
                        if session.get(base + "/health", timeout=1).status_code == 200:
                            break
                    except requests.RequestException:
                        pass
                    time.sleep(0.25)
                else:
                    raise TimeoutError("temporary server startup deadline")
                props = session.get(base + "/props", timeout=5).json()
                report["server_props"] = {key: props.get(key) for key in (
                    "total_slots", "default_generation_settings", "model_alias")}
                report["server_template_tokens"] = {}
                report["server_queries"] = []
                for key in ("basic_0", "professional_0", "basic_60", "professional_60"):
                    template = session.post(base + "/apply-template", json={
                        "messages": roles[key]}, timeout=5)
                    template.raise_for_status()
                    report["server_template_tokens"][key] = count(template.json()["prompt"])
                    if args.inspect_server_only:
                        continue
                    start = time.monotonic()
                    if args.verify_message_port:
                        from llm import client
                        from llm.local_backends import require_llama_message_capacity

                        client.LOCAL_LLM_TYPE = "llama_server"
                        client.LOCAL_LLM_URL = base
                        try:
                            budget = require_llama_message_capacity(base, {
                                "messages": roles[key], "model": "capacity-probe", "max_tokens": args.max_tokens,
                                **({"response_format": {"type": "json_schema", "json_schema": {
                                    "name": "response", "schema": {"type": "object"}}}}
                                   if key.startswith("basic") else {}),
                            }, timeout=args.timeout)
                            reply = client._local_messages_query(roles[key], temperature=0.0,
                                max_tokens=args.max_tokens, model="capacity-probe", timeout=args.timeout,
                                json_output=key.startswith("basic"))
                            result = {"budget": budget, "reply": reply,
                                      "json_output": key.startswith("basic")}
                        except Exception as exc:
                            result = {"error": type(exc).__name__ + ": " + str(exc)}
                        report["server_queries"].append({"case": key, "message_port": True,
                            "seconds": round(time.monotonic()-start, 3), "result": result})
                        save()
                        print(f"message port {key}: {'rejected' if 'error' in result else 'generated'}", flush=True)
                        continue
                    response = session.post(base + "/v1/chat/completions", json={
                        "model": "capacity-probe", "messages": roles[key], "max_tokens": 24,
                        "temperature": 0.0, "stream": False}, timeout=args.timeout)
                    result = response.json()
                    report["server_queries"].append({"case": key, "status": response.status_code,
                        "seconds": round(time.monotonic()-start, 3), "result": result})
                    save()
                    print(f"server {key}: HTTP {response.status_code}", flush=True)
                if args.live_cooperative:
                    report["cooperative_turns"] = asyncio.run(probe_cooperative_turns(base, timeout=args.timeout))
                    save()
            except Exception as exc:
                report["server_probe_error"] = type(exc).__name__ + ": " + str(exc)
            finally:
                reap(proc)
                report["server_reaped"] = proc.poll() is not None
                save()
    if args.live_cli:
        legacy = subprocess.run([binary("llama-cli"), "--interactive", "--help"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=15, **hidden_flags())
        report["cli_legacy_flag"] = {"returncode": legacy.returncode,
                                    "invalid_argument": "invalid argument: --interactive" in legacy.stderr}
        # The modern binary may reject the repo's old flag. Explicit conversation
        # mode measures this binary's real state; it is not a fix or parity claim.
        commands = (("Remember the secret word ORCHID. Reply OK only.\n"
                     "What was the secret word? Reply one word only.\n"
                     "/clear\nWhat was the secret word? If unknown, reply UNKNOWN.\n/exit\n")
                    if args.cli_input_contract == "memory" else
                    "Reply with the single word OK to every request.\n\nUser: Say BLUE.\nAssistant:\n/exit\n")
        report["cli_input_contract"] = args.cli_input_contract
        proc = subprocess.Popen([binary("llama-cli"), *common, "--conversation", "--simple-io",
                                 "--no-display-prompt", "-n", "24", "--temp", "0",
                                 "--reasoning", "off", "--reasoning-budget", "0",
                                 "--chat-template-kwargs", '{"enable_thinking":false}'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", **hidden_flags())
        try:
            stdout, stderr = proc.communicate(commands, timeout=args.timeout)
            report["cli_conversation"] = {"returncode": proc.returncode,
                                          "stdout": stdout, "stderr": stderr}
        except subprocess.TimeoutExpired:
            reap(proc)
            stdout, stderr = proc.communicate()
            report["cli_conversation"] = {"timeout": True, "stdout": stdout, "stderr": stderr}
        finally:
            reap(proc)
            save()
        print("CLI probe finished", flush=True)
    return 1 if (report.get("server_probe_error")
                 or report.get("cli_conversation", {}).get("timeout")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
