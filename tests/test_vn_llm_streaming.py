"""A complete speech header can precede a verified, separately committed JSON tail."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from vn_player.llm_client import VNLLMClient, _speech_prefix
from vn_player.schemas import VNProfile


HEADER = '{"decision":"speak","importance":0.8,"confidence":0.9,"speak":{"text":"こんにちは。","priority":"normal"},'
TAIL = '"context_patches":[],"reason_label":"other"}'


class Stream:
    def __init__(self, parts):
        self.parts, self.closed = parts, False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def __aiter__(self):
        for part in self.parts:
            if isinstance(part, asyncio.Event):
                await part.wait()
            elif isinstance(part, Exception):
                raise part
            else:
                yield NS(model="test-model", choices=[NS(delta=NS(content=part), finish_reason=None)])
        yield NS(model="test-model", choices=[NS(delta=NS(content=None), finish_reason="stop")])


def install(monkeypatch, responses):
    from config import settings
    import openai
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "offline-test-only")
    create = AsyncMock(side_effect=responses)
    sdk = NS(chat=NS(completions=NS(create=create)), close=AsyncMock())
    sdk.with_options = Mock(return_value=sdk)
    factory = Mock(return_value=sdk)
    monkeypatch.setattr(openai, "AsyncOpenAI", factory)
    return sdk, factory


def test_header_delivers_before_tail_and_reuses_session_client(monkeypatch):
    async def run():
        tail, ready = asyncio.Event(), asyncio.Event()
        stream = Stream([HEADER[:46], HEADER[46:], tail, TAIL])
        sdk, factory = install(monkeypatch, [stream, Stream(['{"decision":"silence"}'])])
        client = VNLLMClient(VNProfile(session_id="stream"))
        delivered, metrics = [], {}
        async def on_ready(header, text_complete):
            delivered.append(header)
            ready.set()
        task = asyncio.create_task(client.complete_json([], lane="immediate", on_ready=on_ready, metrics=metrics))
        await asyncio.wait_for(ready.wait(), 2)
        assert not task.done(), "speech delivery must not wait for the JSON memory tail"
        assert delivered[0]["speak"]["text"] == "こんにちは。"
        tail.set()
        parsed, raw = await task
        assert parsed == json.loads(raw) and len(delivered) == 1
        assert metrics["first_token_at_ms"] <= metrics["speech_prefix_at_ms"] <= metrics["completed_at_ms"]
        await client.complete_json([], lane="immediate")
        assert factory.call_count == 1
        assert all(call.kwargs["stream"] for call in sdk.chat.completions.create.call_args_list)
        assert sdk.with_options.call_args.kwargs == {"max_retries": 0}
        await client.aclose()
        sdk.close.assert_awaited_once()
        assert stream.closed
    asyncio.run(run())


@pytest.mark.parametrize("tail", [
    '"decision":"hold"}',  # Duplicate fields cannot rewrite a delivered decision.
    '"context_patches":[{"text":"unfinished',
    RuntimeError("connection interrupted"),
])
def test_invalid_tail_never_retries_or_rewrites_header(monkeypatch, tail):
    async def run():
        sdk, _ = install(monkeypatch, [Stream([HEADER, tail])])
        client = VNLLMClient(VNProfile(session_id="broken-tail"))
        ready = AsyncMock()
        parsed, raw = await client.complete_json([], lane="immediate", on_ready=ready)
        assert parsed is None and raw.startswith(HEADER)
        ready.assert_awaited_once()
        sdk.chat.completions.create.assert_awaited_once()
        await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("raw", [
    HEADER[:45],  # Decision/playback fields are not all available.
    HEADER.replace('"decision":"speak"', '"decision":"silence"'),
    HEADER.replace('"confidence":0.9', '"confidence":true'),
    HEADER.replace('"importance":0.8', '"importance":NaN'),
    HEADER.replace('"こんにちは。"', '""'),
])
def test_incomplete_or_invalid_headers_cannot_authorize_speech(raw):
    assert _speech_prefix(raw) is None


def test_json_string_escapes_and_fences_are_parsed_as_data():
    text = '文字 \\ "quoted" { brace } and \n newline'
    prefix = json.dumps({"decision": "speak", "importance": .8, "confidence": .9,
                         "speak": {"text": text}}, ensure_ascii=True)[:-1] + ','
    for position in range(len(prefix) - 2):
        assert _speech_prefix(prefix[:position]) is None
    assert _speech_prefix('```json\n' + prefix)[0]["speak"]["text"] == text


def test_settings_change_does_not_close_an_inflight_old_client(monkeypatch):
    async def run():
        from config import settings
        release, ready = asyncio.Event(), asyncio.Event()
        first, factory = install(monkeypatch, [Stream([HEADER, release, TAIL])])
        second = NS(chat=NS(completions=NS(create=AsyncMock(return_value=Stream(['{"decision":"silence"}'])))),
                    close=AsyncMock())
        second.with_options = Mock(return_value=second)
        factory.side_effect = [first, second]
        client = VNLLMClient(VNProfile(session_id="settings-change"))
        async def on_ready(_header, text_complete):
            ready.set()
        pending = asyncio.create_task(client.complete_json([], lane="immediate", on_ready=on_ready))
        await asyncio.wait_for(ready.wait(), 2)
        monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "new-offline-test-key")
        assert (await client.complete_json([], lane="immediate"))[0]["decision"] == "silence"
        first.close.assert_not_awaited()
        release.set()
        assert (await pending)[0]["decision"] == "speak"
        await client.aclose()
        first.close.assert_awaited_once()
        second.close.assert_awaited_once()
    asyncio.run(run())
