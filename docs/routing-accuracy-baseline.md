# Routing strategies and the 2026-09-12 baseline

This is historical evidence from the integration branch at `e182052f`, not a new
accuracy measurement of the public 0.15 Alpha candidate. Per-case frozen scores
are published in [the evidence table](evidence/routing-accuracy-2026-09-12.json),
including hashes of the original captures. Private execution traces are not included.

## Three strategies, shared execution owners

| Strategy | Cooperative | Work planner | Responsibility and intended use |
| --- | --- | --- | --- |
| Original Chat authority | off | ignored | ChatRuntime emits detailed delegation proposals. The existing control authority, including compound decomposition when enabled, grounds them before shared dispatch. Retained for the established route and comparison. |
| Cooperative basic | on | off | The role's coordination contract handles conversation addressing, continuation, and detailed Work proposals without a separate professional planner. It offers the context-based conversation flow with fewer decision stages, but has a narrower supported batch boundary. Retained as an explicit configuration. |
| Professional cooperative | on | on | The role can acknowledge and make a coarse Work proposal; a separate role-free planner decides Work/message relationships, targets, and supported multiple operations. Host acceptance and shared domain owners still decide whether an effect may execute. This is the 0.15 default and the route used for the voice acceptance sample. |

The flags are `COOPERATIVE_CHAT_ENABLED` and `COOPERATIVE_WORK_PLANNER_ENABLED`.
These are alternative assemblies, not three routers run on every user turn. Ordinary
role-only Chat does not call the Work planner. AUIP has its own application decision
scope. A planner cannot repair a new-Work request that the role never proposes.

All strategies share the existing Work/Provider/permission/presentation facilities.
Context is a conversation address; Work owns deliverable requirements and results.
Changing the speaking context does not itself change the executor for a requested task.

## Frozen results

Thirty inputs per arm included 26 scored cases and four unscored wish/question-form
boundary observations. The same seed Work ledger, user text, and frozen history were
used. Semantic queries used real models with temperature zero; execution adapters were
inert. The original route had its production-default compound decomposition enabled.

There are three routes and four measured configurations: changing the professional
model does not create a fourth route.

| Configuration | Frozen score | Accuracy |
| --- | ---: | ---: |
| Original / DeepSeek V4 Flash | 17/26 | 65.4% |
| Cooperative basic / DeepSeek V4 Flash | 21/26 | 80.8% |
| Professional / DeepSeek V4 Pro planner | 21/26 | 80.8% |
| Professional / DeepSeek V4 Flash planner | 23/26 | 88.5% |

The same-model original-to-professional difference is **+23.1 percentage points**
on this sample. Basic-to-professional is **+7.7 points**; the entire gain should not
be attributed to adding the planner alone. These small, selected samples do not
establish statistical significance or population accuracy.

The professional route improved distinctions such as asking an existing executor
about its implementation versus starting another deliverable. For the three scored
multiple-intent inputs, original/basic/professional-Flash scored 1/3, 0/3, and 2/3.
One retained failure combined an implementation question and a title amendment;
only the message survived.

## Interpretation limits

- The published score keeps the original 26-case denominator. Two professional
  mismatches were later judged acceptable or a defective ambiguity fixture; removing
  them after observing outcomes would inflate the comparison asymmetrically.
- Basic's explicit-Provider-constraint loss and the role's initial request recall
  remain distinct concerns. A high professional score does not validate basic.
- Fixture/transport mistakes in exploratory runs were corrected before these
  captures; the earlier runs are excluded. No rerun of the language model happened
  while preparing this public evidence table.
- These are route-decision results, not real Provider execution, AUIP/Browser entry
  recall, microphone, first-audio, network latency, or end-to-end acceptance results.
- Contract regressions prove behavior given a decision; they cannot substitute for
  measuring whether a real model chooses the right decision.

Use the [0.15 acceptance scope](alpha-0.15.md) for the separate physical voice and
public integration checks. New accuracy claims should rerun a frozen comparison
with both improvements and regressions reported.
