# Public source release tooling

These tools prepare a reviewable source archive. They do not change product
semantics, install runtime dependencies, move assets, rewrite Git history, or
silently waive a release blocker.

Separately distributed runtime media uses `tools/external_assets.py`; it is not
part of the public source-archive workflow. See
`docs/external_asset_bundles.md`.

The public archive uses the open-source model recorded in
`source_release_policy.json`. Its root `LICENSE` applies only to Amadeus
first-party code and modifications; third-party and external asset terms remain
separate.

## 1. Validate third-party provenance

```powershell
.venv\Scripts\python.exe tools\check_third_party_provenance.py
```

This validates schema, local evidence paths, and coverage for declared
third-party inventory roots. Known `review`/`blocked` components are allowed to
exist in the inventory.

To ask whether every included component is actually releasable:

```powershell
.venv\Scripts\python.exe tools\check_third_party_provenance.py --release-ready
```

That command is expected to fail until every included component in
`LICENSES/provenance.json` has `gate_status: ready`.

## 2. Audit the observed cu124 environment

The base audit is offline and read-only:

```powershell
.venv\Scripts\python.exe tools\audit_cu124_dependencies.py `
  --output-json build\audit\cu124-dependencies.json `
  --output-markdown build\audit\cu124-dependencies.md `
  --observed-output build\audit\windows-py312-cu124-observed.txt
```

To include known-vulnerability data, run pip-audit from an isolated throwaway
environment and pass its JSON result back into the offline auditor:

```powershell
# Run pip-audit from an isolated throwaway env (uvx) against the live venv.
uvx pip-audit@2.10.1 `
  --path .venv\Lib\site-packages `
  --format json --desc off --aliases on --progress-spinner off `
  --output build\audit\pip-audit-cu124.json

.venv\Scripts\python.exe tools\audit_cu124_dependencies.py `
  --pip-audit-json build\audit\pip-audit-cu124.json `
  --pip-audit-version 2.10.1 --pip-audit-service pypi `
  --output-json build\audit\cu124-dependencies.json `
  --output-markdown build\audit\cu124-dependencies.md `
  --observed-output build\audit\windows-py312-cu124-observed.txt
```

`pip-audit` returns exit code `1` when it finds vulnerabilities. That is a
finding, not an instruction to bulk-upgrade the working GPU environment.

The observed requirements snapshot contains names and versions only. It is not
a resolver lock and must not be used as an authoritative installation input.

## 3. Check the source archive policy

```powershell
.venv\Scripts\python.exe tools\build_source_release.py `
  --manifest-output build\release\source-manifest.json
```

The check:

- starts from `git ls-files`, so untracked sessions, models, caches, and local
  work products cannot enter by directory accident;
- selects explicit roots/files and applies explicit exclusions;
- refuses proprietary/user-supplied provenance components;
- blocks included third-party components whose evidence is unresolved;
- checks the first-party license decision;
- rejects dirty selected files;
- scans filenames, personal absolute paths, likely secrets, and oversized
  source files;
- suppresses matched secret text in every diagnostic;
- records the SHA-256 and size of every selected file.

On a dirty development tree, a non-releasable diagnostic report can be
generated with:

```powershell
.venv\Scripts\python.exe tools\build_source_release.py `
  --allow-dirty-check `
  --manifest-output build\release\source-manifest.json
```

`--allow-dirty-check` never makes the result release-ready and cannot be used
with archive creation.

## 4. Build a deterministic archive

Only a clean tree with no gate error can produce an archive:

```powershell
.venv\Scripts\python.exe tools\build_source_release.py `
  --manifest-output build\release\source-manifest.json `
  --output dist\amadeus-source.zip
```

Archive entries are sorted, share the Git commit timestamp, preserve executable
mode where Git records it, and include `SOURCE_MANIFEST.json`. Existing outputs
are not overwritten unless `--overwrite` is explicit.

## 5. Current expected blockers

At the 2026-08-29 audit baseline, BigVGAN, AP-BWE, pixi-live2d-display, and
Basis/KTX2 have a recorded disposition. Generated GPT-SoVITS caches, the
unverified custom Japanese dictionary, the inactive UVR5 utility, and the
AGPL-derived Cantonese frontend are explicitly excluded without deleting the
private checkout copies.

The complete current Amadeus GPT-SoVITS inference implementation, including
`local_tts_infer.py`, is selected for the public source archive. Its exact
immediate upstream MIT basis and the retained SoundStorm/`opencpop-strict.txt`
caveats are recorded in `LICENSES/GPT-SoVITS-UPSTREAM-RELIANCE.md`.

No known provenance component is currently selected as a release blocker.
Archive creation can still fail for a dirty selected file, secret/path scan
finding, missing required file, or a future unresolved component.

These are useful failures. Do not turn them into broad exceptions merely to
make the command green; either resolve the evidence, exclude the component, or
record a narrow, reviewed policy exception.

## 5. Publish the application and optional resources together

Publish checked optional dictionary resources as separate attachments of the
**application Release**, alongside the source ZIP, `source-manifest.json` and
`SHA256SUMS.txt`. The dictionary remains optional; do not embed it in the source
archive or combine it with voice/model media. The Release notes identify the
application version, dictionary data version, tested compatibility and limits.
The resource page links to that application Release.

Before publishing each application version:

1. Synchronize Python/Electron versions and lockfiles, record changes and upgrade
   requirements, and pass the final preparation commit's required CI.
2. Tag the accepted mainline commit. Wait for the tag's **Source Release** workflow
   to pass installation, build, previous-release upgrade, packaging and startup
   using only extracted sources. Download its source archive and manifest.
3. Choose the checked dictionary for this application. Run the production
   `validate_japanese_dictionary.py` gate with this application's code; verify and
   clean-install the ZIP through this application's `external_assets.py`. If the
   data is unchanged, reuse the identical ZIP only after it passes these checks.
   For corrected/expanded data, prepare a new data version and immutable filename.
4. Create a draft application Release and attach the tested source ZIP, source
   manifest, dictionary ZIP and dictionary checksum sidecar. `SHA256SUMS.txt`
   covers every attached payload (including the dictionary and source manifest).
   Record source/license notices and any experimental or unqualified behavior.
5. Verify uploaded names, sizes and digests before publishing. Download the
   public files and recheck their SHA-256, source member manifest and dictionary
   installation. Update the resource page after the application Release is live.

Do not hardcode a dictionary data version into the application or require future
code to support an old resource indefinitely. The existing asset `spec_version`
is the format compatibility boundary. A breaking pack change advances that
contract and ships a matching resource with the new application. Keep older
Release assets for their corresponding application versions; never replace a
same-named published ZIP. No recurring publication or automatic upload is implied.

Starting pair: application `v0.17.1-alpha.0`, optional dictionary `2026.10.20.3`.
The separate `japanese-context-reading` model is not part of this distribution;
its known quality failures remain disclosed and it is disabled by default.
