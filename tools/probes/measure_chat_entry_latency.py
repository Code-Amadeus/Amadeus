"""Opt-in read-only catalog comparison and synthetic real-model first-sentence probe.

No microphone, TTS inference/playback, live Session writes or application launch.
The ledger is opened read-only. Only synthetic dialogue goes to the model.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import logging
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from types import MethodType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def old_draft_reader(revision):
    import server.work_read_model as module

    source = subprocess.check_output(
        ["git", "show", f"{revision}:server/work_read_model.py"],
        cwd=ROOT, text=True, encoding="utf-8")
    cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "WorkReadModel")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "draft_apps")
    scope = dict(vars(module))
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<baseline-draft-reader>", "exec"), scope)
    return scope["draft_apps"]


def readonly_store(path):
    from agent_host.work_ledger_store import WorkLedgerStore

    # Skip schema initialization/migration against the running application's DB.
    store = WorkLedgerStore.__new__(WorkLedgerStore)
    store.db_path = str(path.resolve())
    store._clock, store._lock, store._closed = time.time, threading.RLock(), False
    store._connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
        isolation_level=None, check_same_thread=False)
    store._connection.row_factory = sqlite3.Row
    store._connection.execute("PRAGMA query_only=ON")
    store._connection.execute("BEGIN")
    return store


async def run(args):
    from server.auip_launch import AuipLaunchCoordinator
    from server.work_ledger_coordinator import WorkLedgerCoordinator

    store = readonly_store(args.ledger)
    coordinator = WorkLedgerCoordinator(store)
    launch = AuipLaunchCoordinator(artifacts=store, work_roster=coordinator, attention=None)
    indexed = coordinator.read_model.draft_apps
    baseline = MethodType(old_draft_reader(args.baseline), coordinator.read_model)
    report = {"baseline_commit":subprocess.check_output(
        ["git", "rev-parse", args.baseline], cwd=ROOT, text=True).strip(),
        "scope":"Read-only production catalog; optional synthetic remote model to real sentence queue, no ASR/TTS/device audio.",
        "local":[], "remote":[]}

    def context(label):
        coordinator.read_model.draft_apps = baseline if label == "baseline" else indexed
        return launch.render_prompt_context(args.session_id, language="ja", include_control_contract=False)

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    try:
        original = context("baseline")
        assert context("indexed") == original, "Current candidate roster changed; do not compare unlike observations"
        report["roster_unchanged"] = True
        for index in range(args.pairs):
            for label in (("baseline", "indexed") if index % 2 == 0
                          else ("indexed", "baseline")):
                start = time.perf_counter()
                context(label)
                report["local"].append({"index":index, "label":label,
                    "ms":round((time.perf_counter() - start) * 1000, 3)})
        if args.remote:
            from core import chat_runtime
            from llm.client import remote_llm_messages_query
            from llm.prompts import get_system_prompt, finalize_system_prompt_language, wrap_user_message_for_language_lock
            from server.cooperative_delivery import query_role_messages
            from server.cooperative_provider_loop import _role_coordination_contract
            from config import settings

            report["provider"] = settings.LLM_PROVIDER
            report["model"] = settings.DEEPSEEK_MODEL_NAME if settings.LLM_PROVIDER == "deepseek" else "configured default"
            # Exercise the production parser/queue, without subtitle calls or hardware.
            chat_runtime._pre_translation_enabled = lambda:False
            chat_runtime._get_expr_ctrl = lambda:SimpleNamespace(register_sentence_actions=lambda *_:None)

            class Timing(logging.Handler):
                def __init__(self):
                    super().__init__()
                    self.rows = []

                def emit(self, record):
                    if "[MODEL-LATENCY]" in record.getMessage():
                        self.rows.append(dict(part.split("=", 1) for part in record.getMessage().split() if "=" in part))

            handler = Timing()
            logger = logging.getLogger("llm.client")
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            prompts = ["今日は穏やかに過ごしたい。一文だけ返して。", "紅茶で一息ついている。一文だけ返して。",
                "今夜は星がきれい。一文だけ感想を教えて。", "読書の休憩中。一文だけ返して。"]
            try:
                for index in range(-1, args.pairs):
                    for label in (("indexed",) if index == -1 else
                                  ("baseline", "indexed", "prepared_voice") if index % 2 == 0
                                  else ("prepared_voice", "indexed", "baseline")):
                        queue = asyncio.Queue()
                        runtime = chat_runtime.ChatRuntime()
                        runtime.configure(pending_sentence_items=queue)
                        turn = f"synthetic-{index}-{label}"
                        stream = runtime.begin_role_text_stream(turn_id=turn)
                        first_text = first_sentence = None
                        # Voice arm measures a completed utterance-local preparation.
                        # It does not simulate or claim actual microphone/ASR timing.
                        preparation = asyncio.create_task(asyncio.to_thread(context, label))
                        if label == "prepared_voice":
                            await preparation
                        start = time.perf_counter()
                        entry = await asyncio.shield(preparation)
                        prepared = time.perf_counter()
                        # Identical role information in every arm, without sending
                        # real roster titles/paths to the provider.
                        entry = entry.split("launchable_apps:\n", 1)[0] + "launchable_apps:\n- none\n[/AUIP launchable applications]"
                        frame = {"source_kind":"user", "history":[], "context":{"id":None, "provider":"pi", "available":False},
                            "retained_contexts":[], "retained_contexts_complete":True, "available_delegate_providers":[],
                            "current":{"source":"user", "turn_id":turn, "text":prompts[index % len(prompts)]}}
                        messages = [{"role":"system", "content":finalize_system_prompt_language(
                            get_system_prompt("base") + "\n\n" + _role_coordination_contract(True) + "\n\n" + entry)},
                            {"role":"user", "content":wrap_user_message_for_language_lock(json.dumps(frame, ensure_ascii=False))}]

                        async def on_text(text):
                            nonlocal first_text, first_sentence
                            if first_text is None:
                                first_text = time.perf_counter()
                            await stream.feed(text)
                            if first_sentence is None and not queue.empty():
                                first_sentence = time.perf_counter()

                        try:
                            await query_role_messages(remote_llm_messages_query, messages, on_text=on_text,
                                turn_id=turn, json_output=False, max_tokens=100, temperature=0.0, timeout=30)
                            await stream.finish()
                            if first_sentence is None and not queue.empty():
                                first_sentence = time.perf_counter()
                            if first_text is None or first_sentence is None:
                                raise RuntimeError("Sample produced no text/sentence")
                            timing = next(r for r in handler.rows if r.get("turn_id") == turn and r["stage"] == "first_text")
                            row = {"index":index, "label":label, "warmup":index == -1,
                                "entry_ms":round((prepared-start)*1000, 2), "model_first_text_ms":float(timing["ms"]),
                                "to_first_text_ms":round((first_text-start)*1000, 2),
                                "to_first_sentence_ms":round((first_sentence-start)*1000, 2),
                                "first_text_to_sentence_ms":round((first_sentence-first_text)*1000, 2)}
                            report["remote"].append(row)
                            print(json.dumps(row), flush=True)
                            save()
                        finally:
                            stream.abort()
            finally:
                logger.removeHandler(handler)
        report["local_median_ms"] = {label:round(statistics.median(r["ms"] for r in report["local"] if r["label"] == label), 3)
            for label in ("baseline", "indexed")}
        report["remote_median_ms"] = {label:{key:round(statistics.median(r[key] for r in report["remote"]
            if r["label"] == label and not r["warmup"]), 2) for key in (
                "entry_ms", "model_first_text_ms", "to_first_text_ms", "to_first_sentence_ms", "first_text_to_sentence_ms")}
            for label in ("baseline", "indexed", "prepared_voice")} if args.remote else {}
        save()
        print(json.dumps({"local":report["local_median_ms"], "remote":report["remote_median_ms"]}), flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--baseline", required=True, help="Explicit pre-fix Git revision")
    parser.add_argument("--pairs", type=int, default=6)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.pairs < 1:
        parser.error("--pairs must be positive")
    asyncio.run(run(arguments))
