from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


GOLDEN_WINDOWS: dict[str, dict[str, Any]] = {
    "early_mystery_orientation": {
        "start_script_id": "a0_010_0362",
        "line_count": 160,
        "why": (
            "Early-game window with low prior-context dependency: Yoko, Okitebori, Honjo Seven Mysteries, "
            "spirit perception, revival secret technique, and the seven-vs-more-than-seven anomaly."
        ),
        "soft_ranges": {
            "speak": [4, 8],
            "hold": [8, 24],
            "story_summary": [8, 14],
            "evidence": [10, 20],
            "hypotheses": [10, 24],
        },
        "critical_beats": [
            {
                "id": "seven_mysteries_real",
                "script_ids": ["a0_010_0390", "a0_010_0391", "a0_010_0392"],
                "ideal": "Kurisu notices the word 'real' and treats it as stronger than ordinary folklore.",
                "expect": {"speak_or_deep": True, "fact": True},
            },
            {
                "id": "spirit_sight_probe",
                "script_ids": ["a0_010_0416", "a0_020_0458", "a0_020_0463", "a0_020_0465"],
                "ideal": "Kurisu tracks the rule that perception/belief may gate supernatural visibility.",
                "expect": {"speak_or_deep": True, "fact": True},
            },
            {
                "id": "revival_secret_definition",
                "script_ids": ["a0_020_0474", "a0_020_0481", "a0_020_0482", "a0_020_0483", "a0_020_0490", "a0_020_0495"],
                "ideal": "Kurisu treats the revival technique as a rule cluster, not just an exciting phrase.",
                "expect": {"speak_or_deep": True, "fact": True},
            },
            {
                "id": "seven_count_anomaly",
                "script_ids": ["a0_020_0501", "a0_020_0502", "a0_020_0503", "a0_020_0505"],
                "ideal": "The reasoner notices that 'seven mysteries' may not literally mean seven.",
                "expect": {"reasoner": True, "fact": True, "speak_required": False},
            },
        ],
        "suppress_regions": [
            {
                "id": "menu_cluster",
                "script_ids": [
                    "a0_020_0001",
                    "a0_020_0002",
                    "a0_020_0003",
                    "a0_020_0008",
                    "a0_020_0009",
                    "a0_020_0010",
                    "a0_020_0011",
                    "a0_020_0012",
                    "a0_020_0013",
                    "a0_020_0014",
                    "a0_020_0015",
                    "a0_020_0016",
                    "a0_020_0017",
                    "a0_020_0074",
                    "a0_020_0075",
                    "a0_020_0143",
                    "a0_020_0144",
                    "a0_020_0195",
                    "a0_020_0196",
                    "a0_020_0221",
                    "a0_020_0225",
                ],
                "ideal": "Compress menu/topic labels. They may guide attention, but should not become hard evidence or spoken reactions.",
            }
        ],
    }
}


def _load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_reactions(report: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for chunk in report.get("chunks") or []:
        lines = {line.get("script_id"): line for line in (chunk.get("snapshot") or {}).get("chunk_lines") or []}
        for reaction in (chunk.get("snapshot") or {}).get("chunk_reactions") or []:
            item = dict(reaction)
            item["_line"] = lines.get(item.get("script_id"), {})
            out.append(item)
    return out


def _count_hits(reactions: list[dict[str, Any]], script_ids: list[str]) -> dict[str, Any]:
    selected = [item for item in reactions if item.get("script_id") in set(script_ids)]
    return {
        "decisions": [item.get("decision") for item in selected],
        "speak": any(item.get("decision") == "speak" for item in selected),
        "hold": any(item.get("decision") == "hold" for item in selected),
        "fact": any(item.get("fact_extractor_applied") for item in selected),
        "summary": any(item.get("summary_applied") for item in selected),
        "reasoner": any(item.get("reasoner_applied") for item in selected),
        "deep": any(((item.get("attention") or {}).get("route") or {}).get("reasoner") == "run_deep" for item in selected),
        "active": bool(selected),
        "items": selected,
    }


def _score_range(value: int, lo: int, hi: int) -> tuple[float, str]:
    if lo <= value <= hi:
        return 1.0, "ok"
    if value < lo:
        return max(0.0, 1.0 - (lo - value) / max(lo, 1)), f"low: {value} < {lo}"
    return max(0.0, 1.0 - (value - hi) / max(hi, 1)), f"high: {value} > {hi}"


def _normalized_speech(text: str) -> str:
    text = re.sub(r"\[EMO[^\]]*\]", "", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def score_report(report: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    reactions = _iter_reactions(report)
    score = 0.0
    weight = 0.0
    findings: list[dict[str, Any]] = []

    counts = {
        "speak": int(report.get("speak_count") or 0),
        "hold": int(report.get("hold_count") or 0),
        "story_summary": int(report.get("final_story_summary_count") or 0),
        "evidence": int(report.get("final_evidence_count") or 0),
        "hypotheses": int(report.get("final_hypothesis_count") or 0),
    }
    for key, (lo, hi) in (spec.get("soft_ranges") or {}).items():
        part, note = _score_range(counts.get(key, 0), int(lo), int(hi))
        score += part
        weight += 1.0
        findings.append({"kind": "range", "target": key, "value": counts.get(key, 0), "score": round(part, 2), "note": note})

    for beat in spec.get("critical_beats") or []:
        hit = _count_hits(reactions, beat.get("script_ids") or [])
        expect = beat.get("expect") or {}
        part = 0.0
        total = 0.0
        checks: dict[str, bool] = {}
        if expect.get("speak_or_deep"):
            checks["speak_or_deep"] = bool(hit["speak"] or hit["deep"])
            total += 1.0
            part += 1.0 if checks["speak_or_deep"] else 0.0
        if expect.get("fact"):
            checks["fact"] = bool(hit["fact"])
            total += 1.0
            part += 1.0 if checks["fact"] else 0.0
        if expect.get("reasoner"):
            checks["reasoner"] = bool(hit["reasoner"] or hit["deep"])
            total += 1.0
            part += 1.0 if checks["reasoner"] else 0.0
        if expect.get("speak_required"):
            checks["speak_required"] = bool(hit["speak"])
            total += 1.0
            part += 1.0 if checks["speak_required"] else 0.0
        beat_score = part / total if total else 1.0
        score += beat_score * 2.0
        weight += 2.0
        findings.append(
            {
                "kind": "critical_beat",
                "id": beat.get("id"),
                "score": round(beat_score, 2),
                "checks": checks,
                "decisions": hit["decisions"],
                "ideal": beat.get("ideal"),
            }
        )

    for region in spec.get("suppress_regions") or []:
        hit = _count_hits(reactions, region.get("script_ids") or [])
        speaks = [item for item in hit["items"] if item.get("decision") == "speak"]
        facts = [item for item in hit["items"] if item.get("fact_extractor_applied")]
        penalty = min(1.0, 0.5 * len(speaks) + 0.2 * len(facts))
        part = max(0.0, 1.0 - penalty)
        score += part * 1.5
        weight += 1.5
        findings.append(
            {
                "kind": "suppress_region",
                "id": region.get("id"),
                "score": round(part, 2),
                "speak_count": len(speaks),
                "fact_count": len(facts),
                "ideal": region.get("ideal"),
            }
        )

    speeches = []
    for item in reactions:
        text = _normalized_speech(item.get("speak") or "")
        if text:
            speeches.append(text)
    repeats = max((speeches.count(text) for text in set(speeches)), default=0)
    repeat_score = 1.0 if repeats <= 2 else max(0.0, 1.0 - (repeats - 2) * 0.25)
    score += repeat_score
    weight += 1.0
    findings.append({"kind": "style", "target": "repeated_speech", "max_repeat": repeats, "score": round(repeat_score, 2)})

    final_score = score / weight if weight else 0.0
    return {
        "schema_version": "vn.golden_score.v1",
        "window": spec,
        "aggregate_score": round(final_score, 3),
        "counts": counts,
        "findings": findings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Score a VN Player long-run report against a soft golden window.")
    parser.add_argument("--report-json", required=True)
    parser.add_argument("--window", default="early_mystery_orientation", choices=sorted(GOLDEN_WINDOWS))
    args = parser.parse_args()
    report = _load_report(Path(args.report_json))
    result = score_report(report, GOLDEN_WINDOWS[args.window])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
