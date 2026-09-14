# 0.15 Alpha: routing authority and continuous interaction

Status: draft integration candidate, not a published release.

This candidate keeps the foreground character conversation responsive while Work
owns requested deliverables, execution identity, accepted requirements, and results.
Interaction contexts provide addressing and continuation through the existing
Provider runtime. They do not become a second writable task system.

## Routing and ownership

- Main Chat interprets the user and can stream its first spoken response before a
  professional Work decision finishes. Acknowledgement is not execution acceptance.
- The optional professional planner interprets new work, amendments, reports,
  cancellations, and messages using the original request and frozen shared history.
- The Host rechecks source, identity, target, permissions, and current execution state
  before shared Work admission. Accepted requirements and ordinary questions remain
  distinct: questions do not create another requirement or execution attempt.
- Interaction contexts preserve a conversation address and compatible native session.
  Work, Project, Draft, Attempt, and Provider session identities remain separate.
- New unplaced deliverables remain Drafts until explicit Project creation/promotion.
- AUIP owns application actions and receipts. A capability-blocked application request
  remains an application refusal and cannot be redirected to an unrelated task.
- Ambiguous App/Work stops do not authorize either stop. Independent requests retain
  their own handling. Speech interruption is separate from Work cancellation.
- Missing or untrusted selected directories block execution at their owning boundary;
  they do not disable ordinary Chat or an independently placed new Draft.

## Configuration

The following explicitly selects the professional route used in the acceptance sample:

```dotenv
COOPERATIVE_CHAT_ENABLED=true
COOPERATIVE_WORK_PLANNER_ENABLED=true
```

The planner uses the selected Main Chat model transport. An optional
`COOPERATIVE_WORK_PLANNER_MODEL` overrides only the planner model. A role model is
independent from the Provider that executes a task.

This draft preserves the existing selectors: cooperative defaults on, while the
professional planner remains opt-in. Setting cooperative off retains the original
Chat strategy over the shared execution and presentation facilities. The basic
cooperative route still has a known explicit-Provider-constraint limitation; the
professional acceptance result does not establish parity for that configuration.

## Other integrated surfaces

The branch includes bounded AUIP after-Work entry and authoring validation/recovery,
optional ACP v1 agents, an artifact appearance preference, complete-file identity for
truncated export previews, and the optional compact companion presentation panel.
The public mainline's uv installation profiles, Linux AEC source build, macOS wallpaper
lifecycle, keyboard chat entry, and optional character retrieval remain in place.
See [ACP providers](acp_provider.md) and [installation profiles](install_profiles.md).

## Voice acceptance evidence

On Windows, with the existing Python 3.12/cu124 environment, real ChatPage,
professional planning enabled, local GPT-SoVITS Japanese speech, and the default
Realtek output device, the following warm ordinary Chat samples were observed:

| Sample | First device-buffer write |
| --- | ---: |
| Ordinary conversation after the initial turn | 1.125 s |
| Ordinary conversation after a completed planner task | 1.078 s |

Both meet the owner-selected **1.5 s warm ordinary Chat** gate. The metric joins the
exact turn/sentence identities and measures Host admission to the first successful
nonempty PortAudio write. It is not a microphone loopback measurement or a long-run
percentile claim. Cold first-turn latency was 5.250 s; three task acknowledgements
were 1.953, 1.469, and 1.484 s. The owner excluded cold startup and task responses from
this particular gate; those observations are retained rather than scored as passes.

These physical samples were collected in the integration workspace before resolving
public-main overlaps. The public candidate receives separate deterministic and build
checks. A physical measurement of the final merge revision remains a separate check.

## Draft boundaries

After-Work automatic entry retains in-process continuation limits. This candidate
does not promise restart persistence for every pending application launch, arbitrary
cross-Provider batches, or exhaustive natural-language routing accuracy. Microphone,
ASR/Wake, packaged platform UI, long-running device behavior, and clean full CI are
separate evidence from the warm typed-Chat sample.

Only current runtime contract tests and their required helpers are added here.
Internal chronological journals, hash-frozen experiment campaigns, recorded sessions,
model weights, personal paths, and unrelated local demonstration assets are not part
of this public delta.

Further structural simplification should follow a demonstrated duplicate owner or
unused live path. This draft does not retire a working routing strategy solely to
reduce line count. Merge cleanup should concentrate on integration defects, public
documentation, and green candidate checks.
