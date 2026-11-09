# Extension authoring

Amadeus accepts experimental local-wheel TTS, MCP, Agent Skill and data-theme
packages. Start with a fixed Host source checkout and record its commit. The
checker below reports the actual Python/platform, Host API and SDK it uses;
it does not emulate another machine or execute plugin code.

## Choose an integration

| Contribution | Author interface | Consumer and application boundary |
| --- | --- | --- |
| TTS | Standalone `amadeus-extension-sdk` 0.1.0 | Separate Runner; select in Voice; backend restart |
| MCP | Declarative stdio/HTTP connection and native MCP | Bind compatible Work Provider; backend restart |
| Skill | Packaged `SKILL.md` and relative resources | Bind Agent; backend restart; changed recorded Skills require a new session |
| Theme | `amadeus.ui.theme/v0` data palette and PNG/JPEG | Electron theme owner; pure themes apply immediately, mixed packages on restart |

Run `profile` to see the current open/closed matrix. ASR, surfaces, installed
AUIP, context sources, observers and character/visual contributions remain
closed. A new manifest type does not create an execution owner. Main Chat does
not receive installed MCP tools or Skill bodies. Installation does not grant
an action permission; OS declarations are disclosures, not an OS sandbox.

## Desktop installation and updates

Windows queues catalog and local-wheel installations for the next app start.
Installation requires native confirmation and proof that the owned backend
process tree has exited.
Updates retain the previous package, configuration and OS-encrypted credentials
until the user keeps the verified update or explicitly rolls it back. In the
same app session, a pending update can be configured and retried: saving its
configuration invalidates prior health results, and confirmation requires health
for the exact new revision. Failed persistence, a failed backend startup, or an
unconfirmed update carried into another app session still requires recovery.
Explicitly disabling a package is preserved by rollback.

Retained-version recovery restores only package records, configuration and
encrypted credentials while the current backend is stopped. It does not delete
either installation or require a graceful-exit receipt. File deletion and new
installation still require physical process-exit evidence. Electron escalates a
failed or timed-out shutdown by terminating the registered Windows Job, after
checking the backend's creation identity and Job membership. Its outside
observer survives and can supply exit evidence once every owned descendant has
stopped. If the Job cannot be verified/opened, the fallback stops the owned
process tree and may also kill the observer; that case still needs later-boot
recovery. Unresolved Work outcomes remain in the Work ledger and
are never marked successful or replayed by plugin recovery.

Cleanup registration and its Python-root probe are maintenance prerequisites,
not prerequisites for core backend startup. If they fail, Electron reports a
warning, starts without cleanup credentials, and fences installation/deletion
for that app session. It also attempts to persist unknown ownership; corrupt or
missing-index evidence is preserved, not reset. Authenticated backend reuse has
the same isolation. The data root and its contents cannot be links/junctions;
links above that root (for example, a relocated user profile) are supported by
both desktop and backend checks.

If containment or observer evidence was lost, a positively observed later
Windows boot retires the old process owners. This uses the OS boot counter,
not the clock, a closed port or an absent root PID. Sleep, an unavailable boot
identity, and a later clean app launch do not clear unknown evidence. Existing
PR profiles without a boot binding first acquire one conservatively; if their
old exit evidence is missing, restart Windows after opening the updated app.
Use Windows **Restart**, rather than relying on shutdown/fast startup or resume,
when recovering lost evidence. Fast-startup and hibernation transitions have
not been qualified by this change. Windows shutdown/session-end cleanup is best effort; recovery does not depend
on shutdown callbacks running to completion.

Linux and macOS retain the existing version-isolated installation path, with
native confirmation at installation time. Backend contributions apply after a
backend restart; standalone themes apply immediately. These platforms do not
create the Windows cleanup ledger or a pending-update rollback journal, and do
not offer scheduled installation or retained-version recovery. Opt-in daily
catalog checks remain notification-only on every platform. This does not claim
POSIX descendant containment; the existing shutdown/cleanup behavior, including
the 2.5-second graceful-stop deadline, is retained.

## Minimal static checker environment

From this Host checkout on Windows with CPython 3.12.10:

```powershell
python -m venv build/extension-authoring-env
build/extension-authoring-env/Scripts/python.exe -m pip install --require-hashes -r tools/requirements-extension-authoring.txt
build/extension-authoring-env/Scripts/python.exe -I tools/extension_package.py profile --format text
build/extension-authoring-env/Scripts/python.exe -I tools/extension_package.py check-package examples/extensions/handoff-skill/src/amadeus_example_handoff --format text
```

Only jsonschema, packaging, PyYAML and their locked transitive dependencies are
installed. No GPU, models, audio stack or full Host installation is needed.
On POSIX use the environment's `bin/python`; a Windows-only manifest will be
reported incompatible there. Windows qualification runs on Windows with the
recorded interpreter and Host revision.

The `extension-authoring` dependency group is resolved in the Host's existing
`uv.lock`. Regenerate this export after changing that lock with uv 0.12.8; CI
checks for drift. Do not hand-edit a second set of pins:

```powershell
uv export --locked --only-group extension-authoring --no-emit-project --no-header --output-file tools/requirements-extension-authoring.txt
```

Build tools are a separate requirement: the checker environment is not a package
builder or the environment for runtime/GUI testing. Do not `uv sync` a voice
installation to the core profile merely to run these static checks.

## Build and check your wheel

Choose one of the [six source examples](../examples/extensions/README.md).
Change the package ID, distribution/import-package name, version and entry-point
metadata together. Keep the manifest directly inside its top-level import
package. Package all declared Skills, references and theme images. Use `X.Y.Z`
versions for the first preview; the Host currently compares manifest and wheel
metadata version strings exactly.

In a separate build environment with the example's `build-system` requirements:

```powershell
python -m pip wheel --no-build-isolation --no-deps --no-index examples/extensions/handoff-skill -w build/extension-examples
build/extension-authoring-env/Scripts/python.exe -I tools/extension_package.py check-wheel build/extension-examples/amadeus_example_handoff_skill-0.1.0-py3-none-any.whl --format text
```

`check-wheel` snapshots and inspects the wheel, then validates its extracted
package resources using the same `validate_installed` owner as the desktop.
It checks Skill frontmatter without importing package modules, starting a
Runner or installing dependencies. Temporary extracted resources are removed.
`check-package` checks only the manifest and package resources. It cannot check
wheel metadata: entry points, manifest/wheel version consistency or declared SDK
dependencies. A source PASS can therefore accompany a rejected wheel. Always run
`check-wheel` on the built artifact before installation. Pass the import-package
directory containing `amadeus-extension.json`, not the outer project directory
containing `pyproject.toml`.

For automation omit `--format text`; JSON is the default and exit code 1 means
failure. Existing `inspect-wheel`, `validate-manifest`, `validate-config` and
`validate-installed` helper results retain their JSON shape. `inspect-wheel`
checks wheel structure/admission; it alone does not validate all packaged
resources. Configuration JSON belongs in a file or stdin, never command-line
secret values. The checker reports missing required configuration without
printing secret values.

## Use the SDK without Host imports

Only TTS packages need the standalone SDK API. Its exact wheel and source are
recorded in [protocol-source.json](../server/extensions/protocol-source.json).
Install that wheel from `vendor/extension-sdk` into the author's build/test
virtual environment with `pip install --no-index --no-deps <wheel>`; the desktop
supplies the same pinned SDK to its private installation environment.

Use the [pinned SDK reference](https://github.com/Code-Amadeus/Amadeus-Extensions/blob/49449a0f3572e74ac7269129c02c5b9a9e685bef/sdk/python/README.md).
Plugins must not import `server`, `core`, `agent_host`, `config` or private TTS
helpers. MCP/Skill/theme packages retain their native contracts instead of
using the TTS Runner as a general execution API.

## Install and validate the actual journey

Use a separately configured development desktop. Go to Settings → Extensions
(or General → Manage plugins), install the wheel, inspect its disclosure,
configure and enable it, then bind an Agent or select Voice/Appearance.
TTS/MCP/Skill changes need a backend restart. Pure themes apply immediately;
mixed packages keep a single loaded version until restart. Changing a Skill
already recorded by a native session requires a new session.

Validate one real call, cancellation/close, an explicit new-version update,
disable, uninstall and reinstall. Check both retained data and explicitly
requested data deletion. A preparation failure preserves the installed version;
a later backend startup failure does not guarantee rollback. Never replay a
failed paid synthesis to hide a failure.

The normal desktop backend currently uses port 17777. A different user-data
directory alone does not give it a separate backend port. Do not start a second
backend over an existing instance; use the offline lifecycle probe or stop your
own test instance first. The native desktop must also have its own settings,
cache and data directories. Never copy normal user credentials into a fixture.

## What a pass means

| Result | Scope |
| --- | --- |
| Protocol-valid | Matches the pinned schema and semantic rules |
| Source check passes | This checkout/interpreter accepts the manifest and package resources; wheel metadata remains unchecked |
| Wheel check passes | The built bytes pass static wheel admission and resource checks; runtime and native checks remain separate |
| Runtime/GUI qualified | Explicitly recorded real calls, native theme validation and lifecycle journeys |
| Catalog reviewed | A particular wheel digest passed the published catalog review policy |

Static checks do not prove image decoding, installed font availability, painted
contrast, Agent discovery, paid services or catalog review. The catalog is not
yet available. First-catalog restrictions are distribution policy; a locally
supported dependency does not become invalid solely because the catalog will
not initially accept it. New types require an owner and contract, and older
clients must refuse unsupported packages while retaining compatible entries.

The application version, Host API (`0.1`), contribution contract (`/v0`) and SDK
version (`0.1.0`) are separate versions. No stable SDK 1.0 promise is implied.
Record the Host commit, OS/Python, SDK pin, package ID/version/SHA-256, commands,
and which of static/process/GUI/remote-service checks actually ran. Use
synthetic inputs and omit credentials and personal paths from shared reports.

## Maintainer validation

Existing entry points include `tests/test_extension_authoring.py`, the scoped
`test_extension_*.py` suites and Electron extension tests. The real
`tools/probes/extension_theme_lifecycle.py` uses independent environments and
SDK Runners but supplies confirmation decisions through a driver. Component
screenshots with synthetic Host responses are not native-dialog acceptance.
Pi/MCP qualification must use the target mainline including #234, not an older
working checkout. See [Host profile](extension_host_profile_v0.1.md) for contracts,
[historical evidence](extension_validation_history_2026-10-13.md) for past runs,
and [ROADMAP](../ROADMAP.md) for the next stages.

Host code remains AGPL-3.0, the standalone SDK/protocol is Apache-2.0 and authored
examples are MIT-0. Code licenses do not grant rights to external art, voices,
models or other media; document those separately.


## Explicit runtime contract checks

Use the full locked Host environment and built Electron/Pi dependencies for this
step. The minimal static checker environment does not contain runtime dependencies.
Only execute your trusted package, in an isolated checkout/profile. Local plugin
code is not OS-sandboxed. The tool does not pip-install, load normal desktop settings,
or infer credentials from a user's regular installation.

```powershell
python -I tools/check_extension_contract.py --execute --kind mcp --wheel <notes-wheel> --record <one-isolated-startup-record.json> --scenario examples/extensions/contract-scenarios/notes.json --output <report.json>
python tools/probes/extension_author_journey.py
```

The first command requires ONE record from the isolated installer's startup snapshot,
not an entire registry; its ID/version/digest/manifest must match the wheel. The
maintainer journey above produces these records under `build/extension-author-journey/`.
For themes, which deliberately have no backend startup record, that driver exports
the actual installed resource location and manifest from the same installer/store.
Do not pass a record from your daily profile. The tool uses the installed interpreter
and immutable package path, but creates new temporary data, cache and native Agent
session directories for each check. Its owned child process tree is bounded by a
wall-clock timeout and terminated on exit. No logs or tool responses are included
in the shareable report; arbitrary exception messages may contain secrets.

Kinds and scenario inputs:

- `tts`: contribution ID, text, declared language and cancellation text. Checks
  initialize/ready, a PCM stream, rejection of an undeclared language, stream
  cancellation and shutdown through the existing Runner/proxy. Remote TTS requires
  `--allow-remote` and may be billable; a second request tests cancellation, and
  neither failed request is replayed. This switch only acknowledges declared remote
  use; it is not a network sandbox for arbitrary package code.
- `mcp`: contribution ID, an existing Provider binding, and a list of synthetic
  tool calls/arguments with optional expected text. Checks native MCP discovery,
  calls and close. This direct transport check does not count as Agent routing;
  the installer journey separately exercises Notes through real Pi with approvals.
- `skill`: currently the existing `pi` binding and package-relative read paths.
  Reads the selected SKILL.md and resources through actual Pi tools, then resumes
  the unchanged native session. A controlled loopback model drives the requests;
  real LLM choice quality and other Provider qualifications remain separate.
- `theme`: empty scenario. Uses the real Electron image/font/palette owner, paints
  each theme through the production renderer theme application, switches and restores
  the built-in theme in a disposable hidden window. No Runner is started.

Reports distinguish static, process contract, native Agent, GUI and remote-service
layers. An unrun layer stays `not_run`. Failures return exit code 1 with a controlled
stage, exception class, worker exit code when available and Runner readiness state
for TTS. Timeout is a failure, not a retry. The tool does not certify installation
solely from a supplied record: use the actual installer-backed journey for that.

Existing adversarial tests cover wrong registration/handshake, illegal PCM,
late audio after cancellation, timeouts, credential projection, unbound Providers,
missing/shadowed Skills, changed/removed Skill revisions, unrelated additions,
invalid theme colors/images/contrast and native recovery. The journey additionally
runs `test_skill_pi_runtime.py` against its actual installed versions and Notes
through the native Pi path. Paid Fish/MiMo service checks remain explicitly separate.

Catalog distribution has a separate [client, mirror and security recovery contract](extension_catalog.md). Its curated restrictions do not change local-wheel or native runtime contracts. The S2 experiment includes a store browser and manual updates; a published reviewed package list and S3 release qualification remain pending.
