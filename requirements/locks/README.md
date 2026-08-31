# Python dependency profiles

Amadeus supports CPython 3.12 on Windows. Dependency ownership is split into
the four install tiers documented in README (core / voice / vad /
local-cu124), and the lock files serve the model-less profiles:

- `pyproject.toml` lists direct runtime dependencies and optional feature sets;
- `windows-py312-cpu.txt` is the exact L1 core resolution used by
  `requirements.txt` (no voice or model packages);
- `windows-py312-ci.txt` adds the exact test/tooling resolution used by
  `requirements-dev.txt`.

Tier profiles L2 (voice) and L3/L4 resolve from `pyproject.toml` extras;
`requirements-cu124.txt` pins the L4 CUDA baseline and requires
`.[voice,vad,local-cu124]`.

These are version locks, not wheel-hash locks. Exact versions plus a clean
install and test run are the current open-source release boundary. Per-wheel
hashes can be added later if Amadeus ships a signed installer or needs a
stricter supply-chain policy.

Regenerate the supported locks from any OS with uv installed:

```bash
uv run tools/generate_python_locks.py   # 或 python3 tools/generate_python_locks.py
```

Do not edit generated lock files by hand. After regeneration, run the clean
environment verifier documented in the repository README.

## CUDA 12.4 local models

`requirements-cu124.txt` is currently an installation profile, not a release
lock. It fixes the tested `torch==2.5.1+cu124` / `torchaudio==2.5.1+cu124`
pair and installs the `local-cu124` direct dependency set from `pyproject.toml`.

`windows-py312-cu124-observed.txt` records the working developer machine. It is
generated from installed distribution metadata by
`tools/audit_cu124_dependencies.py`, and is deliberately not an installation
input because it also contains incidental GUI, test, deprecated SDK, and
transitive packages.

The cu124 profile can become a release lock only after it resolves and passes
all of the following in a new Windows 11/Python 3.12 environment:

1. `pip check`;
2. `python tools/verify_python_environment.py --profile cu124`;
3. ASR and TTS import smoke tests without downloading models implicitly;
4. one real-device ASR/TTS smoke run.

Until then, the cu124 profile remains the first-release product baseline based
on the current working local installation, without being described as a
universal clean-install lock. CPU/model-less remains the reproducible CI and
compatibility contract; remote model and voice APIs are explicit compatibility
routes rather than the default product experience.
