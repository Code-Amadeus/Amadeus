from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.ai_os_schema import (  # noqa: E402
    browser_canvas_payload,
    canvas_payload,
    markdown_canvas_payload,
    work_note_payload,
    work_signal,
)


def main() -> None:
    signal = work_signal(
        kind="report",
        label="report",
        text="Found a useful source and kept the browser session alive.",
        detail="semantic",
        importance="important",
    )
    assert signal["schema_id"] == "amadeus.ai_os.v1"
    assert signal["kind"] == "report"
    assert signal["importance"] == "important"
    assert work_signal(kind="provider_specific_detail", label="x", text="y")["kind"] == "status"

    workflow = canvas_payload(
        mode="unknown-provider-mode",  # unknown modes collapse to workflow
        phase="active_work",
        title="Provider work signal",
        lead="Compact progress belongs on the CRT surface.",
        progress=42,
        signals=[signal],
    )
    assert workflow["mode"] == "workflow"
    assert workflow["phase"] == "Work"
    assert workflow["signals"][0]["label"] == "report"

    browser = browser_canvas_payload(
        phase="Preview",
        title="Example",
        excerpt="Readable browser source excerpt.",
        url="https://example.com/",
        browser_session_id="browser_abc",
        links=[{"title": "Docs", "url": "https://example.com/docs"}],
        screenshot="data:image/png;base64,abc",
        signals=[signal],
        progress=80,
    )
    assert browser["mode"] == "browser"
    assert browser["browserSessionId"] == "browser_abc"
    assert browser["artifact"]["kind"] == "browser.snapshot"
    assert browser["artifact"]["content"]["browserSessionId"] == "browser_abc"

    markdown = markdown_canvas_payload(
        phase="Result",
        title="OpenClaw result report",
        lead="Result summary.",
        markdown="### Result\nDone.",
        signals=[signal],
    )
    assert markdown["mode"] == "markdown"
    assert markdown["markdown"].startswith("### Result")
    assert markdown["artifact"]["kind"] == "markdown.report"

    note = work_note_payload(
        source="provider",
        provider="openclaw",
        run_id="openclaw_123",
        session_id="session_1",
        phase="result",
        title="OpenClaw result report",
        summary="Done.",
        signals=[signal],
        importance="important",
    )
    assert note["phase"] == "Result"
    assert note["observer_policy"] == "auto"
    assert note["speak"] is False

    print("ai os runtime schema smoke ok")


if __name__ == "__main__":
    main()
