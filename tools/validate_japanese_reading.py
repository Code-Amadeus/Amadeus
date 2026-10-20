"""Offline contrastive reading gate through the actual Japanese production frontend."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.tts_text_processor import correct_pronunciation_for_tts  # noqa: E402

CASES_PATH = ROOT / "tools/data/japanese_reading_cases.json"


def target_reading(features: list[dict], surface: str) -> str | None:
    """Compare complete native nodes, including targets spanning several nodes."""
    for start in range(len(features)):
        word, reading = "", ""
        for feature in features[start:]:
            word += feature["string"]
            reading += feature["read"]
            if word == surface:
                return reading
            if not surface.startswith(word):
                break
    return None


def validate(project_root: Path) -> dict:
    import pyopenjtalk
    sys.path.insert(0, str(ROOT / "GPT_SoVITS"))
    from text import japanese
    from text.cleaner import clean_text
    from tts.japanese_reading import configure_japanese_reading
    from pyopenjtalk.utils import split_kana_mora
    from text.symbols2 import symbols

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    original = japanese.run_frontend
    collected = []

    def observe(jtalk, text):
        features = original(jtalk, text)
        collected.extend(features)
        return features

    results, summaries = {}, {}
    try:
        japanese.run_frontend = observe
        for name, enabled in (("base", False), ("context", True)):
            start = time.perf_counter()
            status = configure_japanese_reading(pyopenjtalk, project_root=project_root, enabled=enabled)
            load_seconds = time.perf_counter() - start
            if enabled and not status["ready"]:
                raise RuntimeError(status["detail"])
            observations, timings = [], []
            for case in cases:
                collected.clear()
                start = time.perf_counter()
                phones, _, normalized = clean_text(correct_pronunciation_for_tts(case["text"]), "ja", "v2")
                timings.append((time.perf_counter() - start) * 1000)
                observed = target_reading(collected, case["surface"])
                features = list(collected)
                raw = japanese.g2p(correct_pronunciation_for_tts(case["text"]))
                plain = japanese.g2p(correct_pronunciation_for_tts(case["text"]), with_prosody=False)
                phonemes = [phone for phone in raw if phone not in {"[", "]", "#"}]
                plain_matches = phonemes == [p.lower() if p in "AEIOU" else p for p in plain]
                # The existing trained frontend maps '#' to its UNK ID. Keep
                # that checkpoint contract; new vocabulary IDs would invalidate
                # the existing embedding table. Distinguish it from unknown phones.
                unsupported = sorted(set(raw) - set(symbols) - {"#"})
                count_mora = lambda value: len(split_kana_mora(value.replace("’", "")))
                mismatches = [{"surface": f["string"], "pron": f["pron"], "declared": f["mora_size"],
                               "actual": count_mora(f["pron"])} for f in features
                              if f["mora_size"] and count_mora(f["pron"]) != f["mora_size"]]
                observations.append({**case, "observed": observed, "passed": observed in case["readings"],
                                     "phones": phones, "normalized": normalized,
                                     "unsupported_phonemes": unsupported,
                                     "plain_frontend_matches": plain_matches,
                                     "mora_mismatches": mismatches})
            results[name] = observations
            summaries[name] = {"passed": sum(r["passed"] for r in observations), "cases": len(cases),
                               "load_seconds": load_seconds, "median_ms": statistics.median(timings),
                               "p95_ms": sorted(timings)[int(.95 * (len(timings) - 1))]}
        regressions = [after["text"] for before, after in zip(results["base"], results["context"])
                       if before["passed"] and not after["passed"]]
        failures = [r for r in results["context"] if not r["passed"] or r["mora_mismatches"]
                    or r["unsupported_phonemes"] or not r["plain_frontend_matches"]]
        return {"summary": summaries, "regressions": regressions, "failures": failures, "results": results,
                "limitations": "Synthetic lexical reading and native prosody checks; not exhaustive or waveform/listening acceptance."}
    finally:
        japanese.run_frontend = original
        configure_japanese_reading(pyopenjtalk, project_root=project_root, enabled=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = validate(args.project_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "failures": len(report["failures"]), "regressions": report["regressions"]}))
    raise SystemExit(bool(report["failures"] or report["regressions"]))
