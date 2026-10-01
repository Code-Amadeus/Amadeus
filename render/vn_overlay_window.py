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

from core.emotion_hints import infer_emotion_from_text

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
    if not emotion:
        emotion = infer_emotion_from_text(text)
    return EMOTION_ALIASES.get(emotion, emotion or "normal"), duration_ms


class PortraitOverlayTk:
    """Window and local message transport; animation lives in AtlasPlayer."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8788, x: int = 60, y: int = 80,
                 backend_url: str = "", on_close: str = "exit", companion_controls: bool = False):
        self._on_close = str(on_close or "exit").strip().lower()
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
        self._character_title = self.frame.create_text(177, 65, text="角色", anchor="w", fill=CARD_ACCENT, font=("Microsoft YaHei UI", -self._px(12), "bold"))
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
        self._on_close = str(on_close or "exit").strip().lower()
        self._controls = None
        self._control_state = {"connected": False, "inputs": {}, "pending": False, "error": ""}
        self._controls_visible = False
        self._hover_control = ""
        self._hover_timer = None
        self._control_buttons = {}
        self._companion_controls_mode = bool(companion_controls)
        self._game_active = False
        self._game_busy = False
        self._game_status_text = ""
        self._game_window = None
        self._character_busy = False
        self._character_name = ""
        self._character_status_requested = False
        self._character_window = None
        self._text_mode = False
        self._mouth_value = 1.0
        self.text_input_var = tk.StringVar(value="")
        self.text_input = (
            tk.Entry(
                self.frame,
                textvariable=self.text_input_var,
                bg="#0b282d",
                fg=CARD_TEXT,
                insertbackground=CARD_ACCENT,
                selectbackground="#28584f",
                selectforeground=CARD_TEXT,
                highlightbackground="#397c73",
                highlightcolor=CARD_ACCENT,
                highlightthickness=1,
                relief="flat",
                bd=0,
                font=("Microsoft YaHei UI", -self._px(12)),
            )
            if self._companion_controls_mode
            else None
        )
        if self.text_input is not None:
            self.text_input.bind("<Return>", self._send_text)
            self.text_input.bind("<Escape>", lambda _event: self._set_text_mode(False))
        self._tooltip = tk.Label(self.frame, bg="#0b282d", fg=CARD_TEXT, font=("Microsoft YaHei UI", -self._px(11)),
                                 wraplength=self._px(258), justify="left", padx=self._px(6), pady=self._px(4), bd=1, relief="solid")
        control_names = ("voice", "text", "vision", "game", "character") if self._companion_controls_mode else ("voice", "vision")
        for name in control_names:
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
                self.reply(
                    200 if self.path == "/health" else 404,
                    {
                        "status": "ok",
                        "application": "amadeus.vn.overlay",
                        "visible": shell.visible,
                        "companion_controls": bool(shell._companion_controls_mode),
                        "backend_url": str(getattr(shell, "_backend_url", "") or ""),
                    },
                )

            def do_POST(self):
                if self.path not in {"/reaction", "/visibility", "/focus"}:
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
            self._controls = VNOverlayControls(backend_url, companion=companion_controls)
        self._poll_timer = self.root.after(40, self._poll)
        self._scan_timer = self.root.after(50, self._scan_tick)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._backend_url = str(backend_url or "")

    def close(self):
        for name in ("_poll_timer", "_layout_timer", "_hover_timer", "_scan_timer"):
            timer = getattr(self, name, None)
            if timer is not None:
                self.root.after_cancel(timer)
                setattr(self, name, None)
        if self._controls:
            self._controls.close()
        self._report_close()
        self.root.destroy()

    def _report_close(self, post=None):
        """Companion mode owns the card process; report the user's close once."""
        from render.vn_overlay_close import post_close

        post_close(getattr(self, "_on_close", "exit"), getattr(self, "_backend_url", ""), post=post)

    def _schedule_layout(self, *_args):
        if self._layout_timer is None:
            self._layout_timer = self.root.after_idle(self._layout_caption)

    def _px(self, value):
        return round(value * self._scale)

    def _layout_caption(self):
        self._layout_timer = None
        text_extra = 44 if self._text_mode else 0
        height = max(226 + text_extra, 84 + math.ceil(self._caption.winfo_reqheight() / self._scale) + 36 + text_extra)
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
        if self.text_input is not None:
            if self._text_mode:
                self.text_input.place(
                    x=self._px(177), y=self._px(height - 44),
                    width=self._px(266), height=self._px(28),
                )
                self.text_input.lift()
            else:
                self.text_input.place_forget()

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
                start_x = 452 - 36 * len(self._control_buttons)
                button.place(x=self._px(start_x + 36 * index), y=self._px(11), width=self._px(28), height=self._px(28))
            else:
                button.place_forget()
        if not hovered:
            self._hide_control_hint()

    def _input_view(self, name):
        state = self._control_state
        inputs = state["inputs"]
        item = inputs.get(name, {})
        if name == "game":
            selected = bool(self._game_active)
            busy = state["pending"] or self._game_busy
            enabled = bool(state["connected"] and not busy)
            detail = "正在启动…" if busy else "已连接" if selected else "已停止"
            if not state["connected"]:
                detail = "未连接"
            extra = f"\n{self._game_status_text}" if self._game_status_text else ""
            return selected, busy, enabled, "Game Companion · " + detail + extra
        if name == "character":
            busy = state["pending"] or self._character_busy
            enabled = bool(state["connected"] and not busy)
            detail = "正在切换…" if busy else (self._character_name or "当前角色")
            if not state["connected"]:
                detail = "未连接"
            return bool(self._character_name), busy, enabled, "角色管理 · " + detail
        if name == "text":
            selected = bool(self._text_mode)
            busy = state["pending"] or item.get("starting", False)
            enabled = bool(
                state["connected"]
                and inputs.get("session_id")
                and not busy
                and item.get("available")
            )
            detail = "正在发送…" if busy else "已开启" if selected else "已关闭"
            if not state["connected"]:
                detail = "未连接"
            elif not inputs.get("session_id"):
                detail = "未开始陪玩"
            elif not item.get("available"):
                detail = "当前不可用"
            error_text = state["error"] or item.get("error")
            return selected, busy, enabled, "键盘输入 · " + detail + (f"\n{error_text}" if error_text else "")
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
            elif name == "text":
                button.create_rectangle(5, 9, 23, 22, outline=color, width=1.5)
                button.create_line(8, 13, 20, 13, fill=color, width=1.5)
                button.create_line(8, 17, 20, 17, fill=color, width=1.5)
                button.create_line(10, 21, 18, 21, fill=color, width=1.5)
            elif name == "game":
                button.create_rectangle(5, 8, 23, 22, outline=color, width=1.5)
                button.create_line(9, 15, 13, 15, fill=color, width=1.5)
                button.create_line(11, 13, 11, 17, fill=color, width=1.5)
                button.create_oval(16, 12, 18, 14, outline=color, width=1.5)
                button.create_oval(19, 15, 21, 17, outline=color, width=1.5)
            elif name == "character":
                button.create_oval(10, 5, 18, 13, outline=color, width=1.5)
                button.create_arc(5, 12, 23, 26, start=0, extent=180, style="arc", outline=color, width=1.5)
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
        if name == "game":
            return self._toggle_game_companion()
        if name == "character":
            return self._toggle_character_menu()
        if name == "text":
            self._set_text_mode(not self._text_mode)
            return "break"
        selected, _, enabled, _ = self._input_view(name)
        if name == "voice" and not selected and self._text_mode:
            self._set_text_mode(False, refresh=False)
        if enabled and self._controls:
            changes = {"voice": not selected} if name == "voice" else {"vision_mode": "off" if selected else "on_question"}
            self._controls.set_inputs(self._control_state["inputs"]["session_id"], **changes)
            self._control_state = self._controls.snapshot()
            self._draw_controls()
        return "break"

    def _toggle_character_menu(self):
        if not self._companion_controls_mode or not self._controls:
            return "break"
        self._character_busy = True
        self._draw_controls()
        self._controls.request("companion.character.list", {}, callback=self._on_character_list)
        return "break"

    def _on_character_list(self, result):
        self.root.after(0, lambda: self._show_character_profiles(result if isinstance(result, dict) else {}))

    def _show_character_profiles(self, result):
        if self._character_window is not None:
            try:
                self._character_window.destroy()
            except Exception:
                pass
        characters = result.get("characters") if isinstance(result.get("characters"), list) else []
        current = result.get("current") if isinstance(result.get("current"), dict) else {}
        current_id = str(current.get("id") or "")
        window = tk.Toplevel(self.root)
        self._character_window = window
        window.title("Character")
        window.configure(bg=CARD_BG)
        window.attributes("-topmost", True)
        window.geometry(f"{self._px(360)}x{self._px(300)}+{self.root.winfo_x()}+{self.root.winfo_y() + self._px(40)}")
        tk.Label(window, text="选择角色", bg=CARD_BG, fg=CARD_ACCENT,
                 font=("Microsoft YaHei UI", -self._px(13), "bold")).pack(anchor="w", padx=12, pady=(12, 6))
        if not characters:
            tk.Label(window, text="没有可用角色包。", bg=CARD_BG, fg=CARD_TEXT,
                     font=("Microsoft YaHei UI", -self._px(11))).pack(anchor="w", padx=12, pady=8)
        else:
            for character in characters[:12]:
                character_id = str(character.get("id") or "")
                label = str(character.get("name") or character_id or "Unnamed")
                marker = "  ✓" if character_id == current_id else ""
                button = tk.Button(
                    window,
                    text=f"{label}{marker}",
                    bg="#0d3339", fg=CARD_TEXT, activebackground="#12444b",
                    activeforeground=CARD_TEXT, anchor="w", relief="flat",
                    font=("Microsoft YaHei UI", -self._px(11)),
                    command=lambda value=character_id: self._switch_character(value),
                )
                button.pack(fill="x", padx=12, pady=3)
        tk.Button(window, text="关闭", bg=CARD_BG, fg="#9fc3bb", relief="flat",
                  command=window.destroy).pack(anchor="e", padx=12, pady=(8, 10))

    def _on_character_status(self, result):
        current = result.get("current") if isinstance(result, dict) else {}
        name = str((current or {}).get("name") or "").strip()
        if name:
            self.root.after(0, lambda value=name: self._set_character_title(value))

    def _set_character_title(self, name):
        value = str(name or "").strip() or "角色"
        self._character_name = "" if value == "角色" else value
        self.frame.itemconfigure(self._character_title, text=value)

    def _ensure_character_status(self):
        if not self._companion_controls_mode or self._controls is None or self._character_status_requested:
            return
        request = getattr(self._controls, "request", None)
        if callable(request) and request(
            "companion.character.status",
            {},
            callback=self._on_character_status,
        ):
            self._character_status_requested = True

    def _switch_character(self, character_id):
        if not character_id or not self._controls:
            return
        if self._character_window is not None:
            try:
                self._character_window.destroy()
            except Exception:
                pass
            self._character_window = None
        self._character_busy = True
        self._draw_controls()
        self._controls.request(
            "companion.character.switch",
            {"character_id": character_id},
            callback=self._on_character_switched,
        )

    def _on_character_switched(self, result):
        self.root.after(0, lambda: self._handle_character_switched(result if isinstance(result, dict) else {}))

    def _handle_character_switched(self, result):
        error = str(result.get("error") or "").strip()
        character = result.get("character") if isinstance(result.get("character"), dict) else {}
        self._character_busy = False
        if error:
            self.text_var.set(f"Character: {error}")
        else:
            name = str(character.get("name") or character.get("id") or "").strip()
            self._set_character_title(name)
            art_dir = str(character.get("art_dir") or "").strip()
            reload_pack = getattr(self, "reload_character_pack", None)
            if art_dir and callable(reload_pack):
                reload_pack(art_dir)
            voice_reload = result.get("voice_reload")
            voice_failed = isinstance(voice_reload, dict) and not bool(voice_reload.get("ok", True))
            if voice_failed:
                self.text_var.set(f"Character: {name} · voice reload failed")
            else:
                self.text_var.set(f"Character: {name}" if name else "Character switched")
        self._draw_controls()

    def _toggle_game_companion(self):
        if not self._companion_controls_mode or not self._controls:
            return "break"
        self._game_busy = True
        self._draw_controls()
        self._controls.request("vn.launch.status", {}, callback=self._on_game_status_for_toggle)
        return "break"

    def _on_game_status_for_toggle(self, result):
        self.root.after(0, lambda: self._handle_game_status(result, toggle=True))

    def _handle_game_status(self, result, *, toggle=False):
        if not isinstance(result, dict):
            result = {}
        status = str(result.get("status") or "").strip().lower()
        self._game_active = status in {"active", "starting", "stopping"}
        self._game_status_text = status or "unknown"
        self._game_busy = False
        if toggle:
            if status in {"active", "starting"}:
                self._controls.request("vn.launch.stop", {}, callback=self._on_game_command_done)
            else:
                self._controls.request("vn.launch.profiles", {}, callback=self._on_game_profiles)
        self._draw_controls()
        self._show_control_hint("game")

    def _on_game_profiles(self, result):
        profiles = result.get("profiles") if isinstance(result, dict) else None
        self.root.after(0, lambda: self._show_game_profiles(profiles if isinstance(profiles, list) else []))

    def _show_game_profiles(self, profiles):
        if self._game_window is not None:
            try:
                self._game_window.destroy()
            except Exception:
                pass
        window = tk.Toplevel(self.root)
        self._game_window = window
        window.title("Game Companion")
        window.configure(bg=CARD_BG)
        window.attributes("-topmost", True)
        window.geometry(f"{self._px(360)}x{self._px(280)}+{self.root.winfo_x()}+{self.root.winfo_y() + self._px(40)}")
        tk.Label(window, text="选择游戏 profile", bg=CARD_BG, fg=CARD_ACCENT,
                 font=("Microsoft YaHei UI", -self._px(13), "bold")).pack(anchor="w", padx=12, pady=(12, 6))
        if not profiles:
            tk.Label(window, text="没有保存的游戏 profile。\n请先在主界面添加 Galgame/VN profile。",
                     bg=CARD_BG, fg=CARD_TEXT, justify="left",
                     font=("Microsoft YaHei UI", -self._px(11))).pack(anchor="w", padx=12, pady=8)
        else:
            for profile in profiles[:8]:
                profile_id = str(profile.get("id") or "")
                label = str(profile.get("name") or profile_id or "Unnamed")
                state = "可启动" if profile.get("runtimeSupported", True) else "不可用"
                button = tk.Button(
                    window,
                    text=f"{label}  ·  {state}",
                    bg="#0d3339", fg=CARD_TEXT, activebackground="#12444b",
                    activeforeground=CARD_TEXT, anchor="w", relief="flat",
                    font=("Microsoft YaHei UI", -self._px(11)),
                    command=lambda value=profile_id: self._start_game_profile(value),
                )
                button.pack(fill="x", padx=12, pady=3)
        tk.Button(window, text="关闭", bg=CARD_BG, fg="#9fc3bb", relief="flat",
                  command=window.destroy).pack(anchor="e", padx=12, pady=(8, 10))

    def _start_game_profile(self, profile_id):
        if not profile_id or not self._controls:
            return
        if self._game_window is not None:
            try:
                self._game_window.destroy()
            except Exception:
                pass
            self._game_window = None
        self._game_busy = True
        self._game_status_text = "starting"
        self._draw_controls()
        self._controls.request(
            "vn.launch.start",
            {
                "profileId": profile_id,
                "launchOverlay": False,
                "overlayUrl": "http://127.0.0.1:8788/reaction",
            },
            callback=self._on_game_command_done,
        )

    def _on_game_command_done(self, result):
        self.root.after(0, lambda: self._handle_game_command_done(result))

    def _handle_game_command_done(self, result):
        if not isinstance(result, dict):
            result = {}
        error = str(result.get("error") or "").strip()
        status = str(result.get("status") or "").strip()
        self._game_busy = False
        self._game_active = status in {"active", "starting", "stopping"}
        self._game_status_text = error or status or "done"
        if error:
            self.text_var.set(f"Game Companion: {error}")
        else:
            self.text_var.set(f"Game Companion: {self._game_status_text}")
        self._draw_controls()

    def _set_text_mode(self, enabled, *, refresh=True):
        if not self._companion_controls_mode:
            return "break"
        enabled = bool(enabled)
        if enabled and self._controls:
            selected, _, can_stop, _ = self._input_view("voice")
            if selected and can_stop:
                self._controls.set_inputs(self._control_state["inputs"]["session_id"], voice=False)
                self._control_state = self._controls.snapshot()
        self._text_mode = enabled
        self._schedule_layout()
        if enabled and self.text_input is not None:
            self.root.focus_force()
            self.text_input.focus_set()
        if refresh:
            self._draw_controls()
        return "break"

    def _send_text(self, _event=None):
        if not self._text_mode or not self._controls or self.text_input is None:
            return "break"
        text = self.text_input_var.get().strip()
        if text and self._controls.send_text(text):
            self.text_input_var.set("")
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
        if payload.get("source") == "vn_preview":
            self._set_emotion(str(payload.get("emotion") or "normal"), "idle")
            return
        if payload.get("source") != "vn_pretranslation":
            self._set_emotion(str(payload.get("emotion") or "normal"), "idle" if payload.get("speaking") is False else "speaking")

    def _poll(self):
        self._ensure_character_status()
        if self._controls is not None:
            try:
                self._mouth_value = self._controls.mouth_value()
            except Exception:
                self._mouth_value = 1.0
        while True:
            try:
                path, payload = self._messages.get_nowait()
            except queue.Empty:
                break
            if path == "/visibility":
                self.visible = payload["visible"]
                self.root.deiconify() if self.visible else self.root.withdraw()
                self._schedule_hover()
            elif path == "/focus":
                self._focus_card()
            else:
                self.apply_reaction(payload)
        if self._controls:
            state = self._controls.snapshot()
            if state != self._control_state:
                self._control_state = state
                self._draw_controls()
        self._poll_timer = self.root.after(40, self._poll)

    def _focus_card(self):
        """Bring an already running card to the front for a repeated launch."""
        self.visible = True
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass
        self._schedule_hover()

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=2)
        return 0
