"""The native VN shell keeps the established caption, emotion and duration contract."""
import http.client
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from render.vn_overlay_window import PortraitOverlayTk, clean_display_text, infer_emotion
from tools.vn_portrait_overlay_lite import overlay_class


def test_presentation_tags_and_installed_emotion_aliases():
    tagged = "[PARAM x=1] Look [EXPR smile][HOTKEY F1][ANIM blink][DELEGATE now] [EMO preset=surprised dur=2.5s]"
    assert clean_display_text(tagged) == "Look"
    assert infer_emotion(tagged, "thinking") == ("sided_surprised", 2500)
    assert infer_emotion("", "sad") == ("sad", 6500)
    assert infer_emotion("", "surprise") == ("sided_surprised", 6500)
    assert infer_emotion("", "serious_speaking") == ("sided_thinking", 6500)
    assert infer_emotion("[EMO preset=shy dur=.2s]") == ("blush", 1000)
    assert infer_emotion("[EMO preset=sad dur=" + "9" * 400 + "s]") == ("sad", 6500)


def test_missing_art_shell_keeps_captions_clean():
    shell = PortraitOverlayTk.__new__(PortraitOverlayTk)
    values = []
    shell.text_var = SimpleNamespace(set=values.append)
    shell._sentence_id = ""
    shell._set_emotion = lambda emotion, state: None
    shell.apply_reaction({"source": "vn_playback", "sentence_id": "s", "speaking": True,
                          "display_text": "Ready [EMO preset=sad]", "emotion": "sad"})
    assert values == ["Ready"]
    shell.apply_reaction({"source": "vn_pretranslation", "sentence_id": "old",
                          "display_text": "stale"})
    assert values == ["Ready"]


def test_non_playback_reactions_use_explicit_duration_then_tag_duration():
    class FakeShell:
        def _resolve_key(self, emotion):
            return emotion

    cls = overlay_class(SimpleNamespace(PortraitOverlayTk=FakeShell,
                                        clean_display_text=clean_display_text,
                                        infer_emotion=infer_emotion))
    overlay = cls.__new__(cls)
    overlay._lite = object()
    overlay._sentence_id = ""
    overlay._return_timer = None
    overlay._current_state = "idle"
    overlay._active_until = 0.0
    values, delays, poses = [], [], []
    overlay.text_var = SimpleNamespace(set=values.append)
    overlay._set_emotion = lambda emotion, state, **kwargs: poses.append((emotion, state))
    overlay.root = SimpleNamespace(after=lambda ms, callback: delays.append(ms) or len(delays),
                                   after_cancel=lambda timer: None,
                                   deiconify=lambda: None, lift=lambda: None)
    overlay.apply_reaction({"text": "Hello [EMO preset=surprised dur=2.5s]", "duration_ms": 4500})
    assert values == ["Hello"]
    assert poses == [("sided_surprised", "speaking")]
    assert delays == [4500]
    overlay.apply_reaction({"text": "Again [EMO preset=sad dur=3s]"})
    assert values[-1] == "Again"
    assert poses[-1] == ("sad", "speaking")
    assert delays[-1] == 3000


def test_invalid_reaction_is_rejected_before_queue_and_next_reaction_is_polled():
    root = MagicMock()
    root.winfo_fpixels.return_value = 96
    root.after.return_value = 1
    with patch("render.vn_overlay_window.tk.Tk", return_value=root), \
         patch("render.vn_overlay_window.tk.Canvas", return_value=MagicMock()), \
         patch("render.vn_overlay_window.tk.Label", return_value=MagicMock()), \
         patch("render.vn_overlay_window.tk.Button", return_value=MagicMock()), \
         patch("render.vn_overlay_window.tk.StringVar", return_value=MagicMock()):
        shell = PortraitOverlayTk(port=0)
    try:
        assert shell._sentence_id == ""
        shell.apply_reaction = Mock()
        port = shell._server.server_address[1]

        def post(payload):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                connection.request("POST", "/reaction", json.dumps(payload), {"Content-Type": "application/json"})
                response = connection.getresponse()
                response.read()
                return response.status
            finally:
                connection.close()

        assert post({"text": "bad", "duration_ms": "forever"}) == 400
        assert post({"text": "good", "duration_ms": 1200}) == 200
        shell._poll()
        shell.apply_reaction.assert_called_once_with({"text": "good", "duration_ms": 1200})
        assert shell._messages.empty()
    finally:
        shell._server.shutdown()
        shell._server.server_close()
        shell._thread.join(timeout=2)
