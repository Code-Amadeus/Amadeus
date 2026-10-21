# Public source release tooling

These tools prepare a reviewable source archive. They do not change product
semantics, install runtime dependencies, move assets, rewrite Git history, or
silently waive a release blocker.

Model and creative media remain separate and use `tools/external_assets.py`.
The pinned Japanese dictionary is included in application archives through the
same resource contract. See `docs/external_asset_bundles.md` and section 6 below.

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
  --dictionary-bundle <pinned-dictionary.zip> `
  --output dist\amadeus-source.zip
```

Archive entries are sorted, share the Git commit timestamp, preserve executable
mode where Git records it, and include `SOURCE_MANIFEST.json`. Existing outputs
are not overwritten unless `--overwrite` is explicit.

## 5. Historical provenance exclusions

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

## 6. Bundle the default dictionary with the application

Every application distribution includes the exact dictionary selected by
`release/japanese_dictionary.json`. Keep data in its independent versioned source
repository and immutable resource ZIP; application Git contains the pin, loader,
validators and tooling, not generated CSV data. Personal dictionaries and compiled
caches are never distribution inputs. Other model/media packs remain separate.

Before publishing an application version:

1. Accept the application and dictionary combination, update the pin if data
   changed, and synchronize application version/lockfiles. No automatic choice of
   an untested newest resource is made at installation time.
2. Build `python tools/build_source_release.py --dictionary-bundle <pinned.zip>
   --output <application-source.zip> --manifest-output <source-manifest.json>`.
   The default nine files are already installed when the archive is extracted.
   Missing or mismatched resources block application archive creation.
3. Build Electron from the extracted full source archive, or stage the pinned ZIP
   with `tools/stage_japanese_dictionary.py` before building from Git. The default
   Electron packaging hook requires the same pinned members and includes them.
4. Test fresh extraction, upgrade of the bundled default, actual native dictionary
   loading, independent update and rollback, and unchanged personal `user.csv`.
   Explicit independent selections retain precedence across application updates.
5. The Source Release workflow builds full application artifacts for tags or a
   full manual dispatch. Supply the accepted ZIP URL through `dictionary_url` or
   set the pin's approved `download_url` before tagging. Downloaded bytes must
   match the committed hash. The URL remains unset before publication approval;
   local builds can still be fully validated using the local ZIP.
6. Only after acceptance and explicit publication approval, attach the complete
   application artifacts, manifests and checksums to the application Release.
   The identical independent dictionary ZIP may also be attached for users who
   want data-only updates/rollback. Never replace same-named published payloads.

PR/developer checks use explicitly named `amadeus-code-only.zip` and
`electron-builder-code-only.cjs`; these are not complete application releases.
They do not claim to validate the private/unpublished real dictionary in cloud CI.
The local acceptance report must bind the real pinned resource to the application
head before release. Current development retains the candidate resource and does
not authorize a new application version, tag, merge or publication.

The context-reading model remains a separate, default-off experiment and is not
part of default dictionary delivery.
