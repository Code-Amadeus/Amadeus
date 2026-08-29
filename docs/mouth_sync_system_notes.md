# Mouth Sync System Notes

Date: 2026-05-23
Updated: 2026-08-29

This note records the current SpriteForge mouth-sync implementation, the failed paths we hit, and the reasoning behind the final conservative design.

## Goal

The mouth-sync system should make SpriteForge speaking loops look closed-mouth during silence or TTS pauses without breaking the underlying high-frame-rate character animation.

The important constraint is that many current speaking loops are authored as "always speaking" loops. Even when TTS amplitude is zero, the base animation may still contain half-open or open-mouth frames. Therefore, silence cannot be handled by simply setting amplitude to zero and hoping the loop frame is visually closed.

## Final Direction

The final runtime strategy is:

1. Keep the original speaking loop playing normally.
2. During silence, overlay a closed-mouth patch on top of the current frame.
3. Clip that patch with a mouth-area mask so only the mouth region is replaced.
4. Track the mouth mask position per frame through `anchorTrack`.
5. Exclude transition nodes from mouth-sync entirely.

This is deliberately different from "jump to a closed frame". Jumping frames breaks motion continuity, especially in loops where hair, body, hand, or head are still moving.

## Runtime Data Flow

The primary signal is derived from the PCM that physical playback is about to
write. VTS remains an optional compatibility sink:

```text
PCM playback window (about 50 ms)
  -> RMS * volume multiplier
  -> MouthSignalRouter primary sink
  -> render.mouth event
  -> JS renderer.setMouth(...)
  -> SpriteRenderer._updateMouthLayer()
```

Streaming synthesis may greedily merge producer chunks to avoid audio underrun.
The audio writer keeps that merged work in one writer-thread job, but splits it
into envelope windows internally. Each mouth value is published immediately
before the corresponding PCM window is written. Publishing only after a whole
merged write would leave the renderer displaying the previous window's value
while audible speech is already playing.

The router also fans out to an optional VTS compatibility path. A failure in
that side path cannot block local rendering or physical audio playback.

For wallpaper mode, the same render event is forwarded to the wallpaper
character runtime:

```text
MouthSignalRouter
  -> HeadlessRenderBridge
  -> render.mouth
  -> local and wallpaper SpriteForge renderers
```

Relevant code:

- `tts/playback.py::StreamPlayer.write_audio_async`
- `tts/mouth_signal.py::MouthSignalRouter`
- `render/headless_bridge.py::HeadlessRenderBridge`
- `render/spriteforge_animator.py::set_mouth_value`
- `render/web/renderer.js::setMouth`

## Config Model

The runtime reads:

```text
assets/spriteforge/runtime/kurisu/spriteforge_mouth_config.json
```

Important fields:

- `profiles[label]`: per speaking-loop profile.
- `openness`: detected mouth openness for each frame.
- `anchor_track`: per-frame mouth anchor rectangle.
- `runtime_overlay_anchor`: geometry for the packaged KTX2 silence overlay.
- `mouth_set`: fallback mouth-set name.

The corresponding KTX2 overlay path lives in
`runtime_manifest.json::mouthOverlays`; runtime configuration contains no PNG
or authoring-workspace path.

The Python bridge converts these profiles into JS renderer configs in:

```text
render/spriteforge_animator.py::_load_mouth_configs
```

Current JS mode for SpriteForge speaking loops is:

```json
{
  "mode": "silence_close",
  "silenceThreshold": 0.08
}
```

In `silence_close` mode, the closed-mouth overlay is only visible when:

```text
!speaking || mouthValue <= silenceThreshold
```

When the character is actively speaking above threshold, the overlay is hidden and the authored speaking animation remains visible.

## Why Mask, Not Jump Frame

We explicitly rejected jump-frame silence.

Jump-frame silence means: when amplitude is zero, display the detected closed-mouth frame of the loop.

That fails because:

- It freezes or pops the whole character when only the mouth should change.
- Hair/body/hand animation discontinuities become visible.
- Some loops do not have a truly closed frame.
- It cannot handle current-frame head/hand motion unless every body part is also warped.

The mask approach is better because:

- The base loop continues to animate.
- Only the mouth region is replaced.
- It is compatible with high-FPS SpriteForge loops.
- It can be improved locally by better mouth anchors without rewriting graph logic.

## Important Label Filtering

Not every node that occurs during a speaking route should receive mouth-sync.

Transition nodes are visual graph hops. They may lead into speaking loops, but they are not stable speaking-loop assets and should not inherit mouth masks.

Relevant constants:

```python
_SPEAKING_PERFORMANCE_LABELS
_SPEAKING_ENTRY_TRANSITION_LABELS
_MOUTH_SYNC_LABELS = _SPEAKING_PERFORMANCE_LABELS - _SPEAKING_ENTRY_TRANSITION_LABELS
```

This fixed the issue where transition assets such as `closed_eye_trans` were treated as speaking mouth-sync targets.

## Key Pitfalls We Hit

### 1. Old resource assumptions no longer apply

The early mouth system was based on older assets and mostly fixed mouth positions. The new SpriteForge resources have different canvas sizes, expression-specific positions, and sometimes frame-by-frame mouth movement.

Result: old fixed-point overlays looked correct in a few frames but drifted badly in wallpaper mode and new speaking loops.

Resolution: use per-profile `anchor_track` and per-frame active index in JS.

### 2. Openness detection can pick the wrong "closed" frame

Example: `smile_speaking` visually had a true closed-mouth frame, but the automatic score picked a neighboring half-open frame.

This happened because "mouth openness = 0" from detection does not always mean visually closed. Small dark lines, teeth, shadows, or detection crop errors can confuse the metric.

Resolution:

- Keep `closed_frame_idx` in config but visually verify it.
- Allow explicit `closed_source`.
- For special cases, override behavior in `_MOUTH_SILENCE_MASK_OVERRIDES`.

### 3. Some loops may not contain a true closed-mouth frame

Some speaking loops are authored as always-speaking. A silence period would leave the character visibly half-open forever.

Resolution: allow `closed_source` from a compatible frame. If available, use a known closed-mouth frame from the same expression/canvas style. Do not force closed selection from a bad loop.

### 4. Key point included the finger in the mask

`key_point_speaking` has a hand/finger near the face. Early mask detection/cropping covered part of the finger, so the replacement patch contaminated the hand area.

Resolution:

- Use `preferOwnClosedFrame` so key point uses its own closed-mouth frame instead of a borrowed transition frame.
- Narrow the mask override for key point:

```python
"_MOUTH_SILENCE_MASK_OVERRIDES['key_point_speaking'] = {
    'overlayAlign': 'canvas',
    'preferOwnClosedFrame': True,
    'silenceMaskAmplitude': 0.80,
    'maskWidthMul': 1.00,
    'maskHeightMul': 0.90,
    'maskCyOffset': 0.0,
}"
```

The reason `overlayAlign = "canvas"` helps here is that the selected closed-mouth source is from the same key-point canvas. When the head/canvas is effectively stable, keeping the patch in canvas coordinates avoids accidental drift into the finger.

### 5. The replacement mouth source can be wrong

At one point the wrong row/source image was selected as the closed-mouth overlay. The result looked mathematically aligned but visually wrong.

Resolution: source-frame validation must be visual, not just numeric. The correct source should be checked in a debug/contact sheet before being trusted.

### 6. Texture readiness caused repeated PIXI errors

The renderer crashed repeatedly with:

```text
Cannot read properties of null (reading 'height')
```

This happened because `_drawMouthMask` read texture dimensions before PIXI had a valid texture/baseTexture/orig.

Resolution in `render/web/renderer.js`:

- Add `_isTextureReady(texture)`.
- Add `_textureHeight(texture, fallback)`.
- Early-return and clear the mask if texture is not ready.

This made mouth overlay safe during async texture loads and frame switches.

### 7. Wallpaper mode had no mouth updates at first

Character render mode received mouth amplitude, but wallpaper mode looked frozen/closed because the wallpaper animator did not receive the VTS mouth callback.

Resolution: add `chatGui.py::_bind_wallpaper_mouth_value`, which wraps `vts_mgr.on_mouth_value` and forwards the value both to the previous callback and to the wallpaper animator.

### 8. Full amplitude-to-mouth-frame mapping was too ambitious for now

The more ambitious plan was:

```text
amplitude -> half/full/open mouth overlay frame
```

In practice this was fragile:

- Open-mouth overlay variants did not always match every expression.
- Frequency mapping could get stuck on one open shape.
- Different loops have different mouth drawings.
- Precise per-frame x/y/rotation/scale would be needed for high quality.

Resolution: use a conservative silence-close layer first. That solves the most visible problem: silence should look closed.

## JS Renderer Behavior

Relevant JS areas:

- `SpriteRenderer._updateMouthLayer`
- `SpriteRenderer._drawMouthMask`
- `SpriteRenderer._getMouthAnchor`
- `SpriteRenderer._isTextureReady`

The overlay is a child of the base sprite:

```js
this._mouthOverlay = new PIXI.Sprite();
this._mouthOverlay.anchor.set(0.5, 1.0);
this._mouthOverlay.mask = this._mouthMask;
this.sprite.addChild(this._mouthOverlay);
```

The mask is drawn in sprite-local coordinates. Anchor coordinates are interpreted relative to the texture center:

```text
localX = cx
localY = cy - textureHeight / 2
```

Default overlay alignment:

```text
overlay.x = currentAnchorX - sourceAnchorX
overlay.y = currentAnchorY - sourceAnchorY
```

Special case:

```text
overlayAlign = "canvas"
```

keeps the overlay at `(0, 0)` when the closed-mouth source is from the same canvas and should not be re-offset.

## Current Known Limitations

- Closed-frame quality is only as good as `closed_frame_idx` / `closed_source`.
- Some expressions still need visual review of the selected closed source.
- The system does not yet do high-quality continuous viseme mapping.
- It does not yet model mouth patch rotation or perspective.
- It assumes the mouth-area ellipse is enough; complex poses may need tighter polygon masks or manually authored masks.

## Verification Checklist

For each speaking expression:

1. Confirm the profile is in `_MOUTH_SYNC_LABELS`.
2. Confirm transitions are excluded.
3. Check selected `closed_frame_idx` visually.
4. Check `closed_source` if the loop has no true closed frame.
5. Preview mask overlay against representative frames.
6. Verify character render mode.
7. Verify wallpaper mode receives amplitude fan-out.
8. Test both:
   - TTS active with amplitude above threshold.
   - silence / pause / TTS ended.

## Files To Check When Debugging

- `render/spriteforge_animator.py`
  - label filtering
  - profile loading
  - closed-source selection
  - special overrides

- `render/web/renderer.js`
  - overlay visibility
  - mask drawing
  - texture readiness
  - per-frame anchor selection

- `chatGui.py`
  - normal renderer mouth config push
  - wallpaper mouth amplitude fan-out

- `assets/spriteforge/runtime/kurisu/runtime_manifest.json`
- `assets/spriteforge/runtime/kurisu/spriteforge_mouth_config.json`
  - profile data
  - anchor tracks
  - manifest-indexed silence overlay geometry

## Rule Of Thumb

Do not solve silence by switching the whole animation frame.

For SpriteForge high-frame-rate speaking loops, silence should be handled as:

```text
keep base loop running
+ overlay closed mouth patch
+ mask only the mouth region
+ track mouth anchor per frame
```

This is the least disruptive strategy and keeps the door open for future full viseme mapping.
