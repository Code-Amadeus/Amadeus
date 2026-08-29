# Browser render vendor maintenance

The default SpriteForge renderer requires the checked-in PixiJS and KTX2
files. Maintainers can verify or regenerate the KTX2 files with:

```powershell
python tools/revendor_pixi_basis_ktx2.py --check
python tools/revendor_pixi_basis_ktx2.py --write
```

The Live2D files are a legacy compatibility boundary, not part of the default
renderer. A host must explicitly add `live2d=1` to the render-page query before
the page attempts to load the local Cubism Core and pixi-live2d-display files.
It must then call `renderApp.loadLive2DModel(modelUrl)` before selecting
`live2d` or `both` mode.

The public source archive does not contain Live2D Cubism Core or a Live2D
model. Supply them under their own terms. The retained
pixi-live2d-display 0.4.0 adapter declares PixiJS 6 peers, while Amadeus uses
PixiJS 7.4.3, so this path remains compatibility-only until it is exercised by
a dedicated model test.
