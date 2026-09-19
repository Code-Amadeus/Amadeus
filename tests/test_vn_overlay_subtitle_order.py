"""Cached subtitles must follow playback identity even on an already-running event loop."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from server import vn_tts_bridge as bridge
from tools.vn_portrait_overlay_lite import overlay_class


def overlay_without_window():
    # Use the real projection method, replacing only Tk drawing/window operations.
    legacy = SimpleNamespace(PortraitOverlayTk=object, clean_display_text=lambda text: text,
                             infer_emotion=lambda text, explicit: (explicit or "normal", 6500))
    cls = overlay_class(legacy)
    overlay = cls.__new__(cls)
    overlay._lite = object()
    overlay._sentence_id = ""
    overlay._return_timer = None
    overlay._current_state = "idle"
    overlay._current_emotion = "normal"
    overlay._active_until = 0.0
    overlay.frame = SimpleNamespace(itemconfigure=lambda *args, **kwargs: None)
    overlay._signal_label = "signal"
    overlay._draw_lite = lambda: None
    values = {"text": ""}
    overlay.text_var = SimpleNamespace(set=lambda text: values.update(text=text), get=lambda: values["text"])
    overlay.root = SimpleNamespace(after=lambda *args: 1, after_cancel=lambda *args: None,
                                   deiconify=lambda: None, lift=lambda: None)
    def set_emotion(emotion, state="idle", **kwargs):
        overlay._current_emotion, overlay._current_state = emotion, state
    overlay._set_emotion = set_emotion
    return overlay


def test_consecutive_cached_subtitles_update_real_overlay_and_old_events_stay_rejected():
    async def run():
        overlay = overlay_without_window()
        loop = asyncio.get_running_loop()
        delivered = []
        def post(url, payload, timeout):
            delivered.append((payload["source"], payload["sentence_id"]))
            overlay.apply_reaction(payload)
        meta = {key: {"overlay_url": "http://127.0.0.1:8788/reaction"} for key in ("first", "second", "third")}
        with patch.dict(bridge._SENTENCE_META, meta, clear=True), patch.object(bridge, "_post_json", side_effect=post):
            first = bridge.schedule_overlay_playback("first", True, loop)
            await first
            # First translation arrives late; subsequent cached translations arrive immediately.
            await bridge.publish_overlay_subtitle("first", "first Japanese", "第一句")
            assert overlay.text_var.get() == "第一句"
            for previous, current, text in (("first", "second", "第二句"), ("second", "third", "第三句")):
                stopped = bridge.schedule_overlay_playback(previous, False, loop)
                started = bridge.schedule_overlay_playback(current, True, loop)
                caption = asyncio.create_task(bridge.publish_overlay_subtitle(current, "Japanese", text))
                await asyncio.gather(stopped, started, caption)
                assert overlay.text_var.get() == text, f"expected {text!r}, got {overlay.text_var.get()!r}"
                assert overlay._sentence_id == current and overlay._current_state == "speaking"
                assert delivered[-2:] == [("vn_playback", current), ("vn_pretranslation", current)]
                await bridge.publish_overlay_subtitle(previous, "stale", "过期字幕")
                await bridge.publish_overlay_playback(previous, False)
                assert overlay.text_var.get() == text and overlay._current_state == "speaking"
    asyncio.run(run())


def test_threaded_playback_callback_still_dispatches_on_host_loop():
    async def run():
        loop = asyncio.get_running_loop()
        delivered = []
        async def publish(sentence_id, speaking):
            delivered.append((sentence_id, speaking, asyncio.get_running_loop() is loop))
        with patch.object(bridge, "publish_overlay_playback", side_effect=publish):
            future = await asyncio.to_thread(bridge.schedule_overlay_playback, "threaded", True, loop)
            await asyncio.wrap_future(future)
        assert delivered == [("threaded", True, True)]
    asyncio.run(run())
