import os
import sys
import types
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import wallpaper_subtitle_runtime
from server import presentation_runtime
from server.assistant_language import (
    current_assistant_language,
    normalize_assistant_language,
    text_matches_assistant_language,
)
from server.task_lookup import render_current_status_facts
from server.work_observer import ObserverSession, WorkObserverCoordinator


def test_primary_language_does_not_follow_wallpaper_caption_mode():
    fake_pipeline = types.SimpleNamespace(TTS_OUTPUT_LANGUAGE="日文")
    original_mode = wallpaper_subtitle_runtime.get_mode()

    try:
        with patch.dict(sys.modules, {"tts.pipeline": fake_pipeline}):
            for mode in ("zh", "ja", "bilingual", "off"):
                wallpaper_subtitle_runtime.set_mode(mode, render_current=False)
                assert current_assistant_language() == "japanese"
    finally:
        wallpaper_subtitle_runtime.set_mode(original_mode, render_current=False)


def test_presentation_locale_and_caption_mode_are_independent():
    original = presentation_runtime.get_config()
    try:
        presentation_runtime.set_config(
            {"presentation_locale": "en-US", "wallpaper_caption_mode": "source"},
            render_current=False,
        )
        assert presentation_runtime.get_config() == {
            "presentation_locale": "en-US",
            "wallpaper_caption_mode": "source",
        }
        presentation_runtime.set_legacy_caption_setting("bilingual", render_current=False)
        assert presentation_runtime.get_config() == {
            "presentation_locale": "zh-CN",
            "wallpaper_caption_mode": "bilingual",
        }
    finally:
        presentation_runtime.set_config(original, render_current=False)


def test_presentation_locale_defaults_to_english():
    assert presentation_runtime.normalize_presentation_locale(None) == "en-US"
    assert presentation_runtime.normalize_presentation_locale("unknown") == "en-US"
    assert presentation_runtime.normalize_presentation_locale("zh-CN") == "zh-CN"


def test_presentation_update_has_one_canonical_profile_and_renderer():
    original = presentation_runtime.get_config()
    rendered = []
    try:
        presentation_runtime.set_renderer(rendered.append)
        rendered.clear()
        presentation_runtime.set_config(
            {"presentation_locale": "ja-JP", "wallpaper_caption_mode": "off"}
        )
        assert rendered == [
            {"presentation_locale": "ja-JP", "wallpaper_caption_mode": "off"}
        ]
        assert "wallpaper_subtitle_language" not in presentation_runtime.get_config()
    finally:
        presentation_runtime.set_renderer(None)
        presentation_runtime.set_config(original, render_current=False)


def test_language_normalization_has_one_primary_axis():
    assert normalize_assistant_language("日文") == "japanese"
    assert normalize_assistant_language("ja-JP") == "japanese"
    assert normalize_assistant_language("英文") == "english"
    assert normalize_assistant_language("en-US") == "english"
    assert text_matches_assistant_language("作業は終わったわ。", "japanese")
    assert not text_matches_assistant_language("工作已经结束。", "japanese")
    assert text_matches_assistant_language("The work is complete.", "english")
    assert text_matches_assistant_language("工作已经结束。", "simplified_chinese")
    assert not text_matches_assistant_language("作業は終わったわ。", "simplified_chinese")


def test_host_status_uses_the_primary_japanese_line():
    display, voice = render_current_status_facts(
        {
            "stage_key": "running",
            "recent_zh": "已完成规则设计",
            "recent_ja": "ルール設計が完了した",
            "blocker_zh": "没有已知阻碍",
            "blocker_ja": "既知の障害はない",
            "next_zh": "继续实现",
            "next_ja": "実装を続ける",
            "fact_kind": "activity_milestone",
        },
        display_language="japanese",
    )

    assert display == voice
    assert "ルール設計" in display
    assert "已完成" not in display


def test_host_status_flows_from_a_localized_milestone_without_quote_template():
    display, _voice = render_current_status_facts(
        {
            "stage_key": "running",
            "recent_ja": "単一HTMLで実装する方針に決定しました。",
            "blocker_zh": "没有已知阻碍",
            "blocker_ja": "既知の障害はない",
            "next_ja": "実装を続ける",
            "fact_kind": "activity_milestone",
        },
        display_language="japanese",
    )
    assert "決定しました。まで" not in display
    assert "単一HTMLで実装する方針に決定しました" in display
    assert "直近の確認内容は" not in display
    assert "「" not in display


def test_observer_keeps_one_valid_japanese_line_across_voice_and_chat():
    observer = WorkObserverCoordinator()
    session = ObserverSession(
        narration_id="narration_a",
        run_id="run_a",
        session_id="session_a",
        provider="locus",
    )
    merged = observer._merge_decision_defaults(
        {
            "source": "work_observer_llm",
            "display_language": "japanese",
            "action": "final_report",
            "terminal": True,
            "append_to_main_chat": True,
            "speak": True,
            "display_text": "実装は完了したわ。自動検証も通っている。",
            "main_chat_entry": "功能已经完成并通过了自动验证。",
        },
        session,
        {"phase": "result", "summary": "自動検証まで完了した。"},
    )
    assert merged["display_text"] == "実装は完了したわ。自動検証も通っている。"
    assert merged["main_chat_entry"] == merged["display_text"]
    assert merged["speak"] is True


def test_observer_uses_bounded_host_japanese_when_both_outputs_are_wrong():
    observer = WorkObserverCoordinator()
    session = ObserverSession(
        narration_id="narration_b",
        run_id="run_b",
        session_id="session_b",
        provider="locus",
    )
    merged = observer._merge_decision_defaults(
        {
            "source": "work_observer_llm",
            "display_language": "japanese",
            "action": "final_report",
            "terminal": True,
            "append_to_main_chat": True,
            "speak": True,
            "display_text": "功能已经完成。",
            "main_chat_entry": "自动验证也通过了。",
        },
        session,
        {"phase": "result", "summary": "自動検証まで完了した。"},
    )
    assert text_matches_assistant_language(merged["display_text"], "japanese")
    assert merged["main_chat_entry"] == merged["display_text"]
    assert "功能已经完成" not in merged["display_text"]


def _main() -> None:
    test_primary_language_does_not_follow_wallpaper_caption_mode()
    test_presentation_locale_and_caption_mode_are_independent()
    test_presentation_update_has_one_canonical_profile_and_renderer()
    test_language_normalization_has_one_primary_axis()
    test_host_status_uses_the_primary_japanese_line()
    test_host_status_flows_from_a_localized_milestone_without_quote_template()
    test_observer_keeps_one_valid_japanese_line_across_voice_and_chat()
    test_observer_uses_bounded_host_japanese_when_both_outputs_are_wrong()
    print("ok: assistant language is independent from subtitle presentation")


if __name__ == "__main__":
    _main()
