# Bundled Eidema core

This wheel is the model-free core dependency built from the independent Eidema
repository. The canonical Python implementation stays in that repository. No
editable checkout is used by Amadeus installations. The SDK's stdlib-only policy
metadata reader is copied byte for byte to `server/memory_policy_metadata.py` so
startup can read committed policy when importing the SDK is unavailable. This
copy cannot acquire the SDK's exclusive owner for explicit recovery.

`provenance.json` records the reviewed source commit, Git tree, build toolchain
and wheel SHA-256. The wheel is included in source archives so a checkout or an
extracted archive can use the same `uv sync --locked` command. Only NumPy is
required by the core; semantic model dependencies remain optional.

To replace it after reviewing and committing a change in the independent repo,
use the build tool from Amadeus's pinned development environment:

```sh
uv run --locked --no-sync python tools/package_eidema.py build --source <Eidema-checkout> --revision <full-reviewed-commit>
uv lock --refresh-package eidema
python tools/package_eidema.py check
```

The helper builds that committed snapshot twice, offline, with the commit time as
`SOURCE_DATE_EPOCH`, and requires identical wheel bytes before writing the bundle.
Its Git export pins `core.autocrlf=false` and `core.eol=lf`, independently of
the builder's checkout preferences. Dirty, untracked and ignored checkout files
never enter that committed snapshot. Packaging tests compare the same committed
source under both autocrlf settings as well as the two offline builds per run.
Use the Python, setuptools, wheel and uv versions recorded in the manifest when
reproducing an existing wheel. A new core version also requires updating the
exact dependency and relative wheel path in `pyproject.toml` before relocking.

For a reviewed prebuilt wheel with matching declaration and lock, run
`python tools/package_eidema.py sync-policy-metadata` to generate the reader and
its provenance record. `check` verifies the copy, contract and hash against the
wheel without importing or executing SDK code. Older wheels without a policy
reader retain their existing bundle verification contract.
