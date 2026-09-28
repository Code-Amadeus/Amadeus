/* Small portrait assets only. The Host still owns speech and emotion signals. */
(function (root) {
  'use strict';
  const FORMAT = 'amadeus.companion-atlas.v1';
  const BYTE_LIMIT = 16 * 1024 * 1024;

  function validate(manifest) {
    if (manifest?.format !== FORMAT || !manifest.emotions?.normal) throw Error('Invalid companion atlas manifest');
    const emotions = Object.values(manifest.emotions);
    if (emotions.length > 24) throw Error('Too many portrait expressions');
    for (const emotion of emotions) {
      if (!emotion?.idle || !emotion?.speaking) throw Error('Missing portrait state');
      for (const spec of [emotion.idle, emotion.speaking, emotion.idleStatic, emotion.speakingAlternate].filter(Boolean)) {
        if (typeof spec.url !== 'string' || !/^(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+\.webp$/.test(spec.url)) throw Error('Unsafe portrait path');
        if (!Number.isInteger(spec.size) || spec.size < 1 || spec.size > 512
            || !Number.isInteger(spec.columns) || spec.columns < 1 || spec.columns > 16
            || !Array.isArray(spec.sequence) || !spec.sequence.length || spec.sequence.length > 2048
            || !spec.sequence.every(i => Number.isInteger(i) && i >= 0 && i < 2048)
            || !Number.isFinite(spec.durationMs) || spec.durationMs <= 0 || spec.durationMs > 60000) throw Error('Invalid portrait timeline');
        const rows = Math.ceil((Math.max(...spec.sequence) + 1) / spec.columns);
        const bytes = rows * spec.columns * spec.size * spec.size * 4;
        if (spec.decodedBytes !== bytes || bytes > BYTE_LIMIT) throw Error('Portrait exceeds decoded memory budget');
      }
    }
    return manifest;
  }

  class CompanionAtlasPlayer {
    constructor(canvas, manifest, baseUrl) {
      this.manifest = validate(manifest);
      this.canvas = canvas;
      // A tiny CPU canvas avoids retaining full atlases in the compositor's GPU cache.
      this.context = canvas.getContext('2d', {willReadFrequently: true});
      this.baseUrl = baseUrl;
      this.entries = new Map();
      this.residentBytes = 0;
      this.epoch = 0;
      this.disposed = false;
      this.paused = false;
      this.started = performance.now();
      this.elapsed = 0;
      this.speechCounts = new Map();
      this.lastSpeaking = false;
      this.serial = Promise.resolve();
      this.currentTask = this.serial;
      this.draws = 0;
    }

    select(emotion, speaking, staticIdle = false) {
      const key = Object.hasOwn(this.manifest.emotions, emotion) ? emotion : 'normal';
      const states = this.manifest.emotions[key];
      if (speaking && (!this.lastSpeaking || key !== this.lastEmotion)) {
        this.speechCounts.set(key, (this.speechCounts.get(key) || 0) + 1);
      }
      this.lastSpeaking = speaking;
      this.lastEmotion = key;
      const alternate = states.speakingAlternate && (this.speechCounts.get(key) || 1) % 2 === 0;
      const spec = speaking ? (alternate ? states.speakingAlternate : states.speaking)
        : (staticIdle && states.idleStatic ? states.idleStatic : states.idle);
      if (this.requested === spec) return this.currentTask;
      this.requested = spec;
      const epoch = ++this.epoch;
      const task = this.serial.then(async () => {
        if (this.disposed || epoch !== this.epoch) return;
        let entry = this.entries.get(spec.url);
        if (!entry) {
          while (this.entries.size >= 2 || this.residentBytes + spec.decodedBytes > BYTE_LIMIT) {
            const [key, value] = this.entries.entries().next().value;
            value.bitmap.close();
            this.residentBytes -= value.bytes;
            this.entries.delete(key);
            if (key === this.active?.url) { clearTimeout(this.timer); this.active = null; }
          }
          const response = await fetch(new URL(spec.url, this.baseUrl));
          if (!response.ok) throw Error(`Portrait HTTP ${response.status}`);
          const blob = await response.blob();
          if (blob.size > BYTE_LIMIT) throw Error('Portrait file exceeds size budget');
          const bitmap = await createImageBitmap(blob);
          if (this.disposed || epoch !== this.epoch) { bitmap.close(); return; }
          const rows = Math.ceil((Math.max(...spec.sequence) + 1) / spec.columns);
          if (bitmap.width !== spec.columns * spec.size || bitmap.height !== rows * spec.size) {
            bitmap.close(); throw Error('Portrait dimensions differ from manifest');
          }
          entry = {bitmap, bytes: spec.decodedBytes};
          this.residentBytes += entry.bytes;
        }
        this.entries.delete(spec.url);
        this.entries.set(spec.url, entry);
        clearTimeout(this.timer);
        this.active = spec;
        this.started = performance.now();
        this.elapsed = 0;
        this.lastTile = -1;
        this.canvas.width = this.canvas.height = spec.size;
        this.render();
      });
      // A failed load stays observable to the caller but cannot poison later state changes.
      this.serial = task.catch(() => { if (epoch === this.epoch) this.requested = null; });
      this.currentTask = task;
      return task;
    }

    render() {
      if (this.disposed || !this.active) return;
      const spec = this.active;
      const elapsed = this.paused ? this.elapsed : performance.now() - this.started;
      const position = (elapsed % spec.durationMs) / spec.durationMs * spec.sequence.length;
      const index = Math.floor(position), tile = spec.sequence[index];
      if (tile !== this.lastTile) {
        const size = spec.size;
        this.context.clearRect(0, 0, size, size);
        this.context.drawImage(this.entries.get(spec.url).bitmap,
          tile % spec.columns * size, Math.floor(tile / spec.columns) * size,
          size, size, 0, 0, size, size);
        this.lastTile = tile;
        this.draws++;
      }
      if (this.paused || spec.sequence.every(t => t === tile)) return;
      let steps = 1;
      while (steps < spec.sequence.length && spec.sequence[(index + steps) % spec.sequence.length] === tile) steps++;
      const delay = (index + steps - position) * spec.durationMs / spec.sequence.length;
      this.timer = setTimeout(() => this.render(), Math.max(1, delay));
    }

    setPaused(paused) {
      if (this.paused === paused) return;
      if (paused) this.elapsed = performance.now() - this.started;
      else this.started = performance.now() - this.elapsed;
      this.paused = paused;
      clearTimeout(this.timer);
      if (!paused) this.render();
    }

    dispose() {
      this.disposed = true;
      ++this.epoch;
      clearTimeout(this.timer);
      for (const entry of this.entries.values()) entry.bitmap.close();
      this.entries.clear();
      this.residentBytes = 0;
    }
  }
  root.CompanionAtlas = {validate, Player: CompanionAtlasPlayer};
})(globalThis);
