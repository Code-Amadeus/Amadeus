from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vn_player.runtime import VNPlayerRuntime


DEFAULT_START_SCRIPT_ID = "a0_010_0287"


def _default_script_path(repo_root: Path) -> Path:
    return (
        repo_root.parent
        / "visual novel player"
        / "ParanormasightChsLocalization"
        / "texts"
        / "zh_Hans"
        / "Hazy_Script.txt"
    )


def _configure_env(args: argparse.Namespace) -> None:
    if args.no_llm:
        os.environ["VN_LLM_ENABLED"] = "0"
        os.environ["VN_IMMEDIATE_LLM_ENABLED"] = "0"
        os.environ["VN_LOOKAHEAD_LLM_ENABLED"] = "0"
        os.environ["VN_REASONER_LLM_ENABLED"] = "0"
        os.environ["VN_SUMMARY_LLM_ENABLED"] = "0"
        return

    os.environ["VN_LLM_ENABLED"] = "1"
    os.environ["VN_IMMEDIATE_LLM_ENABLED"] = "1" if args.immediate_llm else "0"
    os.environ["VN_LOOKAHEAD_LLM_ENABLED"] = "1" if args.lookahead_llm else "0"
    os.environ["VN_REASONER_LLM_ENABLED"] = "1" if args.reasoner_llm else "0"
    os.environ["VN_SUMMARY_LLM_ENABLED"] = "1" if args.summary_llm else "0"
    os.environ["VN_REASONER_EVERY_LINES"] = str(max(1, args.reasoner_every))
    os.environ["VN_SUMMARY_EVERY_LINES"] = str(max(1, args.summary_every))


def _line_window(runtime: VNPlayerRuntime, start_script_id: str, line_count: int) -> list[Any]:
    start = runtime.script_index._by_id.get(start_script_id)
    if start is None:
        raise ValueError(f"start script id not found: {start_script_id}")
    lines = runtime.script_index.lines
    return lines[start.order : min(len(lines), start.order + max(1, line_count))]


def _compact_line(line: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": line.get("seq"),
        "line_id": line.get("line_id"),
        "script_id": line.get("script_id"),
        "speaker": line.get("speaker"),
        "text": line.get("text"),
    }


def _compact_reaction(result: dict[str, Any]) -> dict[str, Any]:
    reaction = result.get("reaction") or {}
    lookahead = result.get("lookahead") or {}
    return {
        "script_id": (result.get("line") or {}).get("script_id"),
        "decision": reaction.get("decision"),
        "reason": reaction.get("reason_label"),
        "importance": reaction.get("importance"),
        "speak": (reaction.get("speak") or {}).get("text", ""),
        "attention": {
            "density": (result.get("attention") or {}).get("density"),
            "current_kind": (result.get("attention") or {}).get("current_kind"),
            "route": (result.get("attention") or {}).get("route"),
            "budget": (result.get("attention") or {}).get("budget"),
            "lane_focus": (result.get("attention") or {}).get("lane_focus"),
            "retrospective_bias": (result.get("attention") or {}).get("retrospective_bias"),
            "reasons": (result.get("attention") or {}).get("reasons"),
        },
        "lookahead_source": lookahead.get("source"),
        "lookahead_target": ((lookahead.get("reaction_plan") or [{}])[0]).get("target_script_id", ""),
        "fact_extractor_applied": (result.get("fact_extractor") or {}).get("applied", []),
        "character_modeler_applied": (result.get("character_modeler") or {}).get("applied", []),
        "reasoner_applied": (result.get("reasoner") or {}).get("applied", []),
        "summary_applied": (result.get("summary") or {}).get("applied", []),
    }


def _snapshot(runtime: VNPlayerRuntime, chunk_results: list[dict[str, Any]], chunk_lines: list[dict[str, Any]]) -> dict[str, Any]:
    store = runtime.store
    assert store is not None
    return {
        "chunk_lines": [_compact_line(line) for line in chunk_lines],
        "chunk_reactions": [_compact_reaction(result) for result in chunk_results],
        "runtime_scene_summary": store.scene_summary(),
        "runtime_story_summary_log": store.story_summary_log()[-8:],
        "runtime_evidence_nodes": store.evidence_nodes()[-12:],
        "runtime_verifier_feedback": store.verifier_feedback()[-12:],
        "runtime_hypotheses": store.hypotheses()[-12:],
        "runtime_characters": store.characters(),
    }


def _observer_messages(profile: Any, snapshot: dict[str, Any], chunk_index: int) -> list[dict[str, str]]:
    system = f"""You are evaluating Amadeus VN Player long-run context quality.

Game: {profile.game_title}
Genre: {profile.game_genre}

Use only the displayed lines and runtime artifacts in the snapshot. Do not use future knowledge.

Return valid JSON only:
{{
  "schema_version": "vn.longrun_eval.v1",
  "chunk_index": {chunk_index},
  "scores": {{
    "summary_coverage": 1,
    "summary_precision": 1,
    "hypothesis_quality": 1,
    "evidence_grounding": 1,
    "character_modeling": 1,
    "reaction_timing": 1,
    "spoiler_safety": 1,
    "retrieval_usefulness": 1
  }},
  "chunk_summary": "neutral summary of what happened in this chunk",
  "strengths": [],
  "issues": [],
  "missing_context": [],
  "good_hypotheses": [],
  "bad_or_weak_hypotheses": [],
  "recommended_tuning": [],
  "notes_for_next_chunk": []
}}

Scoring: 1 = poor, 3 = usable, 5 = excellent.
Be strict. The goal is to improve the runtime, not to flatter it.
"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False, indent=2)},
    ]


async def _observe_chunk(runtime: VNPlayerRuntime, snapshot: dict[str, Any], chunk_index: int, use_llm: bool) -> dict[str, Any]:
    if not use_llm or runtime.llm is None or runtime.profile is None:
        return _heuristic_observer(snapshot, chunk_index)
    parsed, raw = await runtime.llm.complete_json(
        _observer_messages(runtime.profile, snapshot, chunk_index),
        lane="longrun_observer",
        max_tokens=1200,
        temperature=0.2,
    )
    store = runtime.store
    if store is not None:
        store.record_model_call(
            "longrun_observer",
            {"snapshot_metrics": _snapshot_metrics(snapshot), "snapshot": _truncate_snapshot(snapshot)},
            parsed or raw,
            ok=parsed is not None,
        )
    if parsed:
        return parsed
    fallback = _heuristic_observer(snapshot, chunk_index)
    fallback["raw_error"] = raw
    return fallback


def _heuristic_observer(snapshot: dict[str, Any], chunk_index: int) -> dict[str, Any]:
    reactions = snapshot.get("chunk_reactions") or []
    summary_log = snapshot.get("runtime_story_summary_log") or []
    summary = summary_log[-1] if summary_log else (snapshot.get("runtime_scene_summary") or {})
    hypotheses = snapshot.get("runtime_hypotheses") or []
    evidence_nodes = snapshot.get("runtime_evidence_nodes") or []
    verifier_feedback = snapshot.get("runtime_verifier_feedback") or []
    speaks = [item for item in reactions if item.get("decision") == "speak"]
    holds = [item for item in reactions if item.get("decision") == "hold"]
    return {
        "schema_version": "vn.longrun_eval.v1",
        "chunk_index": chunk_index,
        "scores": {
            "summary_coverage": 3 if summary.get("summary") else 1,
            "summary_precision": 3,
            "hypothesis_quality": 3 if hypotheses else 1,
            "evidence_grounding": 4 if evidence_nodes else (3 if any(h.get("evidence_line_ids") for h in hypotheses if isinstance(h, dict)) else 2),
            "character_modeling": 3 if (snapshot.get("runtime_characters") or {}).get("characters") else 1,
            "reaction_timing": 4 if holds or speaks else 2,
            "spoiler_safety": 4,
            "retrieval_usefulness": 3 if hypotheses or summary.get("summary") else 1,
        },
        "chunk_summary": summary.get("summary") or "No runtime summary yet.",
        "strengths": ["rules produced sparse reactions"] if speaks or holds else [],
        "issues": [] if summary.get("summary") else ["story_summary_log is still empty"],
        "missing_context": [],
        "good_hypotheses": [h.get("claim") for h in hypotheses[-3:] if isinstance(h, dict) and h.get("claim")],
        "bad_or_weak_hypotheses": [
            f.get("patch_id") for f in verifier_feedback[-3:] if isinstance(f, dict) and str(f.get("status", "")).startswith(("weak", "rejected"))
        ],
        "recommended_tuning": [],
        "notes_for_next_chunk": [],
    }


def _truncate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    out = dict(snapshot)
    out["chunk_lines"] = list(out.get("chunk_lines") or [])[-12:]
    out["chunk_reactions"] = list(out.get("chunk_reactions") or [])[-12:]
    out["runtime_hypotheses"] = list(out.get("runtime_hypotheses") or [])[-8:]
    out["runtime_evidence_nodes"] = list(out.get("runtime_evidence_nodes") or [])[-8:]
    out["runtime_verifier_feedback"] = list(out.get("runtime_verifier_feedback") or [])[-8:]
    out["runtime_story_summary_log"] = list(out.get("runtime_story_summary_log") or [])[-4:]
    return out


def _snapshot_metrics(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "evaluation_only": True,
        "production_lane": False,
        "call_shape": "chunk_snapshot_for_observer_not_runtime",
        "chunk_lines": len(snapshot.get("chunk_lines") or []),
        "chunk_reactions": len(snapshot.get("chunk_reactions") or []),
        "runtime_story_summary_segments": len(snapshot.get("runtime_story_summary_log") or []),
        "runtime_evidence_nodes": len(snapshot.get("runtime_evidence_nodes") or []),
        "runtime_hypotheses": len(snapshot.get("runtime_hypotheses") or []),
    }


def _llm_input_audit(session_root: Path) -> dict[str, Any]:
    path = session_root / "model_calls.jsonl"
    by_lane: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return {
            "note": "no LLM calls were recorded",
            "production_runtime_shape": "vn.line is still processed one displayed line at a time",
            "by_lane": {},
        }
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(raw)
        except Exception:
            continue
        lane = str(item.get("lane") or "unknown")
        req = item.get("request") or {}
        metrics = req.get("context_metrics") or req.get("snapshot_metrics") or _infer_request_metrics(req)
        bucket = by_lane.setdefault(
            lane,
            {
                "calls": 0,
                "production_lane": lane not in {"longrun_observer"},
                "evaluation_only": lane in {"longrun_observer"},
                "max_current_line_count": 0,
                "max_short_memory_lines": 0,
                "max_future_window_lines": 0,
                "max_chunk_lines": 0,
                "max_story_summary_segments": 0,
                "max_hypothesis_nodes": 0,
                "max_evidence_nodes": 0,
                "call_shapes": [],
            },
        )
        bucket["calls"] += 1
        _max_into(bucket, "max_current_line_count", metrics.get("current_line_count"))
        _max_into(bucket, "max_short_memory_lines", metrics.get("short_memory_lines"))
        _max_into(bucket, "max_future_window_lines", metrics.get("future_window_lines"))
        _max_into(bucket, "max_chunk_lines", metrics.get("chunk_lines"))
        _max_into(bucket, "max_story_summary_segments", metrics.get("story_summary_segments") or metrics.get("runtime_story_summary_segments"))
        _max_into(bucket, "max_hypothesis_nodes", metrics.get("hypothesis_nodes") or metrics.get("runtime_hypotheses"))
        _max_into(bucket, "max_evidence_nodes", metrics.get("evidence_nodes") or metrics.get("runtime_evidence_nodes"))
        shape = str(metrics.get("production_call_shape") or metrics.get("call_shape") or "")
        if shape and shape not in bucket["call_shapes"]:
            bucket["call_shapes"].append(shape)
    return {
        "production_runtime_shape": "100-line evaluation feeds vn.line sequentially; production lanes receive one current line plus local context, not a 100-line chat batch.",
        "lookahead_policy": "lookahead may inspect a bounded future window for timing, then runtime sanitizes it into abstract hints before immediate/reasoner lanes see it.",
        "observer_policy": "longrun_observer is an evaluation-only judge over chunk snapshots; it is not part of the runtime reaction loop.",
        "by_lane": by_lane,
    }


def _infer_request_metrics(request: dict[str, Any]) -> dict[str, Any]:
    ctx = request.get("context_pack") or request.get("context") or request.get("snapshot") or {}
    if not isinstance(ctx, dict):
        return {}
    return {
        "current_line_count": 1 if ctx.get("current_line") else 0,
        "short_memory_lines": len(ctx.get("short_memory") or []),
        "future_window_lines": len(ctx.get("future_window") or []),
        "chunk_lines": len(ctx.get("chunk_lines") or []),
        "story_summary_segments": len(ctx.get("story_summary_log") or ctx.get("runtime_story_summary_log") or []),
        "hypothesis_nodes": len(ctx.get("hypotheses") or ctx.get("runtime_hypotheses") or []),
        "evidence_nodes": len(ctx.get("evidence_nodes") or ctx.get("runtime_evidence_nodes") or []),
    }


def _max_into(bucket: dict[str, Any], key: str, value: Any) -> None:
    try:
        number = int(value or 0)
    except Exception:
        number = 0
    bucket[key] = max(int(bucket.get(key) or 0), number)


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# VN Player Long-Run Evaluation",
        "",
        f"Session: `{report['session_id']}`",
        f"Script window: `{report['start_script_id']}` + {report['line_count']} lines",
        f"Runtime LLM: `{report['runtime_llm']}`",
        f"Observer LLM: `{report['observer_llm']}`",
        "",
        "## Aggregate",
        "",
        f"- Lines processed: {report['processed_lines']}",
        f"- Reactions: {report['reaction_counts']}",
        f"- Attention: {report['attention_counts']}",
        f"- Speak count: {report['speak_count']}",
        f"- Hold count: {report['hold_count']}",
        f"- Final hypotheses: {report['final_hypothesis_count']}",
        f"- Final evidence nodes: {report['final_evidence_count']}",
        f"- Final characters: {report['final_character_count']}",
        f"- Story summary segments: {report['final_story_summary_count']}",
        f"- LLM input audit: {report.get('llm_input_audit', {})}",
        "",
        "## Final Story Summary Log",
        "",
    ]
    story_log = report.get("final_story_summary_log") or []
    if story_log:
        for entry in story_log[-8:]:
            lines.append(f"- `{entry.get('source_script_id', '')}` {entry.get('summary', '')}")
    else:
        lines.append("(empty)")
    lines.extend(["", "## Chunk Reports", ""])
    for chunk in report.get("chunks") or []:
        observer = chunk.get("observer") or {}
        scores = observer.get("scores") or {}
        lines.append(f"### Chunk {chunk['chunk_index']}")
        lines.append("")
        lines.append(f"- Lines: `{chunk['from_script_id']}` -> `{chunk['to_script_id']}`")
        lines.append(f"- Decisions: {chunk['decision_counts']}")
        if scores:
            lines.append(f"- Scores: {scores}")
        lines.append("")
        lines.append("Summary:")
        lines.append("")
        lines.append(str(observer.get("chunk_summary") or "(empty)"))
        lines.append("")
        for key, title in [
            ("strengths", "Strengths"),
            ("issues", "Issues"),
            ("recommended_tuning", "Recommended Tuning"),
            ("notes_for_next_chunk", "Notes For Next Chunk"),
        ]:
            values = observer.get(key) or []
            if values:
                lines.append(f"{title}:")
                for value in values:
                    lines.append(f"- {value}")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _count_decisions(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        decision = str((result.get("reaction") or {}).get("decision") or "unknown")
        counts[decision] = counts.get(decision, 0) + 1
    return counts


def _count_attention(results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {
        "density": {},
        "kind": {},
        "fact_extractor": {},
        "character_modeler": {},
        "reasoner": {},
        "summary": {},
        "retrospective_active": {},
    }
    for result in results:
        attention = result.get("attention") or {}
        route = attention.get("route") or {}
        for bucket, value in [
            ("density", attention.get("density")),
            ("kind", attention.get("current_kind")),
            ("fact_extractor", route.get("fact_extractor")),
            ("character_modeler", route.get("character_modeler")),
            ("reasoner", route.get("reasoner")),
            ("summary", route.get("summary")),
            ("retrospective_active", "active" if (attention.get("retrospective_bias") or {}) else "inactive"),
        ]:
            key = str(value or "unknown")
            counts[bucket][key] = counts[bucket].get(key, 0) + 1
    return counts


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    _configure_env(args)
    repo_root = ROOT
    project_root = Path(args.project_root).resolve() if args.project_root else repo_root
    script_path = Path(args.script_path).resolve() if args.script_path else _default_script_path(repo_root)

    emitted: list[tuple[str, dict[str, Any]]] = []
    speaks: list[dict[str, Any]] = []

    async def emit(method: str, params: dict[str, Any]) -> None:
        emitted.append((method, params))

    async def speak(payload: dict[str, Any]) -> None:
        speaks.append(payload)

    runtime = VNPlayerRuntime(project_root, event_emit=emit, speak_callback=speak)
    await runtime.start(
        {
            "session_id": args.session_id,
            "script_path": str(script_path),
            "output_language": args.output_language,
            "game_id": args.game_id,
            "game_title": args.game_title,
            "game_genre": args.game_genre,
            "prompt_pack": args.prompt_pack,
        }
    )
    selected_lines = _line_window(runtime, args.start_script_id, args.line_count)

    all_results: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    chunk_results: list[dict[str, Any]] = []
    chunk_line_events: list[dict[str, Any]] = []

    for idx, script_line in enumerate(selected_lines, start=1):
        result = await runtime.ingest_line(
            {
                "script_id": script_line.script_id,
                "text": script_line.text,
                "speaker": script_line.speaker,
                "scene_id": script_line.scene_id,
                "metadata": {"source": "longrun_eval"},
            }
        )
        all_results.append(result)
        chunk_results.append(result)
        chunk_line_events.append(result.get("line") or {})

        if idx % args.chunk_size == 0 or idx == len(selected_lines):
            chunk_index = len(chunks) + 1
            snap = _snapshot(runtime, chunk_results, chunk_line_events)
            observer = await _observe_chunk(runtime, snap, chunk_index, args.observer_llm and not args.no_llm)
            chunks.append(
                {
                    "chunk_index": chunk_index,
                    "from_script_id": chunk_line_events[0].get("script_id", ""),
                    "to_script_id": chunk_line_events[-1].get("script_id", ""),
                    "decision_counts": _count_decisions(chunk_results),
                    "snapshot": snap,
                    "observer": observer,
                }
            )
            chunk_results = []
            chunk_line_events = []

    store = runtime.store
    assert store is not None
    final_hypotheses = store.hypotheses()
    final_evidence = store.evidence_nodes()
    final_characters = store.characters().get("characters") or []
    final_story_summary_log = store.story_summary_log()
    report = {
        "schema_version": "vn.longrun_report.v1",
        "session_id": args.session_id,
        "start_script_id": args.start_script_id,
        "line_count": args.line_count,
        "processed_lines": len(selected_lines),
        "evaluation_method": {
            "runtime_ingestion": "sequential vn.line calls",
            "production_lanes": "one current line plus local context pack",
            "not_used": "no production lane receives all evaluated lines as one chat prompt",
            "observer": "optional evaluation-only chunk judge",
            "chunk_size": args.chunk_size,
        },
        "runtime_llm": {
            "immediate": bool(args.immediate_llm and not args.no_llm),
            "lookahead": bool(args.lookahead_llm and not args.no_llm),
            "reasoner": bool(args.reasoner_llm and not args.no_llm),
            "summary": bool(args.summary_llm and not args.no_llm),
        },
        "observer_llm": bool(args.observer_llm and not args.no_llm),
        "reaction_counts": _count_decisions(all_results),
        "attention_counts": _count_attention(all_results),
        "speak_count": sum(1 for result in all_results if (result.get("reaction") or {}).get("decision") == "speak"),
        "hold_count": sum(1 for result in all_results if (result.get("reaction") or {}).get("decision") == "hold"),
        "final_scene_summary": store.scene_summary(),
        "final_story_summary_count": len(final_story_summary_log),
        "final_story_summary_log": final_story_summary_log[-20:],
        "final_evidence_count": len(final_evidence),
        "final_evidence_nodes": final_evidence[-20:],
        "final_verifier_feedback": store.verifier_feedback()[-40:],
        "final_hypothesis_count": len(final_hypotheses),
        "final_character_count": len(final_characters),
        "final_hypotheses": final_hypotheses[-20:],
        "final_characters": final_characters[-20:],
        "chunks": chunks,
    }
    report["llm_input_audit"] = _llm_input_audit(store.root)

    output_dir = store.root / "evaluations"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "longrun_report.json"
    md_path = output_dir / "longrun_report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    report["output_json"] = str(json_path)
    report["output_markdown"] = str(md_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Long-run evaluation for Amadeus VN Player mode.")
    parser.add_argument("--script-path", default="")
    parser.add_argument("--project-root", default="")
    parser.add_argument("--session-id", default="longrun_vn_player")
    parser.add_argument("--start-script-id", default=DEFAULT_START_SCRIPT_ID)
    parser.add_argument("--line-count", type=int, default=36)
    parser.add_argument("--chunk-size", type=int, default=12)
    parser.add_argument("--output-language", default="zh")
    parser.add_argument("--game-id", default="paranormasight_the_mermaids_curse")
    parser.add_argument("--game-title", default="Paranormasight: The Mermaid's Curse")
    parser.add_argument("--game-genre", default="mystery")
    parser.add_argument("--prompt-pack", default="mystery")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--immediate-llm", action="store_true")
    parser.add_argument("--lookahead-llm", action="store_true")
    parser.add_argument("--reasoner-llm", action="store_true")
    parser.add_argument("--summary-llm", action="store_true")
    parser.add_argument("--observer-llm", action="store_true")
    parser.add_argument("--reasoner-every", type=int, default=6)
    parser.add_argument("--summary-every", type=int, default=12)
    args = parser.parse_args()
    report = asyncio.run(_run(args))
    print(
        json.dumps(
            {
                "session_id": report["session_id"],
                "processed_lines": report["processed_lines"],
                "reaction_counts": report["reaction_counts"],
                "attention_counts": report["attention_counts"],
                "speak_count": report["speak_count"],
                "hold_count": report["hold_count"],
                "final_hypothesis_count": report["final_hypothesis_count"],
                "final_evidence_count": report["final_evidence_count"],
                "final_character_count": report["final_character_count"],
                "final_story_summary_count": report["final_story_summary_count"],
                "llm_input_audit": report["llm_input_audit"],
                "output_json": report["output_json"],
                "output_markdown": report["output_markdown"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
