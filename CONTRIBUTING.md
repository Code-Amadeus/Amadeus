# Contributing

Amadeus currently verifies development on Windows with CPython 3.12.10 and
Node.js 22. Start with the model-less CPU profile described in `README.md`; local
CUDA ASR/TTS remains an optional profile.

Read [ROADMAP.md](ROADMAP.md) before proposing a feature. It distinguishes the
active product direction, candidates that are not commitments, and features the
project does not intend to absorb.

## Choose the right path

Small bug fixes, documentation, tests, dependency maintenance, and UI changes
that affect presentation only may open a pull request directly.

Open an Issue before implementation when a change would alter any of these:

- Main Chat, Project, Draft, Artifact, Work, or AUIP product semantics;
- Host, Provider, MCP, Skill, permission, or execution-authority boundaries;
- durable state, database layout, public configuration, schema, protocol, or API;
- the default platform, dependency, model, or asset requirements;
- a compatibility promise, deprecation, or removal visible to downstream users.

Experimental work must be isolated, disabled by default, observable in tests,
and removable without changing the supported path. An experiment does not gain
mainline status merely because its implementation exists.

## Development setup

Create the supported CPU/model-less environment from the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe tools\verify_python_environment.py --profile ci
.\.venv\Scripts\python.exe -X utf8 tools\run_tests.py
cd electron
npm ci
npm run build
npm audit --audit-level=high
```

Contributors are not expected to install CUDA, local models, reference voices, or
the character pack to validate the supported baseline.

## Pull request scope

- Keep one pull request to one coherent problem. Do not mix feature work with
  unrelated cleanup.
- State the owning layer and fix the defect there. Models interpret semantics;
  the Host owns identity, durable state, permissions, execution authority, and
  ledger facts.
- Prefer existing contracts over a new field, state, schema, fallback, model
  pass, or provider-specific branch.
- Do not add an API, compatibility path, or abstraction without a real caller.
- When replacing a path, prove whether a live caller still needs it and remove
  superseded code, tests, and documentation when safe.
- Test semantic contracts and user-visible outcomes rather than incidental
  wording, timing, filenames, or tool order.

Public configuration, schema, protocol, and persisted-state changes must explain
compatibility impact. Amadeus is pre-1.0, so a justified breaking change is
allowed, but it must be explicit and include a migration or clean-reset path.

## Evidence expected in a pull request

- Describe the problem and why the selected layer owns it.
- List the user-visible effect, including "none" for internal changes.
- Report the exact commands and relevant manual journeys that were run.
- Include before/after screenshots for visible Electron changes.
- Update documentation and examples when a setting, schema, workflow, or
  supported product boundary changes.
- Keep the CPU/model-less path green. Optional model, network, or character
  evidence may supplement it but cannot replace the deterministic baseline.

Python changes are expected to pass the relevant tests and the full CI suite.
Electron changes are expected to pass `npm run build`; dependency changes must
also pass the corresponding audit. Draft pull requests may be opened before all
checks pass when their remaining work is stated clearly.

The Windows Python CI also launches the built Electron application in an
isolated, model-less profile. Contributors do not need to run this for ordinary
changes. When changing desktop startup, backend authentication, navigation, or
shutdown, build Electron and run it from the repository root with:

```powershell
.\.venv\Scripts\python.exe -X utf8 tools\smoke_electron_model_less.py
```

The smoke requires local port `17777` to be free and never sends a chat turn or
loads an optional model or character package.

## Review and merge

- Product-semantic and public-contract changes require a linked Issue before
  they are ready for merge.
- The maintainer has final responsibility for product direction and whether a
  change belongs in mainline. Passing tests is necessary but does not by itself
  establish product fit.
- Pull requests merge only after the required Python and Electron checks for
  their scope pass and review concerns are resolved.
- External pull requests use squash merge so each accepted change has one clear
  mainline commit.
- Review may request a smaller scope, a different owning layer, or removal of a
  speculative compatibility path. Rejection should state the product or
  engineering reason.

Do not commit API keys, tokens, sessions, runtime logs, model weights, voice material,
copyright-sensitive character media, or personal absolute paths. Use neutral fixtures
such as `C:\Users\user\...`. Third-party code and assets must retain their original
notices and provenance.

Security-sensitive reports should follow [SECURITY.md](SECURITY.md), not a public
Issue containing exploitable details.

## License

Unless agreed otherwise before submission, contributions intentionally submitted for
inclusion are offered under the same GNU Affero General Public License v3.0
(AGPL-3.0) terms as Amadeus first-party code. Do not submit material that you cannot license on those terms, and
retain the original notices for third-party code.

The source-release gate is a required release invariant. Any newly included
third-party code or asset must add its source, revision, license evidence, and
release disposition. Do not weaken or bypass that gate in a feature
contribution.
