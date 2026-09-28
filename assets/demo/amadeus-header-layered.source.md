# Amadeus header with separately processed character

Created with built-in ImageGen edits. First, the character from the
[original organization header](https://github.com/Code-Amadeus/.github/blob/fbbfc221d43d415dbe865566ccccc94625ecd1d3/profile/assets/code-amadeus-hero.png)
was processed as the separate `amadeus-character-projection.png` panel,
preserving source detail and distinguishing hair, face, headset and clothing.
The approved panel was then combined with the dotted AMADEUS wordmark and
Real-Time subtitle. After red/white and all-green color studies, the current
revision refines the original red-haired design: muted coral hair, soft pale
mint face/headset/clothing, and a cleaner dot matrix with restrained contrast.
The final prompt requests stable material color levels and even mark size,
spacing and coverage, especially across the face, instead of random highlights
or density changes. The original red-haired banner is the edit target; the
clean green study is a supporting texture reference. The green study drew on
the [resource website artwork](https://code-amadeus.github.io/assets/character.png),
which is identical to the retained `amadeus-character-projection.png` panel.
The full banner layout, text and recognizable character drawing are retained.
The separate green character panel is retained as the source reference;
superseded header concepts are not part of the published asset set.

This is brand artwork, not an application screenshot. The source character's
rights are unchanged. The existing `assets/demo/**` source-archive exclusion applies.

## Character conversion prompt

Use case: identity-preserve / precise-object-edit.
Input is the ORIGINAL RED/BLACK Code Amadeus organization banner.
Task: process ONLY its right-side anime woman as a separate character panel for later compositing. This is an image conversion of the actual source illustration, not a redraw or new character concept.
Output only the RIGHT CHARACTER PANEL on the original near-black background, approximately 1050 wide by 724 high, as if using the original banner region from just before the head to the right edge. Include the entire source head, hair flowing right, circular headset and shoulder; preserve the original panel's scale and spatial relationships. Omit all left wordmark/slogan text. No new typography.
Source fidelity is critical:
Preserve the EXACT original woman from the source: precise bowed left-facing profile and closed eyelid, angular nose-lips-chin, fringe over forehead, multi-layer sweeping long hair, circular headset's inner and outer shapes, neck and collar panels, shoulder plate, and every major original negative-space channel and horizontal fragmented run.
Do NOT simplify her into broad smooth cartoon ribbons. Do NOT expand the head or change the nose/chin. Do NOT create extra massive black bands. Retain original fine hair splits, narrow edge wisps, material panel seams, and intentional cutout geometry. Treat this as a faithful conversion of the original source shapes to a digital projection.
Convert source red and ivory surfaces into a mint-green glyph/halftone projection:
- Tiny consistently sized typographic cells: fine short dashes, small dots and miniature ASCII-like marks. Avoid the previous oversimplified pattern of big identical empty O-circles.
- Respect the source's local shape and texture detail. Use a stable sampling scale so the original drawing remains legible.
- Keep one consistent base hue/brightness within each source material region. Original shading can be translated through orderly mark coverage/density in a small number of levels, NOT noisy random per-dot colors.
- Source ivory face/headphone/collar areas read as light mint; original red hair areas read as a restrained medium jade. Deeper linework stays black.
- Original tiny irregular texture should become controlled fine dithering, not be erased into featureless flat shapes. The outcome should retain the graphic richness of the source, not become a simplified logo.
- Preserve the source's horizontal dissolving fragments, clean their boundaries and align individual marks; remove only accidental speckle noise.
This remains a TWO-DIMENSIONAL stylized anime drawing composed of luminous digital marks, not a photoreal woman, not a 3D sculpture and not a generic Matrix face. Restrained crisp phosphor edges, no blur, no fog, no extra glow pedestal. Use black negative space between marks. Do not make solid skin and overlay lines.
Keep the source silhouette and relative proportions exact. Make no inventions. Output the separate processed character panel, no left text, no browser framing.

## Composition prompt

Composite the supplied two images into the final Amadeus README banner.
IMAGE 1: the approved complete Amadeus header, with strong dotted "AMADEUS", the Real-Time descriptive subtitle, and footer. This is the base canvas.
IMAGE 2: the separately processed green anime character panel that the user has now approved. This is the FINAL RIGHT-SIDE REPLACEMENT, not inspiration for a new drawing.
Operation:
- Keep Image 1's LEFT TEXT REGION EXACTLY: all typography, dotted wordmark glyphs, colors, scale, position and footer. Do not regenerate or redesign it.
- Replace Image 1's entire right character region with IMAGE 2 as a single intact panel. Preserve the approved panel's detailed fringe/hair, closed-eye facial profile, headset cutout geometry, layered white-to-jade matrix tones, collar and shoulder seams, fragments and panel decorations EXACTLY. Do not redraw, simplify, retexture, smooth or change the panel.
- Canvas remains 2172x724 approximately, exact 3:1 ratio. The approved panel should occupy roughly the rightmost 1050 pixels (about 48% of the total width). Scale the panel uniformly to the canvas height and place it at the right edge. No nonuniform stretching.
- Preserve the character's position comparable to Image 1. Its head stays upper-right, face pointed to left, shoulder cropped by the bottom edge as in the approved panel.
- Blend ONLY the empty near-black background at the panel join to avoid a visible rectangular seam. No heavy gradient over the character. Use the panel's own delicate arc instead of duplicating two arcs.
- Do NOT change the left wordmark to smooth solid type.
Exact text retained from Image 1:
"AMADEUS"
"Real-Time Multimodal AI Agent"
"for Desktop Interaction"
"VOICE / PRESENCE / ACTION"
No CODE, no slogan changes, no new text. NO redesign of the approved character. This is a faithful two-region composite of final approved assets. Output one complete final 3:1 banner.

## Selective hair-color edit prompt

Edit the supplied approved README header with ONE precise local color change.
Output the same complete wide 3:1 banner, matching source dimensions approximately
2172x724. ONLY recolor the right-side anime woman's HAIR DOTS / GLYPHS from jade
green to restrained warm crimson / vermilion red (base around #d75a56, highlights
around #e77468). Include her crown, bangs and long flowing strands, preserving
the exact hair silhouette, black strand separations, dot grid, dot density,
layered source shading, and fine wisps. This is a selective recoloring, not a
redraw. Keep hair's brightness comparable to its original medium-green
brightness; do not make it neon bright. Her FACE, headset, neck, clothing and
shoulder MUST stay their current mint-green color. Headset-origin rectangular
signal fragments remain mint green. Background near black-green, thin green arc
and HUD marks, all left-side dotted AMADEUS typography, exact subtitle
'Real-Time Multimodal AI Agent' / 'for Desktop Interaction', and footer
'VOICE / PRESENCE / ACTION' remain completely unchanged. Preserve their exact
positioning, shapes, fonts and brightness. No red spill onto skin, headphones,
clothing, text, background or arc. Keep the same clean fine halftone dots, no
blur, no added glow or shadow, no new elements, no smoothing or simplification.
Return just the edited image without framing. This is a comparison variant to
assess selective red hair against the existing green design.

## White character edit prompt

Use case: precise-object-edit.
Asset type: Amadeus README banner.
Input: the supplied full 2171x724 PNG is the edit target. Keep its complete wide 3:1 composition.
User request: make the ENTIRE right-side character feel cleaner and fresher by changing ALL of her mint-green dots/glyphs to neutral white, while retaining her coral-red hair.
Recolor the face, circular headset (including its little inner ring), neck, collar, shoulder plates, clothing seams, shoulder circular detail, and ALL rectangular signal/dissolving fragments belonging to the character on the right to neutral soft white (main marks approximately #ededeb, brighter marks #f7f7f5). Absolutely no green or cyan tint should remain anywhere in the character or its dissolving signal fragments. Coral-red crown, bangs and flowing hair remain coral-red with the same shapes.
Make the white treatment restrained, crisp and airy: fine consistent dot sizes, black separation between dots, clean silhouettes, no green halos, no extra bloom or fog, no heavy glowing masses. Remove only accidental isolated speckle and colored fringe around the character; retain the deliberate pixel disintegration. Keep the original intricate halftone structure and dark internal seams. Do not turn dotted surfaces into solid fills, or simplify hair/headset/facial anatomy.
Preserve the exact bowed left-facing profile, closed eyelid, nose, chin, hair arrangement, headset structure, clothing, and scale. No redesign or new decorative elements.
The LEFT wordmark and all typography stay visually exactly as supplied, retaining their current colors, positions, scale and lettering:
"AMADEUS"
"Real-Time Multimodal AI Agent"
"for Desktop Interaction"
"VOICE / PRESENCE / ACTION".
Retain the dark near-black background and the restrained thin green outer arc and its left-side HUD marks; avoid added background texture or glow. Leave plenty of dark negative space.
Return just the complete banner, no crop, no frame, no explanation.

## Clean green revision prompt

Use case: precise-object-edit / style-transfer.
Asset type: Amadeus README header, complete 2172x724 wide 3:1 banner.
IMAGE 1 is the EDIT TARGET: the full README banner. Preserve its overall layout, exact left wording, proportions, character pose and recognizable source silhouette.
IMAGE 2 is the STYLE AND COLOR REFERENCE: the actual green character art used on the Code Amadeus resource website. Use its calm monochromatic mint/jade palette, fine graphic detail, black negative space and restrained brightness. Do not copy its taller framing into the banner.

The user's objection is that the red-and-white right side feels disconnected from the green left side, and that arbitrary changes in dot density look dirty. Produce ONE cohesive, cleaner all-green version:
- Recolor ALL right-side character elements, including all hair, face, headset, neck, collar, shoulder and all signal fragments, into the same restrained mint/jade family as the left wordmark. No red, coral, orange or pure white remains in the character.
- Use only two or three flat, deliberate green levels: a medium jade for the hair, a soft light mint for face/headset/clothing, subdued mint for fine accents. Maintain one constant level across each continuous material region. Do not add random brightness changes, mottled patches or gradients within a face, lock of hair, or clothing panel.
- Critically, regularize the character's dot matrix: a single fine, evenly spaced, orderly orthogonal grid with consistent small mark size and consistent coverage. Uniform pitch and density across neighboring regions; their color/brightness defines material hierarchy, NOT random changes in density. Keep substantial clean black gaps between marks so the image does not become a solid luminous mass when seen at README width. At the target resolution aim for roughly 4px center spacing and fine 1.5–2px marks, visually consistent rather than an oversized polka-dot filter. Do not reproduce the reference's incidental dot noise or irregular scattered glyphs.
- Preserve intentional large black hair separations, fine wisps, closed eyelid, nose/chin profile, headset circle, collar and shoulder seams, and intentional rightward rectangular dissolution. No anatomy changes, no invented hair ribbons, no added blocks.
- The left AMADEUS wordmark keeps its exact shape, scale and location and remains dotted; tidy only stray halos/noise so it belongs to the same crisp visual family. Keep the exact subtitle lines and typography, with a very pale mint near-white that fits the restrained green palette. Keep the original footer.
- Keep the original full composition, margins and dark negative space, with a quiet near-black background. Remove the murky green haze and unnecessary scattered noise. Outer arc and small HUD details stay thin and subordinate. No blooms, shadows, fog, scanline overlays, grain, simulated distressed print, vignette glare, new ornaments, boxes or frame.

Exact text, unchanged:
AMADEUS
Real-Time Multimodal AI Agent
for Desktop Interaction
VOICE / PRESENCE / ACTION

Goal: as calm, clean and monochromatically coherent as the green resource website image, with MORE stable dot sampling. A precise digital graphic, not a random texture effect. Keep the full original banner canvas and all essential drawing details.

## Refined coral and mint revision prompt

Use case: precise-object-edit.
Asset: the Amadeus README banner, complete wide 3:1 composition, approximately 2172x724.
IMAGE 1 is the EDIT TARGET: the original coral-haired / mint-character banner. Preserve its exact character drawing, typography, placement and overall proportions.
IMAGE 2 is a SUPPORTING REFERENCE for cleaner sampling and restrained contrast: the all-green revision. Borrow only its calmer visual texture. The final image must have RED HAIR, not green hair.

Refine the original banner rather than redesigning it. The user's goal is a cleaner face and cleaner flat color levels, while retaining the red-haired identity:
1. Hair: muted, slightly desaturated dusty coral, around #c0786c. One stable midtone over each continuous hair region, at most one restrained darker level where the original separation needs it. No bright orange, pink-white highlights, random sparkling dots, mottled patches or gradient shading. Preserve every main hair split, fringe, wispy edge and flowing strand.
2. Face, headset, neck, collar, shoulder and signal fragments: a unified soft pale mint around #b2d5c1, with at most one quieter mint tone for secondary details. Especially on the FACE, use a calm uniform field of marks. No whitening the cheek, no bright noisy forehead/chin patches, no random green-to-white variation.
3. CLEAN DOT MATRIX is the main work, not merely changing hue. Make marks small, crisp and evenly spaced on an orderly grid. Maintain constant dot size and pitch, with consistent coverage across the interior of each material. Use color levels and the original black linework to define form, never random stippling or arbitrary local density changes. Clear black air between marks; avoid dense surfaces that merge into a luminous slab at README display width. Do not turn the face into a solid fill, or use giant dots. Do not draw artificial concentric contour bands or fake shading gradients.
4. Keep facial anatomy, closed eyelid, nose, lips, chin, circular headphones, seams and black negative-space channels intact. Retain the intentional horizontal dissolving fragments, but remove incidental isolated speckle and fuzzy colored halos.
5. Preserve IMAGE 1's entire left text region EXACTLY: dotted AMADEUS shape, its scale and location; subtitle font, line breaks, spacing and positions; footer. The right-side mint should harmonize with the wordmark, and should not overwhelm it.
6. Keep the quiet near-black green background, fine arc and sparse HUD details. No added grain, glows, fog, noisy texture, bloom, shadows, decorations or frames.

Exact unchanged text:
"AMADEUS"
"Real-Time Multimodal AI Agent"
"for Desktop Interaction"
"VOICE / PRESENCE / ACTION"

Final palette: restrained dusty coral hair + soft pale mint character and wordmark + near-black negative space. A precise, calm digital graphic. Return one complete final banner.
