# Companion prototype validation

## September 11 incremental multi-display update

The [multi-display milestone](companion_multidisplay_update.md) was integrated into the public prototype branch independently of the daily development checkout. Results below were rerun on this integration, on Windows:

| Check | Result |
| --- | --- |
| Electron production build | Passed |
| Complete Electron unit suite | 116 passed, including the existing startup opt-in, small-display and Wallpaper tests |
| Targeted Python tests | 21 passed: window following, disabled observer, existing desktop observer and local bridge security |
| Ruff on changed Python source/tests | Passed |
| Compiled-renderer dual-display fixture | Four reports passed: cross-display dragging and reload, independent readers, family dragging, editing and return menus; primary 2560×1440 and secondary 1080×1920 |
| Default settings | Existing opt-in source/startup, muted reminder default and source-navigation allowlist retained; no audio backend changes |

The fixture finished at 2026-09-11 15:38 UTC. Its own profile and reports stay under ignored `runtime/companion-preview/`. Its pointer input is injected through Chromium; this is not a claim that every OS interaction, external side-chat source, audio device or mixed-DPI layout was exercised. Actual user confirmation of uninterrupted Codex voice input belongs to the related daily version, as described in the milestone notes.

The full Python suite, dependency audit and model-less startup smoke below are historical September 7 results; they were not all rerun for this UI-scoped update. No default-branch merge or release tag is part of this milestone.

## September 7 initial prototype qualification

Validated on Windows on 2026-09-07, after integrating the prototype with upstream `afe0e74552be5faf9041d6f488d9e87b329cf8e3`. This records local, CPU/model-less qualification of the experimental source in Issue #61. It is not a GitHub Actions result or qualification of every optional model tier.

The isolated environment used CPython 3.12.10, uv 0.12.8 and Node.js 24.19.0. The upstream Windows workflow pins Node.js 22.21.1; that exact Node version was not exercised locally.

| Check | Observed result |
| --- | --- |
| Locked Python environment | `uv lock --check --offline` and `uv sync --frozen --extra dev --python 3.12.10` passed; 107 packages installed |
| `python tools/verify_python_environment.py --profile ci` | Passed; no broken requirements |
| `python -X utf8 tools/run_tests.py` | All 224 suites passed; runner reported 1,859 passed and 26 skipped |
| `python -m ruff check .` | Passed |
| `python tools/architecture/generate_views.py --check` | Seven views checked, passed |
| Python dependency audit | 107 dependencies checked; no reported vulnerabilities, using the existing CI exception `PYSEC-2026-1845` |
| `npm ci` | Passed |
| `npm test` | 83 passed, including additional display-placement regressions |
| `npm run build` | Passed; existing bundle-size advisory remains |
| `npm audit --audit-level=high` | Zero reported vulnerabilities |
| `python tools/smoke_electron_model_less.py` | Passed: backend startup/authentication, chat and backend connections, settings/optional-asset UI, unavailable-runtime visibility, project/artifact navigation, and clean Electron/backend exit; no page or console errors |
| `python tools/check_third_party_provenance.py --release-ready` | Passed, zero errors; six unmatched optional-component warnings |
| Strict source release check | Passed on the committed, clean tree: 1,057 selected files, 12 policy exclusions, zero errors or warnings; no dirty-tree override |
| Contribution privacy review | All 56 changed text files reviewed for known personal labels, local task IDs, credentials and private paths; no unresolved findings. Fictional test paths and runtime environment interpolation were reviewed as benign |
| Live local observer read | The public branch's observer read the current preparation task from the installed local Codex database/rollout: one task returned without observer errors; no model requests or source writes. This is an adapter check, not the complete UI/speech journey |
| Display placement contracts | Single-screen compact placement, small/scaled logical work areas, taskbar offsets, negative coordinates and simulated preferred-display removal/reconnection passed. A fixed minimum-size overflow and secondary taskbar overlap were reproduced before the fix. This does not simulate an OS hot-plug event or establish visual acceptance |

## Comparison with the daily development checkout

The source snapshot was compared with local `develop` at `85dc34302176ac56f4a6aac5addcc4c8200a380a`. Within the 56 contribution files, 37 have identical Git blobs, 16 existing files differ, and three are new. Identical files include the task cards/layout/styles, task projection, reminder queue, Codex observer and its handler, notification translation/streaming and core TTS changes.

The remaining differences account for upstream integration (including chat/wallpaper events), the single-`.venv` runtime convention, explicit opt-in defaults, lazy audio observation and audio-event forwarding, work-area-constrained display placement and size limits, privacy-cleaned fixtures, documentation and related tests. The whole repositories also differ because this snapshot incorporates newer upstream work. They are not byte-for-byte copies, and the contributor's private settings/assets are not part of the public source.

Automated checks above apply to the prepared branch. Identical source components and a passing adapter read do not prove that every visual, source-navigation or real voice interaction behaves identically to the daily installation; those acceptance limits remain listed below.

The full suite includes regression coverage for disabled observer access, task projection, reminder acknowledgement/queue behavior, speech cancellation and playback receipts. A final integration fix forwards both busy and clear `companion.audio-activity` events to the client; its WebSocket regression test passed in the full run.

## Reproduce the qualified environment

The normal CI installation command remains `uv sync --locked --extra dev`. During local preparation, online lock verification stalled fetching metadata from an unselected optional GPU wheel. The following commands validated the existing lock offline and installed its selected development environment without changing `pyproject.toml` or `uv.lock`:

```powershell
uv lock --check --offline
uv sync --frozen --extra dev --python 3.12.10
.\.venv\Scripts\python.exe tools/verify_python_environment.py --profile ci
```

The first full-suite run encountered Windows error 206 in the unchanged AUIP authoring test because the user-profile temporary path was too long. A second, complete run passed with a short process-local temporary directory. No application or test-runner workaround was added:

```powershell
$testTemp = 'D:\Temp\amadeus-tests'
New-Item -ItemType Directory -Path $testTemp -Force | Out-Null
$env:TEMP = $testTemp
$env:TMP = $testTemp
$env:TMPDIR = $testTemp
$env:AMADEUS_E2E_NO_TTS = '1'
$env:TTS_DEVICE = 'cpu'
.\.venv\Scripts\python.exe -X utf8 tools/run_tests.py
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe tools/architecture/generate_views.py --check
.\.venv\Scripts\python.exe -m pip_audit --local --progress-spinner off --ignore-vuln PYSEC-2026-1845

Push-Location electron
npm ci
npm test
npm run build
npm audit --audit-level=high
Pop-Location

# Requires port 17777 to be free; close an idle existing desktop first.
.\.venv\Scripts\python.exe tools/smoke_electron_model_less.py --report-dir runtime/electron-smoke
```

The original daily desktop was closed normally for the isolated smoke test and restored afterward. The smoke used its own settings profile and did not send a chat request or invoke a model service. Its success does not establish Companion's complete interactive acceptance.

## Final source check and limitations

The strict source check passed on the committed, clean tree. Its local manifest identifies the exact commit. Reproduce it as follows; do not use `--allow-dirty-check` as release evidence:

```powershell
.\.venv\Scripts\python.exe tools/build_source_release.py --manifest-output runtime/source-release-final.json
git diff --check origin/main...HEAD
git status --short
git log --oneline origin/main..HEAD
```

Raw logs and manifests remain local under ignored `runtime/` because they can contain machine paths. The contribution contains source, tests and sanitized documentation; it does not add private runtime records, credentials, demo recordings, character assets or model weights.

Skipped tests reflect absent optional model/voice/creative packages or platform-specific capabilities. Actual model speech, complete observer/source navigation, real meeting-audio avoidance, normal click-through, all display/DPI arrangements, GPU tiers and macOS/Linux behavior need further qualification. The demo in Issue #61 predates this upstream integration and does not substitute for these checks.
