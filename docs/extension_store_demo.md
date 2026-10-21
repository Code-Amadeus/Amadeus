# Local end-to-end plugin store demonstration

This is the functional-prototype acceptance path. It does not publish an index,
push a branch, approve real artifacts or change repository protection. The
maintainer side is the existing Git/CLI workflow; no separate administration
website is part of v1.

## Prerequisites and entry point

Use the experimental Host checkout without a personal .env, with its Python
core/dev and independent Electron/Pi Node dependencies installed. Build the
frontend with npm --prefix electron run build. Do not use directory junctions
to another checkout's dependencies.

The catalog toolchain requires Windows and CPython 3.12.10. Supply a real clean
Git checkout of Host commit 1840c8e4f646992ddaebea3139c8895f29743a8f and its
minimal checker environment installed from tools/requirements-extension-authoring.txt
with pip --require-hashes. The protocol tools verify the commit, checker inputs,
Python/platform and dependency-file hash themselves. Use the protocol experiment
at 153153598a9f94e3b44a477e99c3017f978d13e5 (or a separately reviewed compatible
revision); its real bootstrap remains empty.

Build reference wheels with python -X utf8 tools/probes/extension_author_journey.py,
or supply the exact Notes MCP and desktop-theme 0.1.0/0.2.0 wheels from the review
package. The demonstration snapshots these four files before serving them.

Run from the Host checkout:

~~~powershell
python -X utf8 tools/probes/extension_store_demo.py --execute --protocol-repo <protocol-checkout> --checker-host <fixed-host-checkout> --checker-python <minimal-venv>\Scripts\python.exe --wheels <reference-wheel-directory> --report-dir build/store-demo
~~~

To exercise the actual packaged Windows desktop, add:

~~~powershell
--packaged-executable electron/build/win-unpacked/amadeus-desktop.exe
~~~

The package still uses the explicitly supplied Python backend/runtime. This does
not claim a self-contained installer or clean-machine qualification.

For this experiment's existing local environments, the complete command is:

~~~powershell
& '..\qualification-20261021\fix-runtime\Scripts\python.exe' -X utf8 tools/probes/extension_store_demo.py --execute --protocol-repo '..\amadeus-extensions' --checker-host '..\qualification-20261021\pinned-host' --checker-python '..\qualification-20261021\checker\Scripts\python.exe' --wheels 'build\extension-author-journey\d151d209c26f\wheels' --report-dir 'build\store-demo'
~~~

Each invocation creates a new run directory, new desktop settings/install/data
directories, a disposable HTTPS certificate and local Git clones without remotes.
An occupied backend port is an error; the demonstration never stops an unrelated
application. Native access dialogs are automatically confirmed only in this owned
test process. The visible desktop closes when the automated journey ends.

## One connected workflow

Each phase first commits proposed inputs in the disposable repository. The
unchanged protected-base check_catalog_ci.py checks those inputs and real wheels.
A local demo-approved ref then models the reviewed-input merge. A separate
publication commit contains the output of the original build_catalog.py.
check_catalog.py checks the transition, a second build with --check verifies
identical bytes, and protected-base publication CI independently rebuilds them.
Only a successful result becomes the HTTPS server's active index.

The Windows desktop consumes those exact file bytes through its normal renderer,
preload, main-process catalog, installer and durable settings owners. Every
successful refresh compares the desktop cache SHA-256 with the generated index
SHA-256. The test transport redirects only the fixed official index request to
loopback; wheel URLs actually name the local HTTPS artifact service. Mirror
updates use that service's separate mirror path. No renderer URL/digest authority
or production catalog override is introduced.

| Sequence | Maintainer operation | Client verification |
| --- | --- | --- |
| 1 | Add Notes MCP and theme 0.1.0 | Browse/install/enable; theme applies; installed Notes passes native MCP save/read/list |
| 2 | Add both 0.2.0 wheels | Installed-view manual updates through mirror HTTPS; enablement/binding retained; fresh application process and Notes execution |
| 3 | Copyright withdrawal of Notes 0.2.0 | Distribution refused, no security decision added; existing installed Notes still works |
| 4 | Security block both exact 0.2.0 artifacts | Installation/activation refused, theme suppressed; offline backup recovery retains bans; local wheel path also blocked |
| 5 | Explicit higher-sequence unban | Both safety decisions become allowed; theme can install again; independent Notes copyright withdrawal still applies |
| 6 | Restore Notes distribution | Notes installs/runs again; replay of authentic sequence 4 refused; unban/high-water sequence survive offline NUL recovery |

After sequence 4, corrupting both settings copies must preserve unavailable safety.
An ordinary settings save or refresh cannot clear it. Explicit official recovery
restores the exact generated security decisions, without inventing lost installation
records. All damage is injected only after stopping the owned application; this is
not physical power-loss testing.

The maintenance tools also reject a hand-edited publication that differs from its
approved inputs and a snapshot that drops known safety decisions. Rejected output
never becomes the server's active index.

## Evidence and limits

The top-level smoke report must say status=passed, publication.ok=true, and report
normal Electron/backend shutdown with no renderer errors. The run includes:

- extension-publication-report.json: client assertions, observed snapshot hashes,
  exact plugin identities, fresh-process count and Notes runtime results.
- publisher/report.json: all six input/publication commits, generator identity,
  exact-rebuild results, four wheel hashes and HTTPS request evidence.
- publisher/<phase>/index.json and publication.json: original generated bytes and
  phase receipts; command-*.json records the real tools and exit codes.
- Original wheel-content inventories/diffs, native MCP contract JSON and screenshots.

The review records explicitly identify demo-author/demo-reviewer and loopback
demo-only evidence. They exercise data contracts, not real human review or GitHub
identity enforcement. An HTTPS loopback certificate is trusted only by the owned
maintenance processes and Electron download session; system trust is unchanged.
Notes calls use the real native MCP client and installed package, not a model turn.

Keep private keys, raw process logs, settings, provider files, environment directories
and temporary repositories out of a shared evidence archive. Export only the
documented result files, snapshots, synthetic input facts, wheels and screenshots;
sanitize local paths. Original index bytes retain their hashes and must not be
rewritten during sanitization.

Public release still requires independent exact-wheel/asset review, public immutable
URLs and pins, remote CI, repository governance and an explicit release decision.
Those are separate from completion of this local functional-prototype journey.
