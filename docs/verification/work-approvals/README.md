# Work approval choices

Related issue: [#224](https://github.com/Code-Amadeus/Amadeus/issues/224).

This change adds two explicit choices while keeping automatic approval off by
default: a persistent all-Work execution policy, and a revocable exact-call Pi
MCP grant confined to the active Provider run. Desktop settings own persistence;
Provider Runtime owns policy application and audit, and Pi owns its native
callback translation. Restoring a native session never restores a grant.

## Review follow-up / 审核修复 (2026-10-17)

The review reproduced an authority-boundary defect on `7bd966f`: an ordinary
SQLite permission-request write failure was swallowed by the event bus, after
which Runtime still sent one automatic allow response with zero permission
records. The new regression selection initially produced 2 passed / 4 failed,
covering request writes, decision writes, hot enable and an unknown response.

Runtime now requires a trusted Work or Cooperative permission owner to locate
the exact committed pending request and commit `policy_allowed` before native
response dispatch. No owner, a failed commit or a cancelled checkpoint cannot
approve the call. The attempt is reserved before awaiting the owner, so an
unknown or interrupted response is never resent. This durable decision records
Host authorization intent; it is not a receipt that the Provider executed the
operation. Conflicting content cannot reuse an existing pending request ID.
Settled request identities remain fenced until their run ends, even when an
expiry write fails. The two expiry/replay probes first failed, then passed
after retaining that fence. Their expiry fixture was then corrected to include
the native automatic-expiry flag and assert the observed persistence error;
both corrected probes pass. Presentation subscribers remain ordinary event-bus
observers.

Fresh local checks of this follow-up:

- Permission owners, real SQLite persistence faults, Runtime, Work permission
  service/scopes, Cooperative scopes/contracts and EventBus: **80 passed**.
- Chat shutdown, Work handler and Provider abstraction: **60 passed** after
  extending the shutdown test double with the new Runtime hook. The initial
  run had 54 passed / 6 failed because that test double lacked the setter;
  those first failures remain distinct from the successful rerun.
- Real pinned Pi subprocess scopes: **8 passed**, including two newly started
  automatically approved Work runs with committed decisions and a third run
  waiting for user approval after the persistent policy is disabled.
- Repository Ruff, all seven generated architecture views, generated README
  overview and `git diff --check` passed. No UI source changed in this follow-up;
  the earlier Electron/render evidence below belongs to its recorded candidate.

中文：本轮先复现“权限请求落库失败但仍自动允许”，再在真实持久化拥有者修复。
请求记录和策略决定均成功提交后才派发；失败、取消或缺少拥有者时拒绝自动授权。
决定是授权意图，不能当作执行成功回执；未知结果不重发，相同请求身份不得换参数。
结算写入失败也不会释放旧请求身份供重放。三组选定回归共 **148 项通过**；首次失败
与修复后复跑分别保留。只使用临时 SQLite、
本地合成模型/MCP 和真实 Pi 子进程，未调用外部模型或替换日常安装版。

## Deterministic checks

- Real Pi 1.2.0 subprocesses, a loopback synthetic model and local MCP peers:
  repeated calls, changed arguments/tools/connections, revocation, cancellation,
  native session restoration and connection changes.
- Host contracts: request publication before automatic response, immediate
  grant-use/expiry audit, immutable user decisions, unknown responses not
  resent, concurrent Work isolation, hot enable/disable and partial failures.
- Presentation contracts: ordinary Work/Cooperative approval and revocation,
  multiple grants, foreground ownership, background expiry, persistent settings
  reopening and accurate feedback after partial application.
- Electron `npm test`: 529 passed. `npm run build` and Pi typecheck passed.
- Final approval/Provider/Work/Cooperative/Canvas/wallpaper regression selection:
  77 passed, including real Pi calls across two automatically approved Work runs
  followed by a third run that waits for approval after the switch is disabled.
- The complete isolated Python suite ran in four shards on the documented
  CPython 3.12.10 baseline: 5,934 passed, 34 skipped and 4 failed initially.
  The new scrollbar declaration violated the existing shared-surface contract;
  it was removed, and the wallpaper/Canvas checks then passed (16 checks).
  An unchanged texture-cache HTTP test hit a Windows connection-aborted error;
  its complete module passed on rerun (25 checks).
- Two unchanged optional Codex Skill tests remain blocked before model/tool
  execution: their fixture sets `windows.sandbox = 'mxc'`, which both the local
  CLI 0.130.0 and locked CLI 0.154.0 reject (accepted values are `elevated` or
  `unelevated`). That independent sandbox fixture is unchanged. These local
  failures do not establish an approval-policy failure; the original failures
  and reruns are reported separately from the hosted CI checks.

An initial CPython 3.12.14 run exposed existing Windows character-test fixtures
that clear `SystemRoot` before importing aiohttp/OpenSSL. Those source/test
files were unchanged. Their 20 checks passed on CPython 3.12.10 without changing
fixtures. No unrelated environment workaround is included in this change.

## Hosted CI / 远端 CI (2026-10-17)

All six hosted workflows succeeded for the frozen code candidate
`2dbb5dcef6f1a664679417d526430ad7c2871e89`:

- [Python Windows](https://github.com/Code-Amadeus/Amadeus/actions/runs/38909693805):
  all three full-suite shards, source/dependency checks, source and packaged
  Electron smoke, the environment ladder and the `cpu-model-less` gate passed.
- [Electron Windows](https://github.com/Code-Amadeus/Amadeus/actions/runs/38909693523):
  renderer tests/build, native UI checks, packaging and packaged wallpaper smoke passed.
- [Python Linux](https://github.com/Code-Amadeus/Amadeus/actions/runs/38909692876)
  and [Python macOS](https://github.com/Code-Amadeus/Amadeus/actions/runs/38909692749) passed.
- [Optional character RAG](https://github.com/Code-Amadeus/Amadeus/actions/runs/38909693771)
  and [local-model installation candidates](https://github.com/Code-Amadeus/Amadeus/actions/runs/38909693628) passed.

The original verification-note commit `7bd966f` changed this Markdown file only;
its executable code and tests were identical to that tested candidate. The local
optional Codex limitations above remain recorded independently of hosted results.

中文：冻结代码的六组远端 CI 全部通过，包括 Windows 三个完整测试分片、安装包启动
和汇总门禁。后续结项提交只补本文档，不修改已验收的代码或测试。

## Rendered evidence

These screenshots render the real Canvas source with synthetic Work identities
in an isolated headless browser. The before view uses the base commit; the after
views use the changed source. No live user Work, model credentials or real GUI
operation is involved. The active-grant check also verifies that the center of
the Revoke button is not covered by another element.

| Before | Exact-call choice | Active grant and persistent-policy status |
| --- | --- | --- |
| ![Per-call approval](before.png) | ![Exact-call approval](after.png) | ![Visible revocation and policy](active.png) |

中文：默认关闭全自动；可选择跨重启保存的所有 Work 自动审批，或仅本次运行的确切
Pi MCP 调用授权。验证使用本地合成模型、MCP 和隔离浏览器，未替换日常安装版或操作用户应用。
