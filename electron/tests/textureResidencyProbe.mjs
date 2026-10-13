// Diagnostic only: observe existing public store/backend boundaries without
// changing requests, priorities, pin ownership, clock or display decisions.
export function installResidencyProbe(limit = 100000, labels = []) {
  const selected = new Set(labels);
  const sprite = window.renderApp._sprite, store = sprite._frameStore, backend = sprite._frameBackend;
  const tracked = new Map(), textures = new WeakMap(), owners = new Map(), restores = [], rows = [];
  let dropped = 0, stopped = false;
  const emit = (kind, fields = {}) => {
    if (stopped) return;
    if (rows.length >= limit) { dropped++; return; }
    rows.push({ kind, t: performance.now(), current: sprite._currentEmotion, ...fields });
  };
  for (const [label, urls] of Object.entries(sprite._frameUrls)) {
    if (sprite._clipConfigs[label]?.loopMode !== 'once_then_hold' && !selected.has(label)) continue;
    urls.forEach((url, index) => {
      tracked.set(url, { label, index });
      const texture = sprite._frames[label]?.[index];
      if (texture) textures.set(texture, url);
      emit('initial', { label, index, ready: !!texture });
    });
  }
  const identity = url => tracked.get(url);
  const wrap = (object, name, factory) => {
    const original = object[name];
    object[name] = factory(original);
    restores.push(() => { object[name] = original; });
  };
  // Seed the observer from existing hints; do not resubmit them to FrameStore.
  for (const [owner, hint] of sprite._prefetchHints) owners.set(owner, new Map(
    (hint.requests || []).filter(item => tracked.has(item.url)).map(item =>
      [item.url, { priority: item.priority, load: hint.load !== false }])));
  for (const [owner, entries] of owners) for (const [url, item] of entries)
    emit('initial-demand', { owner, ...identity(url), ...item });
  wrap(store, 'replaceDemand', original => function(owner, items, options = {}) {
    const next = new Map((items || []).filter(item => tracked.has(item.url)).map(item =>
      [item.url, { priority: item.priority, load: options.load !== false }]));
    const previous = owners.get(owner) || new Map();
    for (const [url, item] of next) {
      const old = previous.get(url);
      if (!old || old.priority !== item.priority || old.load !== item.load)
        emit('demand', { owner, ...identity(url), ...item });
    }
    for (const url of previous.keys()) if (!next.has(url)) emit('demand-remove', { owner, ...identity(url) });
    owners.set(owner, next);
    return original.call(this, owner, items, options);
  });
  wrap(backend, 'load', original => function(url, signal) {
    if (!tracked.has(url)) return original.call(this, url, signal);
    const id = identity(url), started = performance.now();
    emit('load-start', id);
    const aborted = () => emit('load-abort', id);
    signal?.addEventListener('abort', aborted, { once: true });
    let result;
    try { result = original.call(this, url, signal); }
    catch (error) { signal?.removeEventListener('abort', aborted); emit('load-error', id); throw error; }
    return Promise.resolve(result).then(value => {
      textures.set(value.texture, url);
      emit('load-end', { ...id, ms: performance.now() - started });
      return value;
    }, error => { emit('load-error', { ...id, error: String(error) }); throw error; })
      .finally(() => signal?.removeEventListener('abort', aborted));
  });
  wrap(backend, 'upload', original => function(texture) {
    const id = identity(textures.get(texture));
    if (!id) return original.call(this, texture);
    const started = performance.now();
    emit('upload-start', id);
    return Promise.resolve(original.call(this, texture)).then(value => {
      emit('upload-end', { ...id, ms: performance.now() - started }); return value;
    });
  });
  wrap(backend, 'destroy', original => function(texture) {
    const id = identity(textures.get(texture));
    if (id) emit('destroy', id);
    return original.call(this, texture);
  });
  wrap(sprite, '_onTextureViewChange', original => function(view, index, texture) {
    const label = view.startsWith('frames:') ? view.slice(7) : null;
    const url = sprite._frameUrls[label]?.[index];
    if (tracked.has(url)) emit(texture ? 'publish' : 'unpublish', identity(url));
    return original.call(this, view, index, texture);
  });
  wrap(sprite, '_showFrame', original => function(index) {
    const label = this._currentEmotion, before = this._displayMisses;
    const value = original.call(this, index);
    if (this._displayMisses > before) {
      const selected = this._sampleFrameIndex(label, index, this._held ? null : this._sampleTimeMs);
      const url = this._frameUrls[label]?.[selected];
      if (tracked.has(url)) emit('miss', { ...identity(url), requested: index,
        owners: [...owners].filter(([, entries]) => entries.has(url)).map(([owner, entries]) =>
          ({ owner, ...entries.get(url) })), store: store.stats() });
    }
    return value;
  });
  window.__residencyProbe = {
    stop() { stopped = true; restores.reverse().forEach(restore => restore()); return { rows, dropped }; },
  };
}
