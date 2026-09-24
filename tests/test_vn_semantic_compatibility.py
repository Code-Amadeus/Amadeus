"""Characterization of the existing Mystery VN semantic journey.

The script in each test is synthetic. These assertions intentionally describe
the current Paranormasight path without importing a licensed game script.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

from vn_player.runtime import VNPlayerRuntime


def _script(tmp_path: Path, lines: list[dict]) -> Path:
    path = tmp_path / "synthetic_mystery.json"
    path.write_text(json.dumps({"lines": lines}, ensure_ascii=False), encoding="utf-8")
    return path


def _runtime(tmp_path: Path, monkeypatch, *, immediate_model: bool = False) -> VNPlayerRuntime:
    monkeypatch.setenv("VN_LLM_ENABLED", "1" if immediate_model else "0")
    monkeypatch.setenv("VN_IMMEDIATE_LLM_ENABLED", "1" if immediate_model else "0")
    monkeypatch.setenv("VN_LOOKAHEAD_LLM_ENABLED", "0")
    monkeypatch.setenv("VN_REASONER_LLM_ENABLED", "0")
    monkeypatch.setenv("VN_SUMMARY_LLM_ENABLED", "0")
    monkeypatch.setenv("VN_RETROSPECTIVE_LLM_ENABLED", "0")
    monkeypatch.setenv("VN_IMMEDIATE_SILENCE_PRESSURE_ENABLED", "0")
    return VNPlayerRuntime(tmp_path / "state")


async def _start(runtime: VNPlayerRuntime, path: Path) -> dict:
    return await runtime.start(
        {
            "session_id": "mystery_compatibility",
            "game_id": "paranormasight_the_mermaids_curse",
            "game_title": "Paranormasight: The Mermaid's Curse",
            "game_genre": "mystery",
            "prompt_pack": "mystery",
            "output_language": "zh",
            "script_path": str(path),
            "lookahead_enabled": True,
            "max_reactions_per_minute": 100,
        }
    )


def test_mystery_alignment_repetition_and_menu_routing(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        path = _script(
            tmp_path,
            [
                {"script_id": "chapter_1_menu", "text": "人物档案", "speaker": "", "scene_id": "menu"},
                {"script_id": "chapter_1_a", "text": "钥匙藏在抽屉里。", "speaker": "小林", "scene_id": "room"},
                {"script_id": "chapter_1_b", "text": "钥匙藏在抽屉里。", "speaker": "小林", "scene_id": "room"},
                {"script_id": "chapter_1_secret", "text": "发现新的证据！凶手是画家。", "speaker": "小林", "scene_id": "room"},
            ],
        )
        runtime = _runtime(tmp_path, monkeypatch)
        status = await _start(runtime, path)
        assert status["profile"]["game_id"] == "paranormasight_the_mermaids_curse"
        assert status["profile"]["prompt_pack"] == "mystery"
        assert status["script"]["line_count"] == 4

        menu = await runtime.ingest_line({"script_id": "chapter_1_menu", "text": "人物档案"})
        assert menu["status"] == "ok"
        assert menu["attention"]["current_kind"] == "topic_label"
        assert menu["attention"]["route"]["immediate"] == "skip"
        assert menu["attention"]["route"]["fact_extractor"] == "skip"
        assert menu["reaction"]["decision"] != "speak"

        aligned = await runtime.ingest_line(
            {"script_id": "chapter_1_a", "text": "??????", "speaker": "??????", "line_id": "live_a"}
        )
        assert aligned["status"] == "ok"
        assert aligned["line"]["text"] == "钥匙藏在抽屉里。"
        assert aligned["line"]["speaker"] == "小林"
        assert aligned["line"]["match"]["type"] == "id"
        assert aligned["line"]["script_id"] == "chapter_1_a"

        duplicate = await runtime.ingest_line({"script_id": "chapter_1_a", "text": "钥匙藏在抽屉里。"})
        assert duplicate["status"] == "ignored"
        assert duplicate["reason"] == "duplicate_exact_script_id"
        assert [line["script_id"] for line in runtime.store.short_memory()] == [
            "chapter_1_menu", "chapter_1_a"
        ]

        repeated = await runtime.ingest_line({"text": "钥匙藏在抽屉里。", "line_id": "live_repeat"})
        assert repeated["status"] == "ok"
        assert repeated["line"]["match"]["type"] == "hash"
        # Hash matching can reuse the previous script ID; only an explicitly
        # supplied exact ID is suppressed as a duplicate.
        assert repeated["line"]["script_id"] == "chapter_1_a"
        assert [line["text"] for line in runtime.store.short_memory()][-2:] == [
            "钥匙藏在抽屉里。", "钥匙藏在抽屉里。"
        ]
        assert runtime.status()["script"]["last_order"] == 1  # hash matching does not advance the anchor

        reveal = await runtime.ingest_line(
            {"script_id": "chapter_1_secret", "text": "发现新的证据！凶手是画家。"}
        )
        assert reveal["status"] == "ok"
        assert reveal["attention"]["route"]["immediate"] == "react"
        assert reveal["reaction"]["decision"] == "speak"
        assert reveal["reaction"]["speak"]["target_script_id"] == "chapter_1_secret"
        assert reveal["reaction"]["reason_label"] == "new_evidence"

    asyncio.run(run())


def test_immediate_context_sees_only_abstract_future_and_verifies_displayed_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    async def run() -> None:
        path = _script(
            tmp_path,
            [
                {"script_id": "m_0", "text": "钥匙藏在抽屉里。", "speaker": "小林"},
                {"script_id": "m_1", "text": "凶手是画家。", "speaker": "小林"},
            ],
        )
        runtime = _runtime(tmp_path, monkeypatch, immediate_model=True)
        await _start(runtime, path)
        response = {
            "decision": "silence",
            "context_patches": [
                {
                    "layer": "candidate_fact", "target": "evidence_nodes",
                    "item": {"id": "displayed", "claim": "钥匙藏在抽屉里", "evidence_script_ids": ["m_0"]},
                },
                {
                    "layer": "evidence", "target": "evidence_nodes",
                    "item": {"id": "future", "claim": "凶手是画家", "evidence_script_ids": ["m_1"]},
                },
                {
                    "layer": "observed_fact", "target": "evidence_nodes",
                    "item": {"id": "model_fact", "claim": "钥匙藏在抽屉里"},
                },
            ],
        }
        runtime.llm.complete_json = AsyncMock(return_value=(response, json.dumps(response)))
        result = await runtime.ingest_line({"script_id": "m_0", "text": "钥匙藏在抽屉里。"})
        assert result["status"] == "ok"
        assert result["reaction"]["decision"] == "silence"
        assert result["lookahead"]["spoiler_policy"] == "abstract_only"
        assert result["lookahead"]["window"]["to_script_id"] == "m_1"
        assert "凶手是画家" not in json.dumps(result["lookahead"], ensure_ascii=False)

        messages = runtime.llm.complete_json.await_args.args[0]
        immediate_view = "\n".join(message["content"] for message in messages)
        assert "钥匙藏在抽屉里" in immediate_view
        assert "凶手是画家" not in immediate_view
        assert "凶手是画家" not in json.dumps(
            runtime.store.read_json(runtime.store.context_pack_dir / "immediate.latest.json", default={}),
            ensure_ascii=False,
        )
        verification = {item["patch_id"]: item for item in result["verification"]}
        assert verification["displayed"]["status"] == "grounded_fact_candidate"
        assert verification["future"]["status"] == "rejected_unseen_evidence"
        assert verification["future"]["hidden_probe"]["unseen_text_withheld"] is True
        assert {item["item_id"] for item in result["context_applied"]} == {"displayed"}
        assert {node["id"] for node in runtime.store.evidence_nodes()} == {"displayed"}

    asyncio.run(run())


def test_mystery_summary_retrospective_and_player_history_are_displayed_only(
    tmp_path: Path, monkeypatch
) -> None:
    async def run() -> None:
        lines = [
            {"script_id": f"m_{index}", "text": f"小林说今天是第{index}天。", "speaker": "小林"}
            for index in range(40)
        ]
        lines.append({"script_id": "m_future", "text": "凶手是画家。", "speaker": "小林"})
        runtime = _runtime(tmp_path, monkeypatch)
        await _start(runtime, _script(tmp_path, lines))
        note = await runtime.player_intervention("note", {"text": "我怀疑小林。"})
        assert note["status"] == "ok"
        assert note["event"]["kind"] == "note"

        summaries = []
        retrospective = None
        for index in range(40):
            result = await runtime.ingest_line({"script_id": f"m_{index}", "text": lines[index]["text"]})
            assert result["status"] == "ok"
            if result.get("summary"):
                summaries.append(result["summary"])
            if result.get("retrospective"):
                retrospective = result["retrospective"]
        assert summaries
        assert all(item["lane"] == "summary_rules" for item in summaries)
        assert runtime.store.story_summary_log()
        assert retrospective is not None
        assert retrospective["lane"] == "retrospective_rules"

        immediate = runtime.store.read_json(runtime.store.context_pack_dir / "immediate.latest.json", default={})
        retro = runtime.store.read_json(runtime.store.context_pack_dir / "retrospective.latest.json", default={})
        assert immediate["recent_player_dialogue"][-1]["text"] == "我怀疑小林。"
        assert retro["window"]["past_lines"] == 40
        assert retro["current_line"]["script_id"] == "m_39"
        assert all(line["script_id"] != "m_future" for line in retro["recent_lines"])
        assert "凶手是画家" not in json.dumps(retro, ensure_ascii=False)
        await runtime.player_intervention("clear", {})
        assert runtime._recent_player_dialogue() == []

    asyncio.run(run())
