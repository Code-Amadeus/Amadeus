"""Notification presentation boundaries, without a live model or audio device."""
import asyncio
import os
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from server.handlers.companion_handler import CompanionHandler
from server.protocol import Method


def handler():
    h = CompanionHandler()
    async def translated(*args, **kwargs):
        yield "確認が必要です。"
    async def speak(payload):
        async for _piece in payload["_voice_stream"]:
            pass
        return {"status": "queued", "last_sentence_id": "last"}
    h.translate = Mock(side_effect=translated)
    h.speak = AsyncMock(side_effect=speak)
    h.interrupt = AsyncMock()
    h.publish = AsyncMock()
    h.busy = lambda: False
    h.chat_busy = lambda: False
    return h


def test_audio_observation_is_lazy_and_started_once_for_a_companion_client():
    async def run():
        h = handler()
        activity = SimpleNamespace(job=None, snapshot={"blocked": False}, refresh=AsyncMock())
        def start():
            activity.job = object()
        activity.start = Mock(side_effect=start)
        h.audio_activity = activity
        await h.handle("companion.cancel", {"request_id": "absent"})
        activity.refresh.assert_not_awaited()
        for _ in range(2):
            assert await h.handle("companion.audio-activity", {}) == {"blocked": False}
        activity.refresh.assert_awaited_once()
        activity.start.assert_called_once()
    asyncio.run(run())


def test_delivery_preserves_original_and_waits_for_real_playback():
    async def run():
        h = handler()
        await h.handle("companion.speak", {"request_id": "one", "text": "需要确认什么？",
                                            "project_name": "Amadeus", "provider": "Codex"})
        await asyncio.sleep(0)
        h.publish.assert_not_awaited()
        assert list(h.recent_spoken) == []
        assert h.translate.call_args.kwargs["project_name"] == "Amadeus"
        assert h.translate.call_args.kwargs["provider"] == "Codex"
        payload = h.speak.call_args.args[0]
        assert h.active["japanese"] == "確認が必要です。"
        assert "voice_text_ja" not in payload
        assert payload["display_text"] == "需要确认什么？"
        assert payload["speed"] == 1.0
        with patch("server.vn_tts_bridge.get_vn_sentence_metadata", return_value={"line_id": "one"}):
            await h.on_playback(Method.TTS_SENTENCE_START, {"sentence_id": "last"})
            assert h.publish.call_args.args == ("one", "started")
            assert list(h.recent_spoken) == ["確認が必要です。"]
            await h.on_playback(Method.TTS_SENTENCE_END, {"sentence_id": "last"})
        await h.job
        assert h.publish.call_args.args == ("one", "finished")
        await h.handle("companion.speak", {"request_id": "two", "text": "另一个任务的结果",
                                            "project_name": "另一个项目", "provider": "Codex", "ended": True})
        await asyncio.sleep(0)
        context = h.translate.call_args.kwargs
        assert context["project_name"] == "另一个项目" and context["ended"] is True
        assert context["recent_spoken"] == ["確認が必要です。"]
        await h.preempt()
    asyncio.run(run())


def test_mute_and_chat_preemption_cannot_interrupt_foreground_chat():
    async def run():
        h = handler()
        await h.handle("companion.speak", {"request_id": "one", "text": "确认"})
        await asyncio.sleep(0)
        await h.handle("companion.cancel", {"request_id": "stale"})
        assert h.active is not None
        await h.handle("companion.cancel", {"request_id": "one"})
        h.interrupt.assert_awaited_once()
        h.interrupt.reset_mock()
        await h.handle("companion.speak", {"request_id": "two", "text": "确认"})
        await asyncio.sleep(0)
        h.chat_busy = lambda: True
        await h.preempt()
        h.interrupt.assert_not_awaited()
        assert h.publish.call_args.args == ("two", "preempted")
        assert (await h.handle("companion.speak", {"request_id": "three", "text": "确认"}))["status"] == "busy"
    asyncio.run(run())


def test_translation_failure_never_sends_original_language_to_tts():
    async def run():
        h = handler(); h.translate.side_effect = RuntimeError("unavailable")
        await h.handle("companion.speak", {"request_id": "one", "text": "Original English"})
        job = h.job
        await job
        assert h.active is None
        h.interrupt.assert_not_awaited()
        assert h.publish.call_args.args == ("one", "unavailable")
    asyncio.run(run())


def test_enqueue_failure_keeps_its_reason_in_diagnostic_logs(caplog):
    async def run():
        h = handler()
        h.speak = AsyncMock(return_value={"status": "error", "reason": "tts_enqueue_confirmation_timeout"})
        await h.handle("companion.speak", {"request_id": "failed-entry", "text": "原文"})
        await h.job
        assert h.publish.call_args.args == ("failed-entry", "unavailable")
        assert "failed-entry" in caplog.text
        assert "tts_enqueue_confirmation_timeout" in caplog.text
    asyncio.run(run())


def test_stream_playback_cannot_finish_the_notification_before_the_last_sentence():
    async def run():
        h = handler()
        continue_generation = asyncio.Event()
        async def translated(*args, **kwargs):
            yield "最初の質問を見てもらえる？"
            await continue_generation.wait()
            yield "条件はそのままよ。"
        h.translate.side_effect = translated
        await h.handle("companion.speak", {"request_id": "one", "text": "完整问题及条件"})
        await asyncio.sleep(0)
        with patch("server.vn_tts_bridge.get_vn_sentence_metadata", return_value={"line_id": "one"}):
            await h.on_playback(Method.TTS_SENTENCE_START, {"sentence_id": "first"})
            await h.on_playback(Method.TTS_SENTENCE_END, {"sentence_id": "first"})
            assert h.publish.call_args.args == ("one", "started")
            assert not h.job.done()
            continue_generation.set()
            await asyncio.sleep(0)
            await h.on_playback(Method.TTS_SENTENCE_END, {"sentence_id": "last"})
        await h.job
        assert h.publish.call_args.args == ("one", "finished")
    asyncio.run(run())


def test_translation_contract_is_full_question_or_short_end_summary():
    from server import vn_tts_bridge as bridge
    requests = []
    class Client:
        @property
        def chat(self): return self
        @property
        def completions(self): return self
        def create(self, **kwargs):
            requests.append(kwargs)
            return [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="確認です。"), finish_reason="stop")])]
    async def run():
        with patch.object(bridge, "_get_client", return_value=Client()), patch.object(bridge.settings, "DEEPSEEK_API_KEY", "test"), patch.dict("os.environ", {"VN_TTS_TRANSLATE_PROVIDER": "deepseek"}):
            assert await bridge.translate_notification("原问题全文") == "確認です。"
            await bridge.translate_notification("实际产出", ended=True, project_name="研究", provider="Codex", recent_spoken=["前の言い回しです。"])
        import json
        from llm.prompts import get_character_prompt
        assert requests[0]["messages"][0]["content"].startswith(get_character_prompt("ja"))
        question = json.loads(requests[0]["messages"][1]["content"])
        assert question["source_text"] == "原问题全文"
        assert question["notification"] == "question" and question["project_name"] == ""
        ended = json.loads(requests[1]["messages"][1]["content"])
        assert ended == {"source_text": "实际产出", "notification": "turn_ended", "project_name": "研究", "provider": "Codex", "recent_spoken_openings": ["前の言い回しです。"]}
        assert requests[0]["max_tokens"] > requests[1]["max_tokens"]
    asyncio.run(run())


def test_notification_reuses_configured_openai_provider_and_request_contract():
    from server import vn_tts_bridge as bridge
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: None)))
    captured = {}
    def create(**kwargs):
        captured.update(kwargs)
        return [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="聞こえますか？"), finish_reason="stop")])]
    client.chat.completions.create = create
    async def run():
        with patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "VN_TTS_TRANSLATE_PROVIDER"}, clear=True), patch.object(bridge.settings, "LLM_PROVIDER", "openai"), patch.object(bridge.settings, "OPENAI_API_KEY", "test"), patch.object(bridge, "_get_client", return_value=client) as get_client:
            assert await bridge.translate_notification("能听见吗？") == "聞こえますか？"
            assert get_client.call_args.args[0] == "openai"
        assert "max_completion_tokens" in captured
        assert "max_tokens" not in captured and "temperature" not in captured
        assert captured["reasoning_effort"] == "none"
    asyncio.run(run())


def test_latin_letters_cannot_reach_japanese_notification_speech():
    from server import vn_tts_bridge as bridge
    async def fragments(text, **kwargs):
        yield text_result[0]
    async def run():
        with patch.object(bridge, "_stream_translate_zh_to_ja", side_effect=fragments):
            for invalid in ("Codexの結果よ。", "ＡＰＩを確認して。"):
                text_result[0] = invalid
                try:
                    await bridge.translate_notification("原文保留 Codex 和 API")
                    assert False, "Latin letters must not enter Japanese TTS"
                except RuntimeError:
                    pass
            text_result[0] = "コーデックスのエーピーアイを確認して。"
            assert await bridge.translate_notification("原文保留 Codex 和 API") == text_result[0]
    text_result = [""]
    asyncio.run(run())


def test_external_audio_prevents_generation_and_stops_only_an_owned_reminder():
    async def run():
        h = handler()
        h.audio_activity = SimpleNamespace(snapshot={"blocked": True}, job=object())
        assert (await h.handle("companion.speak", {"request_id":"one","text":"结果"}))["status"] == "busy"
        h.translate.assert_not_called()
        h.audio_activity.snapshot["blocked"] = False
        await h.handle("companion.speak", {"request_id":"two","text":"结果"})
        await asyncio.sleep(0)
        await h.defer_for_external_audio()
        h.interrupt.assert_awaited_once()
        assert h.publish.call_args.args == ("two", "deferred")
        h.interrupt.reset_mock()
        await h.defer_for_external_audio()
        h.interrupt.assert_not_awaited()
    asyncio.run(run())
