"""Standalone VN portrait window using the bundled Companion Lite assets."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from PIL import Image, ImageColor

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from render.companion_atlas_tk import AtlasPlayer  # noqa: E402
from render import vn_overlay_window  # noqa: E402


def overlay_class(shell=vn_overlay_window):
    class LitePortraitOverlay(shell.PortraitOverlayTk):
        def __init__(self, *args, lite_dir: Path, static_idle: bool = False, **kwargs):
            self._lite_dir, self._static_idle = lite_dir, static_idle
            self._lite = None
            self._atlas_timer = self._return_timer = None
            self._sentence_id = ""
            try:
                super().__init__(*args, **kwargs)
            except Exception:
                # A present but invalid pack is an error, not the missing-art mode.
                # Tk may already have created the root when atlas validation fails.
                if self._lite:
                    self._lite.close()
                if getattr(self, "root", None):
                    self.root.destroy()
                raise
            # Preserve the existing sweep/path/cadence, with one quarter of its RGB
            # distance from the card background. Decoration should not compete with text.
            background = ImageColor.getrgb(shell.CARD_BG)
            for line in self._scan_lines:
                foreground = ImageColor.getrgb(self.frame.itemcget(line, "fill"))
                muted = tuple(round(bg + (fg - bg) * 0.25) for bg, fg in zip(background, foreground))
                self.frame.itemconfigure(line, fill="#{:02x}{:02x}{:02x}".format(*muted))
            self.root.bind("<Unmap>", self._visibility, add="+")
            self.root.bind("<Map>", self._visibility, add="+")
            self.root.bind("<Destroy>", self._dispose, add="+")

        def _load_frames(self):
            if (self._lite_dir / "manifest.json").is_file():
                self._lite = AtlasPlayer(self._lite_dir)
            else:
                super()._load_frames()

        def _resolve_key(self, emotion):
            if not self._lite:
                return super()._resolve_key(emotion)
            key = super()._resolve_key(emotion)
            return key if key in self._lite.emotions else "normal"

        def _set_emotion(self, emotion, state="idle", *, advance_variant=False):
            super()._set_emotion(emotion, state)
            if self._lite:
                self._lite.select(self._current_emotion, self._current_state == "speaking", self._static_idle,
                                  advance_variant=advance_variant)
                self._draw_lite()

        def _draw_lite(self):
            from PIL import ImageTk

            # Hold the current pose during the short sentence-end grace period.
            # The next speaking variant can enter directly, without routing through idle.
            settling = self._current_state == "speaking" and self._active_until == 0.0
            self._lite.set_paused(not self.root.winfo_viewable() or settling)
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
            new_sentence = playback and payload.get("speaking") is True and sentence != self._sentence_id
            if (subtitle or (playback and payload.get("speaking") is False)) and sentence and sentence != self._sentence_id:
                return
            if playback and payload.get("speaking") is True:
                self._sentence_id = sentence
            raw = str(payload.get("text") or payload.get("speak") or "")
            text = shell.clean_display_text(payload.get("display_text")) or shell.clean_display_text(raw)
            if text:
                self.text_var.set(text)
            if subtitle:
                return
            if playback and payload.get("speaking") is False and (
                self._current_state != "speaking" or (self._return_timer is not None and self._active_until == 0.0)
            ):
                return  # Duplicate completion must not extend the existing return deadline.
            if self._return_timer is not None:
                self.root.after_cancel(self._return_timer)
                self._return_timer = None
            emotion, duration = shell.infer_emotion(raw, str(payload.get("emotion") or ""))
            speaking = payload.get("speaking")
            state = str(payload.get("portrait_state") or payload.get("state") or "").strip().lower()
            if state not in {"idle", "speaking"}:
                state = "idle" if speaking is False else "speaking"
            def neutral():
                self._return_timer = None
                self._active_until = 0.0
                self._set_emotion("normal", "idle")

            self._idle_deadline = 0.0
            self._active_until = float("inf") if state == "speaking" else 0.0
            if playback and speaking is False:
                # Do not select an idle atlas at each sentence boundary. Pause this pose;
                # a following start enters its speaking variant directly and cancels return.
                self.frame.itemconfigure(self._signal_label, text="STANDBY")
                self._draw_lite()
                self._return_timer = self.root.after(350, neutral)
                return
            self._set_emotion(emotion, state, advance_variant=new_sentence)
            if state == "idle":
                self._return_timer = self.root.after(350, neutral)
            elif not playback:
                duration = max(350, min(60000, int(payload.get("duration_ms") or duration)))
                self._return_timer = self.root.after(duration, neutral)
            if getattr(self, "visible", True):
                self.root.deiconify()
                self.root.lift()

    return LitePortraitOverlay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lite-dir", type=Path, default=ROOT / "assets/companion/kurisu")
    parser.add_argument("--static-idle", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--x", type=int, default=60)
    parser.add_argument("--y", type=int, default=80)
    args = parser.parse_args()
    overlay = overlay_class()(lite_dir=args.lite_dir, static_idle=args.static_idle,
                              host=args.host, port=args.port, x=args.x, y=args.y)
    return overlay.run()


if __name__ == "__main__":
    raise SystemExit(main())
