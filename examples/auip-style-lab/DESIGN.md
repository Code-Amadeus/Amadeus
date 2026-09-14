# Amadeus artifact visual language — v0.1 experiment

Status: experimental authoring guidance, 2026-09-13. This is not a production
default, AUIP protocol extension, or accepted product contract.

Revision v0.1 incorporates the user's visual review: broad warm/light surfaces
overpower Amadeus. Keep warmth small in area, or substantially reduce its
saturation. The frozen v0 guide remains inside the first two run workspaces.

## Intent

An Amadeus artifact should feel like an instrument inside a quiet laboratory:
precise controls, restrained light, and an expressive subject. Recognition must
survive removal of the Preview frame. Different applications must retain
different layouts and content palettes.

The existing sources are `electron/src/renderer/styles/workPreview.css` and
`render/web/crt_canvas_surface.js`. Extract their visual relationships; do not
copy their tiny desktop-overlay typography into application content.

## Three levels of freedom

1. **Host frame:** the real Preview/Slice owns window chrome, connection,
   permissions and participation facts. Do not imitate another window inside
   the application or invent an Amadeus connection/actor indicator.
2. **Application controls:** use the supplied base variables and opt-in classes
   for buttons, inputs, focus, labels and separators. Keep the charcoal/teal
   ground, mint accent, thin lines, mono metadata and restrained illumination.
   Organize controls around the task; no prescribed page layout.
3. **Subject:** freely design the board, illustration, chart, document, canvas,
   or game world. Give it a deliberate domain palette, geometry and typography.
   Mint is an interaction accent, not a requirement for every data series/object.
   Large subject surfaces stay dark or low-saturation neutral. Warm hues belong
   primarily to small objects, annotations and accents. Do not fill a whole
   board with bright wood, orange, beige or amber just to signal freedom.

## Concrete rules

- Ground `#07171b`, surface `#0c2429`, primary text `#d8eee9`, supporting text
  `#9cb9b4`, mint accent `#6dffe2`. Use `base.css` variables as the source.
  App success/warning/error have both text or shape and color. Existing Host
  status colors remain owned by the Host; this experiment does not redefine them.
- Treat area, saturation and lightness together: high area means lower chroma
  and usually lower lightness. A useful composition target is roughly 80–90%
  dark/neutral structure, with domain colors in the remaining small areas;
  this is a visual guide, not a pixel quota or runtime validation rule. Warm
  accents should normally occupy less than 10–15% of the main application view.
  Documents/photos/data with intrinsic color may keep their true content;
  the surrounding UI still follows this balance.
- Prefer matte, muted domain hues to glossy candy colors. Example small
  objects: terracotta `#a77868`, aged brass `#b3a078`, grey teal `#6f9690`.
  Example large subject ground: `#12282b` to `#182e30`. These are illustrative,
  not mandatory colors for every application. Preserve shape/size differences
  so desaturation does not make game objects ambiguous.
- Controls have 1px borders, 6–8px corners, at least 36px height; touch layouts
  use 44px. A larger content surface may be square or have a small radius.
  Corner brackets are optional and should identify one focal region at most.
- Use a 4/8px spacing rhythm. Main content needs breathing room (typically
  24–40px desktop padding). Responsive layouts must work at 390px and 1280px.
- Metadata/numbers may be monospace, 12px or larger. Ordinary Chinese body text
  should be 15–17px, line height at least 1.5. A title may use a different font.
  Do not use heavily tracked Chinese paragraphs or make every label uppercase.
- Focus must be visible with a clear outline. Hover is subdued; one primary
  action may have a mint fill. Disabled controls remain identifiable. Do not
  remove keyboard operation, native input behavior, or text selection.
- Glow only marks a small active/focus region. No glow on paragraphs, no
  scanning overlay across text/charts, no continuous flicker, no stacked CRT
  effects. Base motion is 120–180ms; honor reduced motion.
- Make hierarchy through position, spacing, weight and restrained separators.
  Do not default to a hero banner plus a grid of unrelated rounded cards.
- Domain graphics must tell the truth. Counts and statuses follow real app
  state. Demo data is labeled. No fake telemetry, connection or participant claim.
- An app-specific decorative variation must not override focus, readable
  contrast, disabled state or layout responsiveness. Aim for WCAG AA text
  contrast (4.5:1 normal text, 3:1 large text) and 3:1 focus/control boundaries
  where they are necessary to identify the control.

## Delivery and boundary

Use `base.css` locally, scoped to `.am-app`. Add app-specific styles separately.
Do not inject styling from Host into arbitrary documents. Ship the exact base
CSS bytes with the application bundle; old bundles must not change when the
authoring kit evolves. This kit carries no AUIP state or permission behavior.
Explicit user visual requirements take precedence over this default guidance.

The reference is a spectrum instrument, not a layout template. A puzzle should
organize around its board, a reader around text, and an instrument around its
measurement. Do not reproduce the reference's column arrangement by habit.

## Experiment and acceptance

First inspect the hand-built reference. Freeze this guide and base stylesheet
before the native Provider run. Give the Provider a different task (Hanoi), this
guide and base CSS, without the reference's HTML/CSS/JS. Preserve its raw output;
record any revision separately so first-pass fidelity remains visible.

Assess each dimension 0 (absent), 1 (partial), 2 (clear):

1. Family resemblance without Host frame.
2. Task-specific composition and a distinct content palette.
3. Legibility, focus, responsive layout and reduced motion.
4. Restrained light/texture, coherent hierarchy, no redundant chrome.
5. Working primary interaction and truthful visible state.

Target: at least 8/10, no zero; additionally require primary-loop checks and
no major overflow/console errors. Report design judgment separately from
automated evidence. One sample is a feasibility experiment, not proof of
reliable generation across models, tasks, repeated runs or real Host Attach.
