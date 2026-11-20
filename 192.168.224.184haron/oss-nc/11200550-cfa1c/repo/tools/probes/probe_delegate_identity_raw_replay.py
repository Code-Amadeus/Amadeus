r"""Replay observed/raw delegation payloads with and without identity context.

Unlike the A/B/C/D role probe, this holds Main Chat output fixed.  It answers
the narrower causal question: when an unresolved role-relative payload reaches
an execution Provider, does request-scoped identity context repair the target
without overriding explicitly named non-role subjects?
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from tools.probes.probe_delegate_identity_handoff_abcd import (
    ROOT,
    _provider_query,
)
from agent_host.provider_identity import (
    MAIN_ROLE_NAME_METADATA_KEY,
    with_main_role_reference,
)
from server.inherited_role_prompt import MAIN_CONVERSATION_ROLE_NAME


DEFAULT_OUTPUT = ROOT / "runtime" / "probes" / "delegate_identity_raw_replay.json"


@dataclass(frozen=True, slots=True)
class ReplayCase:
    case_id: str
    task: str
    expected_target: str


CASES = (
    ReplayCase(
        "observed_live_payload_20260828",
        "你能做一个关于你自己的网页吗？如果需要相关的形象素材，你应该去公开的web资源查找，不要留白，然后导出到桌面",
        "makise_kurisu",
    ),
    ReplayCase(
        "raw_short_chinese_self_page",
        "给你自己做个个人网页，放桌面。",
        "makise_kurisu",
    ),
    ReplayCase(
        "raw_english_self_page",
        "Create a personal website about yourself and save it to the desktop.",
        "makise_kurisu",
    ),
    ReplayCase(
        "raw_japanese_self_page",
        "あなた自身についての個人ウェブページを作って。",
        "makise_kurisu",
    ),
    ReplayCase(
        "explicit_codex_subject_control",
        "Create a personal website about Codex.",
        "codex",
    ),
    ReplayCase(
        "explicit_einstein_subject_control",
        "Create an informative website about Albert Einstein.",
        "other",
    ),
    ReplayCase(
        "style_discretion_control",
        "Create a quantum-computing introduction page in whatever style you think is suitable.",
        "other",
    ),
)


def _run_once(case: ReplayCase, *, model: str, with_context: bool) -> dict:
    task = (
        with_main_role_reference(
            case.task,
            metadata={MAIN_ROLE_NAME_METADATA_KEY: MAIN_CONVERSATION_ROLE_NAME},
            execution_provider="codex",
        )
        if with_context
        else case.task
    )
    started = time.perf_counter()
    verdict = _provider_query(
        task,
        model=model,
        identity_context=None,
    )
    latency_ms = (time.perf_counter() - started) * 1000.0
    return {
        "target": verdict["target"],
        "reason": verdict["reason"],
        "correct": verdict["target"] == case.expected_target,
        "latency_ms": round(latency_ms, 1),
    }


def main(*, model: str, repeats: int, output: Path) -> None:
    rows: list[dict] = []
    for repeat in range(1, max(1, repeats) + 1):
        for case in CASES:
            without = _run_once(case, model=model, with_context=False)
            with_context = _run_once(case, model=model, with_context=True)
            rows.append(
                {
                    "case": asdict(case),
                    "repeat": repeat,
                    "without_identity_context": without,
                    "with_identity_context": with_context,
                }
            )
            print(
                f"repeat={repeat} case={case.case_id} "
                f"without={without['target']} with={with_context['target']}",
                flush=True,
            )

    def summarize(key: str) -> dict:
        results = [row[key] for row in rows]
        positives = [
            row[key]
            for row in rows
            if row["case"]["expected_target"] == "makise_kurisu"
        ]
        controls = [
            row[key]
            for row in rows
            if row["case"]["expected_target"] != "makise_kurisu"
        ]
        return {
            "correct": sum(bool(row["correct"]) for row in results),
            "total": len(results),
            "self_reference_correct": sum(bool(row["correct"]) for row in positives),
            "self_reference_total": len(positives),
            "explicit_control_correct": sum(bool(row["correct"]) for row in controls),
            "explicit_control_total": len(controls),
            "false_persona_injection": sum(
                row["target"] == "makise_kurisu" for row in controls
            ),
            "median_latency_ms": round(
                statistics.median(row["latency_ms"] for row in results), 1
            ),
        }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "temperature": 0.0,
        "thinking": "disabled",
        "repeats": max(1, repeats),
        "cases": len(CASES),
        "summary": {
            "without_identity_context": summarize("without_identity_context"),
            "with_identity_context": summarize("with_identity_context"),
        },
        "rows": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"report={output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    main(
        model=str(args.model),
        repeats=max(1, int(args.repeats)),
        output=args.output,
    )
