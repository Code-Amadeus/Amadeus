# Browser render vendor maintenance

The default SpriteForge renderer requires the checked-in PixiJS and KTX2
files. Maintainers can verify or regenerate the KTX2 files with:

```powershell
python tools/revendor_pixi_basis_ktx2.py --check
python tools/revendor_pixi_basis_ktx2.py --write
```

Live2D is an experimental, explicitly selected character backend; Sprite stays
the default. The render pages always include `live2d_character.js`, but it loads
the user-supplied Cubism Core and the vendored pixi-live2d-display adapter only
when the active character profile selects Live2D (Settings → Characters →
Appearance). See [character visuals and Live2D](live2d_character_visuals.md).

The public source archive does not contain Live2D Cubism Core or a Live2D
model. Supply them under their own terms. The retained
pixi-live2d-display 0.4.0 adapter declares PixiJS 6 peers, while Amadeus uses
PixiJS 7.4.3. Real-model acceptance on this combination is recorded in
[character visuals and Live2D](live2d_character_visuals.md); other models remain
unqualified.
