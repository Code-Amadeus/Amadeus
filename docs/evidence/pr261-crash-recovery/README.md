# PR #261: recovery after an unclean backend exit

Baseline: `019c79bfdab664cea3a8555a42877dc362ed8abe`.

The external review's composition probe was reproduced against that revision:
an unconfirmed update plus an owner without a graceful-exit receipt blocked
both backend launch and scheduled rollback on every subsequent launch. The
normal-exit control restored the previous version. Direct installer rollback
tests had missed the production update-boundary predicate.

## Repaired contracts

- Recovery holds the desktop's stopped-root/startup mutex, verifies the retained
  wheel and native confirmation, and restores the previous package record and
  encrypted configuration. It does not require historical child-exit evidence,
  apply frontend changes, or collect either installation. The recovery scope
  cannot authorize a new installation. A currently running backend still blocks
  recovery.
- Installation and deferred file removal continue to require physical process
  exit evidence. The outside Windows Job observer now waits for the root and all
  descendants even after a crash or unsettled teardown. A durable containment
  marker, written only after successful root assignment, prevents an empty but
  never-assigned Job from becoming evidence. Missing marker, invalid identities,
  unreadable records and live descendants remain blocked.
- Graceful Provider/Work completion is diagnostic information, independent of
  local process lifetime. No Work outcome, recovery decision, lease or replay
  authority is changed by cleanup-index retirement. Existing `settled` receipts
  retain their original validation; new `exited` receipts certify only physical
  exit.
- A later Windows boot can retire otherwise unrecoverable process owners and
  unknown state. The index stores the OS-maintained BootId counter documented
  by [Microsoft](https://learn.microsoft.com/windows-hardware/design/device-experiences/oem-hvci-enablement).
  Neither wall-clock estimates nor a closed listener count as a reboot. A
  missing/backward boot identity grants no retirement. Evidence files remain
  archived. An older index without BootId first acquires the current binding
  without clearing owners; if old exit evidence is missing, Windows must be
  restarted after that adoption.
- Windows query/session-end events request best-effort cleanup without vetoing
  shutdown. A cancelled query does not leave the app in permanent quit intent.
  Crash recovery does not rely on these callbacks completing. Linux/macOS keep
  the original 2.5-second graceful-stop timeout.

## Regression coverage

The installer composition test executes the actual boundary declarations from
`index.ts`, with real store, journal, catalog and cleanup-index implementations.
It installs an update, registers an unresolved owner, reopens, schedules recovery,
and verifies repeated later launches can use the old version while maintenance
and removal remain blocked. A later injected boot permits deferred removal.
It also checks live-root rejection and retained credentials. A separate theme
test prevents record restoration from incidentally deleting either version.

Native Windows tests exercise root termination without graceful approval,
unsettled Work diagnostics, and a descendant surviving its parent both with and
without graceful approval. The descendant is left running for the negative
check and released normally for the positive check. Boot-transition tests cover
same-boot refusal, unavailable/backward identity, legacy adoption, unknown-state
retirement and fresh owners after retirement.

The shipping-app failure journey includes both normal rollback and a second
pending update whose owned backend is forcibly terminated before rollback.
The test captures the exact ChildProcess created with its isolated backend
nonce; it never searches for or terminates another process by name. The first
qualification attempt exposed a test-instrument error (the main module's
private variable was unavailable to inspector evaluation). The fixture now
captures spawn and refreshes the ESM binding; production exports no test hook.

## Validation and limits

- `cd electron; npm test`: **685 passed, 0 skipped**.
- `cd electron; npm run build`: passed configuration/SDK/Pi/TypeScript/Vite checks;
  the existing large-chunk warning remains.
- Focused Python cleanup, lifecycle and smoke-probe suite: **96 passed**.
- Python lint on changed files, JavaScript syntax checks and `git diff --check`:
  passed.
- Real offline author journey: **12 checks passed**, including native Provider
  regressions and retained/default versus explicit package-data removal.
- Full shipping-app qualification: **14 normal extension checks + 23
  failure/recovery checks passed**, including **13 app restarts**. Both the
  forced pending-update backend termination and subsequent rollback/old-runtime
  startup assertions passed. Final Electron and backend exit was confirmed.

Commands used (generated fixture paths are local to this qualification):

```powershell
cd electron
npm test
npm run build
cd ..
.\.venv\Scripts\python.exe -m pytest tests/test_backend_cleanup.py tests/test_electron_model_less_smoke.py tests/test_chat_ingress_lifecycle.py tests/test_vts_worker_lifecycle.py -q --basetemp=build/pr261-recovery-python-all
.\.venv\Scripts\python.exe -X utf8 tools/probes/extension_author_journey.py
.\.venv\Scripts\python.exe -X utf8 tools/probes/extension_catalog_journey.py --execute --store-ui --prepare-only --failure-fixture --wheels build/extension-author-journey/86129dff75f6/wheels
.\.venv\Scripts\python.exe -X utf8 tools/smoke_electron_model_less.py --report-dir build/pr261-recovery-app-final --extension-fixture build/extension-catalog-journey/220348df14c1 --extension-failures
```

The final application report is under
`build/pr261-recovery-app-final/electron_model_less_20261025T101246Z_99d7e4/`.
Raw logs are not committed because they contain local paths. The full packaged
executable publication journey was not rerun for this patch.

This is Windows qualification. Boot changes are injected into the OS-read
boundary; the real OS counter is read and checked, but no physical reboot or
Windows logoff was performed. Linux/macOS routing remains fixture-tested, not a
new native OS installation qualification. Catalog transport and native dialogs
use the existing isolated smoke fixture; plugin packages, application startup,
backend processes, process ownership and persisted state are real. No shared
dependency environment was synchronized and the worktree's junctions remain
untouched.
