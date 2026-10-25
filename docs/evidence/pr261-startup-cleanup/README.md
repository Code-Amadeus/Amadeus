# PR #261: isolate maintenance failures and preserve the exit observer

Follow-up to external review of `c7a0c1a0`, validated on Windows on 2026-10-26.

## Problems and owning layers

1. Desktop startup awaited cleanup registration, reused-backend marking and the
   Python code-root probe without isolating errors. A zero-filled index, receipts
   without their index, or a failed probe could prevent the ordinary backend from
   starting. Both cleanup implementations also rejected junctions above the data
   root, although extension storage supports relocated parent directories.
2. Electron's `taskkill /T /F` escalation killed the backend's outside-Job observer
   along with the backend. The earlier `child.kill()` crash fixture did not cover
   that path. Missing exit proof then required Windows restart before maintenance.

## Changes and preserved boundaries

- `launchBackend` isolates only cleanup setup failures. It reports the warning,
  omits cleanup launch credentials, fences maintenance for the current desktop
  session and attempts to persist unknown ownership. Damaged state is preserved;
  foreign-backend, authentication, extension-validation and readiness failures
  retain their existing behavior. Ready/assembling authenticated backend reuse
  uses the same error isolation. POSIX does not run the Windows setup/probe.
- An interrupted write-through index replacement cannot make a previously empty
  index look clean to a later desktop: uncommitted index transactions also fence
  maintenance. No damaged or missing-index state is reset to fresh.
- Desktop and Python checks reject links at or inside the data root, and accept
  parent-directory junctions. Receipt paths are checked against the same root.
- Each owned ChildProcess retains its own registered launch identity. Escalation
  calls the existing cleanup module's explicit Job-termination entry point. It
  checks registration, containment marker, PID creation identity and Job
  membership before terminating that Job. The observer stays outside it.
- An unavailable/invalid Job falls back to the existing owned process-tree stop,
  with a warning. That fallback may kill the observer. Neither termination method
  itself authorizes package deletion: independent exit evidence is still required.
  Work outcomes, rollback boundaries and Linux/macOS shutdown are unchanged.

## Reproducible validation

From `electron/`:

```powershell
npm test
npm run build
```

- **701 tests passed, 0 skipped.** New startup tests execute the actual
  `index.ts` launch/stop functions with the real cleanup store, covering zeroed
  and missing indexes, registration/probe failure, retry fencing, reused and
  foreign backends, launch-identity binding and POSIX routing.
- Native Windows tests exercise both a rejected shutdown request and the actual
  15-second graceful deadline, with a live child in the Job and an unrelated
  process outside it. The observer publishes `exited`, the ledger verifies, and
  the unrelated process remains alive. Wrong process creation evidence is
  rejected before termination. The parent-junction case also runs the real
  Python tracking module, observer, Job terminator and desktop verifier.
- Build passed: config ownership, SDK provenance, Pi type-checking, TypeScript
  and Vite. The existing Vite bundle-size warning remains.

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_backend_cleanup.py tests/test_electron_model_less_smoke.py tests/test_chat_ingress_lifecycle.py tests/test_vts_worker_lifecycle.py -q
.\.venv\Scripts\python.exe -X utf8 -m tools.probes.backend_cleanup_startup
.\.venv\Scripts\python.exe -m ruff check server/backend_cleanup.py tools/probes/backend_cleanup_startup.py
git diff --check
```

- **96 Python tests passed**, changed Python lint and diff checks passed.
- The startup probe runs the shipping built Electron app and normal authenticated
  Python backend in four isolated profiles: ordinary, zero-filled cleanup index,
  missing index with retained receipts, and a junction above the user-data root.
  Each runs the existing 15-check model-less journey (startup, authenticated
  runtime status, GUI navigation, settings and final Electron/backend exit), plus
  preservation/verification of cleanup evidence. The fixture changes only
  isolated state before launch; it does not mock the product's startup logic.
- Synthetic fixture logs/reports remain in ignored `build/pr261-r3-*`; no user
  credentials or raw process logs are included in this evidence note. The probe
  removes only its own junction after verifying the exact target. The checkout's
  existing dependency junctions are unchanged.

## Limits

No physical shutdown, logoff, fast startup, hibernation, or OS reboot was performed.
Use Windows Restart for lost-evidence recovery; fast-startup/hibernate transitions
remain unqualified. Native Linux/macOS application journeys and the full packaged
publication journey were not rerun locally. The cancelled-shutdown query behavior,
startup catalog networking, performance observations and pre-existing dependency
changes are outside this focused follow-up. No dependency, CI workflow, timeout,
test skip, review approval or merge policy was changed.

## CI follow-up on 2026-10-26

The first hosted run of `3413e883` exposed a test-fixture integration mistake:
`backendLaunch.test.mjs` simulated Windows in the extracted desktop functions,
but omitted the platform argument when calling the real cleanup module. On a
Linux runner that module correctly chose its POSIX no-op route, so the zeroed
index, missing index and successful-registration tests failed. The fixture now
explicitly supplies `win32`, as the existing cleanup protocol tests do. The
assertions and separate Linux/macOS routing cases are retained. The focused
startup suite passes locally (10 tests, 0 skipped); native Linux validation is
performed by fresh hosted CI.

Two Windows jobs also failed before project tests at `actions/setup-dotnet@v5`.
The `8.0.x` lookup resolved SDK **8.0.427**, but its `aka.ms` redirect targeted
`dotnetorigin.blob.core.windows.net`, which returned **409: public access is not
permitted**. Retrying reproduced the failure. Microsoft's release metadata and
the public `builds.dotnet.microsoft.com` URL confirm the same SDK is available.
The three Windows setup steps now pin **8.0.427**, rather than changing SDK
generation or using another distributor. The official action installer's
`-Version 8.0.427 -DryRun` resolves the public URL, whose HEAD request returns
200. No project test, assertion, timeout or failure gate is removed.

Sources checked: [setup-dotnet installer](https://github.com/actions/setup-dotnet/blob/v5/src/installer.ts),
[bundled installation script](https://github.com/actions/setup-dotnet/blob/v5/externals/install-dotnet.ps1),
[Microsoft .NET 8 release metadata](https://builds.dotnet.microsoft.com/dotnet/release-metadata/8.0/releases.json).
