"""Repository-owned Tk shell for the VN Companion Lite portrait renderer."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ctypes
import json
import math
import queue
import re
import sys
import threading
import time
import tkinter as tk

from PIL import Image, ImageColor, ImageDraw, ImageTk

CARD_BG = "#071e24"
CARD_BORDER = "#397c73"
CARD_ACCENT = "#91dfcc"
CARD_TEXT = "#d6f4e9"
TRANSPARENT_KEY = "#ff00ff"
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

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8788, x: int = 60, y: int = 80,
                 backend_url: str = ""):
        if sys.platform == "win32":
            # Render at monitor resolution instead of letting Windows enlarge a 96-DPI bitmap.
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        self.root = tk.Tk()
        self._scale = self.root.winfo_fpixels("1i") / 96
        self.root.title("Amadeus · VN Companion")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.card_width, self.card_height = 470, 226
        self.root.configure(bg=TRANSPARENT_KEY)
        self.root.geometry(f"{self._px(self.card_width)}x{self._px(self.card_height)}+{self._px(x)}+{self._px(y)}")
        self.root.attributes("-alpha", .88)
        if self.root.tk.call("tk", "windowingsystem") == "win32":
            self.root.attributes("-transparentcolor", TRANSPARENT_KEY)
        self.visible = True
        self.avatar_size = self._px(132)
        self._sentence_id = ""
        self._current_emotion, self._current_state = "normal", "idle"
        self._active_until = self._idle_deadline = 0.0
        self.frame = tk.Canvas(self.root, bg=TRANSPARENT_KEY, bd=0, highlightthickness=0)
        self.frame.pack(fill="both", expand=True)
        self._background_item = self.frame.create_image(0, 0, anchor="nw")
        background = ImageColor.getrgb(CARD_BG)
        self._scan_lines = []
        for color in ("#0c2b30", "#11373a", "#194440", "#28584f", "#153b3b"):
            # Preserve the user's earlier 25% sweep contrast refinement.
            muted = tuple(round(bg + (fg - bg) * .25) for bg, fg in zip(background, ImageColor.getrgb(color)))
            self._scan_lines.append(self.frame.create_line(0, 0, 0, 0, fill="#%02x%02x%02x" % muted))
        self.frame.create_text(22, 25, text="A M A D E U S", anchor="w", fill=CARD_ACCENT, font=("Consolas", -self._px(12), "bold"))
        self._link_label = self.frame.create_text(448, 25, text="HOLO-LINK / VN", anchor="e", fill="#6d9f96", font=("Consolas", -self._px(10)))
        self.frame.create_line(22, 43, 448, 43, fill="#20473f")
        self._signal_bars = [self.frame.create_line(25 + i * 5, 210, 25 + i * 5, 212, fill="#528e83", width=2) for i in range(8)]
        self._signal_label = self.frame.create_text(77, 211, text="STANDBY", anchor="w", fill="#6d9f96", font=("Consolas", -self._px(9)))
        self.avatar_label = tk.Label(self.frame, bg=CARD_BG, borderwidth=0)
        self.avatar_label.place(x=self._px(23), y=self._px(58), width=self.avatar_size, height=self.avatar_size)
        self.frame.create_text(177, 65, text="牧瀬 紅莉栖", anchor="w", fill=CARD_ACCENT, font=("Microsoft YaHei UI", -self._px(12), "bold"))
        self.frame.scale("all", 0, 0, self._scale, self._scale)
        self.text_var = tk.StringVar(value="准备好了，继续故事吧。")
        caption = tk.Label(self.frame, textvariable=self.text_var, bg=CARD_BG, fg=CARD_TEXT,
                           wraplength=self._px(266), justify="left", anchor="nw", font=("Microsoft YaHei UI", -self._px(14)),
                           padx=0, pady=0, bd=0, highlightthickness=0)
        self._caption = caption
        caption.place(x=self._px(177), y=self._px(84), width=self._px(266))
        self._layout_timer = None
        self.text_var.trace_add("write", self._schedule_layout)
        self._schedule_layout()
        self._drag = (0, 0)
        for widget in (self.frame, self.avatar_label, caption):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
        self.root.bind("<ButtonPress-3>", lambda _event: self.close())
        self._controls = None
        self._control_state = {"connected": False, "inputs": {}, "pending": False, "error": ""}
        self._controls_visible = False
        self._hover_control = ""
        self._hover_timer = None
        self._control_buttons = {}
        self._tooltip = tk.Label(self.frame, bg="#0b282d", fg=CARD_TEXT, font=("Microsoft YaHei UI", -self._px(11)),
                                 wraplength=self._px(258), justify="left", padx=self._px(6), pady=self._px(4), bd=1, relief="solid")
        for name in ("voice", "vision"):
            button = tk.Canvas(self.frame, width=28, height=28, bg=CARD_BG, highlightthickness=0, takefocus=True)
            button.bind("<Button-1>", lambda event, key=name: self._toggle_input(key))
            button.bind("<Return>", lambda event, key=name: self._toggle_input(key))
            button.bind("<space>", lambda event, key=name: self._toggle_input(key))
            button.bind("<Enter>", lambda event, key=name: self._show_control_hint(key))
            button.bind("<Leave>", lambda event: self._hide_control_hint())
            self._control_buttons[name] = button
        self.root.bind("<Enter>", self._schedule_hover)
        self.root.bind("<Leave>", self._schedule_hover)
        self._draw_controls()
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
        if backend_url:
            from render.vn_overlay_controls import VNOverlayControls
            self._controls = VNOverlayControls(backend_url)
        self._poll_timer = self.root.after(40, self._poll)
        self._scan_timer = self.root.after(50, self._scan_tick)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def close(self):
        for name in ("_poll_timer", "_layout_timer", "_hover_timer", "_scan_timer"):
            timer = getattr(self, name, None)
            if timer is not None:
                self.root.after_cancel(timer)
                setattr(self, name, None)
        if self._controls:
            self._controls.close()
        self.root.destroy()

    def _schedule_layout(self, *_args):
        if self._layout_timer is None:
            self._layout_timer = self.root.after_idle(self._layout_caption)

    def _px(self, value):
        return round(value * self._scale)

    def _layout_caption(self):
        self._layout_timer = None
        height = max(226, 84 + math.ceil(self._caption.winfo_reqheight() / self._scale) + 36)
        self.card_height = height
        self.root.geometry(f"{self._px(self.card_width)}x{self._px(height)}")
        # Supersample the original frame at native DPI. Use a binary outside mask:
        # Tk's Windows color key cannot composite fractional alpha with the desktop.
        # This keeps the edge free of magenta fringes while smoothing the border inside it.
        resolution = self._scale * 4
        def box(*values):
            return tuple(round(value * resolution) for value in values)
        surface = Image.new("RGBA", (self._px(self.card_width) * 4, self._px(height) * 4))
        draw = ImageDraw.Draw(surface)
        stroke = max(1, round(resolution))
        draw.rounded_rectangle(box(4, 3, self.card_width - 5, height - 6), radius=round(18 * resolution), fill=CARD_BG, outline=CARD_BORDER, width=stroke)
        draw.rounded_rectangle(box(6, 5, self.card_width - 7, height - 8), radius=round(16 * resolution), outline="#153b3a", width=stroke)
        draw.rounded_rectangle(box(18, 53, 160, 195), radius=round(15 * resolution), fill="#0b2a2e", outline="#28574f", width=stroke)
        for line_y in range(64, 188, 5):
            draw.line(box(22, line_y, 156, line_y), fill="#102f32", width=stroke)
        for left, top, sx, sy in ((16, 51, 1, 1), (162, 51, -1, 1), (16, 197, 1, -1), (162, 197, -1, -1)):
            draw.line(box(left, top + sy * 10, left, top, left + sx * 10, top), fill="#70bfae", width=stroke)
        draw.line(box(168, 81, 168, 119), fill="#579e91", width=stroke)
        draw.line(box(168, 123, 168, 146), fill="#2a6159", width=stroke)
        for dot_x in range(self.card_width - 45, self.card_width - 20, 6):
            draw.rectangle(box(dot_x, height - 25, dot_x + .5, height - 24.5), fill="#5c9b8e")
        draw.line(box(177, height - 19, self.card_width - 24, height - 19), fill="#163b38", width=stroke)
        draw.line(box(177, height - 19, 201, height - 19), fill="#518e80", width=stroke)
        with surface.resize((self._px(self.card_width), self._px(height)), Image.Resampling.LANCZOS) as frame:
            with frame.getchannel("A") as alpha:
                frame.putalpha(alpha.point(lambda value: 255 if value >= 128 else 0))
            self._card_photo = ImageTk.PhotoImage(frame, master=self.root)
        self.frame.itemconfigure(self._background_item, image=self._card_photo)
        surface.close()

    def _scan_tick(self):
        # Decoration stays independent of the shared portrait animation.
        if self.visible:
            now = time.monotonic()
            scan_y = 12 + (now % 5.0) / 5.0 * (self.card_height - 24)
            for offset, line in enumerate(self._scan_lines):
                self.frame.coords(line, *map(self._px, (13, scan_y + offset - 3, self.card_width - 14, scan_y + offset - 3)))
            for index, bar in enumerate(self._signal_bars):
                speaking = self._current_state == "speaking"
                amplitude = 2 + ((int(now * 9) + index * 3) % 9) if speaking else 1
                self.frame.coords(bar, *map(self._px, (25 + index * 5, 211 - amplitude / 2, 25 + index * 5, 211 + amplitude / 2)))
                self.frame.itemconfigure(bar, fill=CARD_ACCENT if speaking else "#477a70")
        self._scan_timer = self.root.after(50 if self.visible else 250, self._scan_tick)

    def _schedule_hover(self, _event=None):
        if self._hover_timer is None:
            self._hover_timer = self.root.after_idle(self._update_hover)

    def _update_hover(self):
        self._hover_timer = None
        x, y = self.root.winfo_pointerxy()
        hovered = (self.visible and self.root.winfo_x() <= x < self.root.winfo_x() + self.root.winfo_width()
                   and self.root.winfo_y() <= y < self.root.winfo_y() + self.root.winfo_height())
        self._controls_visible = hovered
        self.frame.itemconfigure(self._link_label, state="hidden" if hovered else "normal")
        for index, button in enumerate(self._control_buttons.values()):
            if hovered:
                button.place(x=self._px(380 + 36 * index), y=self._px(11), width=self._px(28), height=self._px(28))
            else:
                button.place_forget()
        if not hovered:
            self._hide_control_hint()

    def _input_view(self, name):
        state = self._control_state
        inputs = state["inputs"]
        item = inputs.get(name, {})
        selected = item.get("enabled", False) if name == "voice" else item.get("mode") == "on_question"
        busy = state["pending"] or item.get("starting", False)
        enabled = bool(state["connected"] and inputs.get("session_id") and not busy and (item.get("available") or selected))
        label = "语音输入" if name == "voice" else "提问时看画面"
        detail = "正在切换…" if busy else "已开启" if selected else "已关闭"
        if not state["connected"]:
            detail = "未连接"
        elif not inputs.get("session_id"):
            detail = "未开始陪玩"
        elif not item.get("available"):
            detail = "当前不可用"
        error = state["error"] or item.get("error")
        return selected, busy, enabled, f"{label} · {detail}" + (f"\n{error}" if error else "")

    def _draw_controls(self):
        for name, button in self._control_buttons.items():
            selected, busy, enabled, _ = self._input_view(name)
            color = "#c6af74" if busy else CARD_ACCENT if selected and enabled else "#6d9f96" if enabled else "#42645e"
            button.delete("all")
            button.configure(cursor="hand2" if enabled else "arrow")
            if name == "voice":
                button.create_oval(11, 5, 17, 17, outline=color, width=1.5)
                button.create_arc(8, 9, 20, 21, start=180, extent=180, style="arc", outline=color, width=1.5)
                button.create_line(14, 21, 14, 24, fill=color, width=1.5)
                button.create_line(10, 24, 18, 24, fill=color, width=1.5)
            else:
                button.create_rectangle(5, 9, 23, 22, outline=color, width=1.5)
                button.create_line(9, 9, 11, 6, 17, 6, 19, 9, fill=color, width=1.5)
                button.create_oval(10, 12, 18, 20, outline=color, width=1.5)
            if not selected:
                button.create_line(5, 5, 23, 24, fill=color, width=1.5)
            if busy:
                button.create_oval(22, 2, 26, 6, fill=color, outline=color)
            button.scale("all", 0, 0, self._scale, self._scale)
            button.itemconfigure("all", width=max(1, self._px(1.5)))
        if self._hover_control:
            self._show_control_hint(self._hover_control)

    def _show_control_hint(self, name):
        self._hover_control = name
        self._tooltip.configure(text=self._input_view(name)[3])
        self._tooltip.place(x=self._px(174), y=self._px(44), width=self._px(274))
        self._tooltip.lift()

    def _hide_control_hint(self):
        self._hover_control = ""
        self._tooltip.place_forget()

    def _toggle_input(self, name):
        selected, _, enabled, _ = self._input_view(name)
        if enabled and self._controls:
            changes = {"voice": not selected} if name == "voice" else {"vision_mode": "off" if selected else "on_question"}
            self._controls.set_inputs(self._control_state["inputs"]["session_id"], **changes)
            self._control_state = self._controls.snapshot()
            self._draw_controls()
        return "break"

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
        self.frame.itemconfigure(self._signal_label, text="VOICE" if state == "speaking" else "STANDBY")

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
                self._schedule_hover()
            else:
                self.apply_reaction(payload)
        if self._controls:
            state = self._controls.snapshot()
            if state != self._control_state:
                self._control_state = state
                self._draw_controls()
        self._poll_timer = self.root.after(40, self._poll)

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=2)
        return 0
