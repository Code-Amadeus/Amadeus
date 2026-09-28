/* Experimental small-portrait player. No Pixi, full-size textures or 60Hz polling. */
(function (root) {
  class CompanionAtlasPlayer {
    constructor(canvas, manifest, baseUrl, options = {}) {
      this.canvas = canvas;
      // Small CPU-rasterized portraits avoid uploading whole atlases into the
      // canvas compositor's separate GPU cache on every emotion switch.
      this.context = canvas.getContext('2d', options.softwareCanvas ? {willReadFrequently:true} : undefined);
      this.manifest = manifest;
      this.baseUrl = baseUrl;
      this.limit = options.byteLimit || 16 * 1024 * 1024;
      this.entries = new Map();
      this.residentBytes = 0;
      this.peakBytes = 0;
      this.draws = 0;
      this.evictions = 0;
      this.events = [];
      this.epoch = 0;
      this.disposed = false;
      this.paused = false;
      this.serial = Promise.resolve();
    }

    select(emotion, speaking) {
      const states = this.manifest.emotions[emotion] || this.manifest.emotions.normal;
      const spec = states[speaking ? 'speaking' : 'idle'] || states.idle;
      if (this.requested === spec) return this.serial;
      this.requested = spec;
      const epoch = ++this.epoch;
      // Only one decode at a time; stale rapid switches never accumulate bitmaps.
      this.serial = this.serial.then(async () => {
        if (this.disposed || epoch !== this.epoch) return;
        let bitmap = this.entries.get(spec.url)?.bitmap;
        if (!bitmap) {
          if (spec.decodedBytes > this.limit) throw Error('Atlas exceeds portrait byte budget');
          while (this.entries.size >= 2 || this.residentBytes + spec.decodedBytes > this.limit) {
            const [key, value] = this.entries.entries().next().value;
            value.bitmap.close();
            this.residentBytes -= value.bytes;
            this.entries.delete(key);
            this.evictions++;
            if (key === this.active?.url) {
              clearTimeout(this.timer);
              this.active = null;
            }
          }
          const response = await fetch(new URL(spec.url, this.baseUrl));
          if (!response.ok) throw Error(`Atlas HTTP ${response.status}`);
          bitmap = await createImageBitmap(await response.blob());
          if (this.disposed || epoch !== this.epoch) { bitmap.close(); return; }
          const bytes = bitmap.width * bitmap.height * 4;
          if (bytes !== spec.decodedBytes) { bitmap.close(); throw Error('Atlas size mismatch'); }
          this.entries.set(spec.url, { bitmap, bytes });
          this.residentBytes += bytes;
          this.peakBytes = Math.max(this.peakBytes, this.residentBytes);
        } else {
          const entry = this.entries.get(spec.url);
          this.entries.delete(spec.url);
          this.entries.set(spec.url, entry);
        }
        clearTimeout(this.timer);
        this.active = spec;
        this.started = performance.now();
        this.elapsed = 0;
        this.lastTile = -1;
        this.canvas.width = this.canvas.height = spec.size;
        this.render();
      });
      return this.serial;
    }

    render() {
      if (this.disposed || !this.active) return;
      const spec = this.active;
      const now = performance.now();
      const elapsed = this.paused ? this.elapsed : now - this.started;
      const position = ((elapsed % spec.durationMs) / spec.durationMs) * spec.sequence.length;
      const index = Math.floor(position);
      const tile = spec.sequence[index];
      if (tile !== this.lastTile) {
        const size = spec.size;
        this.context.clearRect(0, 0, size, size);
        this.context.drawImage(this.entries.get(spec.url).bitmap,
          (tile % spec.columns) * size, Math.floor(tile / spec.columns) * size,
          size, size, 0, 0, size, size);
        this.draws++;
        this.events.push({ t: now, url: spec.url, index });
        if (this.events.length > 4000) this.events.shift();
        this.lastTile = tile;
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

    snapshot() {
      return { draws: this.draws, residentBytes: this.residentBytes, peakBytes: this.peakBytes,
        atlasCount: this.entries.size, evictions: this.evictions, paused: this.paused,
        active: this.active?.url, durationMs: this.active?.durationMs,
        frameCount: this.active?.sequence.length, events: this.events };
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
  root.CompanionAtlasPlayer = CompanionAtlasPlayer;
})(globalThis);
