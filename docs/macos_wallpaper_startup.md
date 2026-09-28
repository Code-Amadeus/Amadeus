# macOS wallpaper startup

`Amadeus Wallpaper.app` is a small native launcher for the repository's
Electron wallpaper mode. The generated app records the absolute checkout path
in `Contents/Resources/project-root`; it does not contain application source or
model weights. Rebuild the app after moving the checkout.

## Build

Prepare the normal Python and Electron environments, then build the production
renderer and app bundle:

```bash
uv sync --locked --extra voice --extra vad --extra local-mps
cd electron && npm install && cd ..
./scripts/build_macos_wallpaper_app.sh --force
```

The last command compiles `scripts/launcher.c`, validates the bundle metadata,
ad-hoc signs the result, and writes `Amadeus Wallpaper.app` at the repository
root. To build elsewhere, pass `--output "/Applications/Amadeus Wallpaper.app"`.

The launcher accepts `AMADEUS_PROJECT_ROOT` as an explicit override. Otherwise
it reads the path embedded by the build script. Both paths are validated by
checking the launcher script and Electron package before use.

## Test and enable login startup

Quit another running Amadeus/Electron development instance before testing:

```bash
open "./Amadeus Wallpaper.app"
./scripts/setup_autostart.sh enable
./scripts/setup_autostart.sh status
```

The LaunchAgent uses `RunAtLoad`, so macOS starts the app after login. The app
starts Electron with `--wallpaper`, leaves the main window hidden, and starts
the wallpaper projection after the backend connects. Clicking its Dock icon
restores the main window; closing that window keeps the wallpaper process alive.
Use Quit from the application menu or Command-Q to stop the wallpaper and its
owned backend cleanly.

Logs are written below `~/Library/Logs/Amadeus/`. Disable login startup with:

```bash
./scripts/setup_autostart.sh disable
```

The launcher never kills a process selected by port. If port 17777 belongs to
an unknown or active listener, Electron reports the conflict and leaves that
process untouched.
