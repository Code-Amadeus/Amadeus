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

The [three-strategy comparison and accuracy baseline](routing-accuracy-baseline.md)
explains original Chat, cooperative basic, and professional cooperative separately.
The historical same-model comparison scored 17/26, 21/26, and 23/26 respectively;
it is distinct from deterministic contract tests and current-candidate acceptance.

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

The routing branch includes bounded AUIP after-Work entry and authoring validation/recovery
and the associated artifact appearance preference. Experimental, opt-in ACP v1 agents, complete-file
review for truncated Slice export previews, and the compact VN-style companion panel
are separate Draft PRs within the same 0.15 Alpha release scope.
The public mainline's uv installation profiles, Linux AEC source build, macOS wallpaper
lifecycle, keyboard chat entry, and optional character retrieval remain in place.
See [installation profiles](install_profiles.md). The standalone companion is reviewed
in [PR #75](https://github.com/Code-Amadeus/Amadeus/pull/75);
[ACP #76](https://github.com/Code-Amadeus/Amadeus/pull/76) and
[Slice export review #77](https://github.com/Code-Amadeus/Amadeus/pull/77)
are stacked on the routing candidate because they consume its shared Host contracts.
ACP's Claude/dsh examples are local integration probes, with no real production use.

## Voice acceptance evidence

The owner-selected gate is warm ordinary Chat reaching its first audio-device write
within 1.5 seconds, with professional planning enabled. Cold startup and task-response
latency are separate measurements. The integration-workspace samples passed that gate.

本次测试从新西兰发起，请求中国大陆的 DeepSeek 服务，首声耗时包含这段跨境网络通信。
这里的首声是 E2E 延迟：从文字请求提交，到音频设备首次成功写入声音，包含模型请求、
网络通信、首句生成和播放链路。在通信延迟更低的环境下，预热后的 E2E 首声有机会进入
1 秒内。开启专业路由后，角色首句仍可先行回应，不必等待专业规划完成。

The test client was in New Zealand, calling the DeepSeek service in mainland China.
First-audio latency includes that cross-border network communication. With lower
communication latency, warm end-to-end first audio may fall below one second.
This means typed-request submission through the first successful audio-device write,
including remote model/network work, first-sentence generation and the playback path.
With professional routing enabled, the role's first response can begin before the
planner completes; more specialized routing need not serialize the first spoken reply.
This does not claim zero planner overhead, and typed input excludes microphone/ASR.
Network transit,
model-service wait and generation were not measured independently, so no estimated
network duration is subtracted and this is not a fixed latency guarantee.

A separate AWS comparison used the configured Bedrock endpoint in Sydney
(`ap-southeast-2`) and Qwen3-235B. It had a different model as well as a different
network destination; its variation cannot be attributed to network distance alone.
It demonstrated that sub-second warm first audio is possible, with variability across
turns. Individual timings remain in the complete local measurement record.

Both measurements used real ChatPage, the existing Python 3.12/cu124 environment,
local GPT-SoVITS Japanese speech, and the default Realtek output device. Ordinary
runtime warmup and the short-opening audio cache remained enabled, matching daily use.
The metric joins exact turn/sentence identities from Host admission to the first
successful nonempty PortAudio write. It is not microphone-loopback latency or a
long-run percentile measurement.

Physical samples were collected in the integration workspace; public-main adaptation
has separate deterministic/build checks. These are not newly collected physical
measurements of every subsequent public commit. The [impact and validation map](alpha-0.15-impact.md)
separates those evidence boundaries.

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
