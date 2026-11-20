// Diagnostic only: simulated availability of one already-routed pending tag.
// Playback still receives the identical public trigger/speaking/release inputs.
export function createTagJourney(options) {
  const actual = [];
  const rotations = [
    ['trans_smile', 'angry_trans', 'sad_trans', 'surprise_trans', 'shy_trans'],
    ['sad_trans', 'trans_smile', 'thinking_trans', 'angry_trans', 'surprise_trans'],
    ['shy_trans', 'thinking_trans', 'trans_smile', 'sad_trans', 'angry_trans'],
  ];
  for (let at = 0, lap = 0; at < options.durationSeconds; at += 30, lap++) {
    const labels = rotations[lap % rotations.length];
    const rows = [
      [0, 'idle'], [3, 'speaking', true], [4, 'trigger', labels[0]],
      [8, 'trigger', labels[0]], [9, 'speaking', false],
      [9.05, 'idle', { presentation_handoff: 'after_speech' }], [9.2, 'trigger', labels[1]],
      [12, 'speaking', true], [12.1, 'trigger', labels[2]],
      [12.2, 'trigger', labels[3]], [12.3, 'trigger', labels[4]],
      [16, 'speaking', false], [16.05, 'idle', { presentation_handoff: 'after_speech' }],
      [20, 'trigger', 'idle_closed_eye'], [27, 'idle'],
    ];
    for (const [offset, command, value] of rows) {
      const when = Math.round((at + offset) * 1000) / 1000;
      if (when >= options.durationSeconds) continue;
      actual.push({ at: when, phase: `tags-${lap}-${offset}-${command}`, command,
        ...(command === 'trigger' ? { label: value } : command === 'speaking' ? { active: value }
          : value ? { options: value } : {}) });
    }
  }
  return addPreviews(actual, options);
}

// Stress every canonical graph label in both silent and speaking contexts.
// These remain public tags; the runtime chooses the actual entry and successors.
export function createAllClipTagJourney(labels, options) {
  const clips = [...new Set(labels)].sort();
  if (!clips.length) throw Error('The runtime has no clip labels');
  const lapSeconds = clips.length * 2 + 4;
  if (options.durationSeconds < 3 + lapSeconds * 2) throw Error('All-clip tags need two complete speech-state laps');
  const actual = [{ at: 0, phase: 'all-tags-startup', command: 'idle' }];
  for (let lap = 0, base = 3; base < options.durationSeconds; lap++, base += lapSeconds) {
    const ordered = lap % 2 ? [...clips].reverse() : clips;
    const rows = [
      { at: base - 0.2, phase: `all-tags-${lap}-reset`, command: 'idle' },
      { at: base - 0.1, phase: `all-tags-${lap}-speech`, command: 'speaking', active: Boolean(lap % 2) },
      ...ordered.map((label, index) => ({ at: base + index * 2, phase: `all-tags-${lap}-${label}`, command: 'trigger', label })),
      { at: base + clips.length * 2, phase: `all-tags-${lap}-stop`, command: 'speaking', active: false },
      { at: base + clips.length * 2 + 0.05, phase: `all-tags-${lap}-handoff`, command: 'idle',
        options: { presentation_handoff: 'after_speech' } },
    ];
    actual.push(...rows.filter(row => row.at < options.durationSeconds));
  }
  return addPreviews(actual, options);
}

function addPreviews(actual, options) {
  let previousTag = -Infinity;
  const previews = actual.filter(row => row.command === 'trigger').map(row => {
    // Only the nearest pending tag owns preparation. Rapid tag bursts therefore
    // have less lead time; they do not preload all remaining sentence actions.
    const at = Math.round(Math.max(row.at - 1, previousTag + 0.01) * 1000) / 1000;
    previousTag = row.at;
    return { at, phase: row.phase + '-preview', command: 'tag-preview', label: row.label,
      previewLabel: options.tagPreview === 'correct' ? row.label : options.tagPreview === 'revoked'
        ? (row.label === 'sad_trans' ? 'surprise_trans' : 'sad_trans') : null };
  });
  return [...actual, ...previews].sort((a, b) => a.at - b.at);
}

// Serialized into the isolated renderer. No claims, model calls or RNG draws.
export function installTagPreviewProbe(limit = 10000) {
  const runtime = window.renderApp._spriteforgeRuntime, sprite = window.renderApp._sprite;
  const rows = [], restores = [];
  let dropped = 0, openMiss = null;
  const state = () => ({ node: runtime.currentNodeId, label: sprite._currentEmotion,
    index: sprite._frameIdx, timeMs: sprite._sampleTimeMs, held: sprite._held,
    speaking: runtime.speechActive, pending: runtime.pendingExpression,
    forced: runtime.forcedNodeId, postSpeech: runtime.postSpeechHoldActive });
  const emit = (kind, fields = {}) => {
    if (rows.length >= limit) { dropped++; return; }
    rows.push({ kind, t: performance.now(), ...fields });
  };
  const closeMiss = reason => {
    if (!openMiss) return;
    emit('miss-span', { ...openMiss, ms: performance.now() - openMiss.startedAt, reason });
    openMiss = null;
  };
  const wrap = (owner, name, factory) => {
    const original = owner[name];
    owner[name] = factory(original); restores.push(() => { owner[name] = original; });
  };
  for (const name of ['trigger', 'setSpeaking', 'release']) {
    wrap(runtime, name, original => function(...args) {
      emit('runtime-call', { method: name, args, before: state() });
      return original.apply(this, args);
    });
  }
  wrap(runtime, '_playNode', original => function(node, ...args) {
    const before = state();
    closeMiss('node-change');
    const value = original.call(this, node, ...args);
    emit('node', { requested: node, before, after: state() });
    return value;
  });
  wrap(sprite, '_showFrame', original => function(...args) {
    const before = this._displayMisses;
    const value = original.apply(this, args);
    if (this._displayMisses > before) {
      if (!openMiss) openMiss = { startedAt: performance.now(), label: this._currentEmotion, count: 0 };
      openMiss.count += this._displayMisses - before;
    } else closeMiss('ready');
    return value;
  });
  window.__tagPreviewProbe = {
    preview(label) {
      const before = state(), labels = new Set();
      if (label) {
        let target = runtime._normalizeTriggerLabel(label);
        const intent = runtime._emotionIntent(target);
        if (intent && runtime.speechActive) target = (runtime.cfg.emotionEntryByIntent || {})[intent] || target;
        const entry = runtime._findTriggerEntry(target) || runtime._nodeByLabel(target)?.id;
        if (entry) {
          labels.add(runtime._label(entry));
          if (runtime._nodeByLabel(target)) labels.add(target);
          for (const edge of runtime.graph.edges || []) {
            if (edge.from === entry && Number(edge.prob || 0) > 0) labels.add(runtime._label(edge.to));
          }
        }
      }
      sprite.prefetchLabels([...labels].filter(Boolean), 'interactive', { reason: 'probe-pending-tag' });
      const after = state();
      if (JSON.stringify(before) !== JSON.stringify(after)) throw Error('Tag preview changed playback state');
      const result = { requested: label || null, labels: [...labels], before, after };
      emit(label ? 'preview' : 'withdraw', result);
      return result;
    },
    stop() {
      this.preview(null); closeMiss('stop'); restores.reverse().forEach(restore => restore());
      return { rows, dropped, limitation: 'Synthetic pending-tag timing; no LLM/TTS queue or real voice interrupt pipeline is connected.' };
    },
  };
  return { installed: true, leadMs: 1000, pendingTags: 1, scope: 'public runtime resource preparation experiment' };
}
