# Provider task scope and current-message context

A Provider receives one assigned task. The Host preserves its accepted source
span, Provider, Project/workspace, permissions and effect identity. Supplying
additional context does not replace that task or authorize another effect.

The parent handoff distinguishes two pieces of evidence already held by the
Host:

- The source wording selected for the assigned operation.
- The full current user message, quoted as context when it differs from that
  operation. This preserves surrounding conditions, prohibitions, references
  and sequencing, including restrictions shared by several operations.

For example, a message may request two file changes and end with “do not use
shell commands for either task.” Each Provider still receives only its own
file change as the task; both can read the common restriction in the quoted
current message. An unrelated request in that quotation is not part of the
assignment. When a condition blocks the current task, the Provider should
report the condition instead of substituting another operation.

This is a deliberate model-input change from the mainline behavior that
preferred only the selected source clause. It is separate from Chat runtime
retirement. Unlike the earlier full-source replacement experiment, it retains
the selected operation's source alongside the surrounding message. A whole
message that is already one task is not quoted twice.

No new model pass, semantic Host classifier, execution schema or metadata field
is introduced. The full current quotation keeps its original line breaks and
the existing 4000-character bound; accepted Work source text already has that
bound. Prior-conversation delivery and its receipts are unchanged.

Host contract tests verify that each task remains bound to its own source,
Provider and target; replacing the accepted task or selected source with a
sibling operation or the full message is rejected. These tests prove admission
and request integrity, not universal model obedience. A Provider can still
misinterpret natural-language context inside the workspace it is allowed to
use. Live tool/file observations are needed to assess that behavior; quoted
context must not be described as a security sandbox.

The [bounded live observations](evidence/provider-handoff-context-2026-10-07.json)
cover two separately assigned file operations, a Work assignment in mixed
AUIP/Work wording, and a condition that forbids writing. All four used the real
Codex adapter; each changed only the permitted files, and the false-condition
case changed none. Actual tool code was inspected, not just tool names. The
initial fixture-isolation failure remains recorded. No live Planner or attached
AUIP application was exercised by this boundary probe.
