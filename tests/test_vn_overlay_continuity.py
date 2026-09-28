"""Real native atlas player + real overlay transition methods, with a fake Tk clock/display."""
import hashlib
import json
import sys
from types import SimpleNamespace

import PIL
from PIL import Image

from render.companion_atlas_tk import AtlasPlayer
from tools.vn_portrait_overlay_lite import overlay_class


def test_sentence_gap_changes_thinking_variant_directly_but_final_stop_returns_idle(tmp_path, monkeypatch):
    states = {}
    for mode in ("idle", "speaking", "speakingAlternate"):
        file = tmp_path / f"{mode}.webp"
        with Image.new("RGBA", (4, 2), (100, 150, 200, 255)) as image:
            image.save(file, lossless=True)
        states[mode] = {"url": file.name, "size": 2, "columns": 2, "sequence": [0, 1],
                        "durationMs": 1000, "decodedBytes": 32, "fileBytes": file.stat().st_size,
                        "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    (tmp_path / "manifest.json").write_text(json.dumps({"format": "amadeus.companion-atlas.v1",
        "emotions": {name: states for name in ("normal", "thinking", "happy")}}), encoding="utf-8")
    now, timers, identity = [0.0], {}, [0]
    def after(ms, callback):
        identity[0] += 1
        timers[identity[0]] = (now[0] + ms / 1000, callback)
        return identity[0]
    def advance(ms):
        target = now[0] + ms / 1000
        while timers:
            key = min(timers, key=lambda key: timers[key][0])
            due, callback = timers[key]
            if due > target:
                break
            timers.pop(key)
            now[0] = due
            callback()
        now[0] = target
    class OriginalShell:
        def _set_emotion(self, emotion, state="idle"):
            self._current_emotion, self._current_state = emotion, state
    legacy = SimpleNamespace(PortraitOverlayTk=OriginalShell, clean_display_text=lambda text: text,
                             infer_emotion=lambda text, explicit: (explicit or "normal", 6500))
    cls = overlay_class(legacy)
    overlay = cls.__new__(cls)
    overlay._lite = AtlasPlayer(tmp_path, clock=lambda: now[0])
    overlay._static_idle = False
    overlay._atlas_timer = overlay._return_timer = None
    overlay._sentence_id = ""
    overlay._current_state, overlay._current_emotion = "idle", "normal"
    overlay._active_until = 0.0
    overlay.avatar_size = 2
    overlay.avatar_label = SimpleNamespace(configure=lambda **kwargs: None)
    overlay.frame = SimpleNamespace(itemconfigure=lambda *args, **kwargs: None)
    overlay._signal_label = "signal"
    text = [""]
    overlay.text_var = SimpleNamespace(set=lambda value: text.__setitem__(0, value))
    overlay.root = SimpleNamespace(after=after, after_cancel=lambda key: timers.pop(key, None),
                                   winfo_viewable=lambda: True, deiconify=lambda: None, lift=lambda: None)
    image_tk = SimpleNamespace(PhotoImage=lambda image, master: object())
    monkeypatch.setitem(sys.modules, "PIL.ImageTk", image_tk)
    monkeypatch.setattr(PIL, "ImageTk", image_tk, raising=False)
    def event(sentence, speaking, emotion="thinking"):
        overlay.apply_reaction({"source": "vn_playback", "sentence_id": sentence,
                                "speaking": speaking, "emotion": emotion})
    try:
        event("first", True)
        advance(100)
        first_spec = overlay._lite.spec
        event("first", False)
        assert overlay._lite.spec is first_spec and overlay._lite.paused
        frozen_elapsed = overlay._lite.elapsed
        deadline = overlay._return_timer
        advance(100)
        event("first", False)
        assert overlay._return_timer == deadline, "duplicate stops must not extend the hold"
        assert overlay._lite.elapsed == frozen_elapsed and overlay._current_emotion == "thinking"
        event("second", True)
        assert overlay._lite.spec["url"] == "speakingAlternate.webp" and not overlay._lite.paused
        assert overlay._current_emotion == "thinking" and overlay._current_state == "speaking"
        assert overlay._lite.speech_counts["thinking"] == 2
        event("second", True)
        assert overlay._lite.speech_counts["thinking"] == 2, "a duplicate start must not select another variant"
        advance(400)
        assert overlay._current_emotion == "thinking", "cancelled old return must not fire"
        event("second", False)
        advance(100)
        event("third", True, "happy")
        assert overlay._current_emotion == "happy", "a genuine expression change must still apply"
        event("second", False)
        assert overlay._current_emotion == "happy" and not overlay._lite.paused
        event("third", False)
        advance(351)
        assert overlay._current_emotion == "normal" and overlay._current_state == "idle"
        event("next-turn", True)
        assert overlay._lite.spec["url"] == "speaking.webp" and overlay._lite.speech_counts["thinking"] == 3
    finally:
        overlay._lite.close()
