(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.FrameStore = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  /**
   * One owner for textures keyed by the caller's canonical URL.
   *
   * replaceViews(key, urls) registers aliases, without requesting a load. The
   * onViewChange(key, index, texture|null) callback updates only current views.
   * The caller resets its sparse array before replacement; removed indices
   * never receive callbacks that could extend the replacement array.
   * replaceDemand(owner, [{url, priority, deadline}]) replaces a finite request
   * set; larger numeric priorities win, then earlier deadlines. replacePins
   * replaces an owner's URL set atomically. Pins imply demand and prevent
   * eviction. Empty sets release the owner. get(url) returns a ready texture.
   * Failed loads retry only on renewed owner need: adding a URL or promoting
   * its demand priority. Retained sets and deadline refreshes never retry.
   * Playback clocks, sampling, source indices and graph choices stay outside.
   *
   * backend.load(url, AbortSignal) returns {texture, cpuBytes, gpuBytes} before
   * upload. backend.upload(texture) resolves when ready; backend.destroy owns
   * both texture and retained payload, and backend.now supplies an LRU clock.
   * A texture publishes only after upload. Its CPU data stays available for
   * Pixi's normal context recovery. This store does not own WebGL recovery.
   *
   * budgetBytes charges retained CPU plus potential GPU bytes. Unique pinned
   * allocations establish a floor above that budget. Admission also reserves
   * uploads. At most maxInFlight jobs can hold additional transient payloads;
   * canceled jobs keep their slot until the backend actually finishes. Stats
   * report known decoded CPU bytes and potential GPU bytes during upload, not
   * fetch/worker/WASM allocations or an absolute process-memory limit.
   */
  function createFrameStore(options) {
    const { backend } = options;
    const budgetBytes = Number(options.budgetBytes);
    if (!Number.isFinite(budgetBytes) || budgetBytes < 0) throw new Error("Invalid texture budget");
    const maxInFlight = Math.max(1, Math.min(3, Math.floor(Number(options.maxInFlight) || 2)));
    const onViewChange = options.onViewChange || (() => {});
    const entries = new Map(), views = new Map(), demands = new Map(), pins = new Map();
    const jobs = new Set();
    const counters = { loads: 0, refetches: 0, evictions: 0, failures: 0, pressure: 0 };
    let destroyed = false, pumpScheduled = false, serial = 0;

    function entryFor(url) {
      const key = String(url || "");
      if (!key) return null;
      let entry = entries.get(key);
      if (!entry) {
        entry = { url: key, views: new Set(), texture: null, cost: null, job: null,
          demand: null, pinned: false, failed: false, pressured: false,
          lastUsed: 0, loads: 0, serial: ++serial };
        entries.set(key, entry);
      }
      return entry;
    }

    function wanted(entry) { return entry.pinned || !!entry.demand; }
    function priority(entry) { return entry.pinned ? Infinity : entry.demand?.priority ?? -Infinity; }
    function deadline(entry) { return entry.demand?.deadline ?? Infinity; }
    function costBytes(cost) { return cost.cpuBytes + cost.gpuBytes; }

    function prune(entry) {
      if (!entry.texture && !entry.job && !entry.views.size && !wanted(entry)) entries.delete(entry.url);
    }

    function allocations() {
      let residentCpuBytes = 0, residentGpuBytes = 0, pinnedBytes = 0, reservedBytes = 0;
      for (const entry of entries.values()) {
        if (!entry.texture) continue;
        residentCpuBytes += entry.cost.cpuBytes;
        residentGpuBytes += entry.cost.gpuBytes;
        if (entry.pinned) pinnedBytes += costBytes(entry.cost);
      }
      for (const job of jobs) {
        if (!job.admitted) continue;
        reservedBytes += costBytes(job.result);
        if (job.entry.pinned) pinnedBytes += costBytes(job.result);
      }
      return { residentCpuBytes, residentGpuBytes, pinnedBytes, reservedBytes };
    }

    function victimsFor(entry) {
      return Array.from(entries.values()).filter(other => other.texture && !other.pinned
        && (!entry || priority(other) < priority(entry)))
        .sort((a, b) => priority(a) - priority(b) || a.lastUsed - b.lastUsed || a.serial - b.serial);
    }

    function notifyEntry(entry, texture) {
      for (const view of Array.from(entry.views)) {
        // A callback may replace a view; never publish its superseded references.
        if (entry.views.has(view)) onViewChange(view.key, view.index, texture);
      }
    }

    function evict(entry) {
      const texture = entry.texture;
      entry.texture = null;
      counters.evictions++;
      notifyEntry(entry, null);
      backend.destroy(texture);
      prune(entry);
    }

    function admission(entry, cost, apply) {
      const amounts = allocations();
      const residentBytes = amounts.residentCpuBytes + amounts.residentGpuBytes;
      const limit = Math.max(budgetBytes, amounts.pinnedBytes + (entry.pinned ? costBytes(cost) : 0));
      let need = residentBytes + amounts.reservedBytes + costBytes(cost) - limit;
      const victims = [];
      if (need > 0) {
        for (const victim of victimsFor(entry)) {
          victims.push(victim);
          need -= costBytes(victim.cost);
          if (need <= 0) break;
        }
      }
      if (need > 0) return false;
      if (apply) for (const victim of victims) evict(victim);
      return true;
    }

    function trim() {
      const amounts = allocations();
      let excess = amounts.residentCpuBytes + amounts.residentGpuBytes + amounts.reservedBytes
        - Math.max(budgetBytes, amounts.pinnedBytes);
      if (excess <= 0) return;
      for (const victim of victimsFor(null)) {
        excess -= costBytes(victim.cost);
        evict(victim);
        if (excess <= 0) break;
      }
    }

    function pressure(entry) {
      if (!entry.pressured) { entry.pressured = true; counters.pressure++; }
    }

    function cancel(job) {
      if (job.cancelled) return;
      job.cancelled = true;
      job.controller.abort();
    }

    function reconcile() {
      for (const entry of entries.values()) { entry.demand = null; entry.pinned = false; }
      for (const items of demands.values()) {
        for (const item of items.values()) {
          const entry = entryFor(item.url), old = entry.demand;
          if (!old || item.priority > old.priority
              || (item.priority === old.priority && item.deadline < old.deadline)) entry.demand = item;
        }
      }
      for (const urls of pins.values()) for (const url of urls) entryFor(url).pinned = true;
      for (const entry of entries.values()) {
        if (!wanted(entry)) {
          entry.failed = false;
          entry.pressured = false;
          if (entry.job) cancel(entry.job);
        }
        prune(entry);
      }
      trim();
      schedulePump();
    }

    function schedulePump() {
      if (destroyed || pumpScheduled) return;
      pumpScheduled = true;
      Promise.resolve().then(() => { pumpScheduled = false; if (!destroyed) pump(); });
    }

    function pump() {
      const candidates = Array.from(entries.values())
        .filter(entry => wanted(entry) && !entry.texture && !entry.job && !entry.failed)
        .sort((a, b) => priority(b) - priority(a) || deadline(a) - deadline(b) || a.serial - b.serial);
      for (const entry of candidates) {
        if (jobs.size >= maxInFlight) break;
        if (entry.cost) {
          if (!admission(entry, entry.cost, false)) { pressure(entry); continue; }
        } else if (!entry.pinned) {
          const amounts = allocations();
          const occupied = amounts.residentCpuBytes + amounts.residentGpuBytes + amounts.reservedBytes;
          if (occupied >= Math.max(budgetBytes, amounts.pinnedBytes) && !victimsFor(entry).length) {
            pressure(entry); continue;
          }
        }
        entry.pressured = false;
        start(entry);
      }
    }

    function start(entry) {
      const job = { entry, controller: new AbortController(), cancelled: false, result: null, admitted: false };
      entry.job = job;
      jobs.add(job);
      if (entry.loads++) counters.refetches++;
      counters.loads++;
      const live = () => !destroyed && !job.cancelled && wanted(entry);
      void (async () => {
        try {
          job.result = await backend.load(entry.url, job.controller.signal);
          const result = job.result;
          if (!result || !result.texture || !Number.isFinite(result.cpuBytes) || result.cpuBytes < 0
              || !Number.isFinite(result.gpuBytes) || result.gpuBytes < 0) throw new Error("Invalid texture load result");
          entry.cost = { cpuBytes: result.cpuBytes, gpuBytes: result.gpuBytes };
          if (!live()) return;
          if (!admission(entry, result, true)) { pressure(entry); return; }
          job.admitted = true;
          await backend.upload(result.texture);
          // Pins/demand may have changed while upload was pending. Recheck the
          // floor and admission before a completed resource becomes visible.
          job.admitted = false;
          if (!live()) return;
          if (!admission(entry, result, true)) { pressure(entry); return; }
          entry.texture = result.texture;
          entry.lastUsed = backend.now();
          job.result = null;
          notifyEntry(entry, entry.texture);
        } catch (_) {
          if (live()) { entry.failed = true; counters.failures++; }
        } finally {
          job.admitted = false;
          if (job.result?.texture) backend.destroy(job.result.texture);
          entry.job = null;
          jobs.delete(job);
          prune(entry);
          if (!destroyed) { trim(); schedulePump(); }
        }
      })();
    }

    function replaceViews(key, urls) {
      if (destroyed) return;
      const previous = views.get(key) || [];
      const next = Array.from(urls || [], (url, index) => {
        const entry = entryFor(url);
        return entry ? { key, index, entry } : null;
      });
      for (const view of previous) if (view) view.entry.views.delete(view);
      for (const view of next) if (view) view.entry.views.add(view);
      if (next.length) views.set(key, next); else views.delete(key);
      for (const view of previous) if (view) prune(view.entry);
      for (let index = 0; index < next.length; index++) {
        if (views.get(key) !== next) break;
        onViewChange(key, index, next[index]?.entry.texture || null);
      }
    }

    function replaceDemand(owner, items) {
      if (destroyed) return;
      const next = new Map();
      for (const item of items || []) {
        const entry = entryFor(item.url);
        if (!entry) continue;
        const candidate = { url: entry.url, priority: Number(item.priority) || 0,
          deadline: Number.isFinite(item.deadline) ? item.deadline : Infinity };
        const old = next.get(entry.url);
        if (!old || candidate.priority > old.priority
            || (candidate.priority === old.priority && candidate.deadline < old.deadline)) next.set(entry.url, candidate);
      }
      const previous = demands.get(owner);
      if ((previous?.size || 0) === next.size && Array.from(next.values()).every(item => {
        const old = previous.get(item.url);
        return old && old.priority === item.priority && old.deadline === item.deadline;
      })) return;
      for (const item of next.values()) {
        const old = previous?.get(item.url);
        if (!old || item.priority > old.priority) entries.get(item.url).failed = false;
      }
      if (next.size) demands.set(owner, next); else demands.delete(owner);
      reconcile();
    }

    function replacePins(owner, urls) {
      if (destroyed) return;
      const next = new Set();
      for (const url of urls || []) { const entry = entryFor(url); if (entry) next.add(entry.url); }
      const previous = pins.get(owner);
      if ((previous?.size || 0) === next.size && Array.from(next).every(url => previous.has(url))) return;
      for (const url of next) if (!previous?.has(url)) entries.get(url).failed = false;
      if (next.size) pins.set(owner, next); else pins.delete(owner);
      reconcile();
    }

    function get(url) {
      const entry = entries.get(String(url || ""));
      if (!entry?.texture) return null;
      entry.lastUsed = backend.now();
      return entry.texture;
    }

    function stats() {
      const amounts = allocations();
      let residentFrames = 0, queued = 0, transientBytes = 0;
      for (const entry of entries.values()) {
        if (entry.texture) residentFrames++;
        else if (wanted(entry) && !entry.job && !entry.failed) queued++;
      }
      for (const job of jobs) if (job.result) {
        transientBytes += job.result.cpuBytes || 0;
        if (job.admitted) transientBytes += job.result.gpuBytes || 0;
      }
      const { residentCpuBytes, residentGpuBytes, pinnedBytes } = amounts;
      return { residentCpuBytes, residentGpuBytes, residentBytes: residentCpuBytes + residentGpuBytes,
        budgetBytes, pinnedBytes, pinnedOverageBytes: Math.max(0, pinnedBytes - budgetBytes),
        residentFrames, transientBytes, maxInFlight, entries: entries.size, queued, inFlight: jobs.size, ...counters };
    }

    function destroy() {
      if (destroyed) return;
      destroyed = true;
      for (const job of jobs) cancel(job);
      for (const entry of entries.values()) {
        if (!entry.texture) continue;
        const texture = entry.texture;
        entry.texture = null;
        notifyEntry(entry, null);
        backend.destroy(texture);
      }
      views.clear(); demands.clear(); pins.clear();
      for (const entry of entries.values()) {
        entry.views.clear(); entry.demand = null; entry.pinned = false; prune(entry);
      }
    }

    return { replaceViews, replaceDemand, replacePins, get, stats, destroy };
  }

  return { createFrameStore };
});
