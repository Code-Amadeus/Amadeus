const assert = require('node:assert/strict');
const test = require('node:test');
const { createFrameStore } = require('../render/web/frame_store.js');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject, done: false };
}

const flush = () => new Promise(resolve => setImmediate(resolve));
const demand = (url, priority = 10, deadline = 0) => ({ url, priority, deadline });

function fixture({ budgetBytes = 20, maxInFlight = 2, autoUpload = true } = {}) {
  let time = 0, textureId = 0;
  const loads = [], uploads = [], destroyed = [], changes = [];
  const backend = {
    now: () => time,
    load(url, signal) {
      const pending = deferred();
      loads.push({ url, signal, pending });
      return pending.promise;
    },
    upload(texture) {
      const pending = deferred();
      uploads.push({ texture, pending });
      if (autoUpload) { pending.done = true; pending.resolve(); }
      return pending.promise;
    },
    destroy(texture) {
      assert.equal(texture.destroyed, false, 'each owned texture is destroyed once');
      texture.destroyed = true;
      texture.payload = null;
      destroyed.push(texture);
    },
  };
  const store = createFrameStore({ backend, budgetBytes, maxInFlight,
    onViewChange: (key, index, texture) => changes.push({ key, index, texture }) });
  function finishLoad(url, cpuBytes = 4, gpuBytes = 6) {
    const call = loads.find(call => call.url === url && !call.pending.done);
    assert.ok(call, `pending load for ${url}`);
    call.pending.done = true;
    const texture = { url, id: ++textureId, destroyed: false, payload: new Uint8Array(cpuBytes) };
    call.pending.resolve({ texture, cpuBytes, gpuBytes });
    return texture;
  }
  function finishUpload(url) {
    const call = uploads.find(call => call.texture.url === url && !call.pending.done);
    assert.ok(call, `pending upload for ${url}`);
    call.pending.done = true;
    call.pending.resolve();
  }
  async function complete(url, cpuBytes = 4, gpuBytes = 6) {
    const texture = finishLoad(url, cpuBytes, gpuBytes);
    await flush();
    return texture;
  }
  return { store, loads, uploads, destroyed, changes, finishLoad, finishUpload, complete,
    setTime: value => { time = value; } };
}

test('shared URLs load once, publish all live aliases, and charge their payload once', async () => {
  const f = fixture();
  f.store.replaceViews('normal', ['shared']);
  f.store.replaceViews('happy', ['shared']);
  f.store.replaceDemand('current', [demand('shared')]);
  f.store.replaceDemand('prefetch', [demand('shared', 5)]);
  f.store.replacePins('poster-a', ['shared', 'shared']);
  f.store.replacePins('poster-b', ['shared']);
  await flush();
  assert.equal(f.loads.length, 1);
  const texture = await f.complete('shared');
  assert.equal(f.store.get('shared'), texture);
  assert.deepEqual(f.changes.filter(change => change.texture).map(change => [change.key, change.index]),
    [['normal', 0], ['happy', 0]]);
  const stats = f.store.stats();
  assert.equal(stats.residentCpuBytes, 4);
  assert.equal(stats.residentGpuBytes, 6);
  assert.equal(stats.residentBytes, 10);
  assert.equal(stats.pinnedBytes, 10);
  assert.equal(stats.residentFrames, 1);
  assert.equal(stats.inFlight, 0);
  assert.equal(stats.transientBytes, 0);
  assert.equal(texture.payload.byteLength, 4, 'retained CPU payload remains available after upload');
  f.store.replaceViews('smile', ['shared']);
  assert.equal(f.changes.at(-1).texture, texture, 'new alias receives the existing resident synchronously');
  f.changes.length = 0;
  f.store.replaceViews('smile', ['shared']);
  assert.equal(f.changes.at(-1).texture, texture, 'replacing an identical view repopulates a reset caller array');
  f.store.replacePins('poster-a', []);
  assert.equal(f.store.stats().pinnedBytes, 10, 'another owner still pins the shared allocation');
  f.store.destroy();
  assert.deepEqual(f.destroyed, [texture]);
});

test('stronger current demand precedes hints, with earlier deadlines breaking equal-priority ties', async () => {
  const f = fixture({ budgetBytes: 40, maxInFlight: 1 });
  f.store.replaceDemand('warm', [demand('warm', 65, 0), demand('shared', 65, 0)]);
  f.store.replaceDemand('current', [demand('later', 100, 100), demand('shared', 100, 5)]);
  await flush();
  assert.deepEqual(f.loads.map(call => call.url), ['shared']);
  await f.complete('shared');
  assert.deepEqual(f.loads.map(call => call.url), ['shared', 'later']);
  await f.complete('later');
  assert.deepEqual(f.loads.map(call => call.url), ['shared', 'later', 'warm']);
  await f.complete('warm');
  assert.equal(f.loads.filter(call => call.url === 'shared').length, 1);
  f.store.destroy();
});

test('cold pinned required frames all complete and establish an explicit unique-byte floor', async () => {
  const f = fixture({ budgetBytes: 12 });
  f.store.replaceViews('required', ['poster', 'closed', 'hold']);
  f.store.replacePins('required', ['poster', 'closed', 'hold']);
  await flush();
  assert.equal(f.loads.length, 2);
  assert.equal(f.store.stats().inFlight, 2);
  await f.complete('poster', 5, 5);
  assert.equal(f.loads.length, 3);
  await f.complete('closed', 5, 5);
  await f.complete('hold', 5, 5);
  assert.equal(f.changes.filter(change => change.texture).length, 3);
  assert.equal(f.store.stats().residentBytes, 30);
  assert.equal(f.store.stats().pinnedBytes, 30);
  assert.equal(f.store.stats().pinnedOverageBytes, 18);
  f.store.replacePins('required', []);
  assert.equal(f.store.stats().residentBytes, 10, 'releasing the floor restores the ordinary budget');
  f.store.destroy();
});

test('upload reservations count against admission and a texture is invisible until upload completes', async () => {
  const f = fixture({ budgetBytes: 10, autoUpload: false });
  f.store.replaceViews('frames', ['a', 'b']);
  f.store.replaceDemand('current', [demand('a'), demand('b')]);
  await flush();
  const a = f.finishLoad('a');
  await flush();
  assert.equal(f.store.get('a'), null);
  assert.equal(f.store.stats().residentBytes, 0);
  assert.equal(f.store.stats().transientBytes, 10);
  const b = f.finishLoad('b');
  await flush();
  assert.equal(b.destroyed, true, 'a pending upload reserves the only resident allocation');
  assert.equal(f.uploads.length, 1);
  assert.equal(f.changes.some(change => change.texture), false);
  f.finishUpload('a');
  await flush();
  assert.equal(f.store.get('a'), a);
  assert.equal(f.store.stats().residentBytes, 10);
  assert.equal(f.store.stats().transientBytes, 0);
  assert.equal(f.store.stats().queued, 1);
  f.store.destroy();
});

test('canceled non-abortable work retains its real concurrency slot until completion', async () => {
  const f = fixture({ maxInFlight: 2 });
  f.store.replaceViews('frames', ['a', 'b', 'c']);
  f.store.replaceDemand('current', [demand('a'), demand('b')]);
  await flush();
  f.store.replaceDemand('current', [demand('c')]);
  await flush();
  assert.ok(f.loads.every(call => call.signal.aborted));
  assert.equal(f.store.stats().inFlight, 2);
  assert.equal(f.loads.length, 2, 'replacement cannot bypass still-running canceled workers');
  const a = await f.complete('a');
  assert.equal(a.destroyed, true);
  assert.equal(f.loads.length, 3);
  assert.equal(f.loads[2].url, 'c');
  assert.equal(f.store.stats().inFlight, 2);
  const b = await f.complete('b');
  assert.equal(b.destroyed, true);
  const c = await f.complete('c');
  assert.deepEqual(f.changes.filter(change => change.texture).map(change => change.texture), [c]);
  assert.equal(f.store.stats().maxInFlight, 2);
  f.store.destroy();
});

test('a replaced view cannot receive the old load, even when its URL remains requested elsewhere', async () => {
  const f = fixture();
  f.store.replaceViews('frames', ['a']);
  f.store.replaceDemand('current', [demand('a')]);
  await flush();
  f.store.replaceViews('frames', ['b', 'a']);
  const a = await f.complete('a');
  assert.deepEqual(f.changes.filter(change => change.texture).map(change => [change.key, change.index, change.texture]),
    [['frames', 1, a]]);
  const changeCount = f.changes.length;
  f.store.replaceViews('frames', []);
  assert.equal(f.changes.length, changeCount, 'removed indices never target a replacement array');
  assert.equal(f.store.get('a'), a, 'removing a view does not remove independent demand ownership');
  f.store.destroy();
});

test('same-URL and shorter view replacements repopulate only the current sparse array', async () => {
  const f = fixture();
  f.store.replaceViews('mouth', ['a', 'unused']);
  f.store.replacePins('mouth', ['a']);
  await flush();
  const a = await f.complete('a');
  assert.equal(f.store.stats().entries, 2);
  for (let attempt = 0; attempt < 2; attempt++) {
    const replacement = new Array(1);
    f.changes.length = 0;
    f.store.replaceViews('mouth', ['a']);
    for (const change of f.changes) replacement[change.index] = change.texture;
    assert.deepEqual(replacement, [a]);
    assert.equal(replacement.length, 1, 'removed indices do not extend the authored source array');
  }
  assert.equal(f.store.stats().entries, 1, 'unused metadata from the longer view is pruned');
  f.store.destroy();
});

test('unused cold URL metadata is pruned across repeated view and demand replacements', async () => {
  const f = fixture();
  for (let i = 0; i < 100; i++) {
    f.store.replaceViews('frames', [`pack-${i}-a`, `pack-${i}-b`]);
    assert.equal(f.store.stats().entries, 2);
  }
  f.store.replaceViews('frames', []);
  assert.equal(f.store.stats().entries, 0);
  // Replacements in the same turn cancel queue ownership before any load starts.
  for (let i = 0; i < 100; i++) f.store.replaceDemand('hint', [demand(`hint-${i}`)]);
  assert.equal(f.store.stats().entries, 1);
  f.store.replaceDemand('hint', []);
  await flush();
  assert.equal(f.store.stats().entries, 0);
  assert.equal(f.loads.length, 0);
  f.store.destroy();
});

test('withdrawn metadata remains while a canceled backend owns it, then is pruned after completion', async () => {
  const f = fixture({ maxInFlight: 1 });
  f.store.replaceViews('frames', ['a']);
  f.store.replaceDemand('current', [demand('a')]);
  await flush();
  f.store.replaceViews('frames', []);
  f.store.replaceDemand('current', []);
  assert.equal(f.store.stats().entries, 1, 'pending cancellation still owns its URL entry');
  const stale = await f.complete('a');
  assert.equal(stale.destroyed, true);
  assert.equal(f.store.stats().entries, 0);
  assert.equal(f.store.stats().inFlight, 0);
  f.store.destroy();
});

test('withdrawn and then reissued demand discards the canceled generation before reloading', async () => {
  const f = fixture({ maxInFlight: 1 });
  f.store.replaceViews('frames', ['a']);
  f.store.replaceDemand('current', [demand('a')]);
  await flush();
  f.store.replaceDemand('current', []);
  f.store.replaceDemand('current', [demand('a')]);
  await flush();
  assert.equal(f.loads.length, 1);
  const stale = await f.complete('a');
  assert.equal(stale.destroyed, true);
  assert.equal(f.loads.length, 2);
  assert.equal(f.changes.some(change => change.texture), false);
  const current = await f.complete('a');
  assert.equal(f.store.get('a'), current);
  assert.equal(f.store.stats().refetches, 1);
  f.store.destroy();
});

test('a full equal-priority horizon stays queued without endlessly evicting and refilling', async () => {
  const f = fixture({ budgetBytes: 20, maxInFlight: 1 });
  const horizon = [demand('a'), demand('b'), demand('c')];
  f.store.replaceDemand('current', horizon);
  await flush();
  await f.complete('a');
  await f.complete('b');
  assert.equal(f.loads.length, 2, 'no speculative decode when equal-priority allocations fill the budget');
  for (let i = 0; i < 20; i++) { f.store.replaceDemand('current', horizon); await flush(); }
  assert.equal(f.loads.length, 2);
  assert.equal(f.store.stats().evictions, 0);
  assert.equal(f.store.stats().queued, 1);
  assert.equal(f.store.stats().pressure, 1);
  f.store.replaceDemand('current', [demand('b'), demand('c')]);
  await flush();
  assert.equal(f.loads.at(-1).url, 'c');
  await f.complete('c');
  assert.equal(f.store.get('a'), null);
  assert.ok(f.store.get('b') && f.store.get('c'));
  assert.equal(f.store.stats().residentBytes, 20);
  assert.equal(f.store.stats().evictions, 1);
  f.store.destroy();
});

test('a decoded unadmittable cost is remembered and capacity changes rescan the blocked job', async () => {
  const f = fixture({ budgetBytes: 15, maxInFlight: 1 });
  f.store.replaceDemand('current', [demand('a', 50)]);
  await flush();
  await f.complete('a');
  f.store.replaceDemand('current', [demand('a', 50), demand('b', 50)]);
  await flush();
  const blocked = await f.complete('b');
  assert.equal(blocked.destroyed, true);
  assert.equal(f.store.stats().residentBytes, 10);
  assert.equal(f.store.stats().transientBytes, 0);
  for (let i = 0; i < 10; i++) {
    f.store.replaceDemand('current', [demand('a', 50), demand('b', 50, i)]);
    await flush();
  }
  assert.equal(f.loads.length, 2, 'known oversized admission does not decode again on deadline updates');
  f.store.replaceDemand('current', [demand('b', 50)]);
  await flush();
  assert.equal(f.loads.length, 3);
  const resident = await f.complete('b');
  assert.equal(f.store.get('b'), resident);
  assert.equal(f.store.get('a'), null);
  assert.equal(f.store.stats().refetches, 1);
  f.store.destroy();
});

test('a capacity-blocked high-priority job does not stop a smaller admissible request behind it', async () => {
  const f = fixture({ budgetBytes: 10, maxInFlight: 1 });
  f.store.replaceDemand('current', [demand('too-large', 100), demand('small', 90)]);
  await flush();
  const tooLarge = await f.complete('too-large', 8, 7);
  assert.equal(tooLarge.destroyed, true);
  assert.equal(f.loads.at(-1).url, 'small');
  await f.complete('small', 2, 3);
  assert.equal(f.store.stats().residentBytes, 5);
  assert.equal(f.store.stats().queued, 1);
  for (let i = 0; i < 5; i++) {
    f.store.replaceDemand('current', [demand('too-large', 100, i), demand('small', 90)]);
    await flush();
  }
  assert.equal(f.loads.length, 2);
  assert.ok(f.store.get('small'));
  assert.equal(f.store.stats().evictions, 0, 'insufficient victims do not cause partial destructive admission');
  f.store.destroy();
});

test('lowest demand priority wins eviction selection, followed by actual use time', async () => {
  const f = fixture({ budgetBytes: 30, maxInFlight: 3 });
  const first = [demand('a', 10), demand('b', 10), demand('c', 20)];
  f.store.replaceDemand('initial', first);
  await flush();
  await f.complete('a'); await f.complete('b'); await f.complete('c');
  f.setTime(10); f.store.get('b');
  f.store.replaceDemand('current', [demand('d', 30)]);
  await flush();
  await f.complete('d');
  assert.equal(f.store.get('a'), null, 'the older weak frame is evicted');
  assert.ok(f.store.get('b'));
  assert.ok(f.store.get('c'), 'stronger demand survives even when equally old');
  assert.equal(f.store.stats().residentBytes, 30);
  assert.equal(f.store.stats().evictions, 1);
  f.store.destroy();
});

test('the actual display stays pinned while a cold hold loads, then releases atomically on handoff', async () => {
  const f = fixture({ budgetBytes: 10, maxInFlight: 1 });
  f.store.replacePins('display', ['previous']);
  await flush();
  const previous = await f.complete('previous');
  f.store.replacePins('hold', ['target']);
  await flush();
  assert.equal(f.store.get('previous'), previous);
  assert.equal(previous.destroyed, false);
  const target = await f.complete('target');
  assert.equal(f.store.stats().residentBytes, 20);
  f.store.replacePins('display', ['target']);
  assert.equal(target.destroyed, false);
  assert.equal(previous.destroyed, true);
  f.store.replacePins('hold', []);
  assert.equal(f.store.get('target'), target);
  assert.equal(f.store.stats().residentBytes, 10);
  assert.equal(f.store.stats().pinnedBytes, 10);
  f.store.destroy();
});

test('unpinning an oversized resource during upload rechecks admission before publication', async () => {
  const f = fixture({ budgetBytes: 10, autoUpload: false, maxInFlight: 1 });
  f.store.replaceViews('frames', ['large']);
  f.store.replaceDemand('current', [demand('large')]);
  f.store.replacePins('hold', ['large']);
  await flush();
  const large = f.finishLoad('large', 8, 12);
  await flush();
  assert.equal(f.store.stats().pinnedBytes, 20);
  assert.equal(f.store.stats().pinnedOverageBytes, 10);
  f.store.replacePins('hold', []);
  assert.equal(f.store.stats().transientBytes, 20);
  f.finishUpload('large');
  await flush();
  assert.equal(large.destroyed, true);
  assert.equal(f.store.get('large'), null);
  assert.equal(f.changes.some(change => change.texture), false);
  assert.equal(f.store.stats().residentBytes, 0);
  assert.equal(f.store.stats().failures, 0);
  assert.equal(f.loads.length, 1);
  f.store.destroy();
});

test('canceling an upload waits for backend completion before destroying it or starting replacement work', async () => {
  const f = fixture({ autoUpload: false, maxInFlight: 1 });
  f.store.replaceDemand('current', [demand('a')]);
  await flush();
  const a = f.finishLoad('a');
  await flush();
  f.store.replaceDemand('current', [demand('b')]);
  await flush();
  assert.equal(a.destroyed, false, 'backend still owns a pending upload operation');
  assert.equal(f.store.stats().inFlight, 1);
  assert.equal(f.store.stats().transientBytes, 10);
  assert.equal(f.loads.length, 1);
  f.finishUpload('a');
  await flush();
  assert.equal(a.destroyed, true);
  assert.equal(f.loads.length, 2);
  assert.equal(f.store.get('a'), null);
  f.finishLoad('b'); await flush(); f.finishUpload('b'); await flush();
  f.store.destroy();
});

test('releasing a pinned floor rescans ordinary demand that was blocked by the floor', async () => {
  const f = fixture({ budgetBytes: 10, maxInFlight: 1 });
  f.store.replacePins('poster', ['large']);
  await flush();
  const large = await f.complete('large', 8, 12);
  f.store.replaceDemand('prefetch', [demand('next')]);
  await flush();
  assert.equal(f.loads.length, 1);
  assert.equal(f.store.stats().queued, 1);
  f.store.replacePins('poster', []);
  await flush();
  assert.equal(large.destroyed, true);
  assert.equal(f.loads.at(-1).url, 'next');
  await f.complete('next');
  assert.equal(f.store.stats().residentBytes, 10);
  f.store.destroy();
});

test('failed work is observable without a silent retry loop and can be requested again after withdrawal', async () => {
  const f = fixture({ maxInFlight: 1 });
  f.store.replaceDemand('current', [demand('a')]);
  await flush();
  f.loads[0].pending.done = true;
  f.loads[0].pending.reject(new Error('synthetic decode failure'));
  await flush();
  assert.equal(f.store.stats().failures, 1);
  assert.equal(f.store.stats().inFlight, 0);
  for (let i = 0; i < 10; i++) { f.store.replaceDemand('current', [demand('a', 10, i)]); await flush(); }
  assert.equal(f.loads.length, 1);
  f.store.replaceDemand('current', []);
  f.store.replaceDemand('current', [demand('a')]);
  await flush();
  assert.equal(f.loads.length, 2);
  await f.complete('a');
  assert.ok(f.store.get('a'));
  assert.equal(f.store.stats().failures, 1);
  f.store.destroy();
});

async function failLoad(f, url) {
  const call = f.loads.find(call => call.url === url && !call.pending.done);
  assert.ok(call, `pending load for ${url}`);
  call.pending.done = true;
  call.pending.reject(new Error('synthetic transient load failure'));
  await flush();
}

test('a new current demand retries a failed globally pinned poster once', async () => {
  const f = fixture({ maxInFlight: 1 });
  f.store.replacePins('required', ['poster']);
  await flush();
  await failLoad(f, 'poster');
  for (let i = 0; i < 5; i++) { f.store.replacePins('required', ['poster']); await flush(); }
  assert.equal(f.loads.length, 1, 'retaining the required pin does not silently retry');
  f.store.replaceDemand('current', [demand('poster', 100)]);
  await flush();
  assert.equal(f.loads.length, 2, 'a newly introduced current request renews the failed need');
  const poster = await f.complete('poster');
  assert.equal(f.store.get('poster'), poster);
  assert.equal(f.store.stats().failures, 1);
  assert.equal(f.store.stats().refetches, 1);
  assert.equal(f.store.stats().pinnedBytes, 10);
  f.store.destroy();
});

test('deadline refreshes retain failure while leaving and returning current renews a globally pinned URL', async () => {
  const f = fixture({ maxInFlight: 1 });
  f.store.replacePins('required', ['poster']);
  f.store.replaceDemand('current', [demand('poster', 100)]);
  await flush();
  await failLoad(f, 'poster');
  for (let i = 0; i < 10; i++) {
    f.store.replaceDemand('current', [demand('poster', 100, i)]);
    await flush();
  }
  assert.equal(f.loads.length, 1, 'identical need and deadline refreshes cannot create a retry loop');
  f.store.replaceDemand('current', []);
  await flush();
  assert.equal(f.loads.length, 1, 'the globally retained pin alone does not renew the request');
  f.store.replaceDemand('current', [demand('poster', 100)]);
  await flush();
  assert.equal(f.loads.length, 2);
  const poster = await f.complete('poster');
  assert.equal(f.store.get('poster'), poster);
  assert.equal(f.store.stats().pinnedBytes, 10);
  f.store.destroy();
});

test('an explicit priority promotion renews failure, while demotion does not', async () => {
  const f = fixture({ maxInFlight: 1 });
  f.store.replacePins('required', ['poster']);
  f.store.replaceDemand('current', [demand('poster', 65)]);
  await flush();
  await failLoad(f, 'poster');
  f.store.replaceDemand('current', [demand('poster', 25)]);
  await flush();
  assert.equal(f.loads.length, 1);
  f.store.replaceDemand('current', [demand('poster', 90)]);
  await flush();
  assert.equal(f.loads.length, 2);
  await f.complete('poster');
  assert.ok(f.store.get('poster'));
  f.store.destroy();
});

test('a newly introduced hold pin renews a failed poster without withdrawing its required pin', async () => {
  const f = fixture({ maxInFlight: 1 });
  f.store.replacePins('required', ['poster']);
  await flush();
  await failLoad(f, 'poster');
  f.store.replacePins('hold', ['poster']);
  await flush();
  assert.equal(f.loads.length, 2);
  await f.complete('poster');
  assert.ok(f.store.get('poster'));
  assert.equal(f.store.stats().pinnedBytes, 10, 'renewed ownership does not double-charge the URL');
  f.store.destroy();
});

test('upload failure destroys its decoded resource and never publishes it', async () => {
  const f = fixture({ autoUpload: false });
  f.store.replaceViews('frames', ['a']);
  f.store.replacePins('display', ['a']);
  await flush();
  const a = f.finishLoad('a');
  await flush();
  f.uploads[0].pending.done = true;
  f.uploads[0].pending.reject(new Error('synthetic upload failure'));
  await flush();
  assert.equal(a.destroyed, true);
  assert.equal(f.store.stats().failures, 1);
  assert.equal(f.store.stats().residentBytes, 0);
  assert.equal(f.store.stats().transientBytes, 0);
  assert.equal(f.changes.some(change => change.texture), false);
  f.store.destroy();
});

test('destroy clears resident views and owns late backend results without new work', async () => {
  const f = fixture({ maxInFlight: 2 });
  f.store.replaceViews('frames', ['resident', 'pending']);
  f.store.replaceDemand('current', [demand('resident'), demand('pending')]);
  await flush();
  const resident = await f.complete('resident');
  f.store.destroy();
  f.store.destroy();
  assert.equal(resident.destroyed, true);
  assert.equal(f.store.stats().residentBytes, 0);
  assert.equal(f.store.stats().inFlight, 1);
  assert.equal(f.loads[1].signal.aborted, true);
  const pending = await f.complete('pending');
  assert.equal(pending.destroyed, true);
  assert.equal(f.store.stats().inFlight, 0);
  assert.equal(f.store.get('resident'), null);
  assert.equal(f.store.get('pending'), null);
  assert.deepEqual(f.changes.filter(change => change.texture).map(change => change.texture), [resident]);
  f.store.replacePins('stale-callback', ['another']);
  await flush();
  assert.equal(f.loads.length, 2);
  assert.equal(f.destroyed.length, 2);
});
