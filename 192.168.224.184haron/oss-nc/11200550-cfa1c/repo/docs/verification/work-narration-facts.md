# Work narration evidence and terminal facts

The observer preserves the modality of each note: a plan is intent, a pending
permission is not execution, and confirmed cancellation is separate from task
completeness. Merging notes must not promote historical plans or superseded
waiting states over a current permission or terminal fact. Explicit failed tool
results produce a diagnostic fact; missing success fields alone do not.

Codex progress fragments are scoped to each message item and consumed when that
item completes, so interleaved messages cannot combine markers and terminal
flushing cannot replay an old plan. Existing event and authorization contracts
are preserved. The observer prompt snapshot changes only for these evidence
rules.

These changes improve factual inputs to narration; they do not establish that
all model-generated phrasing is accurate. Audible speech during the downstream
Amadeus integration test was confirmed normal by the user. Pi/ACP integration
and complete continuous desktop task acceptance remain separate checks.
