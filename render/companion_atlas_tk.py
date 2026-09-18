"""Native portrait-only adapter for the same Companion Lite atlas/timeline contract."""
from __future__ import annotations

from collections import OrderedDict
import json
import math
from pathlib import Path
import time

from PIL import Image

from render.companion_pack import BYTE_LIMIT, CompanionPackError, load_companion_pack, validate_clip


class AtlasPlayer:
    """At most two decoded atlases/16 MiB, plus the caller's one displayed tile.

    Tk owns its window. This class owns no widget, whole-window style or speech source.
    Selection/timing matches render/web/companion_atlas.js; no interpolation or recrop.
    """

    def __init__(self, root: Path, *, clock=time.monotonic):
        self.root = Path(root)
        load_companion_pack(self.root)
        self.emotions = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))["emotions"]
        self.clock = clock
        self.entries: OrderedDict[str, Image.Image] = OrderedDict()
        self.resident_bytes = 0
        self.speech_counts: dict[str, int] = {}
        self.last_speaking = False
        self.last_emotion = ""
        self.spec = None
        self.started = clock()
        self.elapsed = 0.0
        self.paused = False
        self.last_tile = None
        self.draws = 0

    def select(self, emotion: str, speaking: bool, static_idle: bool = False):
        key = emotion if emotion in self.emotions else "normal"
        if speaking and (not self.last_speaking or key != self.last_emotion):
            self.speech_counts[key] = self.speech_counts.get(key, 0) + 1
        self.last_speaking, self.last_emotion = speaking, key
        states = self.emotions[key]
        alternate = "speakingAlternate" in states and self.speech_counts.get(key, 1) % 2 == 0
        spec = (states["speakingAlternate"] if alternate else states["speaking"]) if speaking else (
            states.get("idleStatic", states["idle"]) if static_idle else states["idle"])
        if spec is self.spec:
            return
        url = spec["url"]
        dimensions = validate_clip(spec)
        if url not in self.entries:
            while len(self.entries) >= 2 or self.resident_bytes + spec["decodedBytes"] > BYTE_LIMIT:
                _, old = self.entries.popitem(last=False)
                self.resident_bytes -= old.width * old.height * 4
                old.close()
            with Image.open(self.root / url) as image:
                if image.format != "WEBP" or image.size != dimensions:
                    raise CompanionPackError("Portrait dimensions differ from manifest")
                self.entries[url] = image.convert("RGBA")
            self.resident_bytes += spec["decodedBytes"]
        elif self.entries[url].size != dimensions:
            raise CompanionPackError("Portrait dimensions differ from manifest")
        self.entries.move_to_end(url)
        self.spec = spec
        self.started, self.elapsed = self.clock(), 0.0
        self.last_tile = None

    def frame(self) -> tuple[Image.Image | None, int | None]:
        """Return a changed tile and milliseconds to its next change (None = no timer)."""
        if self.spec is None:
            return None, None
        spec = self.spec
        elapsed = self.elapsed if self.paused else (self.clock() - self.started) * 1000
        position = (elapsed % spec["durationMs"]) / spec["durationMs"] * len(spec["sequence"])
        index = math.floor(position)
        tile = spec["sequence"][index]
        image = None
        if tile != self.last_tile:
            size, columns = spec["size"], spec["columns"]
            left, top = tile % columns * size, tile // columns * size
            image = self.entries[spec["url"]].crop((left, top, left + size, top + size))
            self.last_tile = tile
            self.draws += 1
        if self.paused or all(value == tile for value in spec["sequence"]):
            return image, None
        steps = 1
        while steps < len(spec["sequence"]) and spec["sequence"][(index + steps) % len(spec["sequence"])] == tile:
            steps += 1
        return image, max(1, math.ceil((index + steps - position) * spec["durationMs"] / len(spec["sequence"])))

    def set_paused(self, paused: bool):
        if paused == self.paused:
            return
        if paused:
            self.elapsed = (self.clock() - self.started) * 1000
        else:
            self.started = self.clock() - self.elapsed / 1000
        self.paused = paused

    def close(self):
        for image in self.entries.values():
            image.close()
        self.entries.clear()
        self.resident_bytes = 0
        self.spec = None
