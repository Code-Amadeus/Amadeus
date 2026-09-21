# Windows managed wallpaper lifecycle

Windows starts in wallpaper mode by default. The existing macOS/Linux startup
rules, Electron scene policy, and shared Python wallpaper bridge are unchanged.
Use `--no-wallpaper` or `AMADEUS_WALLPAPER=0` for an ordinary Windows window.
`AMADEUS_WALLPAPER_HOST=external` retains manual Lively/Wallpaper Engine hosting.

## Startup and exit

1. Electron starts its existing Python runtime and wallpaper bridge.
2. The Windows-only setup checks the existing Lively installation. If missing,
   Windows Package Manager installs the pinned official 2.2.1.0 release and its
   dependencies, with installer autolaunch disabled.
3. A new Lively profile is initialized before its first launch: no setup wizard,
   WebView2 selected, no separate Lively login startup or tray icon. Existing
   profiles are preserved. Windows permission prompts can still appear during
   dependency installation.
4. The Windows helper connects to Lively's named-pipe RPC, waits for readiness,
   saves the previous wallpaper state, writes the Amadeus URL project and mounts
   it. Only then does Electron create its existing interactive Slice.
5. Turning wallpaper off or choosing **Quit Amadeus** from the Amadeus tray
   removes the Amadeus scene and restores the previous scene. Full application
   exit restores the wallpaper before stopping the runtime. Closing the main
   window hides it; the tray's Quit command performs a full exit.

The Windows tray shows preparing, starting and restoring states. Operations
that remain in setup or restoration for more than three seconds also show a
Windows notification (subject to the user's notification/quiet-time settings).
Setup explicitly identifies that first use may download/install Lively. Failed
setup or mounting can be retried in the same Amadeus process after the external
problem is repaired; rejected attempts are not cached. A failed stop is reported
by the desktop host and resolves `false` across IPC rather than rejecting into
renderer event subscriptions.

There is no permanent Windows wallpaper replacement. Lively's optional
screenshot-to-desktop/lockscreen features are suppressed while the Amadeus scene
is active, including the startup restoration of an existing Lively profile.
The underlying Windows picture/slideshow is left intact. The original browser
is selected before recreating the previous Lively scene; its image-update
options are restored afterwards.

In per-monitor mode only the primary display is taken over. Existing span or
duplicate arrangements are preserved. Restoration uses monitor device IDs and
the original Lively project, not a screenshot of its last frame. It does not
attempt to restore a video's exact playback timestamp.

If the user selects another wallpaper or changes layout during the session,
their new selection wins. An already-running Lively is never shut down. A
Lively process started by Amadeus is shut down only after restoration and only
if no new user-selected scene requires it.

## Recovery and ownership

`%LOCALAPPDATA%\Amadeus\wallpaper\session.json` is the durable before-image.
It is written and flushed before desktop mutation. A cross-checkout file lock
prevents concurrent Amadeus sessions from overwriting it. The helper stays alive
when Electron dies; stdin EOF triggers the same restoration as normal exit.
If the helper is also killed, the next managed start recovers the journal before
taking another snapshot. Failed restoration retains the journal and reports an
error. It is never marked successful merely because a command was dispatched.

If Lively must be started with its screenshot-to-wallpaper options temporarily
disabled, `Lively Wallpaper\Amadeus.launch-settings.json` backs up just those
two launch settings until RPC can restore them. This is not a second desktop
snapshot. The `recover` command also handles an interrupted launch preparation.

Manual recovery, after closing Amadeus:

```powershell
build\windows-wallpaper\host\Amadeus.Wallpaper.exe recover
```

`recover` is an operator diagnostic/recovery command, also used by the desktop
experiments; the regular application calls `run`, which performs recovery
inline. Normal exit and a parent-only crash already trigger cleanup. If both
processes are killed, or restoration fails, recovery requires a later managed
start or the diagnostic command. There is no always-running repair service.

Individual RPC deadlines are not an overall exit/setup deadline: several
displays can take longer to restore, and an external installer may stall.
Status/notifications make the wait visible, but this implementation does not
force-kill the installer or recovery process after an arbitrary timer. A global
cancellation/time-budget policy remains a release-acceptance item.

## Build and distribution

Source runs need .NET 8 SDK on the build machine to build the helper once.
The published Windows x64 helper includes .NET 8.0.31, so end users do not need
an SDK. The Electron Windows packaging hook builds and includes the helper and
its dependency notices. It runs no Windows setup for macOS/Linux targets.
Experimental logs, profile backups and screenshots live outside the published
`host` directory and are not included in the application package.

Electron packaging writes to `electron/build`, separate from compiled input
in `electron/dist`; otherwise electron-builder excludes its own output folder
and can omit the compiled main entry. The package explicitly includes the
Windows tray icon under `resources/assets/icons/app`. A missing or invalid icon
does not abort startup, and the main window remains visible if the tray lacks
an icon.

```powershell
powershell -NoProfile -File scripts\setup_windows_wallpaper.ps1 -BuildOnly -Rebuild
# Also prepare/install the host when required:
powershell -NoProfile -File scripts\setup_windows_wallpaper.ps1
```

Lively remains a separate unmodified GPL-3.0 application acquired from the
publisher through winget. The helper talks to a version-specific RPC interface;
unsupported Lively versions fail visibly and are not silently downgraded.
An already-running Microsoft Store installation can be reused. A stopped Store
installation currently needs to be started once; setup detects it and refuses
to install a duplicate. Fully automatic provisioning currently targets the
standalone installer distribution, and requires Windows App Installer/winget.

This change does not turn the source release into a complete packaged Amadeus
runtime installer. Existing Python/model/asset provisioning remains unchanged.

## Verification

```powershell
dotnet run --project wallpaper\windows\Amadeus.Wallpaper.Tests
cd electron
npm test
npm run build
```

The Windows Electron CI job also runs the C# contract executable, builds an
unpacked Windows application with electron-builder (including `beforePack`),
and runs `electron/scripts/smoke-windows-wallpaper.cjs` against that package.
This smoke imports the packaged startup policy and tray component, creates a
real Windows tray, checks missing-icon behavior, and runs the packaged helper
in read-only `inspect` mode. It does not install Lively or replace the CI
desktop. Actual Lively mounting remains a separate real-desktop experiment.

Real desktop experiments (temporarily replace the current wallpaper and restore
it in cleanup):

```powershell
.venv\Scripts\python.exe tools\probes\windows_wallpaper_lifecycle.py
dotnet run --project wallpaper\windows\Amadeus.Wallpaper.Tests -- --cold-start-probe build\windows-wallpaper
```

The lifecycle probe requires a real renderer connection, then verifies the
original scene/options/running state after normal exit, parent-pipe loss, and
helper termination plus journal recovery. It also kills an actual parent so
both stdin and stdout readers disappear, and checks restoration still completes.
The cold-start probe backs up the
user's settings and layout, starts installed Lively with a fresh configuration,
checks that no first-run UI opens, mounts a test scene, verifies release of the
newly owned host, and restores the original profile in finally.
It is not a substitute for a clean-machine installer test.

For the complete Electron journey, run
`electron/scripts/probe-windows-wallpaper.cjs` using the Electron executable,
with stdout/stderr redirected to persistent files. Do not detach a GUI Electron
process from short-lived terminal pipes: subsequent console writes can raise
`EPIPE`. The probe launches the real backend and verifies the real before-quit
hook; it writes results to `build/windows-wallpaper/electron-experiment.json`.

Validated locally: Windows x64, Lively 2.2.1.0, one active monitor. Multi-monitor
restoration and user changes are covered by contract tests; physical multi-
monitor and a clean-machine install still need release qualification.
