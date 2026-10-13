import { keyframeAtOrBefore } from './ivf.mjs';

// Bounded sequential supply, independent of the animation's clock. All output
// ownership is explicit, including stale callbacks after a restart or failure.
export class FrameDecoder {
  constructor(frames, config, { capacity = 16, Decoder = globalThis.VideoDecoder,
    Chunk = globalThis.EncodedVideoChunk, onAvailable = () => {}, onError = () => {} } = {}) {
    if (!Number.isInteger(capacity) || capacity < 2) throw Error('Invalid queue capacity');
    Object.assign(this, { frames, config, capacity, Decoder, Chunk, onAvailable, onError });
    this.ready = new Map(); this.owned = new Set(); this.pending = new Set(); this.generation = 0;
    this.outstanding = 0; this.paused = false;
    this.stats = this.emptyStats(); this.setPhase('initial');
  }

  emptyStats() { return { submitted: 0, outputs: 0, closed: 0, peakOwned: 0, peakOutstanding: 0, sessions: 0, staleOutputs: 0, errors: 0 }; }
  count(name) { this.stats[name]++; this.phaseStats[name]++; }
  peak() {
    for (const stats of [this.stats, this.phaseStats]) {
      stats.peakOwned = Math.max(stats.peakOwned, this.owned.size);
      stats.peakOutstanding = Math.max(stats.peakOutstanding, this.outstanding + this.owned.size);
    }
  }
  setPhase(name) { this.phase = name; this.phaseStats = this.emptyStats(); this.peak(); }
  snapshot() {
    return { phase: this.phase, total: { ...this.stats }, scoped: { ...this.phaseStats },
      liveFrames: this.owned.size, readyFrames: this.ready.size, outstanding: this.outstanding,
      pendingIdentities: this.pending.size, decoderState: this.decoder?.state ?? 'absent',
      paused: this.paused, generation: this.generation, target: this.target ?? null };
  }
  setPaused(value) { this.paused = Boolean(value); if (!this.paused) this.pump(); }

  start(index = 0) {
    const keyframe = keyframeAtOrBefore(this.frames, index);
    this.stop();
    const generation = this.generation;
    this.target = index; this.next = keyframe;
    this.outstanding = 0; this.flushing = false; this.error = null;
    this.decoder = new this.Decoder({
      output: frame => {
        this.count('outputs'); this.owned.add(frame);
        if (generation !== this.generation || !this.decoder) {
          this.count('staleOutputs'); this.release(frame); return;
        }
        const sourceIndex = frame.timestamp; // Private identity in microseconds; never a playback clock.
        if (!Number.isInteger(sourceIndex) || sourceIndex < 0 || sourceIndex >= this.frames.length
            || !this.pending.delete(sourceIndex)) {
          this.release(frame); this.fail(Error('Unexpected decoded frame identity')); return;
        }
        this.outstanding = this.pending.size; this.peak();
        if (sourceIndex < this.target) this.release(frame);
        else this.ready.set(sourceIndex, frame);
        this.onAvailable(sourceIndex);
        this.pump();
      },
      error: error => { if (generation === this.generation) this.fail(error); },
    });
    this.count('sessions');
    try { this.decoder.configure(this.config); this.pump(); }
    catch (error) { this.fail(error); }
  }

  demand(index) {
    if (!Number.isInteger(index) || index < 0 || index >= this.frames.length) throw Error('Invalid target');
    if (!this.decoder || index < this.target) { this.start(index); return; }
    this.target = index;
    for (const [i, frame] of this.ready) if (i < index) { this.ready.delete(i); this.release(frame); }
    this.pump();
  }

  pump() {
    const decoder = this.decoder;
    if (!decoder || this.paused || this.error || decoder.state !== 'configured') return;
    try {
      while (this.next < this.frames.length && this.next <= this.target + this.capacity - 1
          && this.outstanding + this.owned.size < this.capacity) {
        const packet = this.frames[this.next++];
        this.pending.add(packet.sourceIndex); this.outstanding = this.pending.size;
        this.count('submitted'); this.peak();
        decoder.decode(new this.Chunk({ type: packet.key ? 'key' : 'delta', timestamp: packet.sourceIndex, data: packet.data }));
      }
      // Flush only at end of stream. Every subsequent pass starts a new session
      // at a real keyframe, so a delta is never submitted after flush().
      if (this.next === this.frames.length && !this.flushing) {
        this.flushing = true;
        const generation = this.generation;
        decoder.flush().catch(error => { if (generation === this.generation) this.fail(error); });
      }
    } catch (error) { this.fail(error); }
  }

  take(index) { const frame = this.ready.get(index); this.ready.delete(index); return frame || null; }
  release(frame) {
    if (!this.owned.delete(frame)) return;
    frame.close(); this.count('closed');
  }
  fail(error) { this.count('errors'); this.error = error; this.stop(); this.onError(error); }
  stop() {
    this.generation++;
    if (this.decoder?.state !== 'closed') this.decoder?.close();
    this.decoder = null;
    for (const frame of this.owned) this.release(frame);
    this.ready.clear();
    this.pending.clear(); this.outstanding = 0;
  }
}
