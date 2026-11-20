# 0.16.0 Alpha acceptance record

Date: 2026-10-11 (Pacific/Auckland). Preparation baseline: `09aecfb`, including
merged #173 and #174. Comparison release: `v0.15.2-alpha.0`.

Release preparation uses a clean, isolated Git worktree. Uncommitted local voice
configuration, private runtime state and external media are excluded.

## Automated and source-package validation

The first preparation commit, `f6bb00c`, passed all 17 PR checks across eight
workflows: Python Windows, Electron Windows, Python Linux, Python macOS, local
model installation candidates, Windows ROCm candidate, optional character RAG,
and Source Release. The Source Release job installs locked dependencies, builds
and packages Electron, verifies native settings upgrade, and launches the
unpacked executable using the extracted source ZIP. Final publication must use
the final clean commit and its corresponding manifest/checksum.

Local checks with Python 3.12.10 and Node 22:

- Electron: **388/388** tests and TypeScript/Vite production build passed after
  the compatible dependency repairs. The existing bundle-size advisory remains.
- Full isolated Python runner executed all discovered suites. Its retained raw
  summary was 5358 passed / 20 skipped and a failing exit: `test_system_settings`
  had 17 failures and `test_tts_emotion_references` had one. Both failures came
  from test-created external-asset junctions while audio qualification was being
  prepared. The product correctly rejected paths outside the asset root.
  The junctions were removed; settings plus installed Pi runtime/web contracts
  then passed **48**, with one optional skip; emotion-reference, Codex approval
  cancellation and semantic-evidence tests passed **80**. The separate clean CI
  run passed all three full-suite shards. The failed local run is not relabeled
  as a green full run. Its aggregate collection and per-suite outcome counts do
  not exactly reconcile, so no combined final test-count claim is derived.
- Environment verifier, Ruff, generated architecture views, whitespace checks
  and third-party provenance release gate passed.
- Clean source archive: **3882 files**, zero policy errors or warnings at the
  first preparation commit. Final release hashes come from the final manifest.

## Native desktop and upgrade

- Shipping model-less Electron launch: **11/11** checks passed, covering backend
  startup, authentication, navigation, configuration visibility and clean exit.
- Actual settings from `v0.15.2-alpha.0`, with a synthetic native-encrypted
  credential, survived upgrade and reopening. No personal profile was migrated.
- Native wallpaper composer and offline startup-settings GUI checks passed;
  **13** Windows lifecycle contracts passed. The composer process emitted two
  Chromium GPU-state diagnostics while exiting; its interaction assertions
  passed. This is not a long-duration GPU or real Lively qualification.
- Extended native role journey: **17/17** checks passed. Two same-name roles
  retained distinct IDs; pending selection did not change the running identity;
  restart applied selection and edits; malformed-role recovery returned to
  Kurisu while preserving the malformed user file; Chat reconnected and owned
  processes exited. An initial local harness used the wrong role-directory
  environment variable; a second attempt overlapped the asset-junction setup.
  Both unsuccessful attempts are retained as instrument/setup failures. The
  final run used the documented directory setting and clean model-less assets.
  The six extra role assertions use a task-local extension; the ordinary shipped
  smoke alone is claimed to cover only its original eleven assertions.

## Real models, actions and device playback

- Codex App Server 0.154.0: native **allow and deny each passed 8/8** assertions.
  Allow continued the same WorkItem/Attempt and wrote the requested synthetic
  file. Deny persisted refusal, closed the permission card and left it absent.
  The first isolated attempts lacked the credential helper's explicit env-file
  binding; those authentication failures were retained. Corrected tests used
  the existing binding and disabled desktop-provider synchronization. The
  initial test's managed non-secret provider path was restored afterward.
- Real Chat and Codex Work control: **10/10** assertions passed with the project
  opened through the normal Session API. The initial request started one run;
  progress inquiry was read-only; amendment delivery was recorded for that same
  run; the final file contained exactly the amended text; WorkItem and Attempt
  identity remained unchanged. The old harness first asked for a context switch
  but never answered the resulting selection, then waited for the retired
  `steer_queued` event despite an existing delivered input receipt. These failed
  attempts remain recorded. The probe now checks the durable delivery receipt
  and actual file result. This does not certify an unattended compound
  context-switch-and-execute request or restore the obsolete event.
- Shipping Electron, DeepSeek V4 Flash and seeded AUIP Gomoku: **37/37**
  structural checks passed. The journey played a complete round through real
  player clicks and model actions, restarted, resigned, concluded, left the app
  and returned to ordinary Chat. Accepted receipts, actor/app identity, bounded
  experience history and the post-leave conversation were verified. The final
  screenshot and role replies were inspected. This run deliberately disabled
  TTS; the seeded application is a fixture, not Provider-generated software.
- Actual local GPT-SoVITS v3 checkpoint, Python 3.12.10, Torch 2.6.0+cu124 and
  NVIDIA RTX 4070 Ti SUPER: one warmup plus four short VN replies produced nonzero
  PCM written to the physical output device through the production VN bridge
  and shared speech pipeline. Existing external voice assets and the installed
  GPU environment were used with the candidate source in a separate extracted
  directory. No media or machine configuration enters the source archive.

| Delivery | First successful device write | PCM peak |
| --- | ---: | ---: |
| Whole speech body | 2.463 s | 0.349 |
| First segment | 1.962 s | 0.520 |
| First segment | 1.765 s | 0.298 |
| Whole speech body | 1.682 s | 0.353 |

The old audio probe observed only the streaming method and missed complete-audio
playback. It now observes successful writes on the actual `PyAudio.Stream` class
used by `PyAudio.open`, covering both delivery paths. The old public `Stream`
alias is a different class in the installed PyAudio. Failed instrument runs were
not counted as passes. These timestamps measure device submission, not acoustic
arrival; there is no new latency target, percentile or regression conclusion.
The maintainer deferred a matched previous-release latency comparison for this
release. Human microphone recognition and subjective listening remain unverified.

Reproduction entry points (use isolated profiles and configured credentials):

```text
python -X utf8 tools/run_tests.py
npm test                         # electron/
npm run build                   # electron/
python -X utf8 tools/smoke_electron_model_less.py
python -X utf8 tools/e2e_codex_app_server_permission.py --decision allow_once
python -X utf8 tools/e2e_codex_app_server_permission.py --decision deny
python -X utf8 tools/e2e_codex_app_server_control.py
python -X utf8 tools/probes/measure_vn_audio_latency.py --live-model-and-audio --baseline-delivery whole-speak
```

The live Codex tests need `CODEX_APP_SERVER_PROVIDER_AUTH_ENV_FILE` to refer to
the configured credential source; set `CODEX_APP_SERVER_SYNC_DESKTOP_PROVIDER=0`
for isolated tests. Never log or publish that file. The audio command plays real
audio and requires the optional GPU/voice dependencies and separately installed
assets. It must not share asset setup with a clean model-less test run.

## Dependency audit disposition

Compatible Electron tooling repairs update concurrently 10.0.5 to 10.0.6
(shell-quote 1.9.0 to 1.12.0), http-cache-semantics 4.2.0 to 4.3.0, and joi
18.2.8 to 18.2.9. Electron has no remaining high/critical audit findings, but
eight moderate build-tool findings remain in the electron-builder/global-agent/
roarr/sprintf-js chain. No force downgrade or broad audit suppression was added.

Pi remains pinned to its qualified 0.86.1 runtime. Its upstream shrinkwrap
installs brace-expansion 5.0.9, retaining the denial-of-service advisories
[GHSA-q2hr-2g5m-vwhr](https://github.com/advisories/GHSA-q2hr-2g5m-vwhr),
[GHSA-qhr7-859c-m2p7](https://github.com/advisories/GHSA-qhr7-859c-m2p7), and
[GHSA-6j4f-fj2g-mc7p](https://github.com/advisories/GHSA-6j4f-fj2g-mc7p).
The audit groups these as one high-severity vulnerable package. A root override
did not repair it. An outer lockfile-only change made the audit green while a
fresh install still contained 5.0.9; that ineffective change was discarded.
Upstream 0.87.1's inspected package also retains 5.0.9. This remains a disclosed
dependency limitation, not a clean Pi audit or a claim of unreachable risk.

Python core/development audit reported no findings after the existing narrow CI
exemption (`PYSEC-2026-1845`; the tool reported two ignored advisory identities).
The project itself is not audited as a PyPI package. Optional GPU dependencies
were not upgraded or assigned a new vulnerability-free claim.

## Evidence boundaries

Deterministic suites, native desktop startup, extracted source installation,
real-model interaction, device playback and human microphone/listening checks
establish different facts. Missing optional dependencies and manual checks are
reported as skipped or unverified, never counted as passes. Raw logs, local
paths, credentials, conversations and runtime media remain outside the source
release; only sanitized aggregate results are recorded here.
