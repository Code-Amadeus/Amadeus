// Post-measurement diagnostics on the installed product page. All pixel
// extraction, exact-target supply, holds and context loss belong to this phase.
export async function runHybridLifecycle({
  host = globalThis.window, timeoutMs = 10000, pollMs = 25,
  now = () => performance.now(), delay = ms => new Promise(resolve => setTimeout(resolve, ms)),
} = {}) {
  const checks = [], captures = [], evidence = {};
  const result = { schema: 'amadeus.frame-video-hybrid-lifecycle.v1', phase: 'post-measurement-diagnostics',
    checks, captures, evidence, readbacks: 0, error: null,
    limitations: ['Readbacks and deliberate clock/hold/context operations occur after the measured journey.',
      'Matched video/KTX2 captures are comparison evidence; lossy video is not required to equal static pixels.',
      'Cleanup counts cover the experiment and foreground FrameStore; driver allocations are not measured.'] };
  const app = host?.renderApp, sprite = app?._sprite, experiment = host?.__frameVideoExperiment;
  const renderer = host?.wallpaperApp?.scene?.app?.renderer;
  const label = experiment?.label || sprite?._currentEmotion;
  let contextLost, contextRestored;
  function check(passed, name, details = {}) {
    checks.push({ name, passed: !!passed, ...details });
    if (!passed) throw Error(name);
  }
  async function until(name, predicate) {
    const deadline = now() + timeoutMs;
    while (!predicate()) {
      if (now() >= deadline) throw Error(`Timed out: ${name}`);
      await delay(pollMs);
    }
  }
  const state = () => ({ label: sprite._currentEmotion, logicalIndex: sprite._frameIdx,
    displayedIndex: sprite._activeFrameIdx, sampleTimeMs: sprite._sampleTimeMs,
    held: sprite._held, heldIndex: sprite._heldFrameIdx, frozenVideo: sprite._videoFrozenHold,
    video: sprite.getExperimentalFrameVideoStats(), provider: experiment.snapshot() });
  const readyTexture = texture => !!texture && !texture.destroyed && !texture.baseTexture?.destroyed
    && texture.baseTexture?.valid && texture.width > 1 && texture.height > 1;
  const body = index => index >= sprite._experimentalFrameVideos.get(label).descriptor.headFrameCount
    && index < sprite._frameUrls[label].length - 1;
  const bodyVideo = () => sprite._currentEmotion === label && !sprite._held && !!sprite._videoDisplay
    && sprite._videoDisplay.texture === sprite.sprite.texture && body(sprite._activeFrameIdx)
    && sprite._activeFrameIdx === sprite._sampleFrameIndex(label, sprite._frameIdx, sprite._sampleTimeMs);
  const staticReady = index => {
    const texture = sprite.sprite.texture, base = texture?.baseTexture;
    const uploaded = !base?._glTextures || base._glTextures[renderer.CONTEXT_UID]?.dirtyId === base.dirtyId;
    return !renderer.gl.isContextLost() && sprite._currentEmotion === label && !sprite._videoDisplay
      && sprite._activeFrameIdx === index && texture === sprite._frames[label]?.[index]
      && readyTexture(texture) && uploaded;
  };
  function pixels(texture = sprite.sprite.texture, capture = null) {
    const target = new host.PIXI.Sprite(texture);
    let values, png;
    try {
      values = renderer.extract.pixels(target); result.readbacks++;
      if (capture) { png = renderer.extract.canvas(target).toDataURL('image/png'); result.readbacks++; }
    } finally { target.destroy({ texture: false, baseTexture: false }); }
    let hash = 2166136261, rgbSum = 0, alphaSum = 0, nonZeroAlphaPixels = 0;
    for (let i = 0; i < values.length; i++) {
      hash = Math.imul(hash ^ values[i], 16777619);
      if (i % 4 === 3) { alphaSum += values[i]; if (values[i]) nonZeroAlphaPixels++; }
      else rgbSum += values[i];
    }
    const glErrors = [];
    for (let i = 0; i < 16; i++) {
      const error = renderer.gl.getError();
      if (error === renderer.gl.NO_ERROR) break;
      glErrors.push(error);
    }
    const summary = { width: texture.width, height: texture.height, bytes: values.length,
      hashAlgorithm: 'fnv1a32', hash: (hash >>> 0).toString(16), rgbSum, alphaSum, nonZeroAlphaPixels, glErrors };
    if (capture) captures.push({ ...capture, png });
    return summary;
  }
  const validPixels = summary => summary.bytes > 0 && summary.rgbSum > 0 && summary.alphaSum > 0 && !summary.glErrors.length;
  const equalPixels = (a, b) => a.hash === b.hash && a.width === b.width && a.height === b.height
    && a.bytes === b.bytes && validPixels(a) && validPixels(b);
  async function holdStatic(index, capture = null) {
    app.holdSpriteFrame(index);
    await until(`uploaded KTX2 source ${index}`, () => sprite._held && sprite._heldFrameIdx === index && staticReady(index));
    return pixels(sprite.sprite.texture, capture);
  }
  function resetSameClip() {
    sprite.setSpeaking(false);
    sprite.setSpeaking(true);
    const reset = state();
    check(reset.label === label && reset.logicalIndex === 0 && reset.sampleTimeMs === null && !reset.held,
      'same-label speech reset preserves clip and resets source clock', { reset });
    sprite.setSpeaking(false);
  }
  try {
    check(!!app && !!sprite && !!renderer && !!experiment && typeof experiment.reinstall === 'function',
      'installed product renderer and explicit reinstall API are available');
    check(sprite._experimentalFrameVideos.has(label) && sprite._frameUrls[label]?.length > 1,
      'verified experimental clip is installed');
    evidence.initial = state();
    app.releaseCharacter(); app.clearSpriteHold(); app.setEmotion(label); app.setIdleAnimation(true);
    if (sprite.getExperimentalFrameVideoStats().failed) experiment.reinstall();
    await until('current body video display', bodyVideo);
    evidence.entry = state(); resetSameClip();
    // Keep the exact first body source cold for its later explicit-hold load.
    // A source reinstall does not evict FrameStore's already loaded textures.
    await until('cold body video after same-label reset', () => bodyVideo()
      && sprite._activeFrameIdx >= sprite._experimentalFrameVideos.get(label).descriptor.headFrameCount + 2
      && !sprite._frames[label][sprite._activeFrameIdx]);
    const beforeFaultLoads = experiment.snapshot().staticBodyLoads;
    evidence.beforeFaultStaticLoads = beforeFaultLoads;
    check(beforeFaultLoads?.normalBody === 0,
      'backend URL counter observes zero static body loads before faults', { loads: beforeFaultLoads });

    const heldIndex = sprite._activeFrameIdx, heldTexture = sprite.sprite.texture;
    const retainedSource = sprite._videoDisplay.source, beforeHold = pixels(heldTexture);
    app.holdSpriteFrame(null);
    const afterHold = pixels();
    evidence.freeze = { sourceIndex: heldIndex, beforeHold, afterHold, state: state(), source: retainedSource.snapshot() };
    check(sprite._held && sprite._heldFrameIdx === heldIndex && sprite._videoFrozenHold
      && sprite.sprite.texture === heldTexture && equalPixels(beforeHold, afterHold),
    'null hold freezes the actual video output with exact pixels after clip release', evidence.freeze);
    check(retainedSource.snapshot().destroyed && !retainedSource.snapshot().running
      && retainedSource.snapshot().queues.every(queue => queue.live === 0 && queue.outputs === queue.closed),
    'frozen output is independent of closed decoder frames', { source: retainedSource.snapshot() });

    const extension = renderer.gl.getExtension('WEBGL_lose_context');
    check(!!extension, 'real WebGL context loss is available');
    let lossObserved = false, restorationObserved = false;
    contextLost = () => { lossObserved = true; };
    contextRestored = () => { restorationObserved = true; };
    renderer.view.addEventListener('webglcontextlost', contextLost);
    renderer.view.addEventListener('webglcontextrestored', contextRestored);
    // Pixi owns restoration. Calling restoreContext again can create a GL error.
    extension.loseContext();
    await until('retained output context failure latches fallback', () => lossObserved && sprite.getExperimentalFrameVideoStats().failed);
    await until('Pixi context restoration', () => restorationObserved && !renderer.gl.isContextLost());
    await until('same held source KTX2 recovery', () => sprite._held && sprite._heldFrameIdx === heldIndex && staticReady(heldIndex));
    const recoveryLoads = experiment.snapshot().staticBodyLoads;
    evidence.contextFallbackStaticLoads = recoveryLoads;
    check(recoveryLoads?.fallbackBody >= 1,
      'backend URL counter observes an actual static fallback body load', { sourceIndex: heldIndex, loads: recoveryLoads });
    const recovered = pixels();
    app.clearSpriteHold();
    const explicitStatic = await holdStatic(heldIndex);
    evidence.contextRecovery = { sourceIndex: heldIndex, lossObserved, restorationObserved, recovered, explicitStatic,
      state: state(), retiredSource: retainedSource.snapshot() };
    check(equalPixels(recovered, explicitStatic) && sprite._activeFrameIdx === heldIndex,
      'restored fallback matches explicit static pixels for the same held source identity', evidence.contextRecovery);

    // Exact boundary supply is diagnostic: timeline sampling at 30FPS can omit
    // index18. Do not alter the authored sampling plan to make a capture happen.
    app.clearSpriteHold(); app.setIdleAnimation(false); experiment.reinstall(); resetSameClip();
    const firstBody = sprite._experimentalFrameVideos.get(label).descriptor.headFrameCount;
    const source = sprite._frameVideoSource;
    source.requestWindow([{ index: firstBody, priority: 100, deadline: Infinity }]);
    await until('exact boundary video source', () => source.has(firstBody));
    const exactVideo = source.texture(firstBody);
    check(!!exactVideo && source.snapshot().displayedIndex === firstBody, 'boundary video carries the exact source identity');
    source.retainDisplay(exactVideo);
    let videoPixels;
    try { videoPixels = pixels(exactVideo, { name: `video-body-${firstBody}`, sourceIndex: firstBody }); }
    finally { source.releaseDisplay(exactVideo); }
    const headPixels = await holdStatic(firstBody - 1, { name: `static-head-${firstBody - 1}`, sourceIndex: firstBody - 1 });
    const staticPixels = await holdStatic(firstBody, { name: `static-body-${firstBody}`, sourceIndex: firstBody });
    const boundaryLoads = experiment.snapshot().staticBodyLoads;
    evidence.explicitHoldStaticLoads = boundaryLoads;
    check(boundaryLoads?.explicitHoldBody >= 1 && boundaryLoads.normalBody === 0,
      'backend URL counter observes an actual explicit static body hold load', { sourceIndex: firstBody, loads: boundaryLoads });
    evidence.boundary = { headSourceIndex: firstBody - 1, bodySourceIndex: firstBody, headPixels, videoPixels, staticPixels,
      comparison: 'Inspect matched video/static PNGs; source identity and nonblank pixels are validated, equality is not asserted.' };
    check(validPixels(headPixels) && validPixels(videoPixels) && validPixels(staticPixels)
      && videoPixels.width === staticPixels.width && videoPixels.height === staticPixels.height,
    'matched boundary captures use valid equal geometry and explicit static source identities', evidence.boundary);

    app.clearSpriteHold(); experiment.reinstall(); app.setIdleAnimation(true);
    await until('playing video before decoder failure', bodyVideo);
    const failureIndex = sprite._activeFrameIdx;
    evidence.beforeInjectedFailure = state(); experiment.failActive();
    check(sprite.getExperimentalFrameVideoStats().failed, 'injected playing decoder failure latches static fallback');
    const fallbackPixels = await holdStatic(failureIndex);
    const failedCreated = experiment.snapshot().created;
    app.clearSpriteHold(); app.setEmotion('normal'); app.setEmotion(label);
    await until('failed clip advances from its static head', () => sprite._frameIdx > 0 && !sprite._held);
    evidence.latchedFallback = { sourceIndex: failureIndex, fallbackPixels, state: state() };
    check(validPixels(fallbackPixels) && sprite.getExperimentalFrameVideoStats().failed
      && !sprite._frameVideoSource && experiment.snapshot().created === failedCreated,
    'static fallback remains latched across playback and reentry until explicit reinstall', evidence.latchedFallback);
    experiment.reinstall();
    await until('video returns after explicit reinstall', bodyVideo);
    evidence.reinstalled = state();
    check(!sprite.getExperimentalFrameVideoStats().failed && experiment.snapshot().created > failedCreated,
      'explicit reinstall starts a fresh video source');
  } catch (error) {
    result.error = String(error?.message || error);
    if (!checks.some(row => !row.passed)) checks.push({ name: 'diagnostic sequence completed', passed: false, reason: result.error });
  } finally {
    if (renderer?.view) {
      if (contextLost) renderer.view.removeEventListener('webglcontextlost', contextLost);
      if (contextRestored) renderer.view.removeEventListener('webglcontextrestored', contextRestored);
    }
    if (app && sprite && experiment) {
      try {
        app.setIdleAnimation(false); experiment.disable(); sprite.setActive(false);
        await until('all foreground static jobs release', () => {
          const stats = sprite._frameStore.stats();
          return !stats.inFlight && !stats.queued && !stats.residentBytes && !stats.transientBytes
            && !experiment.snapshot().active.length;
        });
        const provider = experiment.snapshot(), staticStore = sprite._frameStore.stats();
        evidence.cleanup = { provider, staticStore, characterActive: sprite._characterActive, videoDisplay: !!sprite._videoDisplay };
        check(!provider.active.length && provider.completedTotals.outputs === provider.completedTotals.closed
          && provider.recentCompleted.every(row => row.retained === 0 && row.queues.every(queue => queue.live === 0)
            && row.output.liveOutputTextures === 0 && row.output.liveUploadTextures === 0 && row.output.borrowedVideoFrames === 0)
          && !sprite._videoDisplay && !sprite._characterActive,
        'all experiment frames/output textures and foreground static resources release', evidence.cleanup);
      } catch (error) {
        result.error ??= String(error?.message || error);
        if (!checks.some(row => row.name.includes('resources release') && !row.passed)) {
          checks.push({ name: 'final resource cleanup', passed: false, reason: String(error?.message || error) });
        }
      }
    }
  }
  result.passed = !result.error && checks.every(row => row.passed);
  result.validation = { checks: checks.length,
    contextLoss: !!evidence.contextRecovery, exactBoundaryCaptures: captures.length };
  return result;
}
