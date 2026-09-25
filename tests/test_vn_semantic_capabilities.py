"""Offline contracts for Base VN and independent semantic capabilities."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from vn_player.llm_client import VNLLMClient
from vn_player.runtime import VNPlayerRuntime
from vn_player.schemas import VNProfile, resolve_capability_defaults


def _script(tmp_path: Path, lines: list[dict]) -> Path:
    path = tmp_path / "synthetic.json"
    path.write_text(json.dumps({"lines": lines}, ensure_ascii=False), encoding="utf-8")
    return path


def test_base_text_only_has_no_implicit_mystery_script_or_rules(tmp_path: Path, monkeypatch) -> None:
    from config import settings
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "offline-test")
    async def run() -> None:
        legacy_script = _script(tmp_path, [{"script_id": "old", "text": "凶手是画家。"}])
        monkeypatch.setenv("VN_SCRIPT_PATH", str(legacy_script))
        monkeypatch.setenv("VN_LLM_ENABLED", "1")
        monkeypatch.setenv("VN_IMMEDIATE_LLM_ENABLED", "1")
        runtime = VNPlayerRuntime(tmp_path / "state")
        status = await runtime.start({"session_id": "base_text", "prompt_pack": "base", "provider": "openai"})
        assert status["script"]["line_count"] == 0
        assert status["script"]["path"] == ""
        assert status["capabilities"]["lookahead"] == {
            "requested": False, "available": False, "enabled": False, "reason": "disabled_by_profile"
        }
        assert status["capabilities"]["reasoning"]["requested"] is False
        assert resolve_capability_defaults("base") == {
            "immediate": True, "interaction": True, "summary": True,
            "retrospective": True, "lookahead": False, "reasoning": False,
        }

        runtime.llm.complete_json = AsyncMock(return_value=({"decision": "silence"}, "{}"))
        result = await runtime.ingest_line({"text": "明天一起去海边吗？", "speaker": "美咲"})
        assert result["status"] == "ok"
        assert result["line"]["script_id"] == ""
        assert result["attention"]["current_kind"] == "dialogue"
        assert result["attention"]["route"]["fact_extractor"] == "skip"
        assert result["reaction"]["decision"] == "silence"
        assert result["lookahead"]["source"] == "empty"
        assert runtime.store.evidence_nodes() == []
        assert runtime.store.hypotheses() == []
        assert runtime.llm.complete_json.await_count == 1
        prompt = runtime.llm.complete_json.await_args.args[0]
        assert "Do not infer a genre" in prompt[0]["content"]
        assert "明天一起去海边吗" in prompt[-1]["content"]

    asyncio.run(run())


def test_base_capabilities_gate_work_independently(tmp_path: Path, monkeypatch) -> None:
    from config import settings
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "offline-test")
    async def run() -> None:
        monkeypatch.setenv("VN_LLM_ENABLED", "1")
        runtime = VNPlayerRuntime(tmp_path / "state")
        status = await runtime.start({
            "session_id": "base_gates", "prompt_pack": "base", "provider": "openai",
            "summary_llm_enabled": False, "retrospective_llm_enabled": False,
            "capabilities": {
                "immediate": False, "interaction": False, "summary": True,
                "retrospective": True, "lookahead": False, "reasoning": True,
            },
        })
        assert status["capabilities"]["immediate"]["enabled"] is False
        assert status["capabilities"]["interaction"]["enabled"] is False
        assert status["capabilities"]["summary"]["enabled"] is True
        assert status["capabilities"]["retrospective"]["enabled"] is False
        assert status["capabilities"]["retrospective"]["reason"] == "retrospective_model_unavailable"
        assert status["capabilities"]["reasoning"]["reason"] == "semantic_type_unsupported"
        runtime.llm.complete_json = AsyncMock(side_effect=AssertionError("disabled lane called model"))
        rejected = await runtime.player_intervention("ask", {"text": "你怎么看？"})
        assert rejected["status"] == "unavailable"
        assert runtime._recent_player_dialogue() == []

        summaries = []
        retrospectives = []
        for index in range(40):
            result = await runtime.ingest_line({"text": f"我们今天去了海边，第{index}天。"})
            assert result["status"] == "ok"
            assert result["reaction"]["reason_label"] == "capability_disabled"
            if result.get("summary"):
                summaries.append(result["summary"])
            if result.get("retrospective"):
                retrospectives.append(result["retrospective"])
        assert summaries and all(item["lane"] == "summary_rules" for item in summaries)
        assert retrospectives == []
        assert runtime.store.story_summary_log()
        assert runtime.store.evidence_nodes() == []
        assert runtime.store.hypotheses() == []
        runtime.llm.complete_json.assert_not_awaited()

    asyncio.run(run())


def test_lookahead_requires_indexed_script_and_verified_alignment(tmp_path: Path, monkeypatch) -> None:
    from config import settings
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "offline-test")
    async def run() -> None:
        monkeypatch.setenv("VN_LLM_ENABLED", "1")
        script = _script(tmp_path, [
            {"script_id": "known_0", "text": "早上好。"},
            {"script_id": "known_1", "text": "发现新的证据！"},
        ])
        runtime = VNPlayerRuntime(tmp_path / "state")
        status = await runtime.start({
            "session_id": "base_aligned", "prompt_pack": "base", "provider": "openai", "script_path": str(script),
            "capabilities": {"lookahead": True},
        })
        runtime.llm.complete_json = AsyncMock(return_value=(None, "offline"))
        assert status["capabilities"]["lookahead"]["reason"] == "alignment_unavailable"
        unknown = await runtime.ingest_line({"script_id": "invented", "text": "completely unrelated live text"})
        assert unknown["status"] == "ok"
        assert unknown["lookahead"]["source"] == "empty"
        assert runtime.status()["capabilities"]["lookahead"]["enabled"] is False
        known = await runtime.ingest_line({"script_id": "known_0", "text": "早上好。"})
        assert known["status"] == "ok"
        assert runtime.status()["capabilities"]["lookahead"]["enabled"] is True
        assert known["lookahead"]["window"]["to_script_id"] == "known_1"
        assert "发现新的证据" not in json.dumps(known["lookahead"], ensure_ascii=False)

    asyncio.run(run())


def test_one_turn_visual_question_is_not_persisted_as_game_evidence(tmp_path: Path, monkeypatch) -> None:
    from config import settings
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "offline-test")
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "offline-test")
    async def run() -> None:
        monkeypatch.setenv("VN_LLM_ENABLED", "1")
        runtime = VNPlayerRuntime(tmp_path / "state")
        status = await runtime.start({"session_id": "base_visual", "prompt_pack": "base", "provider": "openai"})
        assert status["visual"] == {"supported": True, "reason": "ready"}
        visual = {"frame": {"dataBase64": "dmlzdWFsLWJ5dGVz", "mime": "image/jpeg"}, "scope": "game_window"}
        response = {"decision": "speak", "speak": {"text": "画面里有人。"}, "context_patches": [
            {"layer": "candidate_fact", "target": "evidence_nodes", "item": {"claim": "画面里有人"}}
        ]}
        runtime.llm.complete_json = AsyncMock(return_value=(response, "{}"))
        result = await runtime.player_intervention("ask", {"text": "画面是什么？", "visual_context": visual})
        assert result["status"] == "ok"
        assert result["reaction"]["context_patches"] == []
        assert runtime.llm.complete_json.await_args.kwargs["visual_context"] == visual
        assert runtime.store.evidence_nodes() == []
        assert "dmlzdWFsLWJ5dGVz" not in json.dumps(result["event"], ensure_ascii=False)
        assert "dmlzdWFsLWJ5dGVz" not in "".join(
            file.read_text(encoding="utf-8") for file in runtime.store.root.rglob("*.json*")
        )

        unsupported = VNPlayerRuntime(tmp_path / "other")
        await unsupported.start({"session_id": "no_visual", "prompt_pack": "base", "provider": "deepseek", "model": "deepseek-reasoner"})
        assert unsupported.status()["visual"]["supported"] is False
        rejected = await unsupported.player_intervention("ask", {"text": "画面是什么？", "visual_context": visual})
        assert rejected == {"status": "unavailable", "reason": "model_unsupported"}

    asyncio.run(run())


def test_visual_client_attaches_image_to_the_same_completion(monkeypatch) -> None:
    import openai
    from config import settings

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "offline-test")
    chat = Mock()
    chat.chat.completions.create.return_value.choices = [Mock(message=Mock(content='{"decision":"silence"}'))]
    monkeypatch.setattr(openai, "OpenAI", Mock(return_value=chat))
    client = VNLLMClient(VNProfile(session_id="visual", provider="openai", model="gpt-4o"))
    visual = {"frame": {"dataBase64": "dGVzdA==", "mime": "image/jpeg"}, "scope": "game_window"}
    raw = client._complete_sync(
        [{"role": "system", "content": "Instructions"}, {"role": "user", "content": "What is visible?"}],
        max_tokens=100, temperature=0.1, visual_context=visual,
    )
    assert raw == '{"decision":"silence"}'
    messages = chat.chat.completions.create.call_args.kwargs["messages"]
    assert messages[-1]["content"][0]["type"] == "text"
    assert "current visual frame" in messages[-1]["content"][0]["text"]
    assert messages[-1]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,dGVzdA=="


def test_explicit_empty_mystery_script_does_not_load_legacy_game(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        old = _script(tmp_path, [{"script_id": "old", "text": "旧游戏脚本。"}])
        monkeypatch.setenv("VN_SCRIPT_PATH", str(old))
        monkeypatch.setenv("VN_LLM_ENABLED", "0")
        runtime = VNPlayerRuntime(tmp_path / "state")
        status = await runtime.start({
            "session_id": "different_mystery", "game_id": "another_game",
            "game_title": "Another Game", "prompt_pack": "mystery", "script_path": "",
        })
        assert status["script"]["path"] == ""
        assert status["script"]["line_count"] == 0
        assert status["capabilities"]["lookahead"]["reason"] == "script_unavailable"

    asyncio.run(run())


def test_all_off_still_records_lines_and_interaction_can_run_without_immediate(tmp_path: Path, monkeypatch) -> None:
    from config import settings
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "offline-test")

    async def run() -> None:
        monkeypatch.setenv("VN_LLM_ENABLED", "1")
        runtime = VNPlayerRuntime(tmp_path / "state")
        await runtime.start({
            "session_id": "all_off", "prompt_pack": "base", "provider": "openai",
            "capabilities": {name: False for name in resolve_capability_defaults("base")},
        })
        runtime.llm.complete_json = AsyncMock(side_effect=AssertionError("disabled lane called model"))
        for index in range(40):
            result = await runtime.ingest_line({"text": f"今日の日記、{index}ページ。"})
            assert result["status"] == "ok"
            assert result["lookahead"]["source"] == "empty"
            assert "summary" not in result
            assert "retrospective" not in result
            assert "reasoner" not in result
        assert len(runtime.store.short_memory()) == 40
        assert runtime.store.story_summary_log() == []
        assert (await runtime.player_intervention("ask", {"text": "何が起きた？"}))["status"] == "unavailable"
        runtime.llm.complete_json.assert_not_awaited()

        second = VNPlayerRuntime(tmp_path / "interaction")
        await second.start({
            "session_id": "interaction_only", "prompt_pack": "base", "provider": "openai",
            "capabilities": {"immediate": False, "interaction": True, "summary": False,
                             "retrospective": False, "lookahead": False, "reasoning": False},
        })
        second.llm.complete_json = AsyncMock(return_value=({
            "decision": "speak", "speak": {"text": "表示された話から考えましょう。"},
        }, "{}"))
        displayed = await second.ingest_line({"text": "今日は図書館に行きました。"})
        assert displayed["reaction"]["reason_label"] == "capability_disabled"
        answered = await second.player_intervention("ask", {"text": "今日はどこへ？"})
        assert answered["status"] == "ok"
        assert answered["reaction"]["decision"] == "speak"
        assert second.llm.complete_json.await_count == 1

    asyncio.run(run())


def test_ambiguous_hash_and_base_mojibake_do_not_unlock_lookahead(tmp_path: Path, monkeypatch) -> None:
    from config import settings
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "offline-test")

    async def run() -> None:
        monkeypatch.setenv("VN_LLM_ENABLED", "1")
        script = _script(tmp_path, [
            {"script_id": "a", "text": "同一句话。"},
            {"script_id": "b", "text": "同一句话。"},
            {"script_id": "c", "text": "另一句台词。"},
        ])
        runtime = VNPlayerRuntime(tmp_path / "state")
        await runtime.start({"session_id": "ambiguous", "prompt_pack": "base", "provider": "openai",
                             "script_path": str(script), "capabilities": {"lookahead": True}})
        runtime.llm.complete_json = AsyncMock(return_value=(None, "offline"))
        ambiguous = await runtime.ingest_line({"text": "同一句话。"})
        assert ambiguous["line"]["match"]["type"] == "hash"
        assert ambiguous["lookahead"]["source"] == "empty"
        assert runtime.status()["capabilities"]["lookahead"]["reason"] == "alignment_unavailable"
        known = await runtime.ingest_line({"script_id": "b", "text": "同一句话。"})
        assert known["status"] == "ok"
        assert runtime.status()["capabilities"]["lookahead"]["enabled"] is True
        broken = await runtime.ingest_line({"text": "??????"})
        assert broken["line"]["match"]["type"] != "sequence_after_anchor"
        assert broken["lookahead"]["source"] == "empty"
        assert runtime.status()["capabilities"]["lookahead"]["enabled"] is False

    asyncio.run(run())


def test_inflight_player_answer_cannot_cross_a_session_restart(tmp_path: Path, monkeypatch) -> None:
    from config import settings
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "offline-test")

    async def run() -> None:
        monkeypatch.setenv("VN_LLM_ENABLED", "1")
        emitted = []
        spoken = []
        runtime = VNPlayerRuntime(
            tmp_path / "state",
            event_emit=lambda name, payload: emitted.append((name, payload)),
            speak_callback=lambda payload: spoken.append(payload),
        )
        await runtime.start({"session_id": "session_a", "prompt_pack": "base", "provider": "openai"})
        store_a = runtime.store
        entered = asyncio.Event()
        release = asyncio.Future()

        async def delayed_completion(*_args, **_kwargs):
            entered.set()
            return await release

        runtime.llm.complete_json = AsyncMock(side_effect=delayed_completion)
        pending = asyncio.create_task(runtime.player_intervention("ask", {"text": "What happened?"}))
        await entered.wait()
        await runtime.stop()
        await runtime.start({"session_id": "session_b", "prompt_pack": "base", "provider": "openai"})
        store_b = runtime.store
        release.set_result(({
            "decision": "speak", "speak": {"text": "The old answer."},
            "context_patches": [{"layer": "interpretation", "target": "hypotheses",
                                 "item": {"id": "old_claim", "claim": "Old session only"}}],
        }, "{}"))
        result = await pending
        assert result["status"] == "ignored"
        assert result["reason"] == "session_changed"
        assert spoken == []
        assert store_b.recent_reactions() == []
        assert store_b.hypotheses() == []
        assert runtime._recent_player_dialogue() == []
        assert not any(name == "vn.reaction" for name, _ in emitted)
        assert (store_a.root / "model_calls.jsonl").exists()
        assert not (store_b.root / "model_calls.jsonl").exists()

    asyncio.run(run())
