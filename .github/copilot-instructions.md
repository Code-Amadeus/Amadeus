# Amadeus — Copilot Workspace Instructions

Amadeus is a local desktop runtime for a real-time multimodal AI agent: voice conversation,
character embodiment, delegated work execution through providers, and a persistent work
control plane. Backend is Python 3.12 (FastAPI/WebSocket host); desktop shell is
Electron + React (TypeScript). Reference platform is Windows; macOS is validated for the
L1/L2 tiers in CI. The project is pre-1.0 (0.1 α).

Before non-trivial changes, read `AGENTS.md` (engineering decision principles),
`CONTRIBUTING.md` (workflow and evidence expectations), and `ROADMAP.md`
(committed direction vs. candidates vs. explicit non-goals).

## Architecture ownership — do not blur these boundaries

- **Main Chat** (`core/`): foreground character conversation. It cannot call MCP tools
  directly, even though MCP/Skills share the Host registry.
- **Provider Runtime** (`agent_host/`, `server/provider_*`): executes delegated work via
  provider-neutral adapters (Browser, Codex App Server / Direct Codex, optional OpenClaw).
- **Work Ledger** (`agent_host/work_ledger_*`): durable projects, drafts, work items,
  attempts, permissions, artifacts, completion facts.
- **AUIP** (`server/auip_*`): revisioned, bounded interaction with attached applications;
  applications remain the authority for their own state and action receipts.
- The **Host** owns identity, durable state, permissions, execution authority, and ledger
  facts. Models interpret semantics; model narration/translation is presentation or
  evidence, never a new authority source.
- Presentation layers (TTS, subtitles, Electron renderer, previews) render accepted facts;
  they own no execution or durable state.

Dated documents under `docs/` are evidence of the state at their date, not automatically
current contracts. `architecture/` diagrams are generated; CI checks them with
`tools/architecture/generate_views.py --check` — regenerate rather than hand-edit views.

## Repository layout

| Path | Contents |
|---|---|
| `server/` | Authenticated local backend, Host control plane, AUIP, WebSocket handler |
| `core/` | Main Chat runtime and session integration |
| `agent_host/` | Provider contracts, adapters, work identity, capabilities, ledger store |
| `asr/` | Conversation / wake-word recognition backends |
| `tts/` | Synthesis backends, sentence pipeline, playback, mouth/lip signals |
| `render/`, `wallpaper/`, `vn_player/` | SpriteForge/PixiJS renderer, desktop placement, experimental VN Player |
| `electron/` | Electron main, preload, React renderer, Settings |
| `tools/` | Verifiers, smoke scripts, test runner, source-release tooling |
| `tests/` | Python unittest-style suites (see `tools/run_tests.py`) |
| `docs/` | Product and protocol decisions |

`main.py` is retired and only prints a deprecation notice — never treat it as the app entry
point. The Python entry is `python -m server.app`; the desktop entry is
`run_electron_utf8.bat` (Windows) or `npm run electron:dev` (macOS).

## Environment and commands

Everything runs through `uv` with Python 3.12 (CI pins uv 0.12.8, Node 22.21.1). The venv
is always the single `.venv` at the repo root — launchers auto-discover it, and CI fails if
a second venv (e.g. `.venv_cu124`) appears. Never invoke the backend with a bare system
Python; always `uv run --locked --no-sync`.

Development setup and baseline validation:

```powershell
uv venv .venv --python 3.12
uv sync --locked --extra dev
uv run --locked --no-sync python tools\verify_python_environment.py --profile ci
uv run --locked --no-sync python -X utf8 tools\run_tests.py        # full Python suites
uv run --locked --no-sync python -m ruff check .                   # lint
uv run --locked --no-sync python tools/architecture/generate_views.py --check
cd electron; npm ci; npm run build; npm test
```

Run the backend (headless / model-less is the deterministic baseline):

```powershell
uv run --locked --no-sync python -m server.app --port 17777
```

Install tiers — all share one `.venv`; `uv sync` is exact, so a command missing an extra
removes that layer. `torch-cpu`, `local-cu124`, and `local-rocm` are mutually exclusive:

- L1 core: `uv sync --locked` — verify `--profile cpu`
- L2 voice: `--extra voice` — verify `--profile voice`
- L3 CPU VAD (no GPU required): `--extra voice --extra vad --extra torch-cpu` — verify `--profile vad-cpu`
- L4 local-cu124 (Windows + NVIDIA): `--extra voice --extra vad --extra local-cu124` — verify `--profile cu124`
- Experimental ROCm stays off by default; see `tools/rocm_sidecar/README.md`.

Optional RAG (`--extra rag`), wake/ASR/TTS/character packs are installed via
`tools/external_assets.py` and are never required for the baseline.

## Conventions and constraints

- Diagnose before changing code. Classify the failure (product-semantic, authority-boundary,
  abstraction, integration, implementation, or test-instrument) and fix it at the owning
  layer with the smallest coherent structural change.
- Do not over-defend local failures: prefer one explainable invariant over overlapping
  guards. No new field, state, schema, fallback, model pass, or provider-specific branch
  unless a real caller needs it and the smaller root-cause fix is proven insufficient.
- Fail closed for permissions, destructive actions, identity ambiguity, and execution
  authority; never add speculative guards that mask defects or make ordinary flows brittle.
- No provider-specific shortcuts, prompt keyword patches, duplicated sources of truth,
  silent retries, or growing exception lists in shared paths.
- When replacing a path, prove no live caller still needs it and remove superseded code,
  tests, and docs in the same change.
- Test semantic contracts and user-visible outcomes, not incidental wording, filenames,
  timing, or tool order, unless those are themselves the contract. Cover both sides of a
  changed boundary. Keep the CPU/model-less path green; model, GPU, or network evidence may
  supplement but never replace the deterministic baseline.
- Public configuration, schema, protocol, or persisted-state changes must state their
  compatibility impact. Breaking changes are allowed pre-1.0 but must be explicit and
  include a migration or clean-reset path.
- Never commit API keys, tokens, sessions, runtime logs, model weights, voice material,
  copyright-sensitive character media, or personal absolute paths — use neutral fixtures
  such as `C:\Users\user\...`. Third-party code and assets keep original notices, and the
  source-release provenance gate (`tools/check_third_party_provenance.py`) must not be
  weakened or bypassed.
- Python tests live in `tests/` as unittest-style suites run through `tools/run_tests.py`,
  which pins control-plane env flags so results do not depend on a developer's `.env`.
  Electron renderer tests live in `electron/tests/*.test.mjs` (`npm test`).
