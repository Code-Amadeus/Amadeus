# Runtime character-pack contract

Amadeus consumes a runtime-only character pack. SpriteForge or another authoring
tool may produce that pack, but authoring projects, PNG sequences, source video,
interpolation output, and editor layout data are not part of the runtime format.

The currently supported format identifier is:

```text
amadeus.spriteforge.character-pack.v1
```

Machine-readable schemas live in `schemas/character-pack-v1/`. Cross-file rules
that JSON Schema cannot express are enforced by `tools/validate_character_pack.py`
and by the runtime pack loader.

## Package layout

```text
character-pack/
  runtime_manifest.json
  graph_config.json
  spriteforge_mouth_config.json
  textures/
    ... .ktx2
```

All paths in the manifest are forward-slash-compatible paths relative to the
pack root. Absolute paths and `..` traversal are rejected. Runtime JSON must not
contain SpriteForge workspace paths or PNG frame references.

## Runtime manifest

`runtime_manifest.json` is the only frame index. A clip entry binds a graph node
label to ordered KTX2 frames and playback timing:

```json
{
  "idle": {
    "phase": "loop",
    "frameIntervalMs": 42,
    "loopMode": "loop",
    "frames": ["textures/idle/0000.ktx2"]
  }
}
```

Supported `loopMode` values are:

- `loop`: repeat and emit a cycle completion at each boundary.
- `once_then_hold`: play once and hold the last frame until the graph runtime
  advances or releases it.

Every graph node label must have a corresponding manifest clip. Extra manifest
clips are allowed because the backend can add bounded runtime nodes, such as
post-speech release states.

`spriteforge_mouth_config.json` may contain empty `expressions` and `profiles`
when the pack has no mouth overlays. When profiles are present, every profile
name must have an entry in `runtime_manifest.json::mouthOverlays`. Authoring-only
fields such as `root`, `phase`, `frame_names`, `closed_source`, and
`speaking_frames` are rejected.

## Graph semantics

`graph_config.json` contains only runtime topology:

```json
{
  "nodes": [
    {"id": "idle", "label": "idle", "isRoot": true},
    {"id": "variant", "label": "idle_variant"}
  ],
  "edges": [
    {"id": "idle_self", "from": "idle", "to": "idle", "prob": 0.8},
    {"id": "idle_variant", "from": "idle", "to": "variant", "prob": 0.2}
  ]
}
```

The contract is intentionally small:

- Node IDs are non-empty and unique; labels are non-empty clip/trigger keys.
- Exactly one node has `isRoot: true`.
- Edge IDs are unique and both endpoints must exist.
- One directed edge is allowed for each `(from, to)` pair.
- `prob` is finite and non-negative.
- `prob > 0` is an automatic traversal weight. Positive outgoing values are
  normalized by the runtime, so they do not need to add up to `1.0`.
- `prob = 0` marks a manual trigger edge. It is never selected by automatic
  traversal; trigger routing may use it as the first hop from its search start.

Labels are also semantic trigger targets. The runtime first looks for an
authored route from the current node, then from the root, and finally falls back
to a directly matching node label. Character-specific aliases and speaking
policy currently remain Amadeus runtime configuration rather than graph fields.

## Validate a pack

From the repository root:

```powershell
python tools/validate_character_pack.py path/to/character-pack
```

The command performs no writes. It checks JSON structure, graph identity and
edge references, path containment, manifest/graph/clip consistency, mouth
overlay references, frame counts, texture counts, and presence of indexed KTX2
files. Success exits with code `0`; an invalid pack exits with code `1` and a
stable error category.

The runnable `examples/character-pack-minimal/` package demonstrates weighted
automatic edges and a zero-weight manual edge using one small generated KTX2
placeholder.

## Authoring boundary

Amadeus does not provide a graph editor in Settings. A graph editor is creator
tooling: it should export this package contract and run the validator before
distribution. The existing SpriteForge graph editor is expected to remain a
separate authoring application rather than becoming part of the Amadeus runtime
GUI.
