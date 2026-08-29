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

from vn_player.llm_client import VNLLMClient
from vn_player.schemas import VNProfile


GOLD_TARGETS: dict[str, dict[str, Any]] = {
    "a0_010_0389": {
        "role": "pre-beat setup",
        "designer_decision": "hold",
        "designer_ideal": "这里先别抢话；下一句如果把怪谈从“不可信”翻成“真货”，再反应。",
        "should_write_fact": False,
    },
    "a0_010_0390": {
        "role": "seven mysteries become real",
        "designer_decision": "speak",
        "designer_ideal": "她咬住了“真货”这个词。[EMO preset=thinking dur=8s] 这不是都市传说介绍，是把民俗推成可验证规则。",
        "should_write_fact": True,
    },
    "a0_010_0416": {
        "role": "first visibility probe",
        "designer_decision": "speak",
        "designer_ideal": "“能看到我”本身成了条件。[EMO preset=thinking dur=8s] 这里要先记成可见性规则，而不是普通寒暄。",
        "should_write_fact": True,
    },
    "a0_020_0008": {
        "role": "menu/topic label",
        "designer_decision": "silence",
        "designer_ideal": "这是 UI/话题标签，只能作为导航提示，不该发言，也不该入证据表。",
        "should_write_fact": False,
    },
    "a0_020_0016": {
        "role": "menu/topic label with clue words",
        "designer_decision": "silence",
        "designer_ideal": "虽然含有“复活秘术”，但它是话题标签；压缩为 attention cue，不写硬证据。",
        "should_write_fact": False,
    },
    "a0_020_0458": {
        "role": "visibility rule continuation",
        "designer_decision": "hold",
        "designer_ideal": "这里可以不重复发言，但应把“兴家也能看到”接到可见性规则节点上。",
        "should_write_fact": True,
    },
    "a0_020_0464": {
        "role": "belief gates visibility",
        "designer_decision": "hold",
        "designer_ideal": "不要重复吐槽；后台写入“怀疑灵异的人即使有天赋也看不见”这个规则候选。",
        "should_write_fact": True,
    },
    "a0_020_0474": {
        "role": "revival technique thread starts",
        "designer_decision": "speak",
        "designer_ideal": "复活秘术先别当成噱头。[EMO preset=thinking dur=8s] 现在要拆的是条件、代价，以及谁在引导这个话题。",
        "should_write_fact": True,
    },
    "a0_020_0495": {
        "role": "revival existence plus belief",
        "designer_decision": "speak",
        "designer_ideal": "她说“实际存在”，但理由是“我相信”。[EMO preset=thinking dur=8s] 这更像信念条件，不是证明。",
        "should_write_fact": True,
    },
    "a0_020_0501": {
        "role": "seven count anomaly",
        "designer_decision": "hold",
        "designer_ideal": "这里别急着讲话；后台记录“七大不可思议”和约九个故事数量不一致。",
        "should_write_fact": True,
    },
    "a0_020_0505": {
        "role": "count anomaly explanation",
        "designer_decision": "silence",
        "designer_ideal": "这个解释暂时降低矛盾强度，但不消除数量异常；更新同一个节点即可。",
        "should_write_fact": True,
    },
}


def _load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chunk in report.get("chunks") or []:
        lines = {line.get("script_id"): line for line in (chunk.get("snapshot") or {}).get("chunk_lines") or []}
        for reaction in (chunk.get("snapshot") or {}).get("chunk_reactions") or []:
            script_id = str(reaction.get("script_id") or "")
            line = dict(lines.get(script_id) or {})
            rows.append(
                {
                    "script_id": script_id,
                    "line": line,
                    "text": str(line.get("text") or ""),
                    "speaker": str(line.get("speaker") or ""),
                    "actual_decision": reaction.get("decision"),
                    "actual_reason": reaction.get("reason"),
                    "actual_speak": reaction.get("speak") or "",
                    "actual_kind": ((reaction.get("attention") or {}).get("current_kind") or ""),
                    "actual_density": ((reaction.get("attention") or {}).get("density") or ""),
                    "runtime_interpretation": {
                        "kind": ((reaction.get("attention") or {}).get("current_kind") or ""),
                        "density": ((reaction.get("attention") or {}).get("density") or ""),
                        "route": ((reaction.get("attention") or {}).get("route") or {}),
                        "note": "Local classification only; does not include actual reaction text or future content.",
                    },
                    "fact_count": len(reaction.get("fact_extractor_applied") or []),
                    "reasoner_count": len(reaction.get("reasoner_applied") or []),
                    "summary_count": len(reaction.get("summary_applied") or []),
                    "lookahead_target": reaction.get("lookahead_target") or "",
                }
            )
    return rows


def _recent_context(rows: list[dict[str, Any]], script_id: str, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({"script_id": row["script_id"], "speaker": row["speaker"], "text": row["text"]})
        if row["script_id"] == script_id:
            break
    return out[-max(1, limit) :]


def _model_prompt(target: dict[str, Any], row: dict[str, Any], recent: list[dict[str, Any]]) -> list[dict[str, str]]:
    system = """你是 Amadeus VN Player 的理想反应评估器。

你要扮演“理想的 Kurisu 即时反应导演”，但只允许使用已经显示的文本。不要使用未来剧情。不要写推理过程。

目标：判断当前台词是否值得 Kurisu 说话，还是只做后台 context 更新。Kurisu 可以犀利、怀疑、带一点吐槽，但必须短，不要像证据表朗读。

返回 JSON only:
{
  "decision": "speak | hold | silence",
  "speak_text": "",
  "emotion_intent": "thinking | surprised | serious_speaking | normal",
  "should_write_fact": true,
  "fact_claim": "",
  "why": ""
}
"""
    payload = {
        "current": {
            "script_id": row["script_id"],
            "speaker": row["speaker"],
            "text": row["text"],
        },
        "local_runtime_interpretation": row.get("runtime_interpretation") or {},
        "recent_displayed_context": recent,
        "constraints": {
            "no_future_text": True,
            "not_every_line_needs_reaction": True,
            "fact_claim_must_be_grounded_in_current_or_recent_text": True,
            "speak_text_should_be_one_or_two_short_sentences": True,
        },
        "evaluation_note": "不要参考 runtime 实际输出；这是独立 ideal response。local_runtime_interpretation 是本地分类信号，不是实际反应。",
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


async def _model_ideal(profile: VNProfile, target: dict[str, Any], row: dict[str, Any], recent: list[dict[str, Any]]) -> dict[str, Any]:
    client = VNLLMClient(profile)
    parsed, raw = await client.complete_json(_model_prompt(target, row, recent), lane="reaction_gold", max_tokens=650, temperature=0.35)
    if parsed:
        return parsed
    return {"decision": "error", "speak_text": "", "should_write_fact": False, "fact_claim": "", "why": str(raw)[:500]}


def _diagnose(target: dict[str, Any], row: dict[str, Any], model: dict[str, Any] | None) -> list[str]:
    notes: list[str] = []
    desired = str(target.get("designer_decision") or "")
    actual = str(row.get("actual_decision") or "")
    if desired != actual:
        if desired == "speak" and actual != "speak":
            notes.append("routing_or_budget_miss: designer expected speech")
        elif desired != "speak" and actual == "speak":
            notes.append("overreaction: designer expected no immediate speech")
        else:
            notes.append(f"cadence_diff: designer={desired}, actual={actual}")
    speak = str(row.get("actual_speak") or "")
    generic_markers = ["这个说法先记下来", "这句话有点别扭", "情绪波动不太自然", "信息密度突然上来了"]
    if speak and any(marker in speak for marker in generic_markers):
        notes.append("style_gap: fallback speech is generic")
    if "复活秘术" in row.get("text", "") and "真货" in speak:
        notes.append("template_mismatch: revival line used folklore-real template")
    should_fact = bool(target.get("should_write_fact"))
    has_fact = int(row.get("fact_count") or 0) > 0
    if should_fact and not has_fact:
        notes.append("context_gap: expected fact/evidence update")
    if not should_fact and has_fact:
        notes.append("context_pollution: fact written where designer expected compression")
    if model:
        model_decision = str(model.get("decision") or "")
        if model_decision and model_decision != "error" and model_decision != actual:
            notes.append(f"model_actual_gap: model_ideal={model_decision}, actual={actual}")
        if bool(model.get("should_write_fact")) != should_fact:
            notes.append(
                f"model_designer_fact_disagreement: model={bool(model.get('should_write_fact'))}, designer={should_fact}"
            )
        elif model.get("should_write_fact") and not has_fact:
            notes.append("model_context_gap: model also wanted fact update")
    return notes or ["ok_or_minor"]


def _render_md(result: dict[str, Any]) -> str:
    lines = [
        "# VN Player Reaction Gold Comparison",
        "",
        f"Report: `{result['source_report']}`",
        f"LLM ideal enabled: `{result['llm_ideal_enabled']}`",
        "",
        "| script_id | line | designer ideal | model ideal | actual | diagnosis |",
        "|---|---|---|---|---|---|",
    ]
    for item in result["items"]:
        model = item.get("model_ideal") or {}
        model_text = ""
        if model:
            model_text = f"{model.get('decision', '')}: {model.get('speak_text') or model.get('fact_claim') or model.get('why', '')}"
        actual = f"{item['actual']['decision']}: {item['actual'].get('speak') or item['actual'].get('reason', '')}"
        lines.append(
            "| {sid} | {line} | {designer} | {model} | {actual} | {diag} |".format(
                sid=item["script_id"],
                line=_cell(item["text"]),
                designer=_cell(f"{item['designer']['decision']}: {item['designer']['ideal']}"),
                model=_cell(model_text or "(not run)"),
                actual=_cell(actual),
                diag=_cell(", ".join(item["diagnosis"])),
            )
        )
    lines.extend(
        [
            "",
            "## Aggregate",
            "",
            f"- Targets: {len(result['items'])}",
            f"- Exact designer decision matches: {result['aggregate']['designer_decision_matches']}",
            f"- Fact expectation matches: {result['aggregate']['fact_expectation_matches']}",
            f"- Generic style gaps: {result['aggregate']['generic_style_gaps']}",
            f"- Context gaps: {result['aggregate']['context_gaps']}",
            f"- Model/actual decision gaps: {result['aggregate']['model_actual_decision_gaps']}",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _cell(value: Any) -> str:
    text = str(value or "").replace("\n", " ").replace("|", " / ")
    return text[:220] + ("..." if len(text) > 220 else "")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    report_path = Path(args.report_json).resolve()
    report = _load_report(report_path)
    rows = _flatten(report)
    by_id = {row["script_id"]: row for row in rows}
    targets = [sid for sid in (args.script_ids or sorted(GOLD_TARGETS)) if sid in GOLD_TARGETS and sid in by_id]

    profile = VNProfile.from_params(
        {
            "session_id": "reaction_gold_eval",
            "game_id": args.game_id,
            "game_title": args.game_title,
            "game_genre": args.game_genre,
            "prompt_pack": args.prompt_pack,
            "output_language": "zh",
            "provider": args.provider,
            "model": args.model,
            "base_url": args.base_url,
        }
    )

    items: list[dict[str, Any]] = []
    for sid in targets:
        target = GOLD_TARGETS[sid]
        row = by_id[sid]
        model: dict[str, Any] | None = None
        if args.llm:
            model = await _model_ideal(profile, target, row, _recent_context(rows, sid, args.context_lines))
        diagnosis = _diagnose(target, row, model)
        items.append(
            {
                "script_id": sid,
                "role": target.get("role", ""),
                "text": row["text"],
                "designer": {
                    "decision": target["designer_decision"],
                    "ideal": target["designer_ideal"],
                    "should_write_fact": bool(target.get("should_write_fact")),
                },
                "model_ideal": model or {},
                "actual": {
                    "decision": row["actual_decision"],
                    "reason": row["actual_reason"],
                    "kind": row["actual_kind"],
                    "density": row["actual_density"],
                    "speak": row["actual_speak"],
                    "fact_count": row["fact_count"],
                    "reasoner_count": row["reasoner_count"],
                    "summary_count": row["summary_count"],
                },
                "diagnosis": diagnosis,
            }
        )

    aggregate = {
        "designer_decision_matches": sum(1 for item in items if item["designer"]["decision"] == item["actual"]["decision"]),
        "fact_expectation_matches": sum(
            1 for item in items if bool(item["designer"]["should_write_fact"]) == (int(item["actual"]["fact_count"] or 0) > 0)
        ),
        "generic_style_gaps": sum(1 for item in items if any("style_gap" in note for note in item["diagnosis"])),
        "context_gaps": sum(1 for item in items if any("context_gap" in note for note in item["diagnosis"])),
        "model_actual_decision_gaps": sum(1 for item in items if any("model_actual_gap" in note for note in item["diagnosis"])),
    }
    result = {
        "schema_version": "vn.reaction_gold.v1",
        "source_report": str(report_path),
        "llm_ideal_enabled": bool(args.llm),
        "context_lines": args.context_lines,
        "items": items,
        "aggregate": aggregate,
    }
    out_dir = Path(args.output_dir).resolve() if args.output_dir else report_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "reaction_gold_comparison.json"
    md_path = out_dir / "reaction_gold_comparison.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_md(result), encoding="utf-8")
    result["output_json"] = str(json_path)
    result["output_markdown"] = str(md_path)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare VN Player actual reactions against designer/model ideal reactions.")
    parser.add_argument("--report-json", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--script-ids", nargs="*", default=[])
    parser.add_argument("--context-lines", type=int, default=24)
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--provider", default=os.environ.get("VN_LLM_PROVIDER", os.environ.get("LLM_PROVIDER", "deepseek")))
    parser.add_argument("--model", default=os.environ.get("VN_LLM_MODEL", "deepseek-v4-flash"))
    parser.add_argument("--base-url", default=os.environ.get("VN_LLM_BASE_URL", ""))
    parser.add_argument("--game-id", default="paranormasight_the_mermaids_curse")
    parser.add_argument("--game-title", default="Paranormasight: The Mermaid's Curse")
    parser.add_argument("--game-genre", default="mystery")
    parser.add_argument("--prompt-pack", default="mystery")
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    print(
        json.dumps(
            {
                "items": len(result["items"]),
                "aggregate": result["aggregate"],
                "llm_ideal_enabled": result["llm_ideal_enabled"],
                "output_json": result["output_json"],
                "output_markdown": result["output_markdown"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
