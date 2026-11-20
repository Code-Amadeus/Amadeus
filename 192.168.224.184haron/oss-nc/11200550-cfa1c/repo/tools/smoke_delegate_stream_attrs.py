from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("AMADEUS_HEADLESS", "1")

from llm.stream_parser import StreamTagParser  # noqa: E402
from tools.text_utils import parse_tags_and_clean  # noqa: E402

_stream_parser = StreamTagParser()


def reset_stream_parser() -> None:
    _stream_parser.reset()


def process_stream_chunk(raw_text: str) -> tuple[str, list[dict]]:
    return _stream_parser.process_chunk(raw_text)


def assert_browser_delegate(action: dict) -> None:
    attrs = action.get("attrs") or {}
    assert action.get("type") == "DELEGATE", action
    assert attrs.get("provider") == "browser", attrs
    assert attrs.get("action") == "click_text", attrs
    assert attrs.get("text") == "Search", attrs
    assert attrs.get("task") == "Type Amadeus in the search box", attrs


def main_smoke() -> None:
    tag = '[DELEGATE provider="browser" action="click_text" text="Search" task="Type Amadeus in the search box"]'

    clean, actions = parse_tags_and_clean(f"Hold on. {tag}")
    assert "DELEGATE" not in clean, clean
    assert len(actions) == 1, actions
    assert_browser_delegate(actions[0])

    reset_stream_parser()
    clean1, actions1 = process_stream_chunk("Hold on. [DELE")
    clean2, actions2 = process_stream_chunk('GATE provider="browser" action="click_text" text="Search" task="Type Amadeus in the search box"]')
    assert clean1 == "Hold on. ", clean1
    assert clean2 == "", clean2
    assert not actions1, actions1
    assert len(actions2) == 1, actions2
    assert_browser_delegate(actions2[0])

    reset_stream_parser()
    clean3, actions3 = process_stream_chunk(f"Hold on. {tag} I already searched it.")
    clean4, actions4 = process_stream_chunk(" More fake result text.")
    assert clean3 == "Hold on. ", clean3
    assert len(actions3) == 1, actions3
    assert_browser_delegate(actions3[0])
    assert clean4 == "", clean4
    assert actions4 == [], actions4

    print("delegate stream attrs smoke ok")


if __name__ == "__main__":
    main_smoke()
