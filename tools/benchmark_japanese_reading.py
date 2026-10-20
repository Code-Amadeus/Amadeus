"""Measure installed context reading on an explicit local JSON sentence list, offline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def benchmark(corpus: Path, project_root: Path) -> dict:
    import psutil
    import pyopenjtalk
    from tts.japanese_reading import configure_japanese_reading
    from tools.tts_text_processor import correct_pronunciation_for_tts

    # Establish the base state before importing the production frontend too.
    # Set ENABLE_JAPANESE_CONTEXT_READING=false in this benchmark's environment
    # so its normal import cannot load an installed model before measurement.
    from config.settings import ENABLE_JAPANESE_CONTEXT_READING
    if ENABLE_JAPANESE_CONTEXT_READING:
        raise ValueError("Run this fresh process with ENABLE_JAPANESE_CONTEXT_READING=false")
    sys.path.insert(0, str(ROOT / "GPT_SoVITS"))
    from text.cleaner import clean_text

    sentences = json.loads(corpus.read_text(encoding="utf-8"))
    if not isinstance(sentences, list) or not sentences or any(not isinstance(s, str) for s in sentences):
        raise ValueError("Corpus must be a nonempty JSON list of sentences")
    process = psutil.Process()

    def memory():
        info = process.memory_info()
        return {name: round(getattr(info, name) / 2**20, 2) for name in ("rss", "private") if hasattr(info, name)}

    before, summaries, memories = [], {}, {}
    changed = 0
    try:
        for name, enabled in (("base", False), ("context", True)):
            memories[name + "_before_load_mib"] = memory()
            start = time.perf_counter()
            status = configure_japanese_reading(pyopenjtalk, project_root=project_root, enabled=enabled)
            load_seconds = time.perf_counter() - start
            if enabled and not status["ready"]:
                raise RuntimeError(status["detail"])
            memories[name + "_after_load_mib"] = memory()
            timings = []
            for index, sentence in enumerate(sentences):
                start = time.perf_counter()
                phones = clean_text(correct_pronunciation_for_tts(sentence), "ja", "v2")[0]
                timings.append(1000 * (time.perf_counter() - start))
                if enabled:
                    changed += phones != before[index]
                else:
                    before.append(phones)
            memories[name + "_after_corpus_mib"] = memory()
            summaries[name] = {"load_seconds": load_seconds, "median_ms": statistics.median(timings),
                               "p95_ms": sorted(timings)[int(.95 * (len(timings) - 1))]}
        return {"sentences": len(sentences), "changed_phone_sequences": changed,
                "timing": summaries, "process_memory": memories,
                "limitations": "Single fresh process; process memory includes native caches/arenas and base outputs. Corpus changes are observations, not correctness judgments. No sentences or audio are exported."}
    finally:
        configure_japanese_reading(pyopenjtalk, project_root=project_root, enabled=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = benchmark(args.corpus, args.project_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
