from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from core.session_manager import conversation_history
from server import chat_translation_runtime
from server.handlers.chat_handler import ChatHandler
from server.handlers.system_handler import SystemHandler
from server.protocol import Method
from server.wallpaper_subtitle_translator import _clean_translation


def test_chat_translation_setting_is_independent_and_disabled_by_default() -> None:
    original = chat_translation_runtime.is_enabled()
    try:
        chat_translation_runtime.set_config({"chat_translation_subtitles_enabled": False})
        assert chat_translation_runtime.get_config() == {
            "chat_translation_subtitles_enabled": False
        }
        assert chat_translation_runtime.set_config(
            {"chat_translation_subtitles_enabled": True}
        ) == ["chat_translation_subtitles_enabled"]
        assert chat_translation_runtime.is_enabled() is True
    finally:
        chat_translation_runtime.set_config({"chat_translation_subtitles_enabled": original})


def test_chat_translation_setting_rejects_non_boolean_values() -> None:
    with pytest.raises(ValueError, match="must be a boolean"):
        chat_translation_runtime.set_config({"chat_translation_subtitles_enabled": "true"})


def test_disabled_or_non_japanese_chat_translation_never_calls_provider() -> None:
    async def run() -> None:
        original = chat_translation_runtime.is_enabled()
        translate = AsyncMock(return_value="不应调用")
        try:
            chat_translation_runtime.set_config({"chat_translation_subtitles_enabled": False})
            with patch(
                "server.wallpaper_subtitle_translator.translate_presentation_subtitle",
                new=translate,
            ):
                disabled = await chat_translation_runtime.translate_completed_message(
                    "作業は終わったわ。"
                )
                chat_translation_runtime.set_config({"chat_translation_subtitles_enabled": True})
                skipped = await chat_translation_runtime.translate_completed_message(
                    "The work is complete."
                )
                chinese = await chat_translation_runtime.translate_completed_message(
                    "工作已经完成。"
                )
            assert disabled == {"status": "disabled", "translation": ""}
            assert skipped == {"status": "not_japanese", "translation": ""}
            assert chinese == {"status": "not_japanese", "translation": ""}
            translate.assert_not_awaited()
        finally:
            chat_translation_runtime.set_config({"chat_translation_subtitles_enabled": original})

    asyncio.run(run())


def test_chat_translate_uses_clean_display_text_without_writing_history() -> None:
    async def run() -> None:
        original_enabled = chat_translation_runtime.is_enabled()
        history_before = [dict(item) for item in conversation_history.dialog]
        try:
            chat_translation_runtime.set_config({"chat_translation_subtitles_enabled": True})
            with patch(
                "server.wallpaper_subtitle_translator.translate_presentation_subtitle",
                new=AsyncMock(return_value="工作已经完成。"),
            ) as translate:
                result = await ChatHandler().handle(
                    Method.CHAT_TRANSLATE,
                    {"text": "作業は終わったわ。", "turn_id": "turn-clean"},
                )
            assert result == {
                "status": "translated",
                "translation": "工作已经完成。",
                "turn_id": "turn-clean",
            }
            translate.assert_awaited_once_with("作業は終わったわ。")
            assert conversation_history.dialog == history_before
        finally:
            chat_translation_runtime.set_config(
                {"chat_translation_subtitles_enabled": original_enabled}
            )

    asyncio.run(run())


def test_chat_translation_output_uses_existing_subtitle_cleaner() -> None:
    assert _clean_translation("```text\n翻译：工作已经完成。\n```") == "工作已经完成。"


def test_system_setting_applies_chat_translation_toggle_without_restart() -> None:
    async def run() -> None:
        original = chat_translation_runtime.is_enabled()
        handler = SystemHandler()
        try:
            handler._get_config = AsyncMock(  # type: ignore[method-assign]
                return_value={"chat_translation_subtitles_enabled": True}
            )
            with patch("server.handlers.system_handler.bus.emit", new=AsyncMock()):
                result = await handler._set_config(
                    {"values": {"chat_translation_subtitles_enabled": True}}
                )
            assert result["values"]["chat_translation_subtitles_enabled"] is True
            assert "chat_translation_subtitles_enabled" in result["updated"]
        finally:
            chat_translation_runtime.set_config({"chat_translation_subtitles_enabled": original})

    asyncio.run(run())
