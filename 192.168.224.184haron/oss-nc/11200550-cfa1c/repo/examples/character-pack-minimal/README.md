# Minimal character pack

This deliberately tiny pack demonstrates one root node, weighted automatic
edges, and a zero-weight manual trigger edge. All three clips reuse a generated
2x2 KTX2 placeholder so the example remains small and passes the same validator
as a real pack.

Validate it from the repository root:

```powershell
python tools/validate_character_pack.py examples/character-pack-minimal
```

Replace the placeholder with authored KTX2 frames before using this as a visual
character. See `docs/character_pack_authoring.md` for the runtime contract.
