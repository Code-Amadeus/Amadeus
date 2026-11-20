"""Verify the selected release dictionary through the real Japanese production frontend.

Run this from an extracted application archive with native voice dependencies
installed. It compiles only disposable caches; it does not install/publish data.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "GPT_SoVITS")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-source", choices=("bundled", "installed"), default="bundled")
    parser.add_argument("--expected-version")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    import pyopenjtalk
    from config import settings
    from config.asset_packages import external_asset_pack_status
    from config.japanese_dictionary import PACK_ID, dictionary_pack_root, load_dictionary_pack
    from text import japanese
    from text.cleaner import clean_text
    from tools.tts_text_processor import correct_pronunciation_for_tts
    from tools.probes.japanese_pronunciation_evidence import target_readings
    from tts.japanese_dictionary import configure_japanese_dictionary, prepare_japanese_text
    from tts.japanese_reading import configure_japanese_reading
    if not settings.ENABLE_JAPANESE_USER_DICTIONARY:
        raise RuntimeError("Default dictionary setting is disabled in the acceptance environment")
    pin = json.loads((ROOT / "release/japanese_dictionary.json").read_bytes())
    expected_version = args.expected_version or pin["version"]
    status = configure_japanese_dictionary(pyopenjtalk, project_root=ROOT)
    configure_japanese_reading(pyopenjtalk, enabled=False)
    if not status["ready"] or status["source"] != args.expected_source or status["version"] != expected_version:
        raise RuntimeError(f"Wrong loaded dictionary: {status}")
    advertised = external_asset_pack_status(PACK_ID, asset_root=ROOT / "assets", index_path=ROOT / "assets/index.json")
    assert advertised["installed"] and advertised["source"] == status["source"] and advertised["version"] == status["version"]
    pack = load_dictionary_pack(dictionary_pack_root(ROOT / "assets"))
    if args.expected_source == "bundled":
        assert pack.identity == pin["resource_identity"]
    original = japanese.run_frontend
    nodes, cases = [], []
    def observed(jtalk, text):
        result = original(jtalk, text)
        nodes.extend(result)
        return result
    japanese.run_frontend = observed
    try:
        for word, reading, sentence in [("menu", "メニュー", "menuを開いてください。"),
                                         ("game", "ゲーム", "このgameを始めましょう。"),
                                         ("Disney", "ディズニー", "Disneyの映画を見ました。"),
                                         ("中立派", "チュウリツハ", "彼は中立派です。")]:
            nodes.clear()
            phones = clean_text(correct_pronunciation_for_tts(sentence), "ja", "v2")[0]
            target = prepare_japanese_text(correct_pronunciation_for_tts(word)).strip()
            actual = target_readings(nodes, target)
            assert actual == [reading], (word, actual)
            cases.append({"word": word, "reading": actual, "phones": phones})
    finally:
        japanese.run_frontend = original
        pyopenjtalk.unset_user_dict()
    report = {"runtime": status, "source_rows": pack.entry_count, "reviewed_corrections": pack.reviewed_rows,
              "resource_identity": pack.identity, "cases": cases, "audio_rendered": False}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"source": status["source"], "version": status["version"], "source_rows": pack.entry_count,
                      "case_matches": len(cases)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
