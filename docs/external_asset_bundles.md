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

py -3.12 tools\external_assets.py status
```

The tool uses only the Python standard library for visual bundles. Character
bundle verification also reuses Amadeus's existing character-pack validator.
No environment variable or path editing is required after installation.

An install is all-or-nothing at the file boundary:

- the complete archive contract and every SHA-256 are checked in staging;
- identical installed files are skipped;
- one different local file rejects the whole install before anything changes;
- `--overwrite` is the explicit opt-in for replacing local assets;
- a failed commit restores files already replaced during that operation.

## Maintainer workflow

The pack definitions live in `assets/index.json`. Build only runtime inputs,
never source workspaces or intermediate PNG sequences:

```powershell
py -3.12 tools\external_assets.py build visual-runtime `
  --output output\amadeus-visual-runtime.zip

py -3.12 tools\external_assets.py build character-kurisu `
  --output output\amadeus-character-kurisu.zip
```

The `visual-runtime` bundle contains the ambient layers, subtitle frame,
scenario runtime directory, and keyboard sound. The base desktop wallpaper is
deliberately absent because it remains built into the repository.

The `character-kurisu` build first validates
`assets/spriteforge/runtime/kurisu/runtime_manifest.json`, requires KTX2-only
runtime textures, and rejects unindexed KTX2 or authoring PNG files. This keeps
the bundle identical to the renderer's actual frame index.

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
