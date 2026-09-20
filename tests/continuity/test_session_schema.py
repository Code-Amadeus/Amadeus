from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from core import session_manager as sm
from core.session_manager import ConversationHistory


def test_new_history_entries_receive_timestamp_and_shared_turn_identity() -> None:
    history = ConversationHistory()
    history.add_user("hello", turn_id="turn-1")
    history.add_assistant("hi", turn_id="turn-1")

    assert history.dialog[0]["turn_id"] == "turn-1"
    assert history.dialog[1]["turn_id"] == "turn-1"
    assert datetime.fromisoformat(history.dialog[0]["created_at"]).tzinfo is not None
    assert datetime.fromisoformat(history.dialog[1]["created_at"]).tzinfo is not None


def test_legacy_session_without_turn_metadata_still_loads(tmp_path) -> None:
    old_dir = sm._SESSION_DIR
    old_dialog = sm.conversation_history.dialog
    old_sid = sm._CURRENT_SESSION_ID
    try:
        sm._SESSION_DIR = str(tmp_path)
        payload = {
            "session_id": "legacy-c1",
            "dialog": [
                {"role": "user", "content": "legacy user"},
                {"role": "assistant", "content": "legacy assistant"},
            ],
            "enable_conversation": True,
        }
        Path(tmp_path, "legacy-c1.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

        ok, enabled = sm.load_session("legacy-c1")
        assert ok is True and enabled is True
        assert "created_at" not in sm.conversation_history.dialog[0]
        assert "turn_id" not in sm.conversation_history.dialog[0]
    finally:
        sm._SESSION_DIR = old_dir
        sm.conversation_history.dialog = old_dialog
        sm._CURRENT_SESSION_ID = old_sid
