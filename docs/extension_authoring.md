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

Regenerate the hash lock with the project's uv 0.12.8:

```powershell
uv pip compile --universal --python-version 3.12.10 --generate-hashes --no-emit-index-url --output-file tools/requirements-extension-authoring.txt tools/requirements-extension-authoring.in
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
`check-package` accepts the import-package directory containing the manifest,
not the outer project directory containing `pyproject.toml`.

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
| Host static check passes | This checkout/interpreter accepts the manifest and checked resources |
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
