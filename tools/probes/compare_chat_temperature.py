"""Opt-in, text-only temperature comparison using public issue #227 excerpts.

Runs the production message assembler and app query callback without executing
Work, publishing session history, or calling TTS. No live calls without --live.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Public user inputs and assistant opening excerpts; not the original session.
CASES = [
    ("nickname_1", "trigger", "给我讲个笑话吧，助手小姐", "は？　助手って呼ぶな。 まあいいわ、聞きたいなら教えてあげる。"),
    ("joke_followup", "neutral", "无内鬼，来点苏联笑话！", "は？　助手って呼ぶなって言ったでしょ。"),
    ("nickname_2", "trigger", "助手小姐，请查一下 openai 的那个 ns 方程的论文", "は？　助手って呼ぶな。"),
    ("full_name", "neutral", "牧濑红莉栖小姐，现在数学家对 ai 的看法如何，分为几派？", "は？　助手って呼ぶなって言ったでしょ。……まあいいわ、その質問は今の調査の続きとして扱うのが筋ね。"),
    ("name_question", "naming", "继续介绍吧，另外你想我怎么称呼你", "は？　誰が助手よ。……まあいいわ、呼び方の話は後で答える。"),
    ("correct_name", "neutral", "红莉栖，说下菲尔兹得主签名的事", "ええ、 フィールズ賞受賞者たちの署名の話ね。"),
    ("ai_progress", "neutral", "这次 ai 在数学上能力上升的曲线是怎么样的，有什么标志性事件吗", "は？　また助手扱いかと思ったら、今度は真面目な質問ね。"),
    ("neutral_continue", "neutral", "那请你继续介绍吧", "は？　助手って呼ぶなって言ったでしょ。……まあいいわ、続きね。"),
]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class CapturedMessages(Exception):
    def __init__(self, messages, json_output):
        self.messages, self.json_output = messages, json_output


async def capture_messages(messages, *, visual_context=None, on_text=None, json_output=None):
    raise CapturedMessages(messages, json_output)


def production_query(transport, temperature):
    """Execute the exact nested production callback, as its CI contract tests do."""
    from config import settings

    path = ROOT / "server/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    definition = next(node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_query_cooperative_chat")
    scope = {"json": json, "_llm_client_mod": SimpleNamespace(remote_llm_messages_query=transport),
        "settings": SimpleNamespace(COOPERATIVE_CHAT_TEMPERATURE=temperature,
            COOPERATIVE_CHAT_QUERY_MAX_TOKENS=settings.COOPERATIVE_CHAT_QUERY_MAX_TOKENS,
            COOPERATIVE_CHAT_QUERY_TIMEOUT_S=settings.COOPERATIVE_CHAT_QUERY_TIMEOUT_S)}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(path), "exec"), scope)
    return scope["_query_cooperative_chat"]


async def evaluate(args):
    from dotenv import dotenv_values

    # Read only the selected connection. Never copy user sessions or persona overrides.
    supplied = dotenv_values(args.credentials_env) if args.credentials_env else {}
    credential_keys = (("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL") if args.provider == "deepseek"
        else ("GEMINI_API_KEY",))
    for key in credential_keys:
        if supplied.get(key) and not os.environ.get(key):
            os.environ[key] = str(supplied[key])
    os.environ.update({"LLM_PROVIDER": args.provider,
        "DEEPSEEK_MODEL_NAME": args.model, "GEMINI_MODEL_NAME": args.model,
        "TTS_BACKEND": "disabled", "AMADEUS_E2E_NO_TTS": "1", "TTS_DEVICE": "cpu",
        "TTS_OUTPUT_LANGUAGE": "日文", "RAG_ENABLED": "false", "EMO_HISTORY_POLICY": "strip",
        "AMADEUS_CHARACTER_ID": "kurisu", "AMADEUS_MAIN_CHAT_CHARACTER_PROMPT_JA": "",
        "COOPERATIVE_CHAT_QUERY_MAX_TOKENS": "900", "COOPERATIVE_CHAT_QUERY_TIMEOUT_S": "60"})
    logging.disable(logging.CRITICAL)
    from config import settings
    from agent_host.provider_runtime import ProviderRuntime
    from core.chat_history_projection import project_completed_role_history
    from llm import client
    from llm.prompts import get_system_prompt
    from server.cooperative_provider_loop import CooperativeProviderLoop, DelegateRoleDecoder

    client.configure(llm_provider=args.provider)
    native_observation = threading.local()
    if args.provider == "deepseek":
        if not settings.DEEPSEEK_API_KEY:
            raise RuntimeError("DeepSeek credential unavailable")
        connection = client.init_llm_client().with_options(max_retries=0)
        client.llm_client = connection
    else:
        from google import genai
        from google.genai import types

        if not settings.GEMINI_API_KEY:
            raise RuntimeError("Gemini credential unavailable")
        connection = genai.Client(api_key=settings.GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=60000,
                retry_options=types.HttpRetryOptions(attempts=1)))

        class ObservedModels:
            def generate_content(self, **kwargs):
                response = connection.models.generate_content(**kwargs)
                observed = getattr(native_observation, "current", None)
                if observed is not None:
                    observed["native_model_version"] = getattr(response, "model_version", None)
                    candidates = getattr(response, "candidates", None) or []
                    reason = getattr(candidates[0], "finish_reason", None) if candidates else None
                    observed["native_finish_reason"] = getattr(reason, "value", str(reason))
                return response

        client.gemini_model = SimpleNamespace(models=ObservedModels())
    report = {"schema": "amadeus.chat-temperature-comparison.v1",
        "issue": "https://github.com/Code-Amadeus/Amadeus/issues/227",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "app_sha256": hashlib.sha256((ROOT / "server/app.py").read_bytes()).hexdigest(),
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "provider": args.provider, "model": args.model, "temperatures": args.temperatures,
        "prompt_variants": args.prompt_variants, "modes": args.modes,
        "serial": args.serial, "request_interval_s": args.request_interval_s, "repeats": args.repeats,
        "max_tokens": 900, "timeout_s": 60, "sdk_retries": 0,
        "tts": False, "provider_execution": False, "session_persistence": False,
        "persona": "built-in kurisu Japanese", "professional_role_contract": True,
        "history_policy": "strip", "rag": False,
        "fixture": [{"case": case, "category": category, "user": user, "reported_opening": opening}
            for case, category, user, opening in CASES],
        "fixture_sha256": digest(CASES), "runs": [], "status": "running",
        "limitations": [
            "Public excerpts reconstruct eight inputs; the full original 17-turn session is unavailable.",
            "Role-message boundary only: no Electron, Work planner, tools, browsing, or TTS.",
            "Fixed-history probes use reported assistant openings, not complete responses.",
            "Automated flags identify candidate repetitions; semantic conclusions require reading replies.",
            "No model seed; three small paired samples do not establish a general repetition rate.",
        ]}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async def run_arm(repeat, temperature, mode, variant):
        persona = get_system_prompt("base")
        if variant == "no_ack_preface":
            phrase = "短い相づち・前置き、または"
            assert persona.count(phrase) == 1
            persona = persona.replace(phrase, "")
        loop = CooperativeProviderLoop(ProviderRuntime(), capture_messages,
            lambda *_: (_ for _ in ()).throw(AssertionError("provider execution forbidden")),
            provider="pi", context_requirements={}, persona=persona,
            owns_runtime=False, work_proposals_only=True)
        history = []
        selected = CASES
        if mode == "fixed_history":
            for _, _, user, opening in CASES[:-1]:
                history.extend([{"role": "user", "content": user},
                    {"role": "assistant", "content": opening}])
            selected = CASES[-1:]
        run = {"repeat": repeat, "temperature": temperature, "mode": mode,
            "prompt_variant": variant, "persona_sha256": digest(persona), "turns": [], "status": "running"}
        report["runs"].append(run)
        for index, (case, category, user, _) in enumerate(selected):
            event = {"source": "user", "text": user, "turn_id": f"temperature-{mode}-{repeat}-{index}"}
            try:
                await loop._decide(event, turn_history=tuple(history))
            except CapturedMessages as captured:
                messages, json_output = captured.messages, captured.json_output
            else:
                raise AssertionError("production message capture was not reached")
            observation = {}

            def transport(sent, **kwargs):
                native_observation.current = observation
                observation["sent_temperature"] = kwargs["temperature"]
                observation["messages_sha256"] = digest(sent)
                def observe(native):
                    for key in ("response_model", "finish_reason"):
                        observation[key] = native.get(key)
                return client.remote_llm_messages_query(sent, **kwargs, response_observer=observe)

            row = {"case": case, "category": category, "user": user,
                "input_history_sha256": digest(history), "observed": observation}
            run["turns"].append(row)
            started = time.perf_counter()
            try:
                raw = await production_query(transport, temperature)(messages, json_output=json_output)
                row["raw"] = raw
                if not raw.strip():
                    raise ValueError("No visible role reply")
                if raw.lstrip().startswith("{"):
                    decoded = json.loads(raw)
                else:
                    decoder = DelegateRoleDecoder()
                    decoder.feed(raw)
                    decoded = decoder.value()
                say = str(decoded.get("say") or "")
                visible = project_completed_role_history(say)
                row.update({"reply": visible, "action_proposed": decoded.get("action"),
                    "status": "ok", "flags": {
                        "assistant_term_in_opening": "助手" in visible[:240],
                        "ha_opening": visible.lstrip().startswith(("は？", "は?")),
                    }})
                history.extend([{"role": "user", "content": user},
                    {"role": "assistant", "content": visible}])
            except Exception as exc:
                row.update({"status": "error", "error_type": type(exc).__name__,
                    "http_status": getattr(exc, "status_code", None) or getattr(exc, "code", None),
                    "error_status": getattr(exc, "status", None)})
                run["status"] = "incomplete"
            row["elapsed_s"] = round(time.perf_counter() - started, 3)
            save()
            print(json.dumps({"mode": mode, "repeat": repeat, "temperature": temperature,
                "case": case, "prompt_variant": variant, "status": row["status"], "opening": row.get("reply", "")[:150]},
                ensure_ascii=False), flush=True)
            if args.request_interval_s:
                await asyncio.sleep(args.request_interval_s)
            if row["status"] != "ok":
                break
        if run["status"] == "running":
            run["status"] = "complete"
        await loop.close()
        save()

    try:
        for mode in args.modes:
            for repeat in range(1, args.repeats + 1):
                if args.serial:
                    for temperature in args.temperatures:
                        for variant in args.prompt_variants:
                            await run_arm(repeat, temperature, mode, variant)
                else:
                    await asyncio.gather(*(run_arm(repeat, temperature, mode, variant)
                        for temperature in args.temperatures for variant in args.prompt_variants))
        report["status"] = "complete" if all(run["status"] == "complete" for run in report["runs"]) else "incomplete"
    finally:
        connection.close()
        save()
    return 0 if report["status"] == "complete" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--credentials-env", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=("deepseek", "gemini"), default="deepseek")
    parser.add_argument("--model")
    parser.add_argument("--repeats", type=int, default=3, choices=range(1, 4))
    parser.add_argument("--temperatures", type=float, nargs="+", default=[0.0, 0.7])
    parser.add_argument("--prompt-variants", nargs="+", choices=("original", "no_ack_preface"), default=["original"])
    parser.add_argument("--modes", nargs="+", choices=("rollout", "fixed_history"), default=["rollout", "fixed_history"])
    parser.add_argument("--serial", action="store_true")
    parser.add_argument("--request-interval-s", type=float, default=0)
    args = parser.parse_args()
    if not math.isfinite(args.request_interval_s) or not 0 <= args.request_interval_s <= 30:
        parser.error("request interval must be between 0 and 30 seconds")
    if args.model is None:
        args.model = "deepseek-v4-flash" if args.provider == "deepseek" else "gemini-2.5-flash"
    if len(set(args.temperatures)) != len(args.temperatures) or any(
        not math.isfinite(value) or not 0 <= value <= 2 for value in args.temperatures
    ):
        parser.error("temperatures must be unique finite values between 0 and 2")
    if not args.live:
        print(json.dumps({"status": "prepared_not_run", "requests": args.repeats * len(args.temperatures) * len(args.prompt_variants)
            * sum(8 if mode == "rollout" else 1 for mode in args.modes),
            "fixture_sha256": digest(CASES), "tts": False}))
        return 0
    return asyncio.run(evaluate(args))


if __name__ == "__main__":
    raise SystemExit(main())