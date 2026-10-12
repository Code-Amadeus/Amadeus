# Work files and interactive applications

Work outputs share one Host ledger. The artifact panel separates ordinary files
from verified AUIP applications, with independent counts and recent lists.
A report delivered beside an application remains an ordinary file. Application
details list the exact verified entry, manifest and Host-receipted members.

Files also lists completed, approved external exports from the Host ledger,
including reports delivered only to Desktop. Pending, denied and Provider-claimed
exports are not completed deliveries. Exported AUIP members stay with their
verified application; an independent export remains a file. External exports
are labelled and have no in-app preview: approval to publish a file does not
broaden the workspace-only preview boundary. The list describes the recorded
delivery, not a fresh verification that an external file still exists unchanged.

Catalog projection runs off the asynchronous request thread because AUIP
verification may read and hash files. Its existing bounded/incomplete-result
semantics are retained.

Only a current artifact revision can be previewed. The Host checks the WorkItem,
workspace containment and registered SHA-256 before returning at most 512 KiB
of UTF-8 JSON, CSV, TSV, Markdown or text. The renderer displays escaped text.
Viewing does not accept the WorkItem or execute HTML.

AUIP Open/Interact retains the manifest, entry and session Attach checks. A file
without a matching registered digest cannot become a runnable application.
An invalid app revision leaves its files visible for diagnosis. Unclaimed files
are not hidden merely because they share a directory or code suffix with an app.

Provider declarations remain evidence under Provider identities; they cannot
mint or replace Host business-file/export receipts. An accepted recovery may
inherit only unchanged hashed outputs of its direct predecessor. Unrelated
files or edits between attempts remain ambiguous.

Installed plugins are capabilities managed separately; output files do not
install plugins or grant them Host history access.

## Compatibility

There is no ledger migration or file copying. Older AUIP records without a
digest must be registered again through the Host-owned delivery path before
opening; the Host does not trust a Provider declaration as replacement proof.

## Validation and interface

`tests/test_work_artifact_preview.py`, `tests/test_work_artifact_registry.py`
and the existing AUIP recovery/export suites cover mixed delivery, causal
revisions, both recent lists, preview containment and authority boundaries.
`electron/tests/workArtifacts.smoke.mjs` exercises the production components
with synthetic data; `tools/smoke_electron_model_less.py` checks real navigation.

Previous combined view (synthetic fixture):

![Combined view before separation](images/work-artifacts/artifacts-before.png)

Independent file view:

![Ordinary files](images/work-artifacts/files-desktop.png)

Application and its registered members:

![Application details](images/work-artifacts/apps-desktop.png)

## Catalog and recovery compatibility

The desktop uses `work.artifacts.list` for both Project and Draft catalogs. The
superseded `project.apps.list` and `draft.apps.list` WebSocket methods are removed;
callers should use the shared catalog's `apps` and `files` collections. Internal
AUIP app lookup remains in its existing owner.

An accepted recovery adds ownership of the predecessor's unchanged output. An
explicit amendment also retains its existing ownership of earlier registered
deliverables; a recovery of that amendment does not narrow it to the most recent
attempt. Unrelated pre-existing dirty files remain outside either exception.
File previews retain their content across unrelated parent rerenders and reload
when the selected artifact, its Work owner or availability changes.
