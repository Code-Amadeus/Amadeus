from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import presentation_runtime
from server.ai_os_schema import canvas_payload, presentation_message, work_signal
from server.canvas_presentation import project_canvas_presentation
from server.handlers.wallpaper_handler import WallpaperHandler


def _process_canvas() -> dict:
    return canvas_payload(
        mode="workflow",
        phase="Work",
        title="Codex work signal",
        lead="Provider-authored milestone stays verbatim.",
        signals=[
            work_signal(
                label="provider",
                text="Codex is in the work phase.",
                detail="run-1",
                presentation={
                    "text": presentation_message(
                        "provider.phase",
                        provider="Codex",
                        phase="Work",
                    )
                },
            ),
            work_signal(
                label="report",
                text="Provider-authored milestone stays verbatim.",
                detail="semantic",
            ),
        ],
        metadata={"provider": "codex", "run_id": "run-1"},
        presentation={
            "title": presentation_message("provider.work_signal", provider="Codex"),
        },
    )


def test_process_copy_follows_locale_without_translating_provider_content() -> None:
    canonical = _process_canvas()

    chinese = project_canvas_presentation(canonical, locale="zh-CN")
    assert chinese["phase"] == "执行"
    assert chinese["title"] == "Codex 工作进展"
    assert chinese["signals"][0]["label"] == "Provider"
    assert chinese["signals"][0]["text"] == "Codex 正处于执行阶段。"
    assert chinese["signals"][1]["label"] == "报告"
    assert chinese["signals"][1]["text"] == "Provider-authored milestone stays verbatim."

    japanese = project_canvas_presentation(canonical, locale="ja-JP")
    assert japanese["phase"] == "実行"
    assert japanese["title"] == "Codex 作業進捗"
    assert japanese["signals"][0]["text"] == "Codex は実行フェーズです。"

    # Projection is non-destructive so one canonical payload can be rendered
    # again after a live locale switch.
    assert canonical["phase"] == "Work"
    assert canonical["title"] == "Codex work signal"


def test_nested_artifact_copy_changes_only_when_explicitly_marked() -> None:
    payload = _process_canvas()
    payload["diff"] = {
        "message": "Working tree is clean.",
        "files": [{"path": "src/语言.ts", "patch": "+const label = '原文';"}],
    }
    payload["metadata"]["presentation"]["diff.message"] = presentation_message("diff.clean")

    projected = project_canvas_presentation(payload, locale="zh-CN")
    assert projected["diff"]["message"] == "工作区没有变更。"
    assert projected["diff"]["files"] == payload["diff"]["files"]


def test_historical_codex_app_server_result_uses_the_current_display_alias() -> None:
    payload = canvas_payload(
        mode="markdown",
        phase="Result",
        title="Codex App Server result report",
        presentation={
            "title": presentation_message(
                "provider.result_report",
                provider="Codex App Server",
            ),
            "markdown": presentation_message(
                "provider.result_markdown",
                provider="Codex App Server",
                status="done",
                task="Test",
                tools="none",
                result="Done.",
            ),
        },
    )
    payload["markdown"] = "### Codex App Server result\nProcess: `done`\nTask: Test\nTools: none\n\nDone."

    projected = project_canvas_presentation(payload, locale="en-US")

    assert projected["title"] == "Codex result report"
    assert projected["markdown"].startswith("### Codex result\n")


def test_wallpaper_handler_reprojects_cached_canvas_on_live_locale_change() -> None:
    class Host:
        def __init__(self) -> None:
            self.canvases: list[dict] = []
            self.profiles: list[dict] = []

        def set_canvas(self, payload: dict) -> None:
            self.canvases.append(payload)

        def set_canvas_presentation(self, profile: dict) -> None:
            self.profiles.append(profile)

    original = presentation_runtime.get_config()
    try:
        presentation_runtime.set_config(
            {"presentation_locale": "en-US"},
            render_current=False,
        )
        handler = WallpaperHandler()
        host = Host()
        handler._wallpaper_host = host
        assert handler._apply_canvas(_process_canvas()) is True
        assert host.canvases[-1]["title"] == "Codex work signal"

        handler.set_canvas_presentation({"presentation_locale": "zh-CN"})
        assert host.profiles[-1]["presentation_locale"] == "zh-CN"
        assert host.canvases[-1]["title"] == "Codex 工作进展"
        assert handler._last_canvas_payload is not None
        assert handler._last_canvas_payload["title"] == "Codex work signal"
    finally:
        presentation_runtime.set_config(original, render_current=False)
