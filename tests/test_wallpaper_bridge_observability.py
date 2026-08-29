"""User-visible canvas routing keeps bounded failure attribution in logs."""

from __future__ import annotations

import logging

from wallpaper.wallpaper_engine_bridge import _BridgeState, _route_canvas_action


def test_canvas_action_route_logs_the_returned_failure_reason(caplog) -> None:
    state = _BridgeState()
    state.canvas_action_handler = lambda _payload: {
        "ok": False,
        "error": "stale_revision",
    }
    caplog.set_level(logging.INFO, logger="wallpaper.wallpaper_engine_bridge")

    result = _route_canvas_action(state, "work", {"action": "retry"})

    assert result == {"ok": False, "error": "stale_revision"}
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "target=work" in message
        and "action=retry" in message
        and "ok=False" in message
        and "error=stale_revision" in message
        for message in messages
    )


def test_canvas_action_route_bounds_untrusted_error_text(caplog) -> None:
    state = _BridgeState()
    state.canvas_action_handler = lambda _payload: {
        "ok": False,
        "error": "reason " + ("x" * 400),
    }
    caplog.set_level(logging.INFO, logger="wallpaper.wallpaper_engine_bridge")

    _route_canvas_action(state, "permission", {"action": "allow_once"})

    message = next(
        record.getMessage()
        for record in caplog.records
        if "canvas action routed" in record.getMessage()
    )
    logged_error = message.split(" error=", 1)[1]
    assert len(logged_error) == 240
    assert "\n" not in logged_error
