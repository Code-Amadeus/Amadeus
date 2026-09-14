# v0.1 user-directed palette revision

The user reviewed the v0 warm board and said broad warm color areas are not
appropriate. Warmth may be small accents, or needs restrained saturation.
Current DESIGN.md v0.1 supersedes the earlier warm-board direction.

Preserve all corrected mechanics, AUIP code, payloads, layout topology, controls
and input files. Change only application CSS and the author report. Do not
rebuild the page, edit base.css or add effects/controls. There is no need to
change the AUIP integration for a palette correction.

The reviewer tried this palette on the current page; implement these concrete
visual decisions cleanly in the owning existing CSS declarations:

- Large playfield: subdued dark grey teal gradient `#12282b` → `#182e30`.
  Body text `#d8eee9`; supporting board labels `#9cb9b4`. No warm full-board wash.
- Surface edge: 1px `#31575a`; corner brackets `#648884`. Remove the broad
  cream illumination and strong glossy shadow; use a restrained inset line
  and small ambient shadow.
- Board floor: `#466460`; peg: dark neutral teal `#36514e`, `#7d9188`, `#48645e`.
  Pole labels `#b8ccc5`.
- Disks: small muted terracotta `#a77868`, medium aged brass `#b3a078`, large
  grey teal `#6f9690`. Keep true size differences. Use ~6px corners and subtle
  edge shading rather than glossy capsule/candy effects.
- Tower hover/selection: subtle light tint; selected outline/inset `#6f9690`.
  Keyboard focus must switch back to clear mint `#6dffe2` against the dark
  board (the previous dark outline was appropriate only on the warm board).
- Error feedback `#e9a8a7`, success feedback `#a5cbb7`; preserve wording,
  semantics and non-color cues. The new palette should improve these states'
  contrast as well.

Rewrite the existing color declarations; avoid accumulating a second duplicate
override block or leaving obsolete warm-board focus rules in place. Preserve
the reference/source separation. Do not search other run directories or the
reviewer's implementation. Verify the final entry at 1280px and 390px, local
move/illegal move/seven-move win/reset, and focus contrast with the new board.
Run the ordinary supplied AUIP preflights for the changed bundle. No SDK edits,
dependencies, production connections, app launch, or git commit.

Record this as a user-directed visual revision, not an autonomous first-pass
success. The previous outputs remain preserved for comparison.
