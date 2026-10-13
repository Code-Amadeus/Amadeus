// Experimental adapter between SpriteRenderer's demand seam and the measured
// WebCodecs/Pixi resources. No product manifest or default activation path.
import { parseIvf } from './ivf.mjs';
import { FrameDecoder } from './decoder.mjs';
import { createVideoOutput } from './pixi-output.mjs';

export async function loadHybridFixture(baseUrl, installedPack, { metadata: suppliedMetadata, signal } = {}) {
  const started = performance.now();
  const base = new URL(baseUrl, location.href);
  const get = async name => {
    const response = await fetch(new URL(name, base), { cache: 'no-store', signal });
    if (!response.ok) throw Error(`Video fixture fetch failed: ${response.status}`);
    return response;
  };
  const metadata = suppliedMetadata || await (await get('clip.json')).json();
  const split = validateMetadata(metadata, installedPack);
  const descriptions = split ? [metadata.streams.color, metadata.streams.alpha] : [metadata];
  const streams = [];
  let bytes = 0;
  for (const description of descriptions) {
    const data = await (await get(description.video)).arrayBuffer();
    const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', data))]
      .map(value => value.toString(16).padStart(2,'0')).join('');
    if (digest !== description.videoSha256) throw Error('Video fixture hash mismatch');
    const ivf = parseIvf(data, description.index);
    if (ivf.frames.length !== metadata.frames || ivf.width !== metadata.layout.codedWidth
        || ivf.height !== metadata.layout.codedHeight) throw Error('Video fixture geometry/count mismatch');
    const config = { codec: description.codec, codedWidth: ivf.width, codedHeight: ivf.height,
      colorSpace: description.colorSpace, hardwareAcceleration: 'prefer-hardware', optimizeForLatency: true };
    if (!(await VideoDecoder.isConfigSupported(config)).supported) throw Error('Video configuration unsupported');
    streams.push({ frames: ivf.frames, config }); bytes += data.byteLength;
  }
  return { metadata, split, streams, bytes, loadMs: performance.now()-started };
}

function validateMetadata(metadata, installedPack) {
  if (typeof metadata.reference?.clip !== 'string' || !metadata.reference.clip || !metadata.reference.productionLineageVerified
      || !metadata.reference.currentPackDimensionsMatch) throw Error('Hybrid probe requires a verified installed PNG clip');
  if (!installedPack?.available || metadata.reference.packVersion !== installedPack.version
      || metadata.reference.manifestSha256 !== installedPack.sourceSha256?.['runtime_manifest.json']) {
    throw Error('Video fixture differs from the currently hosted pack identity');
  }
  if (!Number.isInteger(metadata.headFrames) || metadata.headFrames < 1 || metadata.headFrames >= metadata.frames-1) {
    throw Error('Invalid hybrid static head');
  }
  const split = metadata.schema === 'amadeus.frame-video-split-experiment.v1';
  if (!split && metadata.schema !== 'amadeus.frame-video-experiment.v1') throw Error('Unknown video fixture');
  return split;
}

export function createHybridDescriptor(PIXI, renderer, fixture, {
  Decoder = FrameDecoder, makeOutput = createVideoOutput, capacity = 16,
} = {}) {
  const active = new Set(), completed = [];
  let created = 0;
  const total = { submitted: 0, outputs: 0, closed: 0, sessions: 0, displays: 0, errors: 0 };
  const descriptor = {
    headFrameCount: fixture.metadata.headFrames,
    createSource({ onFrame, onError }) {
      const id = ++created, output = makeOutput(PIXI, renderer, fixture.metadata.layout, { separateAlpha: fixture.split });
      const retains = new Map();
      let destroyed = false, finished = false, failed = false, running = false, displayedIndex = -1;
      let displays = 0, lastTarget = null, uploadMs = 0, unpackSubmitMs = 0;
      const queues = fixture.streams.map(stream => new Decoder(stream.frames, stream.config, {
        capacity,
        onAvailable: index => { if (!destroyed && queues.every(queue => queue.ready.has(index))) onFrame(index); },
        onError: error => fail(error),
      }));
      const stop = () => { for (const queue of queues) queue.stop(); running = false; };
      const fail = error => {
        if (failed || (destroyed && !retains.size)) return;
        failed = true; total.errors++; stop(); onError(error);
      };
      const lost = () => fail(Error('WebGL context lost; requesting static frame recovery'));
      renderer.view.addEventListener('webglcontextlost', lost);
      const snapshot = () => ({ id, destroyed, retained: retains.size, running, displayedIndex, lastTarget,
        displays, uploadMs, unpackSubmitMs, queues: queues.map(queue => ({ ...queue.stats, live: queue.owned.size })),
        output: output.snapshot?.() });
      const finish = () => {
        if (finished || !destroyed || retains.size) return;
        finished = true; renderer.view.removeEventListener('webglcontextlost', lost);
        active.delete(source);
        const stats = snapshot(); completed.push(stats); if (completed.length > 32) completed.shift();
        for (const queue of queues) for (const key of ['submitted','outputs','closed','sessions']) total[key] += queue.stats[key];
        total.displays += displays;
      };
      const source = {
        hasAny() { return displayedIndex >= 0 || [...queues[0].ready.keys()].some(i => queues.every(q => q.ready.has(i))); },
        has(index) { return displayedIndex === index || queues.every(queue => queue.ready.has(index)); },
        texture(index) {
          if (displayedIndex === index) return output.texture;
          if (destroyed || !queues.every(queue => queue.ready.has(index))) return null;
          const frames = queues.map(queue => queue.take(index));
          let consumed = false;
          try {
            const shown = output.present(...frames);
            if (!shown) return null;
            consumed = true; displayedIndex = index; displays++;
            uploadMs += shown.uploadMs; unpackSubmitMs += shown.unpackSubmitMs;
            return shown.texture;
          } catch (error) { consumed = true; fail(error); return null; }
          finally {
            frames.forEach((frame, i) => {
              if (consumed || destroyed || failed) queues[i].release(frame);
              else queues[i].ready.set(index, frame);
            });
          }
        },
        requestWindow(targets) {
          if (destroyed || failed) return;
          if (!targets.length) { stop(); return; }
          const target = targets[0].index;
          lastTarget = target;
          const starting = !running;
          running = true;
          for (const queue of queues) {
            if (starting) queue.start(target);
            else queue.demand(target);
            // A synchronous codec error retires the whole pair, including the
            // stream whose dispatch has not started yet.
            if (failed || destroyed) break;
          }
        },
        retainDisplay(texture) {
          const held = retains.get(texture);
          if (held) held.count++;
          else retains.set(texture, { count: 1, release: output.retain(texture) });
        },
        releaseDisplay(texture) {
          const held = retains.get(texture);
          if (!held) throw Error('Unowned video display release');
          if (--held.count === 0) { held.release(); retains.delete(texture); }
          finish();
        },
        releaseClip: stop,
        destroy() { if (destroyed) return; destroyed = true; stop(); output.destroy(); finish(); },
        snapshot,
        injectFailure: fail,
      };
      active.add(source);
      return source;
    },
  };
  return {
    descriptor,
    snapshot: () => ({ created, completedTotals: { ...total }, active: [...active].map(source => source.snapshot()),
      recentCompleted: completed, compressedBytes: fixture.bytes }),
    failActive: () => { for (const source of [...active]) source.injectFailure(Error('Injected video decoder failure')); },
  };
}

// A descriptor is cheap metadata. Compressed bytes, decoder queues and output
// textures belong to the active source, not to all registered animations.
export function createLazyHybridDescriptor(metadata, load, makeExperiment) {
  const active = new Set(), completed = [];
  const totals = { submitted: 0, outputs: 0, closed: 0, sessions: 0, displays: 0, errors: 0 };
  let created = 0, loads = 0, loadMs = 0;
  return {
    descriptor: { headFrameCount: metadata.headFrames, createSource(callbacks) {
      const controller = new AbortController();
      let inner = null, experiment = null, destroyed = false, finished = false, loading = true, retained = 0, window = [];
      const id = ++created;
      const snapshot = () => ({ id, loading: !inner && !destroyed, destroyed, retained,
        running: false, displayedIndex: -1, queues: [], ...inner?.snapshot() });
      const finish = () => {
        if (finished || loading || !destroyed || retained) return;
        finished = true;
        if (experiment) {
          const stats = experiment.snapshot();
          for (const key of Object.keys(totals)) totals[key] += stats.completedTotals[key] || 0;
          completed.push(...stats.recentCompleted); if (completed.length > 32) completed.splice(0, completed.length-32);
        }
        active.delete(proxy); inner = experiment = null;
      };
      const proxy = {
        hasAny: () => inner?.hasAny() || false,
        has: index => inner?.has(index) || false,
        texture: index => inner?.texture(index) || null,
        requestWindow(targets) { if (!destroyed) { window = targets; inner?.requestWindow(targets); } },
        retainDisplay(texture) { if (!inner) throw Error('Video is not ready'); inner.retainDisplay(texture); retained++; },
        releaseDisplay(texture) { inner.releaseDisplay(texture); retained--; finish(); },
        releaseClip() { window = []; inner?.releaseClip(); },
        destroy() { if (destroyed) return; destroyed = true; controller.abort(); inner?.destroy(); finish(); },
        snapshot,
        fail() { experiment?.failActive(); },
        stats() { return experiment?.snapshot(); },
      };
      active.add(proxy);
      const started = performance.now(); loads++;
      Promise.resolve().then(() => load(controller.signal)).then(fixture => {
        if (destroyed) return;
        loadMs += performance.now() - started;
        experiment = makeExperiment(fixture);
        inner = experiment.descriptor.createSource(callbacks);
        if (destroyed) { inner.destroy(); finish(); return; }
        inner.requestWindow(window);
      }).catch(error => { if (!destroyed) callbacks.onError(error); }).finally(() => { loading = false; finish(); });
      return proxy;
    } },
    snapshot() {
      const live = [...active];
      return { created, loads, fixtureLoadMs: loadMs, completedTotals: { ...totals },
        active: live.map(source => source.snapshot()), recentCompleted: [...completed],
        compressedBytes: live.reduce((sum, source) => sum+(source.stats()?.compressedBytes || 0),0) };
    },
    failActive() { for (const source of [...active]) source.fail(); },
  };
}

// Aliases are derived from the installed indexed assets, not guessed from tag
// spelling. Different frame order or playback rate is a separate clip contract.
export function findVideoAliases(sprite, entries) {
  const canonical = new Set(entries.map(entry => entry.label)), aliases = new Map();
  for (const [label, urls] of Object.entries(sprite._frameUrls)) {
    if (canonical.has(label)) continue;
    const target = entries.find(entry => sprite._frameIntervals[label] === entry.metadata.frameIntervalMs
      && urls.length === sprite._frameUrls[entry.label].length
      && urls.every((url,index) => url === sprite._frameUrls[entry.label][index]));
    if (target) aliases.set(label,target.label);
  }
  return aliases;
}

export async function installHybridProbe(baseUrl, installedPack, { bundle = false } = {}) {
  const sprite = window.renderApp._sprite, renderer = window.wallpaperApp.scene.app.renderer;
  const base = new URL(baseUrl, location.href), entries = [];
  if (bundle) {
    const response = await fetch(new URL('bundle.json', base), { cache: 'no-store' });
    if (!response.ok) throw Error('Video bundle unavailable');
    const manifest = await response.json();
    if (manifest.schema !== 'amadeus.frame-video-bundle-experiment.v1'
        || manifest.packVersion !== installedPack.version
        || manifest.manifestSha256 !== installedPack.sourceSha256?.['runtime_manifest.json']) throw Error('Video bundle pack mismatch');
    const labels = new Set();
    for (const item of manifest.clips) {
      if (labels.has(item.label) || item.path !== item.label+'/') throw Error('Invalid bundle label/path');
      labels.add(item.label);
      const url = new URL(item.path, base);
      const response = await fetch(new URL('clip.json',url), { cache: 'no-store' });
      if (!response.ok) throw Error('Video clip metadata unavailable');
      const metadata = await response.json();
      validateMetadata(metadata, installedPack);
      if (metadata.reference.clip !== item.label) throw Error('Video bundle label mismatch');
      const experiment = createLazyHybridDescriptor(metadata,
        signal => loadHybridFixture(url, installedPack, { metadata, signal }),
        fixture => createHybridDescriptor(window.PIXI, renderer, fixture));
      entries.push({ label: item.label, metadata, experiment });
    }
  } else {
    const fixture = await loadHybridFixture(base, installedPack);
    entries.push({ label: fixture.metadata.reference.clip, metadata: fixture.metadata,
      experiment: createHybridDescriptor(window.PIXI, renderer, fixture) });
  }
  const bodyUrls = new Map();
  for (const {label, metadata} of entries) {
    if (sprite._frameUrls[label]?.length !== metadata.frames) throw Error('Installed runtime frame count differs');
    if (sprite._frameIntervals[label] !== metadata.frameIntervalMs) throw Error('Installed runtime frame interval differs');
    for (const url of sprite._frameUrls[label].slice(metadata.headFrames,-1)) bodyUrls.set(url,label);
  }
  const aliases = findVideoAliases(sprite,entries);
  const registrations = [...entries.map(({label,experiment})=>({label,experiment})),
    ...[...aliases].map(([label,target])=>({label,experiment:entries.find(entry=>entry.label===target).experiment}))];
  const loads = { normalBody: 0, requiredBody: 0, explicitHoldBody: 0, fallbackBody: 0 };
  const backend = sprite._frameBackend, originalLoad = backend.load;
  backend.load = function(url, ...args) {
    const canonical = bodyUrls.get(url);
    const label = canonical && aliases.get(sprite._currentEmotion) === canonical ? sprite._currentEmotion : canonical;
    if (label) {
      const state = sprite.getExperimentalFrameVideoStats(label);
      const reason = state.failed ? 'fallbackBody' : sprite._held && sprite._currentEmotion === label ? 'explicitHoldBody'
        : sprite._requiredFrameUrls.get(label)?.includes(url) ? 'requiredBody' : 'normalBody';
      loads[reason]++;
    }
    return originalLoad.call(this,url,...args);
  };
  for (const {label,experiment} of registrations) sprite.setExperimentalFrameVideoSource(label, experiment.descriptor);
  const single = entries.length === 1;
  return {
    label: single ? entries[0].label : null, labels: entries.map(row=>row.label), aliases: Object.fromEntries(aliases),
    snapshot() {
      const clips = Object.fromEntries(entries.map(({label,experiment})=>[label,experiment.snapshot()]));
      if (single) return {label:entries[0].label, aliases:Object.fromEntries(aliases), ...clips[entries[0].label], staticBodyLoads:{...loads},
        renderer:sprite.getExperimentalFrameVideoStats(entries[0].label)};
      const values = Object.values(clips), totals = {};
      for (const value of values) for (const [key,count] of Object.entries(value.completedTotals)) totals[key]=(totals[key]||0)+count;
      return {labels:entries.map(row=>row.label), aliases:Object.fromEntries(aliases), clips, staticBodyLoads:{...loads},
        created:values.reduce((sum,v)=>sum+v.created,0), compressedBytes:values.reduce((sum,v)=>sum+v.compressedBytes,0),
        active:values.flatMap(v=>v.active), completedTotals:totals, recentCompleted:values.flatMap(v=>v.recentCompleted),
        renderer:sprite.getExperimentalFrameVideoStats()};
    },
    failActive() { for (const {experiment} of entries) experiment.failActive(); },
    reinstall() { for (const {label,experiment} of registrations) sprite.setExperimentalFrameVideoSource(label,experiment.descriptor); },
    disable() { for (const {label} of registrations) sprite.setExperimentalFrameVideoSource(label,null); backend.load=originalLoad; },
  };
}
