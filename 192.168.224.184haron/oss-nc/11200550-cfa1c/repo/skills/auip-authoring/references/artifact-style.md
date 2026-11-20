# Amadeus artifact appearance v1

This is a generation preference, not an AUIP capability or permission. Use it
for a new app when the Host enables it. Explicit user visual requirements win;
preserve existing applications' design unless restyling is requested. A custom
design can omit the shared stylesheet. Do not manufacture style compliance.

## Visual language

Aim for a quiet laboratory instrument: dark blue-green structure, mint
interaction details, thin separators and restrained light. Keep controls in
the family while choosing the layout, geometry and content typography for the
actual task. A puzzle revolves around its board; a reader around text. Avoid a
fixed dashboard template or an extra imitation Host titlebar/connection badge.

Treat color area, saturation and brightness together. Large UI surfaces stay
dark or low-saturation neutral. Warm colors belong to small objects and
accents; avoid a large bright wood/beige/orange board. Roughly 80–90% neutral
structure is a useful composition reference, not a pixel quota. Preserve true
colors in photos, documents and data. Muted terracotta, brass and grey teal are
possible small-object palettes, not mandatory colors for every app.

Use mono type for metadata and numbers (at least 12px), readable body text
(typically 15–17px) and a 4/8px spacing rhythm. Prefer matte surfaces, 1px
boundaries and small corners. Reserve glow for a small active/focus region;
keep scanlines, flicker and glow off paragraphs and data. Respect reduced
motion. Controls should remain recognizable without color alone.

## Use and delivery

Copy [the base stylesheet](../assets/amadeus-v1.css) into the delivered app as
`styles/amadeus-v1.css`, link it with a normal stylesheet link and apply
`.am-app` to the application root. Adjust the relative URL if the entry is in a
subdirectory. This asset is intended for copying, unlike the opaque SDK.
Keep the base bytes intact; add domain layout/styles in a separate application
stylesheet. Retain its versioned filename when exporting the bundle.

The supplied classes are optional controls and type styles, not a layout
template. Override their semantic variables or add scoped domain selectors
where the content needs variation. Preserve the `--am-style-version` marker;
it lets the entry probe check that the linked base is actually applied, without
enforcing colors or typography. An upgrade of this authoring kit never updates
already generated applications in place.

The Host does not inject CSS into running apps. Disabling the generation
preference does not remove CSS from existing apps. Styling never owns AUIP
state, action identity, connection or completion facts.

## Check the result

The existing entry preflight checks stylesheet loading (including imports),
local bundle containment and application of a referenced Amadeus base. Missing
or unloaded CSS is an application delivery error. It also reports bounded
layout/focus observations as advisory information; these do not reject an app
for an intended wide surface, custom focus treatment or different art direction.

Inspect desktop and narrow layouts, test the actual primary interaction and
keyboard focus, and check text/controls against their real backgrounds. Warm
content may need a dark focus line; dark content usually needs a light one.
State geometry must match the real mechanics. Browser CSS error recovery and
a successful boot are not proof of design quality or complete accessibility.
