# Third-party notices and pre-release provenance status

Status: **pre-release inventory — no known unresolved provenance component is
selected as a release blocker under the stated immediate-upstream policy**.

Amadeus first-party code, documentation, modifications, and recorded original
brand assets are published under the PolyForm Noncommercial License 1.0.0.
That source-available license does not replace or narrow third-party rights.
Publishing components in the same repository does not change their existing
MIT, Apache-2.0, GPL, CC0, or proprietary terms.

This file records source and license evidence for third-party material checked
into the Amadeus repository. The machine-readable authority is
`LICENSES/provenance.json`. Package-manager dependencies are accounted for by
the cu124 dependency audit and future SBOM rather than duplicated here.

## Verified for the stated disposition

### GPT-SoVITS upstream license

- Source: <https://github.com/RVC-Boss/GPT-SoVITS>
- Closest verified source baseline: commit
  `9da7e17efe05041e31d3c3f42c8730ae890397f2` (2025-04-01).
- License: MIT, copyright RVC-Boss (2024)
- Local evidence: `LICENSES/GPT-SoVITS-MIT.txt` and
  `LICENSES/GPT-SoVITS-NESTED-NOTICES.md`.
- Import comparison: of 173 non-BigVGAN files in the initial Amadeus import,
  155 are exact baseline blobs, 11 differ, and 7 are local-only. Amadeus later
  changed 33 scoped paths, so the tree is explicitly marked modified.
- Nested permissive evidence: Apache-2.0 for VALL-E/PaddleSpeech/ESPnet and
  related files; BSD-2-Clause-style terms for CMUdict; BSD-3-Clause for the
  adapted PyTorch attention implementation; and retained MIT attributions for
  VITS, HiFi-GAN, EnCodec/vector quantization, F5-TTS, language splitting, and
  Whisper-derived portions.
- Disposition: included. The release relies on the exact immediate upstream
  repository-wide MIT grant and preserves the SoundStorm/private-source and
  `opencpop-strict.txt` caveats in
  `LICENSES/GPT-SoVITS-UPSTREAM-RELIANCE.md`. This does not assert a standalone
  SoundStorm license or relicense any upstream copyright.

### NVIDIA BigVGAN

- Source: <https://github.com/NVIDIA/BigVGAN>
- Source revision: `v2.4`, commit
  `7d2b454564a6c7d014227f635b7423881f14bdac`
- License: MIT for BigVGAN; Apache-2.0 notices occur in Apex-derived CUDA
  compatibility and alias-free activation material.
- Local evidence: `GPT_SoVITS/BigVGAN/LICENSE` and the restored upstream
  `incl_licenses/LICENSE_1`, `LICENSE_2`, `LICENSE_3`, and `LICENSE_5` files.
- Local changes: package-relative imports, inference-only configuration, CUDA
  device selection, and Windows kernel build/cache handling.
- Disposition: included with its upstream MIT, HiFi-GAN, Snake, Julius, and
  Apache-2.0 evidence preserved.

### AP-BWE

- Source: <https://github.com/yxlu-0102/AP-BWE>
- License: MIT, copyright Ye-Xin Lu (2023)
- Local evidence: `LICENSES/AP-BWE-MIT.txt`
- Purpose: optional 24 kHz to 48 kHz audio bandwidth extension for GPT-SoVITS
  v3. Production TTS profiles do not enable it.
- Disposition: implementation and weights are excluded from the public source
  archive. The private checkout retains a lazy-loaded manual compatibility path.

### PixiJS

- Source: <https://github.com/pixijs/pixijs>
- Checked-in bundle: PixiJS v7.4.3
- License: MIT
- Local evidence: notice retained in `render/web/vendor/pixi.min.js`.

### pixi-live2d-display

- Source: <https://github.com/guansss/pixi-live2d-display>
- Source revision: npm `pixi-live2d-display@0.4.0`, git tag `v0.4.0`
  (`12148332b8838ca00ad76764e0e7521abf980e3b`)
- Upstream license: MIT
- Local evidence: `LICENSES/pixi-live2d-display-MIT.txt`; the checked-in bundle
  Git blob exactly matches the npm `dist/cubism4.min.js` payload.
- Disposition: included only as an explicit opt-in legacy adapter. It declares
  PixiJS 6 peers and is not presented as verified PixiJS 7 support. Cubism Core
  and Live2D models are user-supplied. See
  `docs/render_vendor_compatibility.md` for the compatibility boundary.

### pixi-basis-ktx2

- Source: <https://github.com/Sparcks/pixi-basis-ktx2>
- Source revision: npm `pixi-basis-ktx2@0.0.22`, gitHead
  `8363b5281448fdf8f7b4745dfbb6c466faa800a7`
- License: MIT, copyright Kristof Van Der Haeghen (2022-2025)
- Local evidence: `LICENSES/pixi-basis-ktx2-MIT.txt`
- Disposition: included because the SpriteForge runtime character pack uses
  KTX2 textures. `tools/revendor_pixi_basis_ktx2.py` pins the npm integrity and
  esbuild version used for the browser-global wrapper.

### Keyboard Soundpack #1

- Source: <https://opengameart.org/content/keyboard-soundpack-1-typing-and-single-keystrokes>
- Author: Unicae
- Source item: `Human Typing/human_vel-002.wav`
- License: CC0-1.0
- Local evidence: `assets/audio/sfx/computer_use_keyboard_loop.LICENSE.txt`

## Must be excluded from the public code archive

### Live2D Cubism Core

`render/web/vendor/live2dcubismcore.min.js` identifies itself as Live2D
proprietary Redistributable Code. It is not open-source code and is excluded
from the default public source archive. A user-supplied runtime or separately
reviewed distribution is required. The self-contained public exclusion record
is `LICENSES/Live2D-Cubism-Core-NOTICE.md`.

Official terms:

- <https://docs.live2d.com/en/cubism-sdk-manual/cubism-core/>
- <https://www.live2d.com/en/sdk/license/>

### Bert-VITS2 Cantonese frontend

`GPT_SoVITS/text/cantonese.py` is a substantive derivative of the
AGPL-3.0-licensed Hugging Face Space
`Naozumi0512/Bert-VITS2-Cantonese-Yue`, not merely a file that mentions it.
The full upstream license is retained in
`LICENSES/Bert-VITS2-Cantonese-Yue-AGPL-3.0.txt`; the implementation is
excluded from the intended PolyForm public source archive. The private
checkout is unchanged.

### GPT-SoVITS caches, custom Japanese dictionary, and UVR5 utility

Generated `*.pickle` lookup caches and `GPT_SoVITS/text/ja_userdic/**` are
excluded. The cache readers rebuild or omit them, while the custom Japanese
dictionary has no recorded distribution source. `tools/uvr5/**` is also
excluded because no Amadeus Electron/server/TTS runtime caller reaches that
upstream WebUI utility tree.

### Character, model, and voice material

Character frames, videos, unverified packaged audio, model weights, LoRA
weights, reference voices, and pretrained model files are not covered by the
repository's code license. They are excluded until separately versioned
packages have source, hash, license, and consent records.

## First-party brand assets

The project owner confirmed that the checked-in application icons and built-in
desktop wallpaper are their original works. Their paths and license scope are
recorded in `LICENSES/FIRST-PARTY-BRAND-ASSETS.md`; both components are ready
for inclusion under the root first-party license.

## Release-blocking gaps

None under the currently stated immediate-upstream reliance policy. The
SoundStorm and `opencpop-strict.txt` source-chain caveats remain documented
risk, not hidden facts.

This is an engineering provenance record, not legal advice.
