"""Repository-owned Tk shell for the VN Companion Lite portrait renderer."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import queue
import re
import threading
import tkinter as tk

CARD_BG = "#071e24"
TAG_RE = re.compile(r"\[(?:PARAM|EXPR|HOTKEY|EMO|ANIM|DELEGATE)\b[^\]]*\]", re.I)
EMO_RE = re.compile(r"\[EMO\s+preset=([\w-]+)(?:\s+dur=([0-9.]+)s)?[^\]]*\]", re.I)
EMOTION_ALIASES = {
    "default": "normal", "idle": "normal", "idle1": "normal", "idle2": "normal", "neutral": "normal",
    "thinking": "sided_thinking", "thinking_trans": "sided_thinking",
    "serious_speaking": "sided_thinking", "speaking_trans": "sided_thinking",
    "surprise": "sided_surprised", "surprise_trans": "sided_surprised", "surprised": "sided_surprised",
    "smile": "happy", "trans_smile": "happy", "shy": "blush", "shy_trans": "blush",
    "angry_trans": "angry", "sad_trans": "sad",
}


def clean_display_text(text: str) -> str:
    return TAG_RE.sub("", str(text or "")).strip()


def infer_emotion(text: str, explicit: str = "") -> tuple[str, int]:
    match = EMO_RE.search(str(text or ""))
    emotion = str(explicit or "").strip().lower()
    duration_ms = 6500
    if match:
        emotion = match[1].lower()
        if match[2]:
            try:
                duration_ms = max(1000, int(float(match[2]) * 1000))
            except (ValueError, OverflowError):
                pass
    return EMOTION_ALIASES.get(emotion, emotion or "normal"), duration_ms


class PortraitOverlayTk:
    """Window and local message transport; animation lives in AtlasPlayer."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8788, x: int = 60, y: int = 80):
        self.root = tk.Tk()
        self.root.title("Amadeus · VN Companion")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=CARD_BG)
        self.root.geometry(f"440x192+{x}+{y}")
        self.visible = True
        self.avatar_size = 144
        self._sentence_id = ""
        self._current_emotion, self._current_state = "normal", "idle"
        self._active_until = self._idle_deadline = 0.0
        self.frame = tk.Canvas(self.root, bg=CARD_BG, highlightthickness=1, highlightbackground="#3a6268")
        self.frame.pack(fill="both", expand=True)
        self._signal_label = self.frame.create_text(174, 23, text="AMADEUS · VN", fill="#8fb4b8", anchor="w", font=("Segoe UI", 9))
        self.avatar_label = tk.Label(self.frame, bg=CARD_BG, borderwidth=0)
        self.frame.create_window(12, 30, window=self.avatar_label, anchor="nw", width=self.avatar_size, height=self.avatar_size)
        self.text_var = tk.StringVar(value="继续游玩，陪伴会随剧情展开。")
        caption = tk.Label(self.frame, textvariable=self.text_var, bg=CARD_BG, fg="#e0eeee",
                           wraplength=244, justify="left", anchor="nw", font=("Microsoft YaHei UI", 10))
        self._caption = caption
        self._caption_item = self.frame.create_window(174, 49, window=caption, anchor="nw", width=248, height=125)
        self._layout_timer = None
        self.text_var.trace_add("write", self._schedule_layout)
        close = tk.Button(self.frame, text="×", command=self.close, bg=CARD_BG, fg="#8fb4b8",
                          relief="flat", borderwidth=0, font=("Segoe UI", 12), takefocus=True)
        self.frame.create_window(422, 19, window=close, width=26, height=26)
        self._drag = (0, 0)
        self.frame.bind("<ButtonPress-1>", self._drag_start)
        self.frame.bind("<B1-Motion>", self._drag_move)
        self._messages: queue.Queue[tuple[str, dict]] = queue.Queue(maxsize=100)
        self._load_frames()
        self._set_emotion("normal", "idle")
        shell = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, code: int, payload: dict):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.reply(200 if self.path == "/health" else 404,
                           {"status": "ok", "application": "amadeus.vn.overlay", "visible": shell.visible})

            def do_POST(self):
                if self.path not in {"/reaction", "/visibility"}:
                    self.reply(404, {"error": "unknown endpoint"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 65536:
                        raise ValueError("invalid request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("expected object")
                    if self.path == "/visibility" and not isinstance(payload.get("visible"), bool):
                        raise ValueError("visible must be a boolean")
                    if self.path == "/reaction" and payload.get("duration_ms") is not None and (
                        isinstance(payload["duration_ms"], bool) or not isinstance(payload["duration_ms"], int)
                    ):
                        raise ValueError("duration_ms must be an integer")
                    shell._messages.put_nowait((self.path, payload))
                except (ValueError, queue.Full) as exc:
                    self.reply(400, {"error": str(exc)})
                    return
                self.reply(200, {"status": "ok"})

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._poll_timer = self.root.after(40, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def close(self):
        if getattr(self, "_poll_timer", None):
            self.root.after_cancel(self._poll_timer)
            self._poll_timer = None
        if self._layout_timer is not None:
            self.root.after_cancel(self._layout_timer)
            self._layout_timer = None
        self.root.destroy()

    def _schedule_layout(self, *_args):
        if self._layout_timer is None:
            self._layout_timer = self.root.after_idle(self._layout_caption)

    def _layout_caption(self):
        self._layout_timer = None
        height = max(125, self._caption.winfo_reqheight())
        self.frame.itemconfigure(self._caption_item, height=height)
        self.root.geometry(f"440x{max(192, height + 64)}")

    def _drag_start(self, event):
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_move(self, event):
        self.root.geometry(f"+{max(0, event.x_root - self._drag[0])}+{max(0, event.y_root - self._drag[1])}")

    def _load_frames(self):
        # Companion art is an optional asset bundle; the window and captions work without it.
        self.avatar_label.configure(text="A", fg="#a4c9cd", font=("Segoe UI", 54))
        self.text_var.set("头像资源未安装。仍可显示陪伴字幕。")

    def _resolve_key(self, emotion):
        key = str(emotion or "").strip().lower()
        return EMOTION_ALIASES.get(key, key or "normal")

    def _set_emotion(self, emotion, state="idle"):
        self._current_emotion, self._current_state = self._resolve_key(emotion), state
        self.frame.itemconfigure(self._signal_label, text="AMADEUS · SPEAKING" if state == "speaking" else "AMADEUS · VN")

    def apply_reaction(self, payload):
        sentence = str(payload.get("sentence_id") or "")
        starting = payload.get("source") == "vn_playback" and payload.get("speaking") is True
        if starting:
            self._sentence_id = sentence
        elif sentence and sentence != self._sentence_id:
            return
        caption = clean_display_text(payload.get("display_text")) or clean_display_text(payload.get("text") or payload.get("speak"))
        if caption:
            self.text_var.set(caption)
        if payload.get("source") != "vn_pretranslation":
            self._set_emotion(str(payload.get("emotion") or "normal"), "idle" if payload.get("speaking") is False else "speaking")

    def _poll(self):
        while True:
            try:
                path, payload = self._messages.get_nowait()
            except queue.Empty:
                break
            if path == "/visibility":
                self.visible = payload["visible"]
                self.root.deiconify() if self.visible else self.root.withdraw()
            else:
                self.apply_reaction(payload)
        self._poll_timer = self.root.after(40, self._poll)

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=2)
        return 0
