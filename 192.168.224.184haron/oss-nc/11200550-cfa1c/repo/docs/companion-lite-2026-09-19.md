# Companion Lite: source selection, edges and memory

The accepted local implementation reuses SpriteForge authoring frames and the
existing VN portrait composition. It precomputes small lossless WebP atlases and
plays them using a CPU Canvas2D surface. No KTX2/Pixi runtime is loaded in Companion.
Media remains outside Git and can be installed with the existing asset bundle tool.

## Source and timing evidence

The existing VN cache was found at the repository's sibling `visual novel player`
workspace. The earlier claim that this local cache was absent was incorrect.
Its 132px crop uses `crop_side_ratio=0.74`, `crop_y_ratio=0.035`, rounded alpha and
per-clip brightness stabilization of strength 0.92. Reconstructing all 42 selected
speaking keyframes for seven expressions produced **zero pixel error** against that
cache before the edge correction was introduced.

Three first-round candidates were produced: densifying the old selected segment at
unchanged 1.02-second duration; full source loops sampled at approximately 24 FPS at
132px; and the same loops at 264px. `disappointed` remains the original three-frame
art. Full loops were matched by frame basename and count to the KTX2 manifest before
borrowing `frameIntervalMs`. Typical speaking loops last 2.04, 3.06 or 5.10 seconds.
The integer timing already stored in the runtime manifest is preserved; it is not
an independently re-timed 30/60 FPS source video.

Further review covered 21 authoring projects, including variants absent from the
runtime graph. Nineteen had readable selected PNG frames. `frozen_portrait` and
`no_nod` had truncated PNG inputs and were explicitly excluded, not silently fixed.
An integer mid-face template estimate helped shortlist variants but cannot establish
absence of subtle head rotation or subpixel movement. Animation previews are the
visual evidence; names such as `static` are not proof of motion properties.

The selected normal idle is `kurisu_idle_static_saved` (`idle1`), retaining its
2.52-second loop at approximately 8 FPS. The original static idle is selectable.
Normal speech uses `speaking_short`. Both `thinking_speaking2` (5.10 seconds) and
`thinking_speaking1` (3.06 seconds) are kept and alternate between speaking runs.
Original head/hair movement is retained. This is not a runtime head-locking filter.

## Edge flicker

The legacy crop helper manually premultiplied RGB but then resized an RGBA image.
Pillow 12.3's `RGBA.resize` itself converts through premultiplied `RGBa`, so the old
path multiplied alpha twice. The new export explicitly resizes `RGBa` once before
converting back to straight RGBA. The crop position and size remain the same.

A second optional export step blends adjacent frames with weights 1:2:1 only where
all three frames have partial alpha (0.02–0.98) and the alpha range is under 0.25.
It operates in premultiplied colour. Opaque face/eye/mouth pixels and fast-moving
silhouettes are excluded. Assertions verify opaque pixels remain byte-identical
through this temporal filter. No averaging is performed across a loop seam.

For the paired first thinking variant, the edge temporal second-difference metric
on the actual portrait background changed from 10.90 (legacy crop) to 7.90 (single
premultiply) to 4.21 (edge-only filter). Normal changed from 12.13 to 8.85 to 4.77.
These metrics include genuine motion and are not proof that every visible artifact
has disappeared. Source loop discontinuities remain possible. The second thinking
variant uses different source frames, so its metrics are not compared to the first
variant as an edge-filter improvement.

## Performance experiment

Windows, Electron 44, one machine. Trials used isolated profiles and identical
470×250 cards. The first trials were accelerated offscreen windows with verified
paint delivery; those incur readback overhead and are reported separately. The
final native-window trials actually showed the panel, used common rAF instrumentation,
and ran sequentially. They did not start a game, voice models or the full wallpaper.

The old card and the final candidate were each run twice with samples at one-second
intervals. The table gives per-phase medians across samples, as ranges across runs:

| Native phase | Existing PNG card: total private MiB | Final candidate: total private MiB |
| --- | ---: | ---: |
| Normal speaking | 217.6–220.7 | 212.0–212.4 |
| Thinking speaking | 226.9–230.1 | 227.8–228.0 |
| After repeated expression changes | 212.3–217.8 | 221.4–222.4 |
| Standby | 212.8–215.9 | 231.2–232.2 (animated idle) |

Actual speaking portrait updates were approximately 5.8–6.0/s for the existing card
and 24.0–24.2/s for the candidate. The final candidate's speaking CPU API percentages
were about 0.99–1.04 versus 0.64–0.76 for the old card. These are Electron-reported
percentages including common instrumentation, not a percentage of one CPU core.
Raw cumulative CPU times are also retained. Static idle and pause stop portrait
redraws; gentle idle runs at about 8.2 updates/s.

GPU-backed Canvas2D was rejected after expression cycling caused GPU-process private
memory retention. Software Canvas2D kept GPU-process private memory near the old
card's level. This measures process RAM, **not physical VRAM**. A 16 MiB/2-entry
explicit bitmap budget bounds retained application atlases; browser decoder and
allocator transients are separate. One-second samples can miss brief peaks.

The subsequent real production-panel smoke used the actual Python Host and asset
server. It observed roughly 33.6 MiB portrait-page private memory at idle and
43.3 MiB while speaking. Its Electron totals include a synthetic Slice page and
must not be directly compared to the isolated-card table. There is no claim of
zero gaming FPS impact, long-duration leak freedom, or a cap on the whole Amadeus
application. Wallpaper resources remain a separate memory-lifecycle concern.

## Integration and behavior

- Default assets: `assets/companion/kurisu/manifest.json`; no sibling-workspace lookup.
- `AMADEUS_COMPANION_PORTRAIT_CACHE`: explicit compatibility with the existing PNG cache.
- Eight expressions, 19 installed files including the alternate thinking animation.
- `companion-kurisu`: independently installable derived media, approximately 11.4 MiB.
- The Host remains the source of speech, subtitles and expression signals.
- A completed speech holds its expression for **350 ms**, then returns to normal idle.
  New speech or a new expression cancels the deadline; subtitle cleanup does not.
  The real Host/panel smoke observed the return at 374 ms including polling overhead.
- Hidden windows pause, static frames have no animation timer, and close releases
  bitmaps and the renderer process. Bitmap dimensions and manifest paths are checked.

The wallpaper's `POST_SPEECH_HOLD_SEC=1.0` remains unchanged. The old Companion had
no explicit neutral-return deadline; it could retain an expression until another
presentation event. This change gives the compact surface an explicit short return.

## Reproduction and local artifacts

The experiment source is retained rather than deleting the tested prototype.
Local evidence/media live under `output/diagnostics/companion-atlas/` (Git-ignored):
`summary.json`, per-run metrics and hashes, `assets-v1/source-evidence.json`, the
paired edge reports, `source-variants-v2/review.html`, and `review-final.html`.
The preview pages include multiple variants and are not performance benchmarks.
The compact [checked-in evidence](evidence/companion-lite-2026-09-19.json) preserves
the final native comparisons, paired edge metrics, bundle hash and production smoke
outcomes without requiring the local media or diagnostic directories.

```powershell
# VN_ROOT and SPRITEFORGE_WORKSPACE are explicit local authoring inputs.
python tools/probes/build_companion_portraits.py --vn-root <VN_ROOT> --workspace <SPRITEFORGE_WORKSPACE> --runtime assets/spriteforge/runtime/kurisu/runtime_manifest.json --output <FRESH_OUTPUT>
python tools/probes/refine_companion_edges.py --vn-root <VN_ROOT> --workspace <SPRITEFORGE_WORKSPACE> --assets <FRESH_OUTPUT> --runtime assets/spriteforge/runtime/kurisu/runtime_manifest.json --idle-label idle1 --thinking-label thinking_speaking1 --output-name candidate132
python tools/probes/refine_companion_edges.py --vn-root <VN_ROOT> --workspace <SPRITEFORGE_WORKSPACE> --assets <FRESH_OUTPUT> --runtime assets/spriteforge/runtime/kurisu/runtime_manifest.json --idle-label idle1 --thinking-label thinking_speaking2 --output-name candidate132b
python tools/package_companion_character.py --candidate <FRESH_OUTPUT>/candidate132b --alternate <FRESH_OUTPUT>/candidate132
python tools/external_assets.py build companion-kurisu --output output/amadeus-companion-kurisu.zip
python tools/external_assets.py verify output/amadeus-companion-kurisu.zip
```

The packager requires a fresh destination. It does not overwrite existing installed
media. For migration, build into a staging checkout and use the existing explicit
bundle overwrite workflow only when replacing a reviewed old pack is intended.

### Optional distribution integration

The 2026-09-19 art collection retains the three original 2026-09-18 collection
members byte-for-byte and adds `04-amadeus-companion-kurisu-2026.09.19.zip` as an
optional nested installable bundle. A standalone Lite download is also available;
existing art users do not need the large collection again. No voice assets change.
The collection is a download container, not an all-components install command.

The shared Python Companion contract now validates build/verify/install/status;
it derives file membership from the portrait manifest rather than a duplicate
hardcoded list. Normal idle/speaking, all referenced variants, safe URLs, timeline
bounds, calculated decode budget, sizes and inner hashes are checked. Authoring
and browser decoding validate actual WebP dimensions. Missing optional media is
allowed; a selected invalid pack fails before commit. Public source release policy
excludes the media tree while retaining the player, packager and documentation.

Release verification: both existing installable art bundles reverified, all three
original nested ZIP hashes unchanged, new 19-file Lite pack clean-installed, and
every completed collection member hash reread successfully. The updated authoring
packager also reproduced the approved eight-expression export. No source merge or
public upload is implied by these local artifacts; consumers need updated player
and catalog code as well as the Lite media. See the [bundle workflow](external_asset_bundles.md).

Checks: timing/alpha invariants, browser bitmap lifetime and rapid-switch tests,
existing projection/docking contracts, Python asset/bridge tests, Electron build,
real production-panel smoke, bundle verification, and clean-directory installation
plus idempotent reinstallation. The old separate docking smoke had a pre-existing
native DPI/bounds restoration failure before this implementation; it was not used
as a passing acceptance claim. Its diagnostic asset allowlist and hidden-rendering
setup were updated for the new script and production visibility throttling; no
docking or bounds behavior was changed by this work.
