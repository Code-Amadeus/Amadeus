"""Observe a fixed checkout through the production text path in a fresh process.

Run separately for each code revision and dictionary state. Inputs and output
phones are evidence, not an accuracy score; an optional target tests application
of an independently supplied reading. This probe never renders or publishes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--dictionary-root", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--enabled", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Use a fresh report path")
    code = args.code_root.resolve()
    sys.path[:0] = [str(code), str(code / "GPT_SoVITS")]
    import pyopenjtalk
    from text import japanese
    from text.cleaner import clean_text
    from tts import japanese_dictionary as runtime
    from tts.japanese_reading import configure_japanese_reading
    from tools.tts_text_processor import correct_pronunciation_for_tts
    # This probe's observation helper is deliberately not imported through the
    # target checkout: old revisions may predate it.
    from japanese_pronunciation_evidence import target_readings

    started = time.perf_counter()
    state = runtime.configure_japanese_dictionary(
        pyopenjtalk, project_root=args.dictionary_root.resolve(), enabled=args.enabled)
    configure_japanese_reading(pyopenjtalk, enabled=False)
    configured = time.perf_counter() - started
    if args.enabled and not state["ready"]:
        raise RuntimeError(state)
    base = pyopenjtalk.OpenJTalk(pyopenjtalk.OPEN_JTALK_DICT_DIR)

    @lru_cache(maxsize=None)
    def signature(reading):
        return pyopenjtalk.g2p(reading, jtalk=base).lower()

    document = json.loads(args.inputs.read_bytes())
    records, nodes = [], []
    original = japanese.run_frontend

    def observed(native, text):
        result = original(native, text)
        nodes.extend(result)
        return result

    japanese.run_frontend = observed
    try:
        for i, item in enumerate(document["records"]):
            nodes.clear()
            text = correct_pronunciation_for_tts(item["text"])
            phones = clean_text(text, "ja", "v2")[0]
            result = {"id": item["id"], "phones": " ".join(phones)}
            if "expected" in item:
                surface = correct_pronunciation_for_tts(item["word"])
                surface = getattr(runtime, "normalize_latin_case", lambda x: x)(surface)
                surface = getattr(runtime, "with_latin_readings", lambda x: x)(surface)
                actual = target_readings(nodes, surface)
                result.update(actual=actual, target_matches=len(actual) == 1 and
                              any(signature(actual[0]) == signature(r) for r in item["expected"]))
            records.append(result)
            if (i + 1) % 5000 == 0:
                print(f"{i + 1}/{len(document['records'])}", flush=True)
    finally:
        japanese.run_frontend = original
    paths = ("GPT_SoVITS/text/japanese.py", "tts/japanese_dictionary.py",
             "tts/japanese_reading.py", "config/japanese_dictionary.py", "tools/tts_text_processor.py")
    report = {"runtime": state, "configure_seconds": configured,
              "elapsed_seconds": time.perf_counter() - started,
              "input_sha256": hashlib.sha256(args.inputs.read_bytes()).hexdigest(),
              "code_sha256_lf": {p: hashlib.sha256((code / p).read_bytes().replace(b"\r\n", b"\n")).hexdigest() for p in paths},
              "groups": dict(Counter(r["group"] for r in document["records"])), "records": records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"records": len(records), "configure_seconds": configured,
                      "elapsed_seconds": report["elapsed_seconds"], "state": state["state"]}))


if __name__ == "__main__":
    main()
