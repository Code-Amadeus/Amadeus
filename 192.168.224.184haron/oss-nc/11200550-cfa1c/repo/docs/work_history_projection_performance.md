# Work history projection performance

Selecting a historical WorkItem validates the current revision, changes only the
view selection, and publishes a fresh Work/Canvas projection. It must not change
workspace routing or authorize execution.

The projection path now avoids three sources of repeated filesystem work:

- A valid selection no longer prepares an extra snapshot used only by the
  missing-revision error response.
- Workspace-root matching checks resolved containment first. Git identity is used
  only for linked worktrees outside the configured roots. An empty registry denies
  without spawning Git; unrelated repositories remain denied.
- A synchronous batch of WorkReadModel rows reads shared workspace existence,
  scratch membership and Draft retention once per path. This reuse ends with the
  call. Subsequent projections recheck filesystem changes. Attempt, permission,
  completion and liveness facts remain per-item reads.

No default host, mouse-through policy or visual behavior changes. No persistent
trust cache or model/provider-specific path is introduced.

## Validation

Focused tests cover trusted containment, linked worktrees, untrusted roots,
missing/stale revision rejection, read-only projection, equivalence with single-row
projection, and directory disappearance/reappearance between calls.

Windows measurements used the real handler with a temporary SQLite ledger, one
registered Project and an existing scratch root, two warmups and 12 samples
(8 for separate directories):

| Generated history | Before median | After median |
| --- | ---: | ---: |
| 24 tasks, shared directory | 577 ms | 23 ms |
| 200 tasks, shared directory | 2073 ms | 62 ms |
| 24 tasks, separate directories | 661 ms | 158 ms |

These are handler-only measurements, not input-to-screen latency guarantees.
The fixtures have no attempts or permissions and do not represent every user
history. A local Lively integration with a temporary real ledger and generated
reports also completed selection-to-DOM checks. Physical mouse delivery and
switching the default Canvas host require separate qualification.
