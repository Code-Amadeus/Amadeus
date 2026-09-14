# 0.15 Alpha impact and validation map

This is a review map, not a statement that every adjacent subsystem has passed
physical acceptance. Routing crosses shared owners, so file preservation and a green
build alone cannot establish the absence of side effects.

## Behavior and affected owners

| Area | Contract that must survive | Evidence and remaining scope |
| --- | --- | --- |
| Main Chat / Session / Context | Use the originating turn's frozen history; preserve later independent turns; acknowledgement does not grant execution | Targeted routing and turn-origin tests, including session switches and pending-turn visibility. Warm typed-Chat physical evidence is documented separately. |
| Work / requirements / inputs | Questions are not amendments; amendments preserve Work identity; acceptance, delivery and completion are separate facts | Transaction, replay, input-receipt, uncertain-result, and process-loss contracts. Real historical journeys support specific paths, not arbitrary combinations. |
| Provider runtime / cancellation | Revalidate exact run/Attempt authority; retain private parent-context receipts without leaking public event cursor gaps | Existing public parent-context behavior was preserved during integration; Provider and WebSocket regression coverage remains required. |
| AUIP / application entry / recovery | Preserve current app ownership, exact deferred Work binding, active-Work preparation, and verified boot/readback | Public active-preparation cases and new after-Work cases were checked together. Recovery tests now configure real test-directory trust explicitly; they do not bypass the new intake recheck. |
| Model transports | Preserve native request/response protocols across role and professional queries | A live Sydney Bedrock Qwen response exposed an unsupported non-streaming `choices` envelope. The existing parser now accepts that native shape; content-block behavior and no-retry error boundaries retain tests. |
| Optional character retrieval | Current-turn knowledge must not change execution input or contaminate another turn; all providers consume the same captured history | Public RAG payload tests were adapted to pass the admission-time snapshot and assert that a later Session's history cannot enter the request. The old fixture patched global history after snapshot capture. |
| Voice / microphone / playback | Optional voice must remain absent in a core-only install; speech cancellation is separate from task cancellation | The microphone test now declares its PyAudio dependency. Voice-tier tests cover its real module; core-only collection skips it. No new microphone/ASR/Wake or acoustic-loopback acceptance is claimed. |
| Desktop / Slice / companion | Preserve public macOS window lifecycle, keyboard input and current Host presentation | Electron projection tests and build passed. Companion cards and export-preview UI are being separated into focused PRs with their own checks. Packaged Windows/macOS interaction still needs its stated physical evidence. |
| Installation / distribution | Preserve uv profiles, Linux AEC, macOS startup, optional RAG, and source provenance | Public CI/profile definitions were preserved. The source scan checks selected files for provenance, secrets, personal paths and size; it is not product acceptance. |

## Evidence ledger

- Initial public candidate `f72ac18a`: targeted Python selection **245 passed, 4 skipped**;
  Electron **59 passed**; Ruff, seven architecture views, build, high-severity npm audit
  gate, and clean source distribution scan passed.
- The initial hosted Windows core job failed during collection because the new microphone
  test imported absent PyAudio. Its dependency declaration is corrected; the supported
  core installation is not expanded to include audio solely to make that test run.
- Initial optional RAG CI exposed outdated global-history fixture assumptions. Snapshot
  payload coverage now checks the intended captured-history boundary instead.
- Follow-up transport/microphone/shared-presentation selection: **135 passed** in the
  existing voice environment. Recovery/RAG/turn-origin selection: **100 passed** in the
  clean core environment. These selections overlap other checks; counts are not additive.
- The broader isolated-per-file core sweep reported **3,872 passed and 51 skipped**,
  with 12 failed checks across five suites. All failures rejected continuation into
  test workspaces that had not been explicitly trusted under the current intake contract.
  After registering those test directories (without relaxing production checks), all
  five affected suites were rerun in isolation: **77 passed**. The original failed
  sweep remains recorded; it is not relabeled as a clean all-suite run.
- ACP, companion/reconnect and Slice export review have been separated from the main
  routing delta. Their own tests and hosted checks belong to those PRs. The final routing
  head still needs its own hosted CI, including clean core and optional RAG jobs.

Initial hosted Electron build/dependency review, source archive, Linux core/voice,
macOS voice, Windows installation ladder, and local-model installation candidates
passed. These are named job scopes; for example, Linux's selected core gate is not the
complete Windows suite. New commits must receive their own CI results.

## Routing defaults and rollback review (2026-09-15)

The selected 0.15 default enables both cooperative routing and the professional
planner. Disabling only the planner selects basic cooperative routing. Disabling
`COOPERATIVE_CHAT_ENABLED` and restarting restores the original Chat/ControlDecision/
Compound entry; it does not install the cooperative planner even if that planner's
flag remains true. Startup assembly checks cover those combinations (**6 passed**).

The rollback contract preserves normal original-route functionality while retaining
shared fixes. It is not byte-for-byte behavior equivalence with public `main`:
target grounding, verified resume checkpoints, Session-bound events and fail-closed
turn admission have changed in shared owners. Frozen old tests exposed both renamed
internal interfaces and these deliberate differences; their failed run is retained
locally and is not represented as a passing baseline. With rollback explicitly
selected, the current corresponding **21 suites passed, 264 tests**, covering original
control, compound dispatch, delegation, pending turns, history and workspace routing.
Professional planning and accepted-effect boundaries separately passed **131 tests**;
these selections overlap and must not be added as unique coverage.

The professional planner returns a plan through shared interpretation contracts;
`WorkEffectExecutor` enters the existing ProviderRuntime and WorkLedgerCoordinator.
Replay/concurrent-dispatch tests verify that the accepted effect does not create a
second native run. Cooperative conversation coordination is an additional routing
strategy, not a second Provider execution engine. Its sizeable ingress/composition
module remains a maintenance concern; this evidence does not certify every changed
line or eliminate the need for focused review.

## Separate evidence types

The [historical real-model routing baseline](routing-accuracy-baseline.md) measures
decision quality on frozen Work-only inputs. Deterministic contracts test the result
of decisions. [Physical first-audio evidence](alpha-0.15.md#voice-acceptance-evidence)
tests a warm typed-request-to-device-write path. None substitutes for the other two.

Known boundaries include basic-route explicit Provider constraints, role-level request
recall, general cross-Provider batches, deferred-entry restart persistence, microphone
and ASR, and full packaged multi-window journeys. Keep the PR in Draft while its final
integration checks or required review are outstanding.
