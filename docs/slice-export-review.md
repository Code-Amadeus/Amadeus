# Slice export review — 0.15 Alpha candidate

Large text files can exceed the on-card preview budget without making an otherwise
valid Desktop export impossible. The Slice labels truncated previews and exposes a
Host-resolved action to show the complete staged file in its folder before approval.

Approval applies to the complete file's recorded size and SHA-256, not just visible
preview text. Path membership, source-byte drift, destination replacement and the exact
current permission identity are rechecked. Viewing the file does not approve or execute
it. Denial leaves the destination untouched; a changed source cannot reuse approval.

The renderer, Canvas action boundary, Work permission handler and export service are
reviewed together to preserve that contract. This is a focused UI/export PR stacked
on the routing candidate's existing Work/permission projection. It does not change
routing choices, introduce another permission owner or include the companion panel.

Tests cover oversized text and embedded image payloads, exact approval/denial, source
and previous-version drift, retained permissions after restart, and the visible card
controls. They do not claim a new packaged desktop user journey or broad visual redesign.
