# Experimental catalog client and security recovery

S1 supplies the desktop transport, installer integration and durable security boundary.
S2 adds discovery, search, version details, download progress and manual updates in the
existing plugin manager. This remains an experimental client, not a published store.
The official catalog and its independent reviews must be published before this client
can provide a public store. The current S0 reference index contains no reviewed packages.

## Authority and compatibility

The only index is `https://raw.githubusercontent.com/Code-Amadeus/Amadeus-Extensions/main/catalog/index.json`.
Its repository permissions and HTTPS delivery are the trust root; this version has no
independent index signature. Renderer IPC selects an action and, where needed, a package
ID and version. Main resolves the URL, digest, review and compatibility. Cache data can be
shown offline but cannot authorize installation: a store install first refreshes the
index. Local installation remains available offline while local security state is intact.

`server/extensions/catalog-source.json` pins an unmodified copy of the S0 policy, schema
and compatibility fixtures in `vendor/extension-catalog/`. This is independent of the
unchanged manifest and SDK pins. Python owns catalog structure/policy and environment
compatibility. Unknown contribution kinds/contracts and mixed unsupported packages are
incompatible as whole entries; other entries and valid security decisions remain usable.
An unknown catalog format or malformed safety structure rejects the complete new index.

A catalog artifact is identified by package ID, version and wheel SHA-256. Installed
`record.digest` already has that meaning. Review and withdrawal status are projections
of that identity, never a persisted source or trust label. Cached review information is
historical; lack of a known ban is not certification that a package is safe.

The curated policy (X.Y.Z, py3-none-any, SDK-only dependencies, restricted MCP commands)
is separate from general local-wheel admission and native execution contracts. After
copying a downloaded wheel into the existing installation transaction, the installer
rechecks its identity and catalog policy. Security/admission are checked before
preparation and again after confirmation and before persistence. Cancellation during
preparation prevents commit; an already running preparation command must finish or be
confirmed stopped before its files can be removed. No automatic rollback is promised.

Both entry points select installation mode from effective wheel dependencies evaluated
by the installing Python interpreter. Resource wheels are extracted. Python wheels
with no effective dependencies or only the supported SDK install offline with
`--no-index --no-deps --only-binary=:all:`. Third-party dependency resolution stays
available only for local installation. No package code is imported by static checks.

## Downloads and mirrors

Desktop requests use Electron net in a separate, nonpersistent session with credentials
omitted. Chromium retains its normal system proxy and certificate checks. Every redirect
must be credential-free HTTPS; at most five redirects, a 120-second total request deadline,
4 MiB index limit and 256 MiB wheel limit apply. Streamed decoded bytes are bounded too;
compressed Content-Length is not mistaken for decoded size. The official digest is
checked by the installer, not by net's ignored `integrity` option.

In the plugin manager, **Artifact mirror base URL** is the existing desktop setting
`AMADEUS_EXTENSION_ARTIFACT_MIRROR`. Empty uses the original wheel URL. A configured
HTTPS directory serves `<base>/<sha256>/<original-wheel-filename>`, for example:

```text
https://mirror.example/extensions/<64-lowercase-hex-digest>/example-1.0.0-py3-none-any.whl
```

The base must not contain credentials, a query or fragment. Mirrors provide bytes only;
there is no index mirror setting. Incorrect mirror content fails static inspection or
identity verification before pip. No silent original/mirror fallback occurs.

Downloads use private staging directories and clean up cancellation/failure. On the
next request after a desktop restart, the client reclaims interrupted directories with
intact download ownership and only expected regular files. Unknown ownership, links and
unexpected contents are retained. The existing desktop single-instance lock owns this
staging root; active downloads in the current process are excluded from collection.

## Durable security and recovery

`settings.json` and its existing `.bak` hold one optional `extensionSecurity` snapshot:
maximum accepted sequence, complete decisions, check time and a content checksum. The
checksum detects accidental corruption; it is not a signature or protection against
local administrative edits. Sequence and decisions always move together through the
existing fsync/atomic replacement writer. The display cache holds neither execution
permission nor rollback protection.

- Read both settings copies and select the highest complete security snapshot. A newer
  complete backup wins even if the primary parses. The normal settings owner still
  prefers readable primary values and encrypted credentials.
- Same-sequence conflicting decisions enter a durable unavailable state. A lower index
  cannot replace accepted facts; omission cannot remove a known decision. A changed
  decision needs a later publication sequence. Explicit higher-sequence `allowed` facts
  lift a ban; uninstall/reinstall or cache deletion do not.
- A damaged field can recover from an intact copy. If no complete security snapshot
  survives, preserve damaged bytes as `.corrupt-*` evidence and durably persist
  unavailability. Ordinary settings saves and restarts cannot turn this into an empty
  ban list. A remembered sequence floor is retained when a conflict can establish one.
- While unavailable, reject new installs and activation, suppress frontend extensions,
  preserve builtin functionality and removal/recovery operations. Existing backend
  execution is reported as still running and needing stop/restart; it is never falsely
  reported as stopped. Desired and actually loaded identities are checked separately.
- **Recover official extension security state** explicitly fetches the sole official
  index. It cannot bypass a known sequence floor. With both settings files destroyed,
  this recovers security facts, not lost package records, configuration or credentials.
  The client does not promise recovery after all local evidence is deliberately deleted.

The writer persists backup before primary. An error between the two writes does not
publish a successful refresh, but the complete newer backup is durable and must govern
the next read. It cannot be overwritten by an older primary during an ordinary save.
Fault sentinels also survive an interrupted write; incomplete recovery stays blocked.

Maintainer withdrawal and copyright withdrawal stop store supply only. A security ban
blocks the exact identity through local and store entry points and future activation.
It does not delete package data, credentials or saved enable intent, and cannot recognize
unknown modified bytes as the same artifact.

## Browsing and manual updates

Use **Installed** to configure, enable, bind, disable or remove packages. **Browse** shows
the official catalog with name/description/capability search and a type filter. Open
**View plugin details** to select a version and inspect its artifact identity, declared
platform/runtime/SDK, review scope, wheel size and activation behavior. Configuration
and catalog views share the same manager; switching views keeps an unsaved draft.

Version recommendations compare all three X.Y.Z integers without floating-point rounding.
Only greater, compatible, non-withdrawn, non-blocked candidates are recommended. A newer
incompatible version does not hide an older compatible update. Local versions outside
X.Y.Z have no guessed update order; select a specific catalog version and review its
native installation confirmation instead. Matching an installed ID/version but different
bytes does not permit replacing the artifact silently.

**Update plugin…** uses the same native confirmation and installer as **Install plugin…**.
Progress comes from main: checking the index, downloading, verifying, awaiting confirmation,
and preparing. Download completion is not installation success. **Cancel installation**
waits for safe cleanup and the operation receipt; a concurrent preparation/persistence
failure is not relabeled as successful cancellation. Existing saved/applied package state
continues to be shown after a failed update. Nothing automatically updates or rolls back.

A new install remains disabled. Configure and enable it in Installed, then choose its
Agents, voice or Appearance. Resource themes can apply immediately; backend contributions
keep their established restart and session boundaries. A security-blocked package can be
disabled and removed, but the editor cannot activate it. Safety recovery is still explicit.

Catalog descriptions and access details are plain text. No third-party images or Markdown
are loaded. Homepage/source buttons send only package ID/version and the requested link
kind to main, which resolves and validates a credential-free HTTPS URL before opening it
externally. The optional `display.access` is derived by the catalog generator from the
wheel: OS disclosures and TTS/MCP service/command templates, excluding configuration
defaults, environment values and bearer tokens. Older indexes without this field show a
clear missing-details notice and retain the native verified-wheel disclosure at install.

Unknown kinds/contracts and mixed future packages remain visible with reasons and disabled
installation. Filtering for a future capability shows the matching version, not an older
version that lacks it. Closed/future voice roles are not silently presented as TTS. These
presentation changes do not open a new native execution owner or change the format major.

## Network coverage and verification

The installed manager offers **Check updates and security status**, the last successful
check time and a separate damage recovery action. There is no automatic startup network
check. Users who never check cannot receive new remote bans. Catalog failure and local
security damage are different states. Opening Browse performs an explicit catalog refresh;
opening Installed alone reads local state and does not initiate a remote check. When
security storage is unavailable, Browse keeps the display cache; only the explicitly
labeled recovery button can request recovery. Navigation is not recovery authorization.

Run the unit suites through `npm test` in `electron/` and the catalog Python tests with
`python -m pytest tests/test_extension_catalog.py`. The explicit Windows probe reuses
wheels produced by the D2 author journey:

```powershell
python tools/probes/extension_author_journey.py
python tools/probes/extension_catalog_journey.py --execute --wheels build/extension-author-journey/<run-id>/wheels
python tools/probes/extension_catalog_journey.py --execute --store-ui --wheels build/extension-author-journey/<run-id>/wheels
```

The probe uses real net requests, installers and reference packages. Public HTTPS is
checked with normal system trust. Loopback fault tests pin only a disposable certificate
inside that test process; they do not change OS roots or add production TLS exceptions.
The index/independent-review records in those tests are fixtures, not published approvals.
Native confirmations are driven automatically. See the [S1 construction record](verification/extensions/community-preview-batch3-2026-10-21.md)
for exact results, hashes, limits and failed intermediate probes.

Transport references: [Electron ClientRequest](https://www.electronjs.org/docs/latest/api/client-request),
[Electron net](https://www.electronjs.org/docs/latest/api/net), and
[certificate verification caching](https://www.electronjs.org/docs/latest/api/session#sessetcertificateverifyprocproc).

The [S2 construction record](verification/extensions/community-preview-batch4-2026-10-21.md) records the real renderer/IPC/installer journey, screenshots, code revisions and remaining publication gates. The `--store-ui` probe uses loopback HTTPS fixture authority and automated confirmations; it does not launch the complete Chat backend or certify public reviews.


## Source and packaged desktop qualification

The packaged frontend ships the static Python checker, its native Skill metadata
reader, protocol/profile files and pinned catalog policy under `resources/extension-host`.
The SDK remains under `resources/extension-sdk`. This subtree is separate from the
externally supplied Python backend: it must not create a `resources/server` package
that shadows that backend. The selected Python interpreter still needs the documented
checker dependencies; bundling these sources does not bundle a Python runtime.

The shipping-shell smoke starts the normal authenticated backend and uses the actual
built renderer, preload and IPC handlers. An explicit `--extension-fixture` installs
real reference wheels in a fresh synthetic legacy settings profile. Its private main
inspector rewrites only fixture catalog/artifact requests to a loopback HTTPS server
and automatically confirms extension access dialogs. The certificate exception stays
inside that process's download session. Pi registers against a synthetic loopback-only
model configuration for binding tests; the probe sends no model request. There is no production test URL/IPC switch,
OS trust change or copied developer profile. These results do not establish independent
review or manual native-dialog acceptance. See the [release checklist](verification/extensions/store-v1.md).

```powershell
python tools/probes/extension_catalog_journey.py --execute --store-ui --prepare-only --wheels <reference-wheel-directory>
python tools/smoke_electron_model_less.py --extension-fixture <returned-fixture-directory> --report-dir build/source-store-smoke
python tools/smoke_electron_model_less.py --extension-fixture <returned-fixture-directory> --packaged-executable electron/build/win-unpacked/amadeus-desktop.exe --report-dir build/packaged-store-smoke
python tools/probes/extension_reader_matrix.py --fixture <returned-fixture-directory>/fixture.json --ref <previous-reader-commit> --ref <current-reader-commit> --output build/reader-matrix
```

The fixed reader matrix exports unmodified Git blobs into isolated directories,
checks compatible neighbors and understood safety decisions beside future kinds,
roles, contracts and delivery modes, and rejects unknown global formats or malformed
safety data. Its report includes exact reader commits and exported file hashes.
