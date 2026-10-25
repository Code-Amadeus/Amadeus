# Issue #264: Python wallpaper helper migration

Date: 2026-10-26 (Pacific/Auckland). Baseline:
`b6616a166925963ad6de4ab0a02f4eef2dccd657`, main after #261 merged.
This is a migration candidate, not a release qualification or merge approval.

## Owning layer and compatibility

This is a Windows integration/dependency replacement. Electron keeps its
lifecycle entry points, status and shutdown ownership. The independent helper
uses the backend interpreter selected by `pythonRuntime.ts`, runs with `-I`,
and ships with Electron resources. It imports only the Python standard library
and stays outside the backend's cleanup tree. No new package is required.

The original lifecycle state machine and #233 reporting/close limitations remain.
PascalCase journals and string phases are retained. The same-pipe receiver
identity is flushed before any Set bytes. Native creation FILETIME is converted
to .NET UTC ticks with integer addition of `504911232000000000`; a real-process
test compiles the original C# expression and asserts exact equality, then verifies
that an existing live Pending record remains fenced. Unknown identity is an
error, not process-exit evidence.

The bounded transport follows GrpcDotNetNamedPipes 3.1.0 at
`da307b551a9d56b62f5780e91a2d95a699f32430`. Synchronous call ownership replaces
the old background-connect dispatch gate. Overlapped IO is cancelled and drained
before releasing buffers. Deadlines/cancellation wait for an entered persistence
callback, and a check after that callback prevents a late first write. Persisted
Pending remains conservative even when no bytes follow. After dispatch, Cancel
is best effort and is not treated as proof that Lively stopped loading.

Settings are a sequence of original protobuf fields. Only 28/48/49 are rewritten;
every other field retains its original bytes and relative order. Tests include
noncanonical varints, duplicate fields, fixed-width values, groups, wrong-wire
unknown occurrences and proto3 false omission. Restoration reads current settings
and preserves newer user choices. Existing launch-image JSON backups also recover.

The C# projects, NuGet lockfile, .NET build hooks/CI setup and runtime-only notices
were removed after running the original baseline and live tick comparison.
The wire-reference Apache attribution/license moved with the Python resource,
and source-policy/provenance records explicitly include the helper.

## Verification

Environment: Windows x64, OS build 26200; CPython 3.12.10, Node 22.21.1,
Electron 44.0.0. Existing local Python/Node environments were reused; no optional
model or microphone was used by the desktop probes.

- Unchanged C# baseline: **50 contracts passed**, before source removal.
- Python helper contracts: **87 passed**. This includes real Windows message
  pipes, exact receiver-before-dispatch, actual journal write obstruction,
  late server availability, deadline/cancellation before and after dispatch,
  persistence spanning a deadline, large separate payloads, exclusive locking,
  old journals and the C#/Python identity comparison.
- Final Electron suite: **703 passed, 0 skipped**. Both source and packaged helper
  launches verify resource selection, isolated mode and the backend interpreter.
- `npm run build`: passed; existing Vite chunk-size warning remains.
- Windows code-only unpacked package: built without a .NET build step. Packaged
  native tray/startup policy/read-only helper smoke passed. The wallpaper resource
  directory contains only helper.py, setup, NOTICE and Apache license.
- Actual packaged model-less application: **15 checks passed**, including
  startup, authentication, navigation and frontend/backend exit.
- Actual Electron managed-wallpaper entry/quit and the `--host-exit` fault probe
  passed: the control-panel gear preserved the mounted scene; helper death
  stopped backend wallpaper ownership, removed the stale Slice, revealed the
  window and reported the error. The stop endpoint checked authentication and
  Origin, and cleanup restored the original desktop.
- Local installed-Lively lifecycle: normal exit, stdin loss, helper termination
  plus journal recovery, and actual parent death all restored the original
  reported scene/options/running state, with a live renderer connection for
  each mounted scenario. The first run exposed a missing probe report directory;
  it was fixed at the probe layer and the complete probe rerun passed.
- Fresh-settings cold start: no first-run UI, WebView2 selected, independent
  autostart disabled, newly owned host exited, original profile/layout/running
  state restored in finally.
- Real Settings RPC: sending the unchanged **539-byte** raw Settings payload back
  produced byte-identical readback. No setting values were changed by this check.
- Clean source/provenance gate at `5a279b8439f24b124c1205abb7bbaade54062bc0`:
  **4,285 selected files, 161 excluded, 0 errors, 0 warnings**; code-only ZIP
  created. Its helper bytes matched the checkout and old .NET sources were absent.
- Independently extracted sources: `npm ci`, build and Windows code-only
  packaging passed with dotnet removed from PATH. The resulting package passed
  the native tray/helper smoke and all **15** real model-less application checks.
  This used the supplied external Python runtime and locked Node dependencies;
  it does not prove that no modern .NET runtime is installed on this machine.
- Changed Python lint and diff whitespace checks passed.

Full Python coverage: **503 distinct test files, 6,703 passed, 19 skipped,
5 expected failures, 0 unexpected failures**. This was a split run, not a single
uninterrupted green command. The original per-file runner completed 289 files
before online `uv export --locked` stalled in `test_profile_contract.py`.
The owned runner tree was interrupted and its original log retained. An offline
locked export resolved in 4 ms; the profile's original 55 assertions then passed
in 5.53 seconds with `UV_OFFLINE=1`.

The task-local `build/resume_wallpaper_regressions.py` recollected the full tree,
checked test discovery and used the unchanged `tools.run_tests._run_suite` for
the 214 unfinished files. Only the profile-contract file received the offline
environment; it retained its original `--locked` arguments. The resumed runner
returned 0, and its wallpaper module ran the final **87** cases. Reconciling the
two logs confirmed 503 unique completed files with no gap or duplicate completion.
No test assertion, timeout or skip rule was relaxed for the run. The online uv
wait is an environment limitation; no upstream uv defect is claimed.

Individual totals above overlap and must not be added together.

Reproducible commands (use the existing project Python environment):

```powershell
# Historical, at the baseline before removal:
dotnet run --project wallpaper/windows/Amadeus.Wallpaper.Tests

.venv/Scripts/python.exe -m pytest tests/test_windows_wallpaper_helper.py
.venv/Scripts/python.exe -X utf8 tools/run_tests.py
npm --prefix electron test
npm --prefix electron run build
# From electron/:
npx electron-builder --config electron-builder-code-only.cjs --win --x64 --dir --publish never
# From the repository root, with the native Electron smoke wrapper used by CI:
.venv/Scripts/python.exe tools/smoke_electron_model_less.py --packaged-executable electron/build/win-unpacked/amadeus-desktop.exe
.venv/Scripts/python.exe tools/probes/windows_wallpaper_lifecycle.py
.venv/Scripts/python.exe tools/probes/windows_wallpaper_cold_start.py
.venv/Scripts/python.exe tools/probes/windows_wallpaper_counterexample.py
.venv/Scripts/python.exe tools/build_source_release.py
```

The separate false-empty/no-op-close diagnostic still reports an unreported
active scene, deleted journal, requested owned-host stop and
`strongRecoveryProven=false`. Its execution is explicitly excluded from passing
recovery counts. Migration does not fix #233.

## Distribution and timing measurement

The exact baseline was published with its original self-contained win-x64
settings, excluding PDBs as in the old package filter. Helper plus notices:
**74,922,671 bytes / 194 files**. The replacement helper plus protocol notices is
**56,451 bytes / 3 files**; the setup script is separate in both comparisons.
This removes about 71.4 MiB of uncompressed helper resources. It is not a claim
about compressed installer size or a complete standalone runtime distribution.

Read-only `inspect` startup on the same live host, one warmup then five samples:
old helper **181, 189, 195, 195, 215 ms**, median **195 ms**; Python helper
**259, 261, 269, 279, 286 ms**, median **269 ms**. These local measurements ran
alongside regression work and establish no startup speed improvement.

Source build/packaging no longer needs the modern .NET SDK, NuGet restore or a
self-contained runtime publish. First-use setup still provisions Lively through
winget if missing. Lively's own prerequisites remain its publisher's concern.
Python/model/asset provisioning remains external, as before.

## Remaining release gates

- Dependency audit is not green: the unchanged Electron lock reports **2 high,
  1 moderate** findings (Electron, undici, fast-uri); the unchanged Pi lock reports
  **1 high, 1 moderate** (undici and its dependent agent package).
  `npm audit --audit-level=high` exits 1 in both directories. Both lockfiles are
  byte-for-byte unchanged from the checked main baseline; this migration adds no
  dependency version, audit exemption or relaxed gate. Relevant high advisories:
  [Electron GHSA-jr5w-w6x2-84p9](https://github.com/advisories/GHSA-jr5w-w6x2-84p9),
  [undici GHSA-xjj6-f33q-929c](https://github.com/advisories/GHSA-xjj6-f33q-929c).
- The installed Lively 2.2.1.0 has the local null-display-name repair documented
  in #233. Its binary was not certified as an unmodified official release here.
  These real-desktop results are supplemental, not stock-Lively acceptance.
- A clean machine without a modern .NET SDK/system runtime, an unmodified
  supported Lively release, physical multiple displays and full release-asset
  checks still require qualification. The local package is explicitly code-only.
- Independent review is required before merge/replacement. No merge or release
  approval is claimed by green local tests.
- [.NET 8 support ends on 2026-11-10](https://dotnet.microsoft.com/en-us/platform/support/policy/dotnet-core).
  If the migration cannot be qualified by then, retarget the old release helper
  and CI to net10.0 separately; do not wait past the support date.
