"""Companion card close ownership: a closed card ends the session that owns it."""
from unittest.mock import patch

from render.vn_overlay_close import close_endpoint, close_target, post_close


def test_backend_ws_endpoint_maps_to_the_companion_close_endpoint():
    assert close_target("ws://127.0.0.1:17777/ws") == "http://127.0.0.1:17777/companion/card-close"
    assert close_target("") == ""


def test_vn_launches_keep_their_own_close_lifetime():
    assert close_endpoint("exit", "ws://127.0.0.1:17777/ws") == ""
    assert post_close("exit", "ws://127.0.0.1:17777/ws") is False


def test_companion_card_close_ends_the_owning_session():
    posted = []
    assert post_close(
        "card-close", "ws://127.0.0.1:17777/ws",
        post=lambda url, body: posted.append((url, body)),
    ) is True
    assert posted == [("http://127.0.0.1:17777/companion/card-close", {})]


def test_a_card_without_a_backend_reports_nothing():
    posted = []
    assert post_close("card-close", "", post=lambda url, body: posted.append((url, body))) is False
    assert posted == []


def test_an_unreachable_owner_never_blocks_the_window_close():
    with patch("render.vn_overlay_close.urllib.request.urlopen", side_effect=OSError("connection refused")):
        assert post_close("card-close", "ws://127.0.0.1:17777/ws") is False
