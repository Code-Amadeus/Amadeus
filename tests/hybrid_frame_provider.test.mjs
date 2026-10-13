import assert from 'node:assert/strict';
import test from 'node:test';
import { FrameDecoder } from '../render/web/frame_video/decoder.mjs';
import { createHybridDescriptor } from '../render/web/frame_video/hybrid_source.mjs';

// The real bounded FrameDecoder runs against a manually driven native decoder
// fake. No WebCodecs implementation, Pixi application or GPU is instantiated.
function fixture(t, { split = true, capacity = 4, configureErrorStream = null } = {}) {
  const queues = [], drivers = [], outputs = [], frames = [], notifications = [], errors = [];
  const layout = { width: 16, height: 9, codedWidth: 16, codedHeight: 10 };
  const view = new EventTarget();
  let nextTexture = 0;
  class NativeDecoder {
    constructor(callbacks) {
      Object.assign(this, { callbacks, state: 'unconfigured', chunks: [], closeCalls: 0 });
      drivers.push(this);
    }
    configure(config) {
      this.config = config;
      if (config.stream === configureErrorStream) throw Error('synthetic configure failure');
      this.state = 'configured';
    }
    decode(chunk) { assert.equal(this.state, 'configured'); this.chunks.push(chunk); }
    async flush() {}
    close() { this.closeCalls++; this.state = 'closed'; }
    emit(index = this.chunks.shift()?.timestamp) {
      assert.ok(Number.isInteger(index), 'the test emits a known source identity');
      const frame = { timestamp: index, stream: this.config.stream, closes: 0,
        close() { this.closes++; assert.equal(this.closes, 1, 'VideoFrame closes once'); } };
      frames.push(frame); this.callbacks.output(frame); return frame;
    }
    error(message = 'synthetic decoder failure') { this.callbacks.error(Error(message)); }
  }
  class Queue extends FrameDecoder {
    constructor(packets, config, options) {
      super(packets, config, { ...options, Decoder: NativeDecoder,
        Chunk: class { constructor(value) { Object.assign(this, value); } } });
      queues.push(this);
    }
  }
  function makeOutput(_PIXI, renderer, actualLayout, options) {
    assert.equal(renderer.view, view); assert.equal(actualLayout, layout);
    assert.equal(options.separateAlpha, split);
    const pool = Array.from({ length: 2 }, () => ({ id: ++nextTexture, destroyed: false, sourceIndex: null }));
    const retained = new Map();
    let displayed = null, destroyed = false;
    const output = {
      pool, retained, presents: [], throwPresent: false, destroyCalls: 0, noSpare: 0,
      get texture() { return displayed; },
      present(...borrowed) {
        assert.equal(destroyed, false, 'a destroyed output cannot present');
        assert.equal(borrowed.length, split ? 2 : 1);
        for (const frame of borrowed) assert.equal(frame.closes, 0, 'upload borrows live frames');
        if (split) assert.equal(borrowed[0].timestamp, borrowed[1].timestamp, 'paired identities match');
        this.presents.push(borrowed.slice());
        if (this.throwPresent) throw Error('synthetic upload failure');
        const next = pool.find(tex => tex !== displayed && !retained.has(tex));
        if (!next) { this.noSpare++; return null; }
        next.sourceIndex = borrowed[0].timestamp; displayed = next;
        return { texture: next, uploadMs: 0.25, unpackSubmitMs: 0.5 };
      },
      retain(tex) {
        assert.ok(pool.includes(tex) && !tex.destroyed, 'a live output is retained');
        retained.set(tex, (retained.get(tex) || 0) + 1);
        let released = false;
        return () => {
          assert.equal(released, false, 'the adapter releases each output retain once');
          released = true;
          const remaining = retained.get(tex) - 1;
          if (remaining) retained.set(tex, remaining); else retained.delete(tex);
          if (!remaining && destroyed) tex.destroyed = true;
        };
      },
      destroy() {
        this.destroyCalls++; destroyed = true;
        for (const tex of pool) if (!retained.has(tex)) tex.destroyed = true;
        displayed = null;
      },
      snapshot() { return { destroyed, liveOutputTextures: pool.filter(tex => !tex.destroyed).length,
        retainedOutputTextures: retained.size, noSpareSkips: this.noSpare }; },
    };
    outputs.push(output); return output;
  }
  const streams = Array.from({ length: split ? 2 : 1 }, (_, stream) => ({
    frames: Array.from({ length: 24 }, (_, index) => ({ sourceIndex: index, key: index % 4 === 0,
      data: new Uint8Array([index]) })), config: { stream },
  }));
  const experiment = createHybridDescriptor({}, { view }, { metadata: { headFrames: 3, frames: 24, layout },
    split, streams, bytes: 480 }, { Decoder: Queue, makeOutput, capacity });
  const sources = [];
  function source() {
    const created = experiment.descriptor.createSource({ onFrame: index => notifications.push(index),
      onError: error => errors.push(error) });
    sources.push(created); return created;
  }
  // Decode each stream through a requested identity, preserving queue order and
  // exercising keyframe preroll drops in the actual FrameDecoder.
  function ready(index, selected = queues.slice(-streams.length)) {
    const result = [];
    for (const queue of selected) {
      const driver = queue.decoder;
      assert.ok(driver, 'the selected queue has an active decoder');
      while (!queue.ready.has(index)) {
        assert.ok(driver.chunks.length, `source ${index} is inside the bounded submitted window`);
        result.push(driver.emit());
      }
    }
    return result;
  }
  t.after(() => {
    for (const created of sources) created.destroy();
    for (const frame of frames) assert.equal(frame.closes, 1, 'all test VideoFrames are released at cleanup');
  });
  return { experiment, source, ready, queues, drivers, outputs, frames, notifications, errors, view };
}

test('separate streams publish only a paired source identity and release both presented frames', t => {
  const f = fixture(t), source = f.source();
  assert.equal(f.experiment.descriptor.headFrameCount, 3);
  source.requestWindow([{ index: 4 }, { index: 5 }]);
  const color = f.queues[0].decoder.emit();
  assert.equal(color.timestamp, 4);
  assert.equal(source.hasAny(), false); assert.equal(source.has(4), false);
  assert.deepEqual(f.notifications, []);
  const alpha = f.queues[1].decoder.emit();
  assert.equal(alpha.timestamp, 4);
  assert.equal(source.hasAny(), true); assert.equal(source.has(4), true);
  assert.deepEqual(f.notifications, [4]);
  const output = source.texture(4);
  assert.equal(output.sourceIndex, 4);
  assert.equal(color.closes, 1); assert.equal(alpha.closes, 1);
  assert.ok(f.queues.every(queue => !queue.ready.has(4) && queue.owned.size === 0));
  assert.equal(source.snapshot().displayedIndex, 4);
  assert.equal(source.snapshot().uploadMs, 0.25); assert.equal(source.snapshot().unpackSubmitMs, 0.5);
});

test('unpaired future outputs cannot notify or satisfy another source index', t => {
  const f = fixture(t), source = f.source();
  source.requestWindow([{ index: 4 }]);
  f.queues[0].decoder.emit(); f.queues[0].decoder.emit();
  const alpha4 = f.queues[1].decoder.emit();
  assert.deepEqual(f.notifications, [4]);
  assert.equal(source.texture(5), null); assert.equal(source.has(5), false);
  assert.equal(alpha4.closes, 0, 'a different pending identity is still owned');
  const alpha5 = f.queues[1].decoder.emit();
  assert.deepEqual(f.notifications, [4, 5]);
  assert.equal(source.texture(5).sourceIndex, 5); assert.equal(alpha5.closes, 1);
});

test('a consumed displayed target remains ready and returns its exact output without another upload', t => {
  const f = fixture(t), source = f.source();
  source.requestWindow([{ index: 4 }]); f.ready(4);
  const first = source.texture(4);
  source.retainDisplay(first);
  assert.ok(f.queues.every(queue => !queue.ready.has(4)));
  assert.equal(source.has(4), true); assert.equal(source.hasAny(), true);
  assert.equal(source.texture(4), first); assert.equal(source.texture(4), first);
  assert.equal(f.outputs[0].presents.length, 1); assert.equal(source.snapshot().displays, 1);
  source.releaseDisplay(first);
});

test('forward demand keeps decoder sessions and backward/reset demand starts each stream from its real keyframe', t => {
  const f = fixture(t), source = f.source();
  source.requestWindow([{ index: 5 }, { index: 6 }]);
  assert.ok(f.drivers.every(driver => driver.chunks[0].timestamp === 4 && driver.chunks[0].type === 'key'));
  f.ready(6);
  const skipped = f.frames.filter(frame => frame.timestamp <= 5);
  source.requestWindow([{ index: 7 }]);
  assert.equal(f.drivers.length, 2); assert.ok(f.queues.every(queue => queue.target === 7));
  assert.ok(skipped.every(frame => frame.closes === 1));
  const old = f.drivers.slice();
  source.requestWindow([{ index: 3 }]);
  assert.equal(f.drivers.length, 4); assert.ok(old.every(driver => driver.state === 'closed'));
  assert.ok(f.drivers.slice(-2).every(driver => driver.chunks[0].timestamp === 0 && driver.chunks[0].type === 'key'));
  assert.ok(f.queues.every(queue => queue.target === 3 && queue.stats.sessions === 2));
  source.requestWindow([]); assert.ok(f.queues.every(queue => queue.decoder === null));
  source.requestWindow([{ index: 4 }]);
  assert.equal(f.drivers.length, 6); assert.ok(f.queues.every(queue => queue.stats.sessions === 3));
});

test('stale native outputs and errors after reset cannot notify, replace a target or leak VideoFrames', t => {
  const f = fixture(t), source = f.source();
  source.requestWindow([{ index: 4 }]); const old = f.drivers.slice();
  source.requestWindow([{ index: 1 }]);
  for (const driver of old) {
    const late = driver.emit(4); assert.equal(late.closes, 1); driver.error('retired decoder error');
  }
  assert.deepEqual(f.notifications, []); assert.deepEqual(f.errors, []);
  assert.equal(source.has(4), false);
  f.ready(1); assert.equal(source.texture(1).sourceIndex, 1);
});

test('retain survives releaseClip/destroy and only the final nested release frees output and completes ownership', t => {
  const f = fixture(t), source = f.source();
  source.requestWindow([{ index: 4 }]); f.ready(4);
  const display = source.texture(4);
  source.retainDisplay(display); source.retainDisplay(display);
  source.releaseClip(); source.destroy(); source.destroy();
  assert.equal(display.destroyed, false); assert.equal(f.outputs[0].destroyCalls, 1);
  assert.equal(f.outputs[0].retained.get(display), 1, 'nested retains use one output ownership lease');
  assert.equal(f.experiment.snapshot().active.length, 1);
  source.releaseDisplay(display);
  assert.equal(display.destroyed, false); assert.equal(f.experiment.snapshot().active.length, 1);
  source.releaseDisplay(display);
  assert.equal(display.destroyed, true); assert.equal(f.experiment.snapshot().active.length, 0);
  const completed = f.experiment.snapshot().recentCompleted;
  assert.equal(completed.length, 1); assert.equal(completed[0].retained, 0);
  assert.ok(completed[0].queues.every(queue => queue.live === 0 && queue.outputs === queue.closed));
  assert.throws(() => source.releaseDisplay(display), /Unowned/);
});

test('context failure of retired but retained output remains observable once, and final release removes its observer', t => {
  const f = fixture(t), source = f.source();
  source.requestWindow([{ index: 4 }]); f.ready(4);
  const display = source.texture(4); source.retainDisplay(display); source.destroy();
  f.view.dispatchEvent(new Event('webglcontextlost'));
  assert.equal(f.errors.length, 1); assert.match(f.errors[0].message, /context lost/);
  assert.equal(display.destroyed, false);
  f.view.dispatchEvent(new Event('webglcontextlost')); source.injectFailure(Error('second failure'));
  assert.equal(f.errors.length, 1); assert.equal(f.experiment.snapshot().completedTotals.errors, 1);
  source.releaseDisplay(display);
  f.view.dispatchEvent(new Event('webglcontextlost')); source.injectFailure(Error('late failure'));
  assert.equal(f.errors.length, 1); assert.equal(f.experiment.snapshot().active.length, 0);
  const retired = f.source(); retired.destroy();
  f.view.dispatchEvent(new Event('webglcontextlost')); retired.injectFailure(Error('unretained retired failure'));
  assert.equal(f.errors.length, 1, 'fully retired sources have no context/error authority');
});

test('a presentation/upload failure closes both taken frames and queued pairs while preserving the previous retained output', t => {
  const f = fixture(t), source = f.source();
  source.requestWindow([{ index: 4 }]); f.ready(5);
  const display = source.texture(4); source.retainDisplay(display);
  f.outputs[0].throwPresent = true;
  assert.equal(source.texture(5), null);
  assert.equal(f.errors.length, 1); assert.match(f.errors[0].message, /upload failure/);
  assert.ok(f.frames.every(frame => frame.closes === 1));
  assert.ok(f.queues.every(queue => queue.owned.size === 0 && queue.ready.size === 0 && queue.decoder === null));
  assert.equal(display.destroyed, false);
  source.requestWindow([{ index: 6 }]); assert.equal(f.drivers.length, 2, 'failed sources cannot silently restart');
  source.destroy(); source.releaseDisplay(display); assert.equal(display.destroyed, true);
});

test('no spare output requeues both unconsumed frames and releases them once on later presentation or stop', t => {
  const f = fixture(t), source = f.source();
  source.requestWindow([{ index: 4 }]); f.ready(6);
  const first = source.texture(4); source.retainDisplay(first);
  const second = source.texture(5); source.retainDisplay(second);
  const pair = f.frames.filter(frame => frame.timestamp === 6);
  assert.equal(source.texture(6), null);
  assert.ok(pair.every(frame => frame.closes === 0));
  assert.ok(f.queues.every(queue => queue.ready.has(6) && queue.owned.has(queue.ready.get(6))));
  assert.equal(source.snapshot().displayedIndex, 5); assert.equal(f.outputs[0].noSpare, 1);
  source.releaseDisplay(first);
  assert.equal(source.texture(6), first); assert.ok(pair.every(frame => frame.closes === 1));
  source.releaseDisplay(second);
  source.requestWindow([{ index: 7 }]); f.ready(7);
  const lastPair = f.frames.filter(frame => frame.timestamp === 7);
  source.releaseClip(); assert.ok(lastPair.every(frame => frame.closes === 1));
});

test('paired frames requeued after no-spare are closed once when the source retires before another presentation', t => {
  const f = fixture(t), source = f.source();
  source.requestWindow([{ index: 4 }]); f.ready(6);
  const first = source.texture(4); source.retainDisplay(first);
  const second = source.texture(5); source.retainDisplay(second);
  const pair = f.frames.filter(frame => frame.timestamp === 6);
  assert.equal(source.texture(6), null); assert.ok(pair.every(frame => frame.closes === 0));
  source.releaseClip(); assert.ok(pair.every(frame => frame.closes === 1));
  assert.ok(f.queues.every(queue => queue.owned.size === 0 && queue.ready.size === 0));
  source.destroy(); assert.equal(first.destroyed, false); assert.equal(second.destroyed, false);
  source.releaseDisplay(first); source.releaseDisplay(second);
  assert.equal(first.destroyed, true); assert.equal(second.destroyed, true);
  assert.ok(f.queues.every(queue => queue.stats.closed === queue.stats.outputs));
});

test('an asynchronous decoder error stops both streams and closes all buffered paired identities exactly once', t => {
  const f = fixture(t), source = f.source();
  source.requestWindow([{ index: 4 }]); f.ready(6);
  assert.ok(f.frames.every(frame => frame.closes === 0));
  const old = f.drivers.slice();
  old[0].error('synthetic asynchronous decoder error');
  assert.equal(f.errors.length, 1); assert.match(f.errors[0].message, /asynchronous decoder error/);
  assert.ok(f.frames.every(frame => frame.closes === 1));
  assert.ok(f.queues.every(queue => queue.decoder === null && queue.owned.size === 0));
  for (const driver of old) {
    const late = driver.emit(7); assert.equal(late.closes, 1);
    driver.error('secondary retired error');
  }
  assert.equal(f.errors.length, 1); assert.equal(source.hasAny(), false); assert.equal(source.texture(6), null);
  source.requestWindow([{ index: 4 }]); assert.equal(f.drivers.length, 2);
});

test('an unexpected decoded source identity triggers observable pairing failure and releases the other stream', t => {
  const f = fixture(t), source = f.source();
  source.requestWindow([{ index: 4 }]);
  const waiting = f.queues[1].decoder.emit();
  const invalid = f.queues[0].decoder.emit(12);
  assert.equal(invalid.closes, 1); assert.equal(waiting.closes, 1);
  assert.equal(f.errors.length, 1); assert.match(f.errors[0].message, /identity/);
  assert.deepEqual(f.notifications, []); assert.equal(f.outputs[0].presents.length, 0);
  assert.ok(f.queues.every(queue => queue.decoder === null && queue.owned.size === 0));
});

test('combined-color-alpha fixture uses one decoder and still preserves output/frame lifetime', t => {
  const f = fixture(t, { split: false }), source = f.source();
  source.requestWindow([{ index: 4 }]); f.ready(4);
  assert.equal(f.queues.length, 1); assert.deepEqual(f.notifications, [4]);
  const display = source.texture(4); source.retainDisplay(display);
  assert.equal(f.frames[0].closes, 1); assert.equal(f.outputs[0].presents[0].length, 1);
  source.destroy(); assert.equal(display.destroyed, false);
  source.releaseDisplay(display); assert.equal(display.destroyed, true);
});

test('a synchronous first-stream configuration failure cannot start or leave the paired stream running', t => {
  const f = fixture(t, { configureErrorStream: 0 }), source = f.source();
  source.requestWindow([{ index: 4 }]);
  assert.equal(f.errors.length, 1); assert.match(f.errors[0].message, /configure failure/);
  assert.ok(f.queues.every(queue => queue.decoder === null), 'every paired decoder is stopped after failure');
  assert.equal(source.snapshot().running, false);
});

test('lazy fixture ownership aborts obsolete entry and cannot create decoders after replacement', async () => {
  const { createLazyHybridDescriptor } = await import('../render/web/frame_video/hybrid_source.mjs');
  let resolve, signal, created = 0;
  const experiment = createLazyHybridDescriptor({headFrames:3}, s => {signal=s; return new Promise(r=>{resolve=r});},
    () => {created++; throw Error('must not create');});
  const source = experiment.descriptor.createSource({onFrame(){assert.fail('obsolete callback');},onError(){assert.fail('obsolete error');}});
  source.requestWindow([{index:3}]); await Promise.resolve();
  source.releaseClip(); source.destroy();
  assert.equal(signal.aborted,true);
  resolve({}); for(let i=0;i<12;i++) await Promise.resolve();
  assert.equal(created,0); assert.deepEqual(experiment.snapshot().active,[]);
});

test('lazy fixture resolves with the newest demand and retains output until display release', async () => {
  const { createLazyHybridDescriptor } = await import('../render/web/frame_video/hybrid_source.mjs');
  let resolve, requests=[], retains=0, destroyed=false;
  const native={hasAny:()=>true,has:()=>true,texture:()=>42,requestWindow:r=>requests.push(r),
    retainDisplay(){retains++;},releaseDisplay(){retains--;},releaseClip(){},destroy(){destroyed=true;},
    snapshot:()=>({destroyed,queues:[],retained:retains})};
  const experiment=createLazyHybridDescriptor({headFrames:3},()=>new Promise(r=>resolve=r),()=>({
    descriptor:{createSource:()=>native},snapshot:()=>({compressedBytes:123,completedTotals:{outputs:7,closed:7},recentCompleted:[]})}));
  const source=experiment.descriptor.createSource({onFrame(){},onError(e){throw e;}});
  source.requestWindow([{index:3}]); source.requestWindow([{index:9}]); await Promise.resolve();
  resolve({}); for(let i=0;i<12;i++) await Promise.resolve();
  assert.deepEqual(requests,[[{index:9}]]);
  source.retainDisplay(42); source.destroy();
  assert.equal(destroyed,true); assert.equal(experiment.snapshot().active.length,1);
  source.releaseDisplay(42);
  assert.equal(experiment.snapshot().active.length,0);
  assert.equal(experiment.snapshot().completedTotals.outputs,7);
});


test('only aliases with identical indexed assets and timing share a canonical video descriptor', async () => {
  const {findVideoAliases}=await import('../render/web/frame_video/hybrid_source.mjs');
  const sprite={_frameUrls:{smile:['a','b'],happy:['a','b'],fast:['a','b'],reverse:['b','a'],short:['a']},
    _frameIntervals:{smile:17,happy:17,fast:5,reverse:17,short:17}};
  const aliases=findVideoAliases(sprite,[{label:'smile',metadata:{frameIntervalMs:17}}]);
  assert.deepEqual([...aliases],[['happy','smile']]);
});
