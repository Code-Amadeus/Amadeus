# License and provenance inventory

This directory is the machine-readable source of truth for code, model, and
asset provenance that affects an Amadeus release.

Amadeus first-party source code, documentation, modifications, and the original
brand assets recorded in `FIRST-PARTY-BRAND-ASSETS.md` are published under the
PolyForm Noncommercial License 1.0.0 in the root `LICENSE`. It permits
noncommercial use, modification, and redistribution; commercial use requires
separate written permission. It governs only material whose copyright is owned
by its Amadeus licensor and does not replace or narrow the MIT, Apache-2.0,
CC0, GPL, or other rights that apply to third-party portions.

- `provenance.json` records where a component came from, the local paths it
  covers, the license evidence currently present, whether it was modified, and
  its release disposition.
- `../THIRD_PARTY_NOTICES.md` is the human-readable notice generated from the
  same evidence set. It is currently a pre-release inventory, not a declaration
  that every redistribution question has been cleared.
- Python and npm packages installed by a release profile belong in the
  dependency SBOM. They are not copied into this manual vendoring inventory
  unless their source is checked into the repository.
- `GPT-SoVITS-MIT.txt` preserves the upstream license independently of the
  first-party license in the repository root.
- `FIRST-PARTY-BRAND-ASSETS.md` records the maintainer-confirmed authorship and
  license scope for the application icons and built-in desktop wallpaper.
- `GPT-SoVITS-NESTED-NOTICES.md` records the closest reproducible import
  baseline, local modification counts, nested source families, and the two
  original-source caveats. `GPT-SoVITS-UPSTREAM-RELIANCE.md` records the
  immediate-upstream MIT basis used for the public source release without
  pretending that SoundStorm itself has a verified MIT license. `Apache-2.0.txt`,
  `CMUdict-BSD-2-Clause.txt`, and `PyTorch-BSD-3-Clause.txt` preserve the
  corresponding permissive license evidence used by that tree.
- `Bert-VITS2-Cantonese-Yue-AGPL-3.0.txt` preserves the license of the
  Cantonese frontend source. The associated implementation is deliberately
  excluded from the public source archive; retaining its notice does not make
  it part of the PolyForm-licensed first-party scope.
- `pixi-basis-ktx2-MIT.txt` and `pixi-live2d-display-MIT.txt` preserve the
  licenses for the checked-in browser vendor bundles.
- `AP-BWE-MIT.txt` preserves the upstream license while the optional
  implementation remains excluded. `Live2D-Cubism-Core-NOTICE.md` records the
  proprietary runtime exclusion without redistributing Cubism Core.

`release/source_release_policy.json` records the selected publication model and
its first-party scope. The standard PolyForm text is used without modification;
third-party and external asset boundaries are recorded separately here and in
`provenance.json`.

Gate states:

- `ready`: evidence is sufficient for the stated release action.
- `review`: a source, exact revision, license copy, or redistribution decision
  is still missing. Included paths block a public release.
- `blocked`: known evidence is insufficient or conflicts with the intended
  public distribution. Included paths block a public release.

Release actions:

- `include`: intended to ship after the gate is `ready`.
- `exclude`: must not be copied into the public source archive.
- `user-supplied`: code may describe the integration, but the material itself
  is not distributed by Amadeus.
- `dependency`: installed from a package manager and accounted for in the
  dependency lock/SBOM rather than vendored here.

This inventory is engineering evidence, not legal advice. A rights holder or
qualified reviewer must approve unresolved character, voice, model, and Live2D
questions before a public release.
