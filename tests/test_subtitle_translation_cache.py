"""Wallpaper cache entries and pending work belong to their translation policy."""
import asyncio

import pytest

from tts.sentence_state import PreTranslationCache


@pytest.mark.asyncio
@pytest.mark.parametrize("old_result", ["旧的中文译文", RuntimeError("old request failed")])
async def test_rebinding_discards_cached_and_late_results(old_result):
    started, release = asyncio.Event(), asyncio.Event()

    async def chinese(_text):
        started.set()
        await release.wait()
        if isinstance(old_result, Exception):
            raise old_result
        return old_result

    async def english(_text):
        return "New English translation."

    cache = PreTranslationCache(chinese)
    await cache.start_translation("old", "同じ文章よ。")
    await started.wait()
    old_entry = await cache.get_translation("同じ文章よ。")
    cache.cache["既存よ。"] = {"status": "completed", "chinese": "已有译文"}
    cache.set_translate_fn(english)
    assert await cache.get_translation("同じ文章よ。") is None
    assert await cache.get_translation("既存よ。") is None
    await cache.start_translation("new", "同じ文章よ。")

    async def settle(entry):
        while entry["status"] == "translating":
            await asyncio.sleep(0)

    new_entry = await cache.get_translation("同じ文章よ。")
    await asyncio.wait_for(settle(new_entry), 1)
    release.set()
    await asyncio.wait_for(settle(old_entry), 1)
    assert await cache.get_translation("同じ文章よ。") == {
        "status": "completed", "chinese": "New English translation.",
    }


@pytest.mark.asyncio
async def test_queued_translation_captures_policy_before_task_starts():
    translated = []

    async def chinese(_text):
        translated.append("zh-CN")
        return "中文"

    async def english(_text):
        translated.append("en-US")
        return "English"

    cache = PreTranslationCache(chinese)
    await cache.start_translation("old", "待って。")
    old_entry = await cache.get_translation("待って。")
    cache.set_translate_fn(english)
    async def settled():
        while old_entry["status"] == "translating":
            await asyncio.sleep(0)
    await asyncio.wait_for(settled(), 1)
    assert translated == ["zh-CN"]
    assert await cache.get_translation("待って。") is None
