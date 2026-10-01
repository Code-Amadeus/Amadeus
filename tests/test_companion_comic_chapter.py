"""Comic chapter summarization is cached and spoiler-safe."""

from __future__ import annotations

from core.companion import CompanionRuntime


ONE_PIXEL_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_comic_chapter_summary_is_cached_and_future_is_not_loaded(tmp_path, monkeypatch) -> None:
    calls = []

    def fake_vision(image_base64: str, *, prompt: str = "", **kwargs):
        calls.append({"chars": len(image_base64), "prompt": prompt})
        return {"description": "第一话摘要：主角在车站重逢，并发现一封旧信。"}

    monkeypatch.setattr("llm.qwen_client.qwen_vision_describe", fake_vision)
    runtime = CompanionRuntime(tmp_path)
    payload = {
        "book_id": "book:comic-1",
        "chapter": "第一话 车站",
        "images": [{"data_url": ONE_PIXEL_PNG, "width": 1, "height": 1}],
        "next_url": "https://example.invalid/comic/chapter-2",
    }
    try:
        first = runtime._handle_comic_chapter(payload)
        assert first["ok"] is True
        assert "主角在车站重逢" in first["summary"]
        assert first.get("cached") is None
        assert len(calls) == 1

        second = runtime._handle_comic_chapter(payload)
        assert second["cached"] is True
        assert len(calls) == 1

        context = runtime.context_block("第一话讲了什么")
        assert "第一话摘要" in context
        assert "future_content=marked_but_not_loaded" in context
        assert "chapter-2" not in context
        assert runtime.scene_status()["hasFuture"] is True
    finally:
        runtime.close()
