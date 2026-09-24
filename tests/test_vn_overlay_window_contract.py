"""The native VN shell keeps the established caption, emotion and duration contract."""
from types import SimpleNamespace

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
