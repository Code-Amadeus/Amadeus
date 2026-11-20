"""Exercise real resource installation, update/rollback, frontend and optional GSV.

Everything runs in a new output workspace. No live pack, release or personal
dictionary is touched. A valid waveform never sets listening acceptance.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import copy
import hashlib
import html
import json
from pathlib import Path
import re
import sys
from functools import lru_cache

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.japanese_dictionary import PACK_ID, PACK_TREE, load_dictionary_pack
from tools.external_assets import build_bundle, install_bundle, verify_bundle
from tools.probes.japanese_pronunciation_evidence import (
    align_reference_conditioning, reference_fingerprints, target_readings,
)
from tools.tts_text_processor import correct_pronunciation_for_tts

CASES = [
    {"id": "menu", "text": "menuを開いてください。", "surface": "menu", "reading": "メニュー"},
    {"id": "game", "text": "このgameを始めましょう。", "surface": "game", "reading": "ゲーム"},
    {"id": "disney", "text": "Disneyの映画を見ました。", "surface": "Disney", "reading": "ディズニー"},
    {"id": "neutral", "text": "彼は中立派です。", "surface": "中立派", "reading": "チュウリツハ"},
]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(args):
    import pyopenjtalk
    from tts.japanese_dictionary import configure_japanese_dictionary, with_latin_readings
    from tts.japanese_reading import configure_japanese_reading
    from tools.validate_japanese_dictionary import BASE_SAMPLES, UNIT_SAMPLES

    sys.path.insert(0, str(ROOT / "GPT_SoVITS"))
    from text import japanese
    from text.cleaner import clean_text

    output = args.output_dir
    output.mkdir(parents=True, exist_ok=False)
    candidate = load_dictionary_pack(args.candidate_root / "assets" / PACK_TREE)
    candidate_metadata = json.loads((candidate.root / "provenance.json").read_bytes())
    expected_readings = defaultdict(set)
    from config.japanese_dictionary import latin_word_key
    for change in candidate_metadata["source_review"]["changes"]:
        expected_readings[latin_word_key(change["after_columns"][0])].add(change["after_columns"][12])
    independent_base = pyopenjtalk.OpenJTalk(pyopenjtalk.OPEN_JTALK_DICT_DIR)
    @lru_cache(maxsize=None)
    def signature(reading):
        return pyopenjtalk.g2p(reading, jtalk=independent_base).lower()
    previous = load_dictionary_pack(args.previous_root / "assets" / PACK_TREE)
    if candidate.format_version != 2 or candidate.version == previous.version:
        raise ValueError("Lifecycle comparison requires a full-source candidate and a different previous version")
    if previous.format_version == 2 and candidate.entry_count != previous.entry_count:
        raise ValueError("Full-source updates must preserve the source denominator")
    archives = {}
    for name, root, pack in [("previous", args.previous_root, previous), ("candidate", args.candidate_root, candidate)]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", pack.version):
            raise ValueError("Version is not safe for an immutable resource filename")
        archive = output / f"amadeus-japanese-pronunciation-{pack.version}.zip"
        build_bundle(project_root=root, output=archive, pack_ids=[PACK_ID], index_path=ROOT / "assets/index.json")
        verify_bundle(archive, index_path=ROOT / "assets/index.json")
        archives[name] = archive
    recipient = output / "isolated recipient"
    personal = recipient / "assets/text/ja/user.csv"
    personal.parent.mkdir(parents=True)
    personal.write_text("ｐｅｒｓｏｎａｌｐｒｏｂｅ,1348,1348,0,名詞,一般,*,*,*,*,テスト,テスト,テスト,1/3,*\n", encoding="utf-8")
    legacy = recipient / "GPT_SoVITS/text/ja_userdic/userdict.csv"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes((candidate.root / "source.csv").read_bytes())
    protected = sha(personal), sha(legacy)
    statuses = {}
    for label, archive, expected in [
        ("install", archives["previous"], previous),
        ("update", archives["candidate"], candidate),
        ("rollback", archives["previous"], previous),
        ("restore_candidate", archives["candidate"], candidate),
    ]:
        installed = install_bundle(archive, project_root=recipient, index_path=ROOT / "assets/index.json", overwrite=label != "install")
        loaded = load_dictionary_pack(recipient / "assets" / PACK_TREE)
        assert loaded.identity == expected.identity and loaded.entry_count == expected.entry_count
        status = configure_japanese_dictionary(pyopenjtalk, project_root=recipient, enabled=True)
        assert status["ready"] and status["version"] == expected.version, status
        configure_japanese_reading(pyopenjtalk, enabled=False)
        statuses[label] = {"version": loaded.version, "resource_identity": loaded.identity,
                           "audio_cache_identity": status["identity"], "installed_files": installed["installed_files"],
                           "many_phones": clean_text("manyを確認してください。", "ja", "v2")[0]}
        assert protected == (sha(personal), sha(legacy))
    assert statuses["install"]["audio_cache_identity"] == statuses["rollback"]["audio_cache_identity"]
    assert statuses["update"]["audio_cache_identity"] == statuses["restore_candidate"]["audio_cache_identity"]
    assert statuses["install"]["audio_cache_identity"] != statuses["update"]["audio_cache_identity"]
    assert install_bundle(archives["candidate"], project_root=recipient, index_path=ROOT / "assets/index.json")["installed_files"] == 0

    base_root, legacy_root = output / "base", output / "legacy"
    base_root.mkdir()
    old_source = legacy_root / "GPT_SoVITS/text/ja_userdic/userdict.csv"
    old_source.parent.mkdir(parents=True)
    old_source.write_bytes((candidate.root / "source.csv").read_bytes())
    # The candidate profile uses the actually installed full package, but a
    # synthetic personal probe cannot influence any benchmark input.
    profiles = {"base": base_root, "legacy": legacy_root, "candidate": recipient}
    def activate(name):
        state = configure_japanese_dictionary(pyopenjtalk, project_root=profiles[name], enabled=name != "base")
        configure_japanese_reading(pyopenjtalk, enabled=False)
        if name != "base":
            assert state["ready"], state
        return state

    document = json.loads(args.corpus.read_bytes())
    corpus = document["records"]
    assert len(corpus) == document["record_count"]
    report = {"status": "candidate_not_published", "source_rows": candidate.entry_count,
              "reviewed_corrections": candidate.reviewed_rows, "original_unverified": candidate.entry_count - candidate.reviewed_rows,
              "resource_identity": candidate.identity, "lifecycle": statuses,
              "personal_and_legacy_preserved": True, "idempotent_reinstall": True,
              "archives": {key: {"file": path.name, "sha256": sha(path), "bytes": path.stat().st_size} for key, path in archives.items()},
              "corpus_sha256": sha(args.corpus), "corpus_sources": document.get("sources"),
              "profiles": {}, "corpus_differences": {}, "audio": [], "listening": "pending",
              "baseline_definition": "Current frontend with the unmodified original legacy CSV; not an old application executable.",
              "source_sha256": {p: sha(ROOT / p) for p in (
                  "config/japanese_dictionary.py", "tts/japanese_dictionary.py", "GPT_SoVITS/text/japanese.py",
                  "tts/japanese_reading.py", "tools/tts_text_processor.py", "local_tts_infer.py")}}
    observed, nodes = {}, []
    original_frontend = japanese.run_frontend
    def observe_frontend(native, text):
        result = original_frontend(native, text)
        nodes.extend(result)
        return result
    japanese.run_frontend = observe_frontend
    try:
        for profile in profiles:
            state = activate(profile)
            cases = []
            for case in CASES:
                nodes.clear()
                phones = clean_text(correct_pronunciation_for_tts(case["text"]), "ja", "v2")[0]
                readings = target_readings(nodes, with_latin_readings(case["surface"]))
                cases.append({**case, "actual": readings, "target_matches": readings == [case["reading"]], "phones": phones})
            controls = {text: clean_text(text, "ja", "v2")[0] for text in BASE_SAMPLES + UNIT_SAMPLES}
            targets = []
            for word, readings in sorted(expected_readings.items()):
                for text in (word, "この" + word + "を確認して。", "これは" + word + "です。"):
                    nodes.clear()
                    clean_text(correct_pronunciation_for_tts(text), "ja", "v2")
                    actual = target_readings(nodes, with_latin_readings(word))
                    targets.append({"word": word, "text": text, "expected": sorted(readings), "actual": actual,
                                    "matches": len(actual) == 1 and any(signature(actual[0]) == signature(r) for r in readings)})
            sequences = []
            for i, row in enumerate(corpus):
                nodes.clear()
                sequences.append(clean_text(row["text"], "ja", "v2")[0])
                if (i + 1) % 1000 == 0:
                    print(f"[{profile}] corpus {i + 1}/{len(corpus)}", flush=True)
            observed[profile] = sequences
            report["profiles"][profile] = {"runtime": state, "cases": cases, "controls": controls,
                                           "target_contexts": len(targets), "target_matches": sum(t["matches"] for t in targets),
                                           "targets": targets,
                                           "target_scope": "Application of source-reviewed targets, not independent lexical or acoustic gold; alternate readings and unscored spans remain visible."}
        for before in ("base", "legacy"):
            changed = [{"index": i, "source": corpus[i]["source"], "id": corpus[i]["id"],
                        "text": corpus[i]["text"], "before": a, "after": b}
                       for i, (a, b) in enumerate(zip(observed[before], observed["candidate"])) if a != b]
            report["corpus_differences"][before] = {"sentences": len(corpus), "changed": len(changed), "records": changed}
        report["base_control_changes"] = [text for text, value in report["profiles"]["base"]["controls"].items()
                                           if value != report["profiles"]["candidate"]["controls"][text]]
        if args.render:
            import numpy as np
            import soundfile as sf
            import torch
            from config import settings
            from local_tts_infer import TTSInferencer
            from tools.probes.probe_gsv_stability import _set_seed
            engine = TTSInferencer(device="cuda:0", gpt_path=settings.TTS_GPT_MODEL_PATH, sovits_path=settings.TTS_SOVITS_MODEL_PATH)
            report["synthesis"] = {"model_version": engine.model_version,
                                   "gpt_sha256": sha(settings.TTS_GPT_MODEL_PATH), "sovits_sha256": sha(settings.TTS_SOVITS_MODEL_PATH),
                                   "reference_sha256": sha(settings.TTS_REF_AUDIO_JA), "seed": 237,
                                   "top_k": 20, "top_p": 0.6, "temperature": 0.6, "speed": 1.0, "sample_steps": 16}
            frozen = None
            for profile in ("legacy", "candidate"):
                activate(profile)
                engine._session_cache.clear()
                _set_seed(0, torch)
                reference = engine.warm_reference_cache(settings.TTS_REF_AUDIO_JA, settings.TTS_REF_TEXT_JA, "日文")
                if frozen is None:
                    frozen = copy.deepcopy(reference)
                elif not align_reference_conditioning(reference, frozen):
                    raise RuntimeError("Reference inputs differ; do not claim controlled paired audio")
                fingerprint = reference_fingerprints(reference)
                report["profiles"][profile]["reference_conditioning"] = fingerprint
                for case in CASES:
                    _set_seed(237, torch)
                    rate, waveform = engine.infer(
                        text=correct_pronunciation_for_tts(case["text"]), ref_audio_path=settings.TTS_REF_AUDIO_JA,
                        prompt_text=settings.TTS_REF_TEXT_JA, text_language="日文", prompt_language="日文", how_to_cut="不切",
                        top_k=20, top_p=0.6, temperature=0.6, speed=1.0, sample_steps=16,
                        enable_cuda_graph=False, enable_static_kv=True)
                    assert len(waveform) and np.isfinite(waveform).all()
                    filename = f"{case['id']}__{profile}.wav"
                    sf.write(output / filename, waveform, rate, subtype="PCM_16")
                    report["audio"].append({"case": case["id"], "profile": profile, "file": filename,
                                            "sample_rate": rate, "seconds": len(waveform) / rate, "sha256": sha(output / filename)})
                assert reference_fingerprints(reference) == fingerprint
            sections = []
            for case in CASES:
                players = "".join(f'<div>{label}<audio controls preload="none" src="{case["id"]}__{profile}.wav"></audio></div>'
                                  for label, profile in [("A：原始完整词表", "legacy"), ("B：完整资源候选", "candidate")])
                sections.append(f'<section><h2>{html.escape(case["text"])}</h2><p>目标：{case["reading"]}</p>{players}</section>')
            (output / "listen.html").write_text('<!doctype html><meta charset="utf-8"><title>完整词典首版验收</title>'
                '<style>body{font:16px system-ui;max-width:900px;margin:40px auto}audio{display:block}section{padding:15px;border-bottom:1px solid #ccc}</style>'
                '<h1>完整词典首版 A/B 验收</h1><p>听感尚未验收；原句、模型、采样参数和参考条件相同。</p>' + "".join(sections), encoding="utf-8")
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        japanese.run_frontend = original_frontend
    print(json.dumps({"lifecycle": "passed", "source_rows": candidate.entry_count,
                      "case_matches": {p: sum(c["target_matches"] for c in v["cases"]) for p, v in report["profiles"].items()},
                      "base_control_changes": report["base_control_changes"], "audio_files": len(report["audio"])}, ensure_ascii=False))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--previous-root", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--render", action="store_true")
    run(parser.parse_args())
