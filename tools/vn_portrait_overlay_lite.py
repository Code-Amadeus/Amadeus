"""Keep the original VN Tk window; replace only its portrait renderer when Lite is installed."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys

from PIL import Image, ImageTk

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from render.companion_atlas_tk import AtlasPlayer  # noqa: E402


def load_legacy(helper: Path):
    helper = helper.resolve(strict=True)
    # The existing VN helper imports its own portrait assets module.
    sys.path.insert(0, str(helper.parent))
    spec = importlib.util.spec_from_file_location("vn_portrait_original", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def overlay_class(legacy):
    class LitePortraitOverlay(legacy.PortraitOverlayTk):
        def __init__(self, *args, lite_dir: Path, static_idle: bool = False, **kwargs):
            self._lite_dir, self._static_idle = lite_dir, static_idle
            self._lite = None
            self._atlas_timer = self._return_timer = None
            self._sentence_id = ""
            super().__init__(*args, **kwargs)
            self.root.bind("<Unmap>", self._visibility, add="+")
            self.root.bind("<Map>", self._visibility, add="+")
            self.root.bind("<Destroy>", self._dispose, add="+")

        def _load_frames(self):
            if (self._lite_dir / "manifest.json").is_file():
                self._lite = AtlasPlayer(self._lite_dir)
            else:
                # The pack is optional; the original cache/legacy behavior remains unchanged.
                super()._load_frames()

        def _resolve_key(self, emotion):
            if not self._lite:
                return super()._resolve_key(emotion)
            key = legacy.EMOTION_ALIASES.get(emotion, emotion or "normal")
            return key if key in self._lite.emotions else "normal"

        def _set_emotion(self, emotion, state="idle"):
            super()._set_emotion(emotion, state)
            if self._lite:
                self._lite.select(self._current_emotion, self._current_state == "speaking", self._static_idle)
                self._draw_lite()

        def _draw_lite(self):
            if self._atlas_timer is not None:
                self.root.after_cancel(self._atlas_timer)
                self._atlas_timer = None
            frame, delay = self._lite.frame()
            if frame is not None:
                try:
                    if frame.size != (self.avatar_size, self.avatar_size):
                        resized = frame.convert("RGBa").resize((self.avatar_size, self.avatar_size), Image.Resampling.LANCZOS).convert("RGBA")
                        frame.close()
                        frame = resized
                    # No old per-frame tint/sweep/re-crop; display the same RGBA tile as Canvas.
                    photo = ImageTk.PhotoImage(frame, master=self.root)
                    self.avatar_label.configure(image=photo)
                    self.avatar_label.image = photo
                finally:
                    frame.close()
            if delay is not None:
                self._atlas_timer = self.root.after(delay, self._draw_lite)

        def _visibility(self, event):
            if self._lite and event.widget == self.root:
                self._lite.set_paused(not self.root.winfo_viewable())
                self._draw_lite()

        def _dispose(self, event):
            if event.widget == self.root:
                for timer in (self._atlas_timer, self._return_timer):
                    if timer is not None:
                        self.root.after_cancel(timer)
                if self._lite:
                    self._lite.close()

        def apply_reaction(self, payload):
            if not self._lite:
                return super().apply_reaction(payload)
            sentence = str(payload.get("sentence_id") or "")
            playback = payload.get("source") == "vn_playback"
            subtitle = payload.get("source") == "vn_pretranslation"
            if (subtitle or (playback and payload.get("speaking") is False)) and sentence and sentence != self._sentence_id:
                return
            if playback and payload.get("speaking") is True:
                self._sentence_id = sentence
            raw = str(payload.get("text") or payload.get("speak") or "")
            text = str(payload.get("display_text") or "").strip() or legacy.clean_display_text(raw)
            if text:
                self.text_var.set(text)
            if subtitle:
                return
            if playback and payload.get("speaking") is False and self._current_state != "speaking":
                return  # Duplicate completion must not extend the existing return deadline.
            if self._return_timer is not None:
                self.root.after_cancel(self._return_timer)
                self._return_timer = None
            emotion, duration = legacy.infer_emotion(raw, str(payload.get("emotion") or ""))
            speaking = payload.get("speaking")
            state = str(payload.get("portrait_state") or payload.get("state") or "")
            if state not in {"idle", "speaking"}:
                state = "idle" if speaking is False else "speaking"
            if playback and speaking is False:
                emotion = self._current_emotion
            self._set_emotion(emotion, state)
            self._idle_deadline = 0.0
            self._active_until = float("inf") if state == "speaking" else 0.0

            def neutral():
                self._return_timer = None
                self._active_until = 0.0
                self._set_emotion("normal", "idle")

            if state == "idle":
                self._return_timer = self.root.after(350, neutral)
            elif not playback:
                duration = max(350, min(60000, int(payload.get("duration_ms") or duration)))
                self._return_timer = self.root.after(duration, neutral)
            self.root.deiconify()
            self.root.lift()

    return LitePortraitOverlay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-helper", type=Path, required=True)
    parser.add_argument("--lite-dir", type=Path, default=ROOT / "assets/companion/kurisu")
    parser.add_argument("--static-idle", action="store_true")
    parser.add_argument("--images-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--x", type=int, default=60)
    parser.add_argument("--y", type=int, default=80)
    parser.add_argument("--crop-side-ratio", type=float, default=0.74)
    parser.add_argument("--crop-y-ratio", type=float, default=0.035)
    args = parser.parse_args()
    legacy = load_legacy(args.legacy_helper)
    overlay = overlay_class(legacy)(args.images_dir, lite_dir=args.lite_dir, static_idle=args.static_idle,
        cache_dir=legacy.DEFAULT_CACHE_DIR, host=args.host, port=args.port, x=args.x, y=args.y,
        crop_side_ratio=args.crop_side_ratio, crop_y_ratio=args.crop_y_ratio)
    return overlay.run()


if __name__ == "__main__":
    raise SystemExit(main())
