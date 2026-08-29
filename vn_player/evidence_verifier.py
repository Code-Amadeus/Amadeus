"""Grounding verifier for VN Player fact/evidence patches."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .schemas import coerce_patch_item
from .text import normalize_for_match, strip_vn_tags


STRICT_LAYERS = {"candidate_fact", "evidence"}
SOFT_LAYERS = {"hypothesis", "interpretation", "summary"}
NARRATIVE_TARGETS = {"scene_summary", "story_summary_log"}


@dataclass
class VerificationResult:
    patch_id: str
    layer: str
    target: str
    status: str
    score: float
    messages: list[str]
    evidence_refs: list[dict[str, Any]]
    hidden_probe: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "layer": self.layer,
            "target": self.target,
            "status": self.status,
            "score": self.score,
            "messages": self.messages,
            "evidence_refs": self.evidence_refs,
            "hidden_probe": self.hidden_probe,
        }


class EvidenceVerifier:
    """Verifies that durable claims are grounded in displayed text.

    The verifier may inspect the full script index for counts/positions, but it
    never returns unseen line text. This lets it warn the reasoner without
    spoiling the immediate persona.
    """

    def __init__(self, store, script_index, *, hidden_probe: bool = True) -> None:
        self.store = store
        self.script_index = script_index
        self.hidden_probe = hidden_probe

    def verify_patches(
        self,
        patches: list[dict[str, Any]],
        *,
        source_line: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        annotated: list[dict[str, Any]] = []
        feedback: list[dict[str, Any]] = []
        for index, patch in enumerate(patches or []):
            if not isinstance(patch, dict):
                continue
            patch_copy = dict(patch)
            patch_copy["item"] = coerce_patch_item(patch_copy.get("item"))
            result = self.verify_patch(patch_copy, source_line=source_line, index=index)
            item = coerce_patch_item(patch_copy.get("item"))
            item["verification"] = result.to_dict()
            patch_copy["item"] = item
            if result.status in {"rejected_missing_evidence", "rejected_unseen_evidence", "rejected_ungrounded"}:
                patch_copy["_verification_rejected"] = True
            annotated.append(patch_copy)
            feedback.append(result.to_dict())
        if feedback:
            self.store.record_verification_feedback(feedback, source_line=source_line)
        return annotated, feedback

    def verify_patch(self, patch: dict[str, Any], *, source_line: dict[str, Any], index: int) -> VerificationResult:
        layer = str(patch.get("layer") or "hypothesis")
        target = str(patch.get("target") or "")
        item = coerce_patch_item(patch.get("item"))
        messages: list[str] = []
        if item.get("_coerced_from_malformed_item"):
            messages.append("context patch item was malformed; coerced to a weak claim")
            if layer in STRICT_LAYERS:
                layer = "hypothesis"
                patch["layer"] = layer
                messages.append("malformed strict fact/evidence patch downgraded to hypothesis")
        patch_id = str(item.get("id") or f"patch_{source_line.get('line_id')}_{index}")
        claim = _claim_text(item)
        if target in NARRATIVE_TARGETS:
            return VerificationResult(
                patch_id=patch_id,
                layer="summary",
                target=target,
                status="narrative_summary_unverified",
                score=0.0,
                messages=messages + ["append-only narrative summary; not treated as fact/evidence"],
                evidence_refs=[
                    {
                        "line_id": source_line.get("line_id"),
                        "script_id": source_line.get("script_id"),
                        "text_hash": source_line.get("text_hash"),
                        "displayed": True,
                    }
                ],
                hidden_probe={"enabled": False},
            )
        line_ids = _string_list(item.get("evidence_line_ids") or item.get("source_line_ids"))
        script_ids = _string_list(item.get("evidence_script_ids") or item.get("source_script_ids"))
        if not line_ids and source_line.get("line_id"):
            line_ids = [str(source_line.get("line_id"))]

        refs = self.store.get_lines_by_refs(line_ids=line_ids, script_ids=script_ids)
        missing_line_ids = [line_id for line_id in line_ids if line_id not in refs["by_line_id"]]
        missing_script_ids = [script_id for script_id in script_ids if script_id not in refs["by_script_id"]]
        displayed_lines = list(refs["lines"])
        hidden_probe = self._hidden_probe(claim, source_line=source_line)

        if missing_line_ids:
            messages.append(f"missing displayed evidence_line_ids: {missing_line_ids}")
        if missing_script_ids:
            messages.append(f"missing displayed evidence_script_ids: {missing_script_ids}")

        if not displayed_lines:
            status = "rejected_missing_evidence" if layer in STRICT_LAYERS else "weak_missing_evidence"
            messages.append("no displayed evidence lines available for this claim")
            return VerificationResult(patch_id, layer, target, status, 0.0, messages, [], hidden_probe)

        evidence_text = "\n".join(str(line.get("text") or "") for line in displayed_lines)
        score = _grounding_score(claim, evidence_text)

        if layer in STRICT_LAYERS and (missing_line_ids or missing_script_ids):
            status = "rejected_unseen_evidence"
        elif layer in STRICT_LAYERS and score < 0.12:
            status = "rejected_ungrounded"
            messages.append("claim has weak lexical grounding in cited displayed lines")
        elif layer in STRICT_LAYERS:
            status = "grounded_fact_candidate"
        elif score < 0.08:
            status = "weak_hypothesis_grounding"
            messages.append("hypothesis is allowed but should be treated as weak until more evidence appears")
        else:
            status = "grounded_hypothesis"

        return VerificationResult(
            patch_id=patch_id,
            layer=layer,
            target=target,
            status=status,
            score=round(score, 3),
            messages=messages,
            evidence_refs=[
                {
                    "line_id": line.get("line_id"),
                    "script_id": line.get("script_id"),
                    "text_hash": line.get("text_hash"),
                    "displayed": True,
                }
                for line in displayed_lines
            ],
            hidden_probe=hidden_probe,
        )

    def _hidden_probe(self, claim: str, *, source_line: dict[str, Any]) -> dict[str, Any]:
        if not self.hidden_probe or not claim or not getattr(self.script_index, "lines", None):
            return {"enabled": False}
        terms = _keywords(claim)[:8]
        if not terms:
            return {"enabled": True, "query_terms": [], "matches_displayed_or_past": 0, "matches_unseen": 0}
        last_order = -1
        try:
            script_id = str(source_line.get("script_id") or "")
            line = self.script_index._by_id.get(script_id)
            if line is not None:
                last_order = int(line.order)
        except Exception:
            last_order = -1
        displayed_or_past = 0
        unseen = 0
        for line in self.script_index.lines:
            text = normalize_for_match(line.text)
            if not any(term in text for term in terms):
                continue
            if last_order >= 0 and line.order > last_order:
                unseen += 1
            else:
                displayed_or_past += 1
        return {
            "enabled": True,
            "query_terms": terms,
            "matches_displayed_or_past": displayed_or_past,
            "matches_unseen": unseen,
            "unseen_text_withheld": True,
            "note": "Unseen matches are only a warning signal; their text is not exposed to the reasoner.",
        }


def _claim_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("claim"),
        item.get("statement"),
        item.get("label"),
        item.get("summary"),
        item.get("notes"),
    ]
    return " ".join(str(part) for part in parts if part).strip()


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    return []


def _keywords(text: str) -> list[str]:
    value = normalize_for_match(strip_vn_tags(str(text or "")))
    if not value:
        return []
    chunks = re.findall(r"[a-z0-9]{3,}|[\u4e00-\u9fff]{2,}", value)
    terms: list[str] = []
    for chunk in chunks:
        if len(chunk) <= 4:
            if chunk not in terms:
                terms.append(chunk)
            continue
        for idx in range(0, len(chunk) - 1):
            gram = chunk[idx : idx + 2]
            if gram not in terms:
                terms.append(gram)
    return terms[:32]


def _grounding_score(claim: str, evidence_text: str) -> float:
    terms = _keywords(claim)
    if not terms:
        return 0.0
    evidence_norm = normalize_for_match(strip_vn_tags(evidence_text))
    hits = sum(1 for term in terms if term in evidence_norm)
    return hits / max(len(terms), 1)
