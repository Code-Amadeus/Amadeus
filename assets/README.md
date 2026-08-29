# Amadeus assets

`assets/` is the stable filesystem boundary for application input. It contains
a small Git-owned bootstrap set and fixed install locations for optional asset
packs. Application code resolves these paths through `config/asset_paths.py`;
asset installers preserve the same repository-relative paths.

## Git-owned bootstrap assets

These files remain in the source repository so a clean clone can build and
start without downloading copyrighted character media:

- `images/amadeus_desktop_wallpaper.png` — the built-in wallpaper and Electron
  CRT fallback. Its existing hard references are intentional.
- `icons/app/app_icon.ico` and `icons/ui/` — Electron and renderer icons.
- asset contracts, schemas, documentation, and minimal synthetic examples.

The old root-level Kurisu PNGs, authored previews, scenario media, ambient
layers, subtitle frame, model files, reference audio, and SpriteForge runtime
textures are local/external assets. Git ignores them even though an installed
copy continues to live below `assets/`.

## External packs

`index.json` is the machine-readable pack catalog. The supported packs are:

| Pack | Installed paths | Purpose |
| --- | --- | --- |
| `visual-runtime` | ambient/subtitle images, scenario runtime, keyboard SFX | Optional wallpaper effects and activities |
| `character-kurisu` | `spriteforge/runtime/kurisu/` | Optional manifest-indexed KTX2 animation |

Both packs are optional. Without them, the built-in wallpaper, Chat, Work, and
headless startup remain available; Settings reports the missing pack instead
of treating it as an application failure.

Install a separately supplied bundle from the repository root:

```powershell
python tools/external_assets.py verify C:\path\to\amadeus-visual-runtime.zip
python tools/external_assets.py install C:\path\to\amadeus-visual-runtime.zip
python tools/external_assets.py status
```

Install the character archive with the same command. Installation is
idempotent: identical files are skipped, while different local files are not
overwritten unless `--overwrite` is explicit.

Maintainers with the complete local assets can build separate or combined
archives:

```powershell
python tools/external_assets.py build visual-runtime `
  --output output\amadeus-visual-runtime.zip

python tools/external_assets.py build character-kurisu `
  --output output\amadeus-character-kurisu.zip

python tools/external_assets.py build visual-runtime character-kurisu `
  --output output\amadeus-runtime-assets.zip
```

The archive stores repository-relative `assets/...` paths, streams large files,
uses ZIP64, and records size and SHA-256 for every member. The installer rejects
path traversal, undeclared files, symlinks, case-insensitive collisions, and
unexpected overwrites before committing files.

## Ownership rules

- `assets/` is read-only application input. Generated output belongs in
  `runtime/`, `output/`, or another explicitly writable state directory.
- The root PolyForm license covers Amadeus first-party code and modifications,
  not character, voice, model, reference-audio, or other external media packs.
- The external bundle mechanism is a transport and integrity boundary, not a
  copyright grant. Only distribute packs whose rights have been reviewed.
- SpriteForge authoring PNGs, videos, interpolation output, and QA renders stay
  in SpriteForge or another source workspace. Amadeus consumes only the runtime
  manifest, graph, mouth config, and indexed KTX2 textures.
- Model families keep their existing directories under `models/`; model files
  and reference voices remain separately supplied.
