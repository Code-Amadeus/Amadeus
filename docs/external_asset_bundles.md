# External runtime asset bundles

Amadeus keeps its source checkout runnable without the optional visual and
character media. The repository owns the desktop wallpaper and application/UI
icons; larger or copyright-sensitive runtime assets are installed separately
at their existing paths.

## Consumer workflow

From a clean clone, install each archive supplied by the maintainer:

```powershell
py -3.12 tools\external_assets.py verify C:\Downloads\amadeus-visual-runtime.zip
py -3.12 tools\external_assets.py install C:\Downloads\amadeus-visual-runtime.zip

py -3.12 tools\external_assets.py verify C:\Downloads\amadeus-character-kurisu.zip
py -3.12 tools\external_assets.py install C:\Downloads\amadeus-character-kurisu.zip

# Optional small portraits; neither the wallpaper nor the full character pack requires this.
py -3.12 tools\external_assets.py verify C:\Downloads\amadeus-companion-kurisu.zip
py -3.12 tools\external_assets.py install C:\Downloads\amadeus-companion-kurisu.zip

py -3.12 tools\external_assets.py verify C:\Downloads\amadeus-asr-qwen3-0.6b.zip
py -3.12 tools\external_assets.py install C:\Downloads\amadeus-asr-qwen3-0.6b.zip

py -3.12 tools\external_assets.py verify C:\Downloads\amadeus-voice-kurisu-gpt-sovits-v3.zip
py -3.12 tools\external_assets.py install C:\Downloads\amadeus-voice-kurisu-gpt-sovits-v3.zip

py -3.12 tools\external_assets.py status
```

The tool uses only the Python standard library for visual and Companion bundles. Character
bundle verification also reuses Amadeus's existing character-pack validator.
No environment variable or path editing is required after installation.

The separately supplied `companion-kurisu` bundle installs small portrait atlases
under `assets/companion/kurisu`. It uses the same install/verify commands and works
without the full `character-kurisu` wallpaper bundle. Missing Lite assets leave
Companion's text/avatar fallback available. Existing PNG caches remain usable via
the explicit `AMADEUS_COMPANION_PORTRAIT_CACHE` override.

An install is all-or-nothing at the file boundary:

- the complete archive contract and every SHA-256 are checked in staging;
- identical installed files are skipped;
- one different local file rejects the whole install before anything changes;
- `--overwrite` is the explicit opt-in for replacing local assets;
- a failed commit restores files already replaced during that operation.

## Maintainer workflow

The pack definitions live in `assets/index.json`. Build only runtime inputs,
never source workspaces or intermediate PNG sequences:

For approved Companion exports, first run `tools/package_companion_character.py`
with `--candidate` and `--alternate` pointing to the reviewed small-atlas outputs.
Then build `python tools/external_assets.py build companion-kurisu --output
output/amadeus-companion-kurisu.zip`. The source/variant selection and reproducible
experiment commands are documented in [Companion Lite](companion-lite-2026-09-19.md).

```powershell
py -3.12 tools\external_assets.py build visual-runtime `
  --output output\amadeus-visual-runtime.zip

py -3.12 tools\external_assets.py build character-kurisu `
  --output output\amadeus-character-kurisu.zip

py -3.12 tools\external_assets.py build companion-kurisu `
  --output output\amadeus-companion-kurisu.zip

py -3.12 tools\external_assets.py build asr-qwen3-0.6b `
  --output output\amadeus-asr-qwen3-0.6b.zip

py -3.12 tools\external_assets.py build voice-kurisu-gpt-sovits-v3 `
  --output output\amadeus-voice-kurisu-gpt-sovits-v3.zip
```

The `visual-runtime` bundle contains the ambient layers, subtitle frame,
scenario runtime directory, and keyboard sound. The base desktop wallpaper is
deliberately absent because it remains built into the repository.

The `character-kurisu` build first validates
`assets/spriteforge/runtime/kurisu/runtime_manifest.json`, requires KTX2-only
runtime textures, and rejects unindexed KTX2 or authoring PNG files. This keeps
the bundle identical to the renderer's actual frame index.

The `companion-kurisu` build indexes only `manifest.json` and its referenced WebP
atlases (including static idle and alternate speaking variants), not the entire
authoring directory. Build, verify, install, and status share a Companion contract
validator: required normal/idle/speaking states, safe relative URLs, frame indices,
duration, calculated decoded-memory budget (16 MiB per atlas), file sizes, and
inner SHA-256 values. Image decoding/dimension checks remain in the authoring tool
and browser; consumers do not need Pillow. The 16 MiB limit is not total app RAM.

Distribute the Companion archive **alongside**, not inside, the other archives for
optional download/install. Old archives without it remain valid. No existing pack
gains a dependency on it, and an absent Companion pack is `not_installed`, not a
validation error. An explicitly selected but incomplete/corrupt pack is rejected.

For users who explicitly want everything, a combined archive is also supported:

```powershell
py -3.12 tools\external_assets.py build visual-runtime character-kurisu companion-kurisu `
  --output output\amadeus-art-with-companion.zip
```

Installation applies every pack declared in an archive; it does not prompt for
components. Use the separate Companion archive to preserve installation choice.

An existing download collection (an outer ZIP containing separate installable ZIPs)
can instead be extended without changing its original nested packages:

```powershell
py -3.12 tools\build_art_collection.py --base-collection <PREVIOUS_COLLECTION.zip> `
  --companion output\amadeus-companion-kurisu.zip --readme <UPDATED_README.md> `
  --date 2026-09-19 --output output\amadeus-runtime-art-assets-2026-09-19.zip
```

This writes a fresh collection, a verification report, and an outer SHA-256 file.
It preserves the original sharing notice and nested package bytes, reverifies
existing installable bundles, clean-installs the Lite pack into a temporary
directory, and checks the completed outer ZIP's member hashes. Lite is a separate
`04-...zip` marked optional in the collection manifest. The outer ZIP is not itself
installable. Consumers must update the application code to a version with the
Companion player/catalog before installing this new pack; existing old art packs
do not require the Lite pack or a contract-version migration.

The two voice-model packs keep the offline runtime under canonical `assets/`
paths. `asr-qwen3-0.6b` contains the complete local Hugging Face snapshot used
by Qwen inference. `voice-kurisu-gpt-sovits-v3` contains only the GPT-SoVITS v3
pretrained/runtime weights, selected v3 checkpoints, and configured reference
audio. Model and voice redistribution terms remain independent of this bundle
format and must be reviewed before publishing either archive.

## Archive contract

An archive contains only:

```text
ASSET_BUNDLE_MANIFEST.json
assets/...
```

The manifest format is `amadeus.external-asset-bundle.v1`; its JSON Schema is
`schemas/external_asset_bundle.schema.json`. The manifest declares pack IDs,
pack-contract versions, the exact member set, file sizes, and SHA-256 values.
Paths are repository-relative so installation preserves the paths already used
by `config/asset_paths.py`.

Bundle hashes provide corruption detection, not publisher authentication. A
release channel should publish the archive SHA-256 independently, and public
distribution still requires a separate rights review.
