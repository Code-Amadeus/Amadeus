import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { parseIvf, keyframeAtOrBefore } from '../tools/probes/frame-video/ivf.mjs';
import { FrameDecoder } from '../tools/probes/frame-video/decoder.mjs';
import { createVideoOutput, readCalibrationIdentity } from '../tools/probes/frame-video/pixi-output.mjs';

function fixture(count = 8) {
  const bytes = Buffer.alloc(32 + count*13);
  bytes.write('DKIF'); bytes.writeUInt16LE(32,6); bytes.write('VP90',8);
  bytes.writeUInt16LE(16,12); bytes.writeUInt16LE(10,14); bytes.writeUInt32LE(count,24);
  const index = [];
  for (let i=0;i<count;i++) { const offset=32+i*13; bytes.writeUInt32LE(1,offset); index.push({sourceIndex:i,offset:offset+12,size:1,key:i%4===0}); }
  return { bytes, index };
}
test('IVF preserves packet identities and refuses truncated or mismatched indices', () => {
  const { bytes,index }=fixture(); const parsed=parseIvf(bytes,index);
  assert.equal(parsed.frames.length,8); assert.equal(parsed.width,16);
  assert.equal(keyframeAtOrBefore(parsed.frames,7),4);
  assert.throws(()=>parseIvf(bytes.subarray(0,-1),index),/Truncated/);
  assert.throws(()=>parseIvf(bytes,[...index].reverse()),/identity/);
  assert.throws(()=>keyframeAtOrBefore(parsed.frames,-1),/Invalid/);
});

class FakeDecoder {
  static instances=[];
  constructor(callbacks) { this.callbacks=callbacks; this.state='unconfigured';this.pending=[];this.flushes=0;FakeDecoder.instances.push(this); }
  configure() { this.state='configured'; }
  decode(chunk) { this.pending.push(chunk); }
  flush() { this.flushes++;return Promise.resolve(); }
  close() { this.state='closed'; }
  output(index=this.pending.shift()?.timestamp) {
    const frame={timestamp:index,closes:0,close(){this.closes++;}};this.callbacks.output(frame);return frame;
  }
}
function queue(capacity=4) {
  const f=fixture(12);return new FrameDecoder(parseIvf(f.bytes,f.index).frames,{},
    {capacity,Decoder:FakeDecoder,Chunk:class {constructor(value){Object.assign(this,value);}}});
}
test('bounded supply advances with requested targets and releases skipped frames',()=>{
  const q=queue();q.start();const d=q.decoder;
  assert.equal(d.pending.length,4);
  for(let i=0;i<4;i++)d.output();
  assert.equal(q.owned.size,4);assert.equal(q.stats.submitted,4);
  q.demand(7);
  assert.equal(q.owned.size,0);assert.equal(q.stats.closed,4);
  for(let i=0;i<4;i++)d.output();
  const frame=q.take(7);assert.equal(frame.timestamp,7);q.release(frame);q.stop();
  assert.equal(q.owned.size,0);assert.equal(q.stats.closed,q.stats.outputs);
  assert.ok(q.stats.peakOutstanding<=4);
});
test('restart begins at a real keyframe and stale outputs cannot enter the new queue',()=>{
  const q=queue();q.start();const old=q.decoder;q.start(6);
  assert.equal(q.decoder.pending[0].timestamp,4);assert.equal(q.decoder.pending[0].type,'key');
  const late=old.output(0);assert.equal(late.closes,1);assert.equal(q.ready.has(0),false);
  q.stop();assert.equal(q.stats.closed,q.stats.outputs);
});
test('end flush runs once and all outputs have one close on shutdown',()=>{
  const q=queue(16);q.start();const d=q.decoder;
  assert.equal(d.flushes,1);
  const frames=Array.from({length:12},()=>d.output());
  q.pump();assert.equal(d.flushes,1);q.stop();q.stop();
  assert.ok(frames.every(f=>f.closes===1));assert.equal(q.stats.closed,12);
});
test('decoder errors release buffered frames and a new session can recover',()=>{
  const q=queue();q.start();const f=q.decoder.output();
  q.decoder.callbacks.error(Error('device lost'));
  assert.equal(q.decoder,null);assert.equal(f.closes,1);assert.match(q.error.message,/device lost/);
  q.start(4);assert.equal(q.error,null);assert.equal(q.decoder.pending[0].type,'key');q.stop();
});

test('unsolicited and duplicate identities fail rather than satisfying the wrong target', () => {
  for (const index of [8, -1, 1.5]) {
    const q = queue(); q.start(); const d = q.decoder;
    const invalid = d.output(index);
    assert.match(q.error.message, /identity/); assert.equal(invalid.closes, 1);
    assert.equal(q.outstanding, 0); assert.equal(q.pending.size, 0); assert.equal(q.owned.size, 0);
  }
  const q = queue(); q.start(); const d = q.decoder;
  const first = d.output(0), duplicate = d.output(0);
  assert.equal(first.closes, 1); assert.equal(duplicate.closes, 1);
  assert.match(q.error.message, /identity/);
});

test('phase counts isolate bounded stalls and resetting a session does not release another session', () => {
  const q = queue(); q.start(); const old = q.decoder;
  q.setPhase('stall'); q.setPaused(true);
  for (let i = 0; i < 4; i++) old.output();
  q.demand(7);
  assert.equal(q.phaseStats.submitted, 0); assert.equal(q.outstanding, 0);
  assert.equal(q.snapshot().scoped.closed, 4);
  q.setPaused(false); assert.equal(q.outstanding, 4);
  q.start(4); const current = q.decoder, before = q.snapshot();
  const late = old.output(4);
  assert.equal(late.closes, 1); assert.equal(q.snapshot().outstanding, before.outstanding);
  assert.equal(q.phaseStats.staleOutputs, 1); assert.equal(current.state, 'configured');
  q.stop(); assert.equal(q.stats.outputs, q.stats.closed);
});

test('repeated reset, full traversal, and stale output close remain bounded for many sessions', () => {
  const q = queue();
  for (let session = 0; session < 300; session++) {
    q.start(0); const decoder = q.decoder;
    for (let target = 0; target < 12; target++) {
      q.demand(target);
      while (!q.ready.has(target)) decoder.output();
      const frame = q.take(target); assert.equal(frame.timestamp, target); q.release(frame);
      assert.ok(q.owned.size + q.outstanding <= q.capacity);
      assert.equal(q.pending.size, q.outstanding);
    }
    q.stop(); decoder.output(11);
    assert.equal(q.owned.size, 0); assert.equal(q.outstanding, 0);
  }
  assert.equal(q.stats.outputs, q.stats.closed); assert.ok(q.stats.peakOutstanding <= q.capacity);
});

test('configure failures close the failing decoder and invalid restart leaves the current session intact', () => {
  const q = queue(); q.start(); const current = q.decoder;
  assert.throws(() => q.start(-1), /Invalid/); assert.equal(q.decoder, current);
  class BadDecoder extends FakeDecoder { configure() { throw Error('unsupported configuration'); } }
  q.Decoder = BadDecoder; q.start();
  assert.match(q.error.message, /unsupported configuration/);
  assert.equal(q.decoder, null); assert.equal(q.outstanding, 0); assert.equal(q.stats.errors, 1);
});

function pixiFixture() {
  const uploaded = [], renders = [], textures = [];
  class Resource {
    constructor(width, height) { Object.assign(this, { width, height }); }
    update() {}
  }
  class BaseTexture {
    constructor(resource) { Object.assign(this, { resource, realWidth: resource.width, realHeight: resource.height }); }
  }
  class Texture {
    constructor(baseTexture) { this.baseTexture = baseTexture; this.destroyed = 0; }
    destroy() { this.destroyed++; }
  }
  class Geometry {
    addAttribute() { return this; } addIndex() { return this; } destroy() {}
  }
  const PIXI = { Resource, BaseTexture, Texture, Geometry,
    ALPHA_MODES: { NO_PREMULTIPLIED_ALPHA: 0 }, MIPMAP_MODES: { OFF: 0 }, SCALE_MODES: { NEAREST: 0 },
    Shader: { from(vertex, fragment, uniforms) { return { vertex, fragment, uniforms, destroy() {} }; } },
    State: { for2d: () => ({}) }, Mesh: class { constructor(geometry, shader) { this.shader = shader; } destroy() {} },
    RenderTexture: { create(options) { const texture = { ...options, destroyed: 0, destroy() { this.destroyed++; } }; textures.push(texture); return texture; } },
  };
  const gl = { pixelStorei() {}, texImage2D(...args) { uploaded.push(args.at(-1)); } };
  const renderer = { gl, texture: { bind(base) { base.resource.upload(renderer, base, {}); } },
    render(mesh, options) { renders.push({ shader: mesh.shader, ...options }); } };
  return { PIXI, renderer, uploaded, renders, textures, layout: { width: 256, height: 127, codedWidth: 520, codedHeight: 128, alphaX: 264 } };
}

test('retained outputs survive owner destruction and the two-texture pool never overwrites a retained image', () => {
  const f = pixiFixture(), output = createVideoOutput(f.PIXI, f.renderer, f.layout);
  const first = output.present({ timestamp: 0 }).texture, releaseFirst = output.retain(first);
  const second = output.present({ timestamp: 1 }).texture, releaseSecond = output.retain(second);
  assert.notEqual(first, second); assert.equal(output.present({ timestamp: 2 }), null);
  assert.equal(output.snapshot().scoped.noSpareSkips, 1); assert.equal(f.uploaded.length, 2);
  releaseFirst(); assert.equal(output.present({ timestamp: 2 }).texture, first);
  output.destroy(); output.destroy();
  assert.equal(first.destroyed, 1); assert.equal(second.destroyed, 0);
  assert.equal(output.snapshot().liveOutputTextures, 1); assert.equal(output.snapshot().liveUploadTextures, 0);
  releaseSecond(); releaseSecond(); assert.equal(second.destroyed, 1);
  assert.equal(output.snapshot().liveOutputTextures, 0);
  assert.throws(() => output.present({ timestamp: 3 }), /destroyed/);
});

test('split color and alpha cannot publish mismatched identities and borrow no frames after upload', () => {
  const f = pixiFixture(), output = createVideoOutput(f.PIXI, f.renderer, { ...f.layout, codedWidth: 256 }, { separateAlpha: true });
  assert.throws(() => output.present({ timestamp: 1 }, { timestamp: 2 }), /identities/);
  assert.throws(() => output.present({ timestamp: 1 }), /identities/); assert.equal(f.uploaded.length, 0);
  output.present({ timestamp: 3 }, { timestamp: 3 });
  assert.equal(f.uploaded.length, 2); assert.equal(output.snapshot().borrowedVideoFrames, 0);
  assert.match(f.renders[0].shader.fragment, /texture2D\(uAlpha, uv\)\.r/);
  output.hold(true); assert.equal(output.present({ timestamp: 4 }, { timestamp: 4 }), null);
  assert.equal(f.uploaded.length, 2); output.destroy();
});

test('upload failures release both borrowed references and retain the previous published output', () => {
  const f = pixiFixture(), output = createVideoOutput(f.PIXI, f.renderer, f.layout, { separateAlpha: true });
  const first = output.present({ timestamp: 1 }, { timestamp: 1 }).texture;
  f.renderer.gl.texImage2D = () => { throw Error('device failure'); };
  assert.throws(() => output.present({ timestamp: 2 }, { timestamp: 2 }), /device failure/);
  assert.equal(output.texture, first); assert.equal(output.snapshot().borrowedVideoFrames, 0);
  assert.equal(output.snapshot().scoped.uploadErrors, 1); output.destroy();
});

function calibrationPixels(sourceIndex, alphaSourceIndex = null) {
  const pixels = new Uint8Array(256 * 127 * 4);
  for (let bit = 0; bit < 5; bit++) for (let y = 24; y < 48; y++) for (let x = bit * 32; x < (bit + 1) * 32; x++) {
    const offset = (y * 256 + x) * 4, color = sourceIndex & 2 ** bit ? 255 : 0;
    pixels.set([color, color, color, 255], offset);
  }
  if (alphaSourceIndex !== null) {
    for (let bit = 0; bit < 5; bit++) for (let y = 48; y < 72; y++) for (let x = bit * 32; x < (bit + 1) * 32; x++) {
      pixels[(y * 256 + x) * 4 + 3] = alphaSourceIndex & 2 ** bit ? 255 : 0;
    }
  }
  return pixels;
}
test('calibration identities come from opaque pixel markers and ambiguous markers are rejected', () => {
  for (let sourceIndex = 0; sourceIndex < 24; sourceIndex++) {
    assert.equal(readCalibrationIdentity(calibrationPixels(sourceIndex), 256, 127).sourceIndex, sourceIndex);
  }
  const ambiguous = calibrationPixels(1);
  for (let y = 32; y < 40; y++) for (let x = 12; x < 20; x++) ambiguous.set([127, 127, 127, 255], (y * 256 + x) * 4);
  assert.equal(readCalibrationIdentity(ambiguous, 256, 127).valid, false);
  const transparent = calibrationPixels(0); transparent[(32 * 256 + 12) * 4 + 3] = 0;
  assert.equal(readCalibrationIdentity(transparent, 256, 127).valid, false);
  assert.throws(() => readCalibrationIdentity(transparent, 256, 128), /dimensions/);
});

test('independent color and alpha pixel identities must match when the reference advertises alpha markers', () => {
  for (let sourceIndex = 0; sourceIndex < 24; sourceIndex++) {
    const identity = readCalibrationIdentity(calibrationPixels(sourceIndex, sourceIndex), 256, 127, { alphaIdentityMarker: true });
    assert.equal(identity.valid, true); assert.equal(identity.sourceIndex, sourceIndex);
    assert.equal(identity.alphaSourceIndex, sourceIndex); assert.equal(identity.alphaMatchesColor, true);
  }
  const mixed = calibrationPixels(7, 8);
  const mismatched = readCalibrationIdentity(mixed, 256, 127, { alphaIdentityMarker: true });
  assert.equal(mismatched.sourceIndex, 7); assert.equal(mismatched.alphaSourceIndex, 8);
  assert.equal(mismatched.valid, false); assert.equal(mismatched.alphaMatchesColor, false);
  // An old reference never asserted that its alpha channel carried frame IDs.
  assert.equal(readCalibrationIdentity(mixed, 256, 127).valid, true);
  const ambiguous = calibrationPixels(7, 7);
  for (let y = 56; y < 64; y++) for (let x = 12; x < 20; x++) ambiguous[(y * 256 + x) * 4 + 3] = 127;
  const invalid = readCalibrationIdentity(ambiguous, 256, 127, { alphaIdentityMarker: true });
  assert.equal(invalid.valid, false); assert.equal(invalid.alphaSourceIndex, null);
});

test('probe exercises lifetime, context recovery and actual markers without playback readbacks in a fake GPU', async () => {
  const f = pixiFixture(), data = fixture(24);
  data.bytes.writeUInt16LE(256, 12); data.bytes.writeUInt16LE(128, 14);
  const descriptor = { video: 'color.ivf', codec: 'vp09.00.51.08', colorSpace: {}, index: data.index };
  const metadata = { schema: 'amadeus.frame-video-split-experiment.v1', pairing: 'sourceIndex',
    streams: { color: descriptor, alpha: { ...descriptor, video: 'alpha.ivf' } },
    layout: { width: 256, height: 127, codedWidth: 256, codedHeight: 128 }, frames: 24,
    frameIntervalMs: 17, headFrames: 18, reference: { clip: 'alpha-calibration', sourceKind: 'synthetic-alpha-ramp', alphaIdentityMarker: true } };
  const view = new EventTarget(); let lost = false, activePhase, readbacks = 0;
  const extension = { loseContext() {
    lost = true; view.dispatchEvent(new Event('webglcontextlost', { cancelable: true }));
    setTimeout(() => { lost = false; view.dispatchEvent(new Event('webglcontextrestored')); }, 0);
  } };
  Object.assign(f.renderer.gl, { getExtension: name => name === 'WEBGL_lose_context' ? extension : null,
    getError: () => 0, isContextLost: () => lost,
    finish() { assert.notEqual(activePhase, 'playback'); } });
  f.renderer.render = (mesh, options) => {
    if (options?.renderTexture) {
      const frame = mesh.shader.uniforms.uColor.baseTexture.resource.frame;
      const alpha = mesh.shader.uniforms.uAlpha.baseTexture.resource.frame;
      assert.equal(frame.closes, 0); assert.equal(alpha.closes, 0); assert.equal(frame.timestamp, alpha.timestamp);
      options.renderTexture.pixels = calibrationPixels(frame.timestamp, alpha.timestamp);
    }
  };
  f.renderer.extract = { pixels(texture) {
    assert.notEqual(activePhase, 'playback'); readbacks++;
    return new Uint8Array(texture.pixels);
  }, canvas() { assert.notEqual(activePhase, 'playback'); return { toDataURL: () => 'data:image/png;base64,eA==' }; } };
  class Application {
    constructor() { this.view = view; this.renderer = f.renderer; this.stage = { addChild() {} }; }
    destroy() { lost = true; }
  }
  class AutomaticDecoder extends FakeDecoder {
    static async isConfigSupported() { return { supported: true }; }
    decode(chunk) { queueMicrotask(() => this.output(chunk.timestamp)); }
  }
  class AutomaticQueue extends FrameDecoder {
    constructor(frames, config, options) {
      super(frames, config, { ...options, Decoder: AutomaticDecoder,
        Chunk: class { constructor(value) { Object.assign(this, value); } } });
    }
  }
  const context = { window: {}, PIXI: { ...f.PIXI, Application, Sprite: class {} }, Event,
    document: { body: { appendChild() {} } }, VideoDecoder: AutomaticDecoder,
    EncodedVideoChunk: class { constructor(value) { Object.assign(this, value); } },
    fetch: async url => url.endsWith('clip.json') ? { json: async () => metadata } : { arrayBuffer: async () => data.bytes },
    performance, setTimeout, clearTimeout, FrameDecoder: AutomaticQueue, parseIvf, createVideoOutput, readCalibrationIdentity,
    console: { info(message) { activePhase = JSON.parse(message.slice('FRAME_VIDEO_EVENT '.length)).phase; } } };
  const html = fs.readFileSync(new URL('../tools/probes/frame-video/probe.html', import.meta.url), 'utf8');
  const body = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1].replace(/^import .*$/gm, '');
  vm.runInNewContext(body, context);
  const result = await context.window.runProbe({ seconds: .06, coldRuns: 1, identityRounds: 2,
    targets: [0, 23, 18, 0], shutdownSettleMs: 0 });
  assert.equal(result.validation.passed, true, JSON.stringify(result.checks.filter(check => !check.passed)));
  assert.equal(result.contextRecovery.status, 'exercised'); assert.equal(result.contextRecovery.exactPixels, true);
  assert.equal(result.phases.find(phase => phase.name === 'playback').readbacks, 0);
  assert.ok(readbacks >= 24 * 3);
  assert.ok(result.identities.some(identity => identity.reason === 'retained final playback output'));
  assert.equal(result.validation.calibration.alphaIdentityMarker, true);
  assert.ok(result.checks.some(check => check.name === 'calibration alpha frame identity'));
  assert.ok(result.identities.every(identity => identity.alphaSourceIndex === identity.expected && identity.alphaMatchesColor));
  assert.equal(result.cleanup.afterOutputDestroy.output.liveOutputTextures, 0);
  assert.ok(result.cleanup.afterOutputDestroy.queues.every(queue => queue.liveFrames === 0));
});
