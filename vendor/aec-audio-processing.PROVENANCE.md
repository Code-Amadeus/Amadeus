# aec-audio-processing 1.0.1 (temporary Linux build workaround)

- Source of truth: [official PyPI release metadata](https://pypi.org/pypi/aec-audio-processing/1.0.1/json).
- Artifact: `aec_audio_processing-1.0.1.tar.gz` (5,897,164 bytes).
- SHA256: `f03a3ce3f45bf15a04a8504d0f692a651180ca2d3171bd9f3de0516e5f2b8360`.
- Upstream version: `1.0.1`; backend: `setuptools.build_meta`.
- Baseline includes WebRTC Audio Processing 2.1 and Abseil 20240722.0.

The only upstream source change is [aec-audio-processing.patch](aec-audio-processing.patch):
add `--force-fallback-for=abseil-cpp` to `setup.py`'s Meson setup arguments.
It keeps incompatible system Abseil out of this dependency's source build;
no WebRTC/Abseil source or audio runtime code is changed. The Linux-only
`tool.uv.sources` marker preserves the Windows/macOS PyPI paths.

All upstream license/attribution files and source notices are retained.
`src/files/THIRD_PARTY_NOTICES.txt` is an additional, verbatim concatenation of
the upstream LICENSE, COPYING, LICENSE.build, AUTHORS and PATENTS files, with
relative-path headings and normalized line endings. The existing `files/*` package-data rule includes it
in locally built wheels; no new wheel publishing infrastructure is required.
The sdist's existing macOS dylib is retained unchanged, not built by Amadeus.
Generated `build/`, `install/`, caches and `*.egg-info/` are not vendored.

To audit: verify the official archive hash, unpack it, apply the patch with
`patch -p1`, and compare files with this directory. Exclude generated
`*.egg-info/` and account for the added notice file. No other source delta is
expected. The path-source lock entry has a dynamic version and does not hash
the directory; the reviewed Git tree plus the artifact identity and patch
fix the source inputs. Python build tools and system compilers are separate
inputs, so this is not a claim of byte-identical wheels.

Remove the vendor tree, patch, this file, Linux source override, its lint
exclusion and AEC provenance/notice entries after a replacement official artifact passes source build with
modern system Abseil, locked Voice installation, verifier and AEC smoke;
recheck Windows/macOS artifact selection and regenerate `uv.lock`.
Remove the source archive's `vendor` include root if no other vendored dependency needs it.
