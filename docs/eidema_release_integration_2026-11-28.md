# Local Eidema release integration

This checkout integrates the verified explicit-memory candidate into current main.
It is a local qualification candidate; these instructions do not enable the preview
by default or assert that a desktop/model quality run has passed.

## Pinned inputs

- Host archive baseline: `4416702a2981dc7e0cedc11fbb36eced489bfb4f`.
- Tracked main snapshot: `4418f1e874047a374cd226377591dd3bac05a561`.
- Effective runtime manifest SHA-256:
  `7632424925b1cd6fd578c97d27e0e1af5488284eea457759199a3e35742a4e48`.
- Native stage manifest SHA-256:
  `c39e4eda45fa230950205ce4511abbbdfb96be0abb7f34873aaefed7db632c16`.
- Independent Eidema source: `fcded43c98faf9f1618e0ea6f8eaf5a01272610f`;
  wheel `eidema-0.2.0a1-py3-none-any.whl`, SHA-256
  `6ed17735e45150e5461e7b04c3ec1d57899f7324b5bf206fe683e216b0f59c88`.

The ordinary changed Host modules and effective Host/LLM/TTS deltas were compared
with the archive baseline and applied to the merged checkout. The SDK is installed
from the pinned wheel through the existing packaging contract. Product imports do
not load archive build directories, SDK source overlays, trial transport or trial
observation harnesses.

## Explicit preview lifecycle

Use a fresh synthetic Session directory and memory database for qualification.
Identity and project scope belong to the Host; request parameters cannot replace
them. Configure the backend before starting it:

| Condition | Effect |
| --- | --- |
| `AMADEUS_MEMORY_USE_ENABLED=true` | Enables memory use, explicit Session lookup and retirement of queued speech that depends on recalled evidence. |
| `AMADEUS_MEMORY_EXTRACTION_ENABLED=true` | Allows separate background formation requests for newly committed and eligible conversation sources. |

For qualification, `AMADEUS_MVP_SCOPED_RECALL=0` disables scoped original selection
inside explicit Session lookup. Selection is otherwise available when memory use is enabled.

After the normal Host startup completes, send the existing `chat.send` request
with `memory_lookup: true`, its real `session_id`, and a fresh `turn_id`. This
retains the admitted question source and bypasses Work/AUIP routing. The desktop
explicit-memory action uses this request. A normal role-proposed `memory_lookup`
continues to use the shared lookup pipeline with its regular evidence budget.

Scoped selection runs only when the bounded initial evidence pack omits originals
for its payload budget, contains no saved-note group, and the accepted question has
a known clock. It can use a bounded semantic reserve when an installed embedder
supports passage splitting. It never includes the current question as evidence.
The selector's actual source reads remain in the final read set even when only a
subset appears in the answer. A revision, withdrawal, identity change or permission
change before publication blocks a stale answer. Answer clocks use minute and
origin-offset presentation; canonical source timestamps remain exact.

The initial packet remains bounded by eight groups, 1,024 estimated tokens and
8,192 bytes. A user-requested single-original follow-up may use 1,536 tokens and
nine groups, while preserving source/byte limits and all earlier read dependencies.
Role-proposed lookup retains the original eight-group/token bound. There is still
only one supplemental read and one final buffered answer.

Desktop formation uses DeepSeek Flash Low in a separate background request.
The Memory panel exposes versioned warm suggestions, evidence and review.
The desktop pauses cold generation while preserving any existing cold binding.
Tools can also pass trusted Python `DesktopMemoryMaintenance` to `bootstrap`
with explicit per-Session authority, scope, providers, clock and SDK budget.
Reformation, adoption selectors and automatic cold context retain their separate
trusted options. Experimental ordinary injection requires an explicitly configured
`AMADEUS_MEMORY_SEMANTIC_THRESHOLD`; the embedder selects the retrieval model only.
Maintenance uses the normal Host store and receiver, yields to foreground/audio
work, and joins issued work before store close.

## Local checks

```powershell
python tools/package_eidema.py check
uv lock --check --offline
uv sync --locked --extra dev
uv run --locked --no-sync python -m pytest -q tests/test_memory_release_integration.py tests/test_memory_source_lookup.py tests/test_memory_lookup_followup.py tests/test_memory_source_delivery.py tests/test_session_source_lifecycle.py
```

The model-free tests use synthetic temporary databases and deterministic role
replies. They validate authority, read sets and lifecycle behavior, not semantic
model quality, live voice playback or a real user's memory database. Full locked
installation, desktop, wallpaper and AUIP qualification remain separate checks.
