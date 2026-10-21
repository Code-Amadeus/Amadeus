# Qwen-ASR packaging provenance

Upstream: https://github.com/Qwen/Qwen3-ASR, PyPI release `qwen-asr==0.0.6`.
Original sdist: `https://files.pythonhosted.org/packages/7f/5b/56c5175d1a4d6ed8602003385570304305e4ae9c40b999d12d75a70c0561/qwen_asr-0.0.6.tar.gz`.
SHA-256: `294893f2340dc2d58d1f1d1cb8ce26ac0a79f254805ab432dda2892bd5c8d13c`.
License: Apache-2.0; original LICENSE, copyright headers, README and manifest retained.

Amadeus modifications (2026-10-21–22): `pyproject.toml` moves `gradio` from mandatory
requirements to the `demo` extra (`gradio>=6.16.0,<7`). All inference/model/CLI implementation files and other shipped upstream inputs
are byte-identical to the sdist, recorded in `qwen-asr/upstream-files.json`. Three
package `__init__.py` files additionally make seven existing public re-exports
explicit (`from module import Name as Name`) to satisfy the repository lint rules
without adding an exclusion. Reversing only these aliases restores the upstream
file hashes; tests also require identical compiled bytecode, names and constants.
No public names or `__all__` values are changed. Generated upstream egg-info/PKG-INFO are omitted so
build metadata comes from the patched pyproject. The upstream release version
remains 0.0.6 for advisory lookup; `uv.lock` records the local directory source.
The Host uses its locked uv source selection, not a metadata override or `--no-deps`
runtime installation. No inference logic or model weights are bundled or changed.

Why: Qwen-ASR imports Gradio only in `qwen_asr/cli/demo.py`. Its normal inference
entry point does not use Gradio. Supported Gradio currently caps Pillow below 13,
preventing the security update of the Host's actual image decoder. Removing just
the Host Gradio constraint instead lets resolution select an obsolete Gradio; that
is not retained. `tools/my_utils.py` likewise imports Gradio/pandas only in its
legacy UI helpers, not when local TTS imports `load_audio`.

The Host local-model profiles install inference dependencies. Qwen's Gradio demo
and the inherited `tools/subfix_webui.py` are not part of those profiles; do not add
an old Gradio/Pillow to the Host environment to run them. A supported separate demo
environment needs compatible upstream releases. This does not disable or move the
Host's configured in-process or sidecar ASR/TTS execution.

Removal condition: an upstream Qwen-ASR release separates optional UI dependencies
(or a supported Gradio release permits the patched Pillow and the Host's other
pinned dependencies). Then replace this directory source with the reviewed upstream
release, remove this packaging patch, and repeat inference import/lock/image tests.
No upstream bug report is claimed: this is a local packaging compatibility change.
