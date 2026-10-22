"""Build an independent full-source resource candidate; never install or publish.

Inputs are versioned data-workspace artifacts, not a live personal dictionary.
The package keeps the complete source and master, reviewed corrections, and
the accumulated checked supplement. Unchanged originals remain unverified.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.japanese_dictionary import KATAKANA_READING, PACK_FILES, PACK_TREE, dictionary_rows, latin_word_key, load_dictionary_pack
from tools.audit_japanese_dictionary import (
    cmu_pronunciations, final_phone_risk, native_base_hashes, native_base_word_lookup,
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def csv_bytes(rows: list[list[str]]) -> bytes:
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    return stream.getvalue().encode("utf-8")


def prepare(data_dir: Path, output_root: Path, version: str) -> dict:
    import pyopenjtalk

    destination = output_root / "assets" / PACK_TREE
    if destination.exists():
        raise ValueError("Use a fresh output root; a resource version must not be overwritten")
    review_data = (data_dir / "review.json").read_bytes()
    review = json.loads(review_data)
    screening = review["screening"]
    cmu_data = (data_dir / "cmudict.rep").read_bytes().replace(b"\r\n", b"\n")
    if sha(cmu_data) != screening["cmudict_sha256"]:
        raise ValueError("CMU screening input differs from its pinned source")
    phones = cmu_pronunciations(cmu_data)
    source_data = (data_dir / "source.csv").read_bytes()
    if sha(source_data) != review["source"]["sha256"]:
        raise ValueError("Source differs from the data workspace's pinned source")
    originals = list(dictionary_rows(source_data))
    if len(originals) != review["source"]["entry_count"]:
        raise ValueError("Full-source denominator changed")
    master = [list(row) for row in originals]
    changes = review["changes"]
    for change in changes:
        number = change["source_row"]
        if type(number) is not int or not 1 <= number <= len(master):
            raise ValueError("Correction source row outside the preserved source")
        if originals[number - 1] != change["before_columns"]:
            raise ValueError("Correction has stale source evidence")
        master[number - 1] = change["after_columns"]
    master_data = csv_bytes(master)
    overlay = review["overlay"]
    overlay_input = (data_dir / "overlay.csv").read_bytes()
    if sha(overlay_input) != overlay["sha256"]:
        raise ValueError("Reviewed overlay changed in the data workspace")
    supplement = list(dictionary_rows(overlay_input))
    native_words = frozenset(overlay["native_words"])
    readings = defaultdict(set)
    for row in master:
        readings[latin_word_key(row[0])].add(row[12])
    # Unreviewed source variants are not evidence of genuine heteronyms.
    # A reviewed winner can replace them; unresolved spellings stay deferred.
    checked = defaultdict(set)
    for row in supplement:
        checked[latin_word_key(row[0])].add(row[12])
    withheld_aliases = {word for word, values in checked.items() if len(values) != 1 and word not in native_words}
    held = review["hold_words"]
    supplement = [r for r in supplement if latin_word_key(r[0]) not in withheld_aliases and latin_word_key(r[0]) not in held]
    checked = defaultdict(set)
    for row in supplement:
        checked[latin_word_key(row[0])].add(row[12])
    for change in changes:
        row = change["after_columns"]
        checked[latin_word_key(row[0])].add(row[12])
    # Published native rows may contain two individually reviewed, acceptable
    # variants (when/without). Neither licenses an unreviewed third reading.
    winners = {word: values for word, values in checked.items() if word not in held}
    alias_words = {latin_word_key(r[0]) for r in supplement if latin_word_key(r[0]) not in native_words}
    pending = {r["source_row"] for r in changes if r["accent_evidence"] == "source_accent_retained_pending_review"}
    known = native_base_word_lookup()
    owned = {latin_word_key(r[0]) for r in originals if known(latin_word_key(r[0]), r[11])}
    suspect = {latin_word_key(row[0]) for row in master
               if final_phone_risk(row[12], phones.get(latin_word_key(row[0]), set()))}
    rejected = {latin_word_key(row[0]) for row in master
                if row[12] in review.get("rejected_readings", {}).get(latin_word_key(row[0]), ())}
    exclusions = []
    for number, row in enumerate(master, 1):
        word = latin_word_key(row[0])
        reason = ("context_sensitive" if word in held else "base_owned" if word in owned else
                  "short_token_native_owner" if word.isascii() and word.isalpha() and len(word) <= 2 else
                  "unreviewed_alternative" if (word in winners and row[12] not in winners[word]) or
                      (len(readings[word]) > 1 and word not in winners) else
                  "known_bad_reading" if word in rejected and word not in winners else
                  "cmu_final_phone_risk" if word in suspect and word not in winners else
                  "native_reading_requires_review" if not KATAKANA_READING.fullmatch(row[12]) else
                  "pending_accent" if number in pending and word not in alias_words else None)
        if reason:
            exclusions.append({"source_row": number, "word": word, "reason": reason})
    overlay_data = csv_bytes(supplement)
    retained_words = {latin_word_key(r[0]) for r in supplement}
    overlay.update(sha256=sha(overlay_data), entry_count=len(supplement),
                   native_words=sorted(native_words & retained_words))
    overlay["release_review"]["accepted_readings"] = {
        k: v for k, v in overlay["release_review"]["accepted_readings"].items() if k in retained_words}
    source = review["source"]
    records = [{"source_row": r["source_row"], "status": "reviewed_reading_correction",
                "before_columns": r["before_columns"], "after_columns": r["after_columns"],
                "reason": r["reason"], "accent_evidence": r["accent_evidence"]} for r in changes]
    metadata = {
        "format_version": 2, "version": version, "language": "ja", "source": source,
        "sha256": sha(master_data), "entry_count": len(master),
        "base_dictionary_sha256": native_base_hashes(),
        "source_review": {"unchanged_status": "original_unverified", "changes": records,
                          "runtime_exclusions": exclusions, "review_input_sha256": sha(review_data)},
        "overlay": runtime_provenance(overlay),
        "screening": screening,
        "acceptance": {"status": "candidate_not_published", "independent_review": "pending",
                       "listening": "pending", "complete_source_review": False},
        "compatibility": "Requires a loader supporting Japanese dictionary format 2; backend restart after update or rollback.",
        "limitations": ["Preserving original coverage does not certify its pronunciation accuracy.",
                        "Source-reviewed reading, lexical accent evidence and heard pronunciation are distinct.",
                        "Context-sensitive and unresolved accent rows remain stored; runtime dispositions are explicit."],
    }
    destination.mkdir(parents=True)
    for name in PACK_FILES:
        if name not in {"userdict.csv", "provenance.json", "NOTICE.md"}:
            shutil.copyfile(data_dir / name, destination / name)
    (destination / "source.csv").write_bytes(source_data)
    (destination / "userdict.csv").write_bytes(master_data)
    (destination / "overlay.csv").write_bytes(overlay_data)
    (destination / "OpenJTalk-COPYING.txt").write_bytes(
        (Path(os.fsdecode(pyopenjtalk.OPEN_JTALK_DICT_DIR)) / "COPYING").read_bytes())
    shutil.copyfile(data_dir / "NOTICE.md", destination / "NOTICE.md")
    (destination / "provenance.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    pack = load_dictionary_pack(destination)
    report = {"version": version, "pack_root": str(destination), "source_rows": len(originals),
              "working_rows": pack.entry_count, "reviewed_corrections": pack.reviewed_rows,
              "original_unverified": pack.entry_count - pack.reviewed_rows, "deleted_source_rows": 0,
              "overlay_rows": len(supplement), "overlay_words": len(retained_words),
              "withheld_ambiguous_aliases": sorted(withheld_aliases), "runtime_exclusions": len(exclusions),
              "resource_identity": pack.identity, "source_sha256": source["sha256"],
              "working_sha256": pack.sha256, "overlay_sha256": pack.overlay_sha256,
              "status": "prepared_not_installed_not_published"}
    (output_root / "preparation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report


def runtime_provenance(value):
    """Keep supporting Alkana comparisons in the data repository, not the pack."""
    if isinstance(value, dict):
        return {key: runtime_provenance(item) for key, item in value.items() if key != "alkana_reading"}
    if isinstance(value, list):
        return [runtime_provenance(item) for item in value]
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="Independent data workspace containing source.csv, review.json, overlay.csv and notices")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.data_dir, args.output_root, args.version), ensure_ascii=False, indent=2))
