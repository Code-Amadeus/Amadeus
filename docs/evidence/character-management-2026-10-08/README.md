# Character management M1 acceptance — 2026-10-08

Base: `f66c5f95a693727453a6eb614118500e271a730d`. Scope is local character
management, active identity labels and explicit startup recovery. The parallel
Live2D implementation is not included in these results.

## Default behavior evidence

Before editing, three isolated captures from unmodified main recorded 155 model
inputs each: default Kurisu, name-only Mira, and Kurisu with a synthetic Japanese
override. The final loader output matches every captured UTF-8 string in all
three cases. The original 155-surface and 107-phrase fixtures were not rewritten.
The original phrase/boundary suite passed 252 tests before editing; the new branch
also passes the phrase checks. No real model, microphone or synthesis was used.

Prompt assemblers, SpriteForge graph algorithms, shared presentation claims,
TTS, scene policy, Provider/Work authority, and asset files have no source diff.
Kurisu's UI metadata is kept outside model-visible names and template bindings.
Amadeus remains the system name. A preview without a backend reads the canonical
builtin UI label without importing the selected conversational identity.

## Automated verification

The full Python runner executed **422 files / 5323 collected nodes** in two
isolated shards in a real Git worktree:

- Shard 0: 2513 passed, 3 skipped, 1 failed (2517 nodes).
- Shard 1: 2788 passed, 13 skipped, 5 expected failures (2806 nodes), exit 0.
- The sole failure required the old literal `Let Kurisu play` to exist in the
  TypeScript source. That source assertion now checks the stable delegate action;
  the actual Kurisu and Mira label output is covered by React rendering tests.
- Subsequent targeted checks of the corrected AUIP surface, final character
  persistence and VN startup/preview changes passed **88/88**. This includes
  actual CLI exit-code classification, invalid persistence, atomic-write faults,
  DEL/Unicode round trips and standalone previews with an invalid selected role.
  The complete two-shard run was not repeated after these final localized fixes.
- Electron: **270/270** passed; `npm run build` passed, with the pre-existing
  bundle-size warning. Parent review checked role IDs for duplicate display names.
- Ruff passed on 1005 tracked Python paths; final changed Python files were
  checked again after the targeted fixes. Git diff whitespace checks passed.

Commands used the existing environment, without dependency installation:

```powershell
.venv/Scripts/python.exe -X utf8 tools/run_tests.py --shard-count 2 --shard-index 0
.venv/Scripts/python.exe -X utf8 tools/run_tests.py --shard-count 2 --shard-index 1
.venv/Scripts/python.exe -X utf8 -m pytest tests/test_character_profiles.py tests/test_auip_experience_surface.py tests/test_vn_overlay_window_contract.py tests/test_vn_overlay_auth.py -q
# From electron/
npm test
npm run build
```

Temp directories, conversations, desktop settings, character records and Work
stores were isolated. Optional local-asset skips are retained, not counted as
passes. Windows sandbox temp-file rename/socket restrictions required running
checks in the ordinary authorized user context; no product workaround was added.

## Actual desktop journeys

The shipping `tools/smoke_electron_model_less.py` launcher with a task-local
scenario extension passed **18/18** checks. It exercised:

1. Existing Chat, Backend, Settings, capability and artifact navigation.
2. Creating two same-name roles with different personality text in the GUI;
   their generated IDs are distinct and visible.
3. Selecting one for next start while the actual running role remains Kurisu.
4. Restarting into that role, opening its own Chat and refusing the foreign
   Kurisu conversation without changing the active conversation.
5. Editing its name/personality, retaining its ID/history, and applying only
   after an explicit restart even without another pending desktop setting.
6. Corrupting only its synthetic role file, observing the startup error and
   using the GUI's explicit recovery action.
7. Returning to Kurisu with the original synthetic history intact; the malformed
   user file is not deleted or overwritten. Electron/backend exit cleanly.

A separate negative **cold-start** journey passed **5/5** checks: desktop
settings initially selected a nonexistent user role, the backend exited before
readiness, Electron still opened, Settings offered recovery without a live
backend, and the button persisted literal `kurisu` and started the real builtin
backend. The test-owned processes exited cleanly.

These task-local extensions were not added as new shipped command-line tools.
The steps above describe their scope; the ordinary model-less launcher alone
is not claimed to cover the added management and recovery assertions.

An initial harness assumed backend restart while remaining in Settings had
already activated a Chat session. It failed with StopIteration. The corrected
journey enters Chat before asserting its session; no product change was made to
satisfy that assumption. The first broad screenshots did not frame the relevant
cards, so the final screenshots below were recaptured from the card elements.
Failed attempts remain in local logs rather than being relabeled as passes.

## UI evidence

Only synthetic role-management cards are captured. No character artwork,
private conversation, credentials or machine paths are included.

![Saved selection while the running identity is unchanged](pending-role.png)

![Updated role after restart, with independent stable IDs](active-role.png)

![Explicit recovery after the selected role becomes invalid](startup-recovery.png)

## Limits and deferred boundaries

This is model-less acceptance, not a test of a real model's persona fidelity or
voice/animation quality. VN, Work commentary and Hybrid openings do not gain the
new personality text. VN explicit old-session-ID ownership remains a separately
recorded issue. Per-role appearance/voice selection, copy/delete/hide, and asset
installation are not implemented. Live2D integration must be validated after
both independent branches are ready; these results do not establish that merge.