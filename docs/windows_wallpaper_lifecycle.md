# Windows managed wallpaper lifecycle

Windows supports two startup routes, selected in **Settings → General → Startup**:
**Open control panel first** (the default), then click **Wallpaper** in the sidebar;
or **Enter wallpaper directly**. Both routes use the same managed
Lively lifecycle. The preference is saved locally and takes effect after fully
quitting and reopening Amadeus; it does not change the current session or restart
the backend. The BAT launcher stays unchanged. Ordinary startup does not prepare
or install Lively or start the wallpaper wake service, so users can begin with
text chat without setting up ASR/TTS. Existing saved startup choices are respected.
Upgrades from the previous direct-wallpaper default also open the control panel
when no startup choice has been saved and no launch override is set. Choose
**Enter wallpaper directly** to retain automatic wallpaper entry.

The wallpaper composer has a borderless gear button, **Open control panel**. It
reveals the Electron main window without stopping wallpaper. The tray provides a
second entry. Closing the control panel while Windows wallpaper is active hides
it again, including when wallpaper was entered manually from the sidebar.

The existing macOS/Linux startup rules and Electron scene policy are unchanged.
Explicit `--wallpaper` / `AMADEUS_WALLPAPER=1` and the Windows opt-out
`--no-wallpaper` / `AMADEUS_WALLPAPER=0` take precedence over the GUI preference.
`AMADEUS_WALLPAPER_HOST=external` retains manual Lively/Wallpaper Engine hosting,
including with **Enter wallpaper directly** selected. Startup mode selects the
presentation; host selection independently disables Amadeus-managed Lively setup
and mounting for external hosts.

## Startup and exit

1. Electron starts its existing Python runtime. Selecting Wallpaper (or the
   direct-wallpaper startup route) starts the wallpaper bridge and managed host.
2. The Windows-only setup checks the existing Lively installation. If missing,
   Windows Package Manager installs the pinned official 2.2.1.0 release and its
   dependencies, with installer autolaunch disabled.
3. A new Lively profile is initialized before its first launch: no setup wizard,
   WebView2 selected, no separate Lively login startup or tray icon. Existing
   profiles are preserved. Windows permission prompts can still appear during
   dependency installation.
4. The Windows helper connects to Lively's named-pipe RPC, waits for readiness,
   saves the previous wallpaper state, writes the Amadeus URL project and mounts
   it. Only then does Electron create its existing interactive Slice.
5. Turning wallpaper off or choosing **Quit Amadeus** from the Amadeus tray
   removes the Amadeus scene and restores the previous scene. Full application
   exit restores the wallpaper before stopping the runtime. Closing the main
   window hides it; the tray's Quit command performs a full exit.

The Windows tray shows preparing, starting and restoring states. Operations
that remain in setup or restoration for more than three seconds also show a
Windows notification (subject to the user's notification/quiet-time settings).
Setup explicitly identifies that first use may download/install Lively. Failed
setup or mounting can be retried in the same Amadeus process after the external
problem is repaired; rejected attempts are not cached. A failed stop is reported
by the desktop host and resolves `false` across IPC rather than rejecting into
renderer event subscriptions.

There is no permanent Windows wallpaper replacement. Lively's optional
screenshot-to-desktop/lockscreen features are suppressed while the Amadeus scene
is active, including the startup restoration of an existing Lively profile.
The underlying Windows picture/slideshow is left intact. The original browser
is selected before recreating the previous Lively scene; its image-update
options are restored afterwards.

In per-monitor mode only the primary display is taken over. Existing span or
duplicate arrangements are preserved. Restoration uses monitor device IDs and
the original Lively project, not a screenshot of its last frame. It does not
attempt to restore a video's exact playback timestamp.

If the user selects another wallpaper or changes layout during the session,
their new selection wins. An already-running Lively is never shut down. A
Lively process started by Amadeus is shut down only after restoration and only
if no new user-selected scene requires it.

## Recovery and ownership

`%LOCALAPPDATA%\Amadeus\wallpaper\session.json` is the durable before-image.
It is written and flushed before desktop mutation. A cross-checkout file lock
prevents concurrent Amadeus sessions from overwriting it. The helper stays alive
when Electron dies; stdin EOF triggers the same restoration as normal exit.
If the helper is also killed, the next managed start recovers the journal before
taking another snapshot. Failed restoration and observed postcondition failures
retain the journal and report an error. Confirmation uses the host's reported
state, subject to the stock Lively protocol limitation described below.

The journal now adds an optional `Phase`: `Preparing` before session settings,
`Pending` with `RequestProcess` (PID and process start time) before dispatching
`SetWallpaper`, and `Settled` only after the native
Set RPC returns normally, before mount confirmation. A normal RPC return only
means that request is no longer pending; it does not prove that a scene mounted.
Failure during settings preparation restores settings without setting or closing
wallpaper. A settled mount failure attempts the original automatic rollback,
including an originally empty desktop, using the exact submitted project path
for scoped close. A reported remaining Amadeus scene outside the original
monitor/path pairs, or a missing original scene that this cleanup attempted to
restore, keeps the journal for retry. An original scene may itself use the same
managed project; restoring that exact monitor/path pair remains valid.

`Session.Mount` owns its failed-mount rollback. The helper uses
`Session.RunMounted` to run its active body and arrange exactly one cleanup only
after Mount succeeds. If rollback or body cleanup also fails, both the primary
and cleanup errors are retained. The Session remembers the process identity
whose cleanup it has taken over, including journal recovery. The outer error
handler only releases a newly started host whose identity is still outside that
ownership and for which no journal exists. Successful cleanup removing a journal
does not authorize another host shutdown; a later host with a different start
identity can still be released if initialization fails before takeover.
A transport/deadline failure after the request receiver is durably recorded but
before native Set returns leaves `Pending`; it does not trigger automatic close,
restore or host shutdown, because the request may still mount late.
`RequestProcess` identifies the actual server of that Set pipe,
independently of `OwnedProcess`, which only governs cleanup ownership. Recovery
is blocked while the recorded process identity remains alive. Confirming its
exit only permits the normal restoration attempt and postcondition checks; it
does not itself prove restoration. Manually restarting Lively is supported, and
does not transfer ownership of the new host to Amadeus or authorize stopping a
host showing a user's new wallpaper. A failed identity query retains the journal
and reports the failure instead of assuming the old process exited.

The Set adapter is coupled to the locked GrpcDotNetNamedPipes 3.1.0 transport. Its
single internal `PipeCallback` binding captures the actual request stream; the
request marshaller reads `GetNamedPipeServerProcessId` after that stream connects
and persists the identity before any request bytes are written. The adapter does
not probe a second connection, retry, or substitute a process-name guess. If the
binding, identity read, or journal write fails, no Set is dispatched and settings
preparation can roll back. A library upgrade must rerun the real synthetic-pipe
contracts before accepting this boundary.

The library's response deadline can expire while its background connection or
serializer is still running. A single dispatch gate closes before `Send` returns
and waits for an already entered identity/persistence boundary to finish. A
connection arriving after that closure cannot identify a receiver, save a journal
or write the Set request, so an unstarted request can finish settings-only rollback.
If persistence already began, the caller waits for it and retains `Pending` on a
deadline error. The transport write may still follow that persisted boundary;
this is uncertain dispatch protected by the journal, not proof of cancellation.

Old published journals without `Phase` retain their original semantics. A
`Pending` journal without `RequestProcess` is invalid: recovery reports an error
without rewriting the journal or changing the desktop. That format and
`MountUnconfirmed` existed only in this PR's unpublished drafts; neither main nor
v0.17.0-alpha.0 wrote `Phase`. Their compatibility branches have been removed
because no existing local journal required them. No recovery data was deleted.

Recover any pending journal with this helper before reverting to an older helper.
Older binaries do not understand `Phase`; downgrade while a journal remains is
not supported, and no recovery record is reset or deleted to enable it.

日志新增可空 `Phase`：设置准备前为 `Preparing`，派发 `SetWallpaper` 前写入
`Pending` 和 `RequestProcess`（PID 与进程启动时间），原生 Set RPC 正常返回后、
挂载确认前才写入 `Settled`。正常返回仅表示该
请求不再 pending，并不证明已挂载。设置阶段失败只恢复可能部分写入的设置，不重设
或关闭壁纸；已结算的挂载失败恢复原自动回滚，包括原来为空的桌面，并用最初提交的
确切项目路径关闭自身场景。恢复后仍在原 monitor/path 配对之外报告 Amadeus，或
本次应恢复的原场景缺失，都会保留日志以便重试；原场景本身可以使用同一维护项目，
恢复该确切 monitor/path 配对仍然有效。

`Session.Mount` 自行负责挂载失败回滚；helper 经 `Session.RunMounted` 运行挂载后
的主体，只在 Mount 成功后安排一次清理。主操作和清理同时失败时保留两个异常。
Session 已接管某个进程的清理（包括旧日志恢复），即记录其身份；外层异常处理只在
进程尚未被接管且没有日志时释放新启动的宿主。成功清理删除日志不代表可以再次停止
宿主；若之后启动的是另一启动身份的宿主，在接管前初始化失败仍可释放该新进程。
若已持久保存请求接收进程、但原生 Set 正常返回前发生传输或 deadline 错误，日志
维持 `Pending`，不自动关闭、恢复或停止宿主，因为请求仍可能迟到挂载。
`RequestProcess` 记录该 Set 管道的实际
服务端进程，与仅用于清理所有权的 `OwnedProcess` 分离。该确切身份仍存活时拒绝恢复；
确认退出仅允许尝试正常恢复和核验后置条件，不代表恢复已成功。支持用户手动重启
Lively；这不转移新宿主的所有权，也不授权停止承载用户新壁纸的宿主。身份查询失败
时保留日志并明确报错，不臆测原进程已退出。

Set 适配器耦合锁定的 GrpcDotNetNamedPipes 3.1.0：仅一处内部 `PipeCallback` 绑定
取得实际请求流，marshaller 在该流连接后读取 `GetNamedPipeServerProcessId`，并在
写入任何请求字节前持久保存身份。不另开探测连接，不重试，也不以进程名称猜测替代。
钩子、身份读取或日志保存失败时不派发 Set，设置准备可正常回滚。升级传输库前必须
重新通过真实合成管道契约，才能接受该边界。

传输库的响应 deadline 可能先于后台连接或序列化结束。单一派发门在 `Send` 返回前
关闭，并等待已进入的身份读取／持久化边界完成。关闭后迟到的连接不得再识别进程、
保存日志或写入 Set 请求，因此尚未开始派发的请求可完成仅恢复设置的回滚。
若持久化边界已开始，调用方须等待其结束，deadline 错误保留 `Pending`。传输写入
仍可能随后发生；这是由日志保护的不确定派发，不能据此声称取消成功。

已发布的无 `Phase` 旧日志保持原语义。缺少 `RequestProcess` 的 `Pending` 日志
视为无效：恢复明确报错，不改写日志或改变桌面。该格式和 `MountUnconfirmed` 仅
存在于本 PR 未发布的草稿；main 与 v0.17.0-alpha.0 均未写过 `Phase`。没有现存本地
日志需要这些兼容分支，因此已将其移除；未删除任何恢复数据。

回退旧 helper 前，须先用本 helper 处理未完成日志。旧程序不理解 `Phase`；日志尚存
时不支持降级，不会为允许降级而重置或删除恢复记录。

This change is limited to phase-aware rollback and error preservation. It does
**not** repair stock Lively 2.2.1.0's reporting/close protocol: `GetWallpapers`
can return a falsely empty stream, and project close can swallow errors and
return success without removing a player. A close response plus an empty stream
cannot prove strong recovery or player exit. For an originally empty desktop,
the retained baseline empty-report behavior can still delete the journal and
request owned-host shutdown while an unreported scene remains. This is a known
unresolved limitation, not an accepted strong-recovery guarantee. Scene removal
is not inferred from a PID, a layout file or a timing heuristic to hide it.

本次范围仅为按阶段回滚和保留异常，**未修复**原版 Lively 2.2.1.0 的报告及关闭
协议：`GetWallpapers` 可能假空，项目关闭也可能吞错后返回成功却没有移除播放器。
关闭回执加空流不能证明强恢复或播放器退出。对于原来为空的桌面，保留的 baseline
空报告行为仍可能在未报告场景存在时删除日志并请求停止自有宿主。这是明确未解决的
限制，不能视为强恢复已验收；本轮不以 PID、磁盘 layout 或延时推断场景移除来掩盖它。

Deterministic contracts separate fake actual state from reported state and cover
rollback, retries, late requests, user choices, journal compatibility and both
errors. Randomly named local pipe contracts additionally verify that Set cannot
execute before Session persists the actual pipe receiver, and executes zero times
when identity, persistence or transport binding fails. These pipes never connect
to Lively or change the desktop. They also cover a connection arriving after its
response deadline and a persistence boundary blocked across that deadline. The
separate `--false-empty-counterexample` diagnostic records the
unresolved false-empty/no-op-close case without adding it to the passing contract
count. These checks do not establish real Lively mounting or close completion.

离线契约分离 fake 真实状态与报告状态，覆盖回滚、重试、迟到请求、用户选择、日志
兼容与双重异常。随机命名的本地管道契约还核验：Session 持久保存实际接收进程前
Set 不得执行，身份、持久化或传输绑定失败时执行次数为零；这些管道不连接 Lively，
也不改变桌面；还覆盖响应超时后连接迟到、持久化边界阻塞跨越响应超时的竞态。
独立 `--false-empty-counterexample` 诊断记录未解决的假空及关闭
no-op 反例，不计入契约通过数；这些检查不证明真实 Lively 挂载或关闭完成。

If Lively must be started with its screenshot-to-wallpaper options temporarily
disabled, `Lively Wallpaper\Amadeus.launch-settings.json` backs up just those
two launch settings until RPC can restore them. This is not a second desktop
snapshot. The `recover` command also handles an interrupted launch preparation.

Manual recovery, after closing Amadeus:

```powershell
build\windows-wallpaper\host\Amadeus.Wallpaper.exe recover
```

`recover` is an operator diagnostic/recovery command, also used by the desktop
experiments; the regular application calls `run`, which performs recovery
inline. Normal exit and a parent-only crash already trigger cleanup. If both
processes are killed, or restoration fails, recovery requires a later managed
start or the diagnostic command. There is no always-running repair service.

Individual RPC deadlines are not an overall exit/setup deadline: several
displays can take longer to restore, and an external installer may stall.
Status/notifications make the wait visible, but this implementation does not
force-kill the installer or recovery process after an arbitrary timer. A global
cancellation/time-budget policy remains a release-acceptance item.

## Build and distribution

Source runs need .NET 8 SDK on the build machine to build the helper once.
The published Windows x64 helper includes .NET 8.0.31, so end users do not need
an SDK. The Electron Windows packaging hook builds and includes the helper and
its dependency notices. It runs no Windows setup for macOS/Linux targets.
Experimental logs, profile backups and screenshots live outside the published
`host` directory and are not included in the application package.

Electron packaging writes to `electron/build`, separate from compiled input
in `electron/dist`; otherwise electron-builder excludes its own output folder
and can omit the compiled main entry. The package explicitly includes the
Windows tray icon under `resources/assets/icons/app`. A missing or invalid icon
does not abort startup, and the main window remains visible if the tray lacks
an icon.

```powershell
powershell -NoProfile -File scripts\setup_windows_wallpaper.ps1 -BuildOnly -Rebuild
# Also prepare/install the host when required:
powershell -NoProfile -File scripts\setup_windows_wallpaper.ps1
```

Lively remains a separate unmodified GPL-3.0 application acquired from the
publisher through winget. The helper talks to a version-specific RPC interface;
unsupported Lively versions fail visibly and are not silently downgraded.
An already-running Microsoft Store installation can be reused. A stopped Store
installation currently needs to be started once; setup detects it and refuses
to install a duplicate. Fully automatic provisioning currently targets the
standalone installer distribution, and requires Windows App Installer/winget.

This change does not turn the source release into a complete packaged Amadeus
runtime installer. Existing Python/model/asset provisioning remains unchanged.

## Windows Slice and input

Windows opens the restored CRT Slice and text composer collapsed. Existing
attention requests can still open their approval surface. The original input
label/dot and authored location remain; expanding reveals a wider, shallow
CRT-styled composer with borderless image, voice and New chat controls.

Image short press attaches a normalized preview (the same image preparation as
Chat); long press toggles watching; right-click selects a window or full screen.
Failed sends preserve the draft and attachment. New chat uses the shared Session
owner and clears the draft only after successful creation.

Windows wallpaper startup defaults to wake-word standby, respecting explicit
process, desktop-setting and `.env` overrides. The gray microphone/standby label
reflects real backend state. Wake detection or microphone activation enters
continuous conversation through the existing wake ASR and auto-send route;
there is no second dictation listener. Short and long microphone presses toggle
that same conversation state. Stopping returns to wake standby; folding the
composer does not stop an active conversation. Wallpaper exit stops its wake
conversation. Other platforms retain the existing hot-window timeout behavior.

`electron/scripts/smoke-wallpaper-composer.cjs` runs the real Slice page in native
Electron against a local bridge fixture, including pointer short/long/right
clicks, attachment encoding/retry, new-chat failure, collapsed startup and legacy
platform behavior. It does not capture microphone audio or make model calls.
The ASR contract tests exercise continuous and timed sessions using synthetic
utterances; hardware microphone quality remains a manual acceptance check.

## Host loss and compatibility

An unexpected native helper exit (including Lively loss reported by the helper)
now invokes the authenticated backend wallpaper-stop endpoint. The backend clears
wallpaper ownership before awaiting cleanup, stops its wake ASR conversation and
wake service, and publishes the usual wallpaper-exited event. Failed installation
or mounting uses the same cleanup. If the owned backend cannot acknowledge the
stop, Electron uses its existing graceful shutdown / owned-process termination
path, so a disconnected host cannot leave that runtime listening indefinitely.

Explicit user shutdown follows the same ownership rule. Both the wallpaper
toggle and switching to Render forward a failed `wallpaper.stop` RPC to the
Windows main process, which uses the existing HTTP cleanup/backend-stop fallback
before completing native restoration. The UI does not report a successful close
when that fallback cannot confirm cleanup. The backend's `wallpaper.exited`
notification still performs surface-only cleanup, without another stop request;
macOS/Linux retain their existing stop path.

The stop request has a five-second local acknowledgement budget. Both Python
HTTP servers use daemon request threads, so shutdown does not join the long-lived
chat/SSE requests; each server uses the default 0.5-second shutdown poll and a
one-second thread join. Wake/microphone cleanup adds its own bounded joins, so
five seconds is an escalation policy rather than a complete worst-case bound.
When this fallback stops the backend, the recovery message explicitly asks the
user to restart Amadeus and explains that WebSocket reconnect alone cannot revive
the process. Original host/setup errors and any cleanup errors remain visible.
Window/dialog failures cannot skip backend cleanup or escape as rejected exit
notifications.

Voice behavior is tracked separately in issue #112; the confirmed default is
standby followed by continuous conversation after waking. With the wake word
enabled, continuous conversation returns to wake standby once the Qwen hot
window passes after the wake or the last reply, as a timed wake session does
(`max(WAKE_AWAKE_SECONDS, ASR_IDLE_UNLOAD_SECONDS)`, 180 seconds by default).
The wake word or the microphone button resumes it. Without a wake word to return
to, continuous conversation has no idle deadline. For continuous sessions,
`awake_remaining` and `awake_remaining_s` count down to wake standby and are
`null` when there is no idle deadline. Timed sessions retain numeric values.
External status clients must accept that nullable state.
ASR stop with a source ignores a different owner, including an empty source;
unscoped `asr.stop {}` keeps the original Chat behavior.

Packaging explicitly includes compiled main/preload/renderer files and package
metadata, plus the Windows icon/helper/setup resources. Python/model/asset runtime
provisioning remains external. CI additionally launches the actual unpacked exe
with the checkout explicitly supplying that Python runtime, verifies connected
Chat/Settings/navigation, and closes both frontend and backend. This proves the
packaged executable path rather than a complete standalone installer.

`electron/scripts/smoke-startup-settings.cjs` exercises the actual settings page,
preload and settings store in a separate profile with all network blocked. It
verifies both persisted choices and their launch policy without a running backend.
The composer native smoke also checks the gear button; the real-desktop lifecycle
probe checks that opening/closing the control panel preserves the mounted scene.

## Ambient character cadence

The idle scenario activates 60 seconds after the most recent voice playback
ends. All voice playback, including background work narration, resets this
clock; duplicate stopped-state events do not restart it. The quiet clock uses
elapsed time rather than render ticks, so a lower frame rate cannot change it.
Once active, each pose samples a 45–60 second dwell before the graph can choose
the next pose. Animated resources keep playing at their authored rate and finish
the current loop before changing poses. Speech interrupts an idle scenario;
work activity enters its scene immediately and retains its narration behavior.

`AMADEUS_SCENARIO_STATIC_SECONDS` overrides the default range with a fixed
interval. Restart the backend and reopen the wallpaper to reload
the policy; changing these values does not require re-encoding character assets.

## Verification

```powershell
dotnet run --project wallpaper\windows\Amadeus.Wallpaper.Tests
cd electron
npm test
npm run build
```

The separate protocol counterexample diagnostic is not an acceptance check:
独立协议反例诊断不计为验收通过：

```powershell
dotnet run --no-restore --project wallpaper\windows\Amadeus.Wallpaper.Tests -- --false-empty-counterexample
```

The Windows Electron CI job also runs the C# contract executable, builds an
unpacked Windows application with electron-builder (including `beforePack`),
and runs `electron/scripts/smoke-windows-wallpaper.cjs` against that package.
This smoke imports the packaged startup policy and tray component, creates a
real Windows tray, checks missing-icon behavior, and runs the packaged helper
in read-only `inspect` mode. It does not install Lively or replace the CI
desktop. Actual Lively mounting remains a separate real-desktop experiment.

Real desktop experiments (temporarily replace the current wallpaper and restore
it in cleanup):

```powershell
.venv\Scripts\python.exe tools\probes\windows_wallpaper_lifecycle.py
dotnet run --project wallpaper\windows\Amadeus.Wallpaper.Tests -- --cold-start-probe build\windows-wallpaper
```

The lifecycle probe requires a real renderer connection, then verifies the
original scene/options/running state after normal exit, parent-pipe loss, and
helper termination plus journal recovery. It also kills an actual parent so
both stdin and stdout readers disappear, and checks restoration still completes.
The cold-start probe backs up the
user's settings and layout, starts installed Lively with a fresh configuration,
checks that no first-run UI opens, mounts a test scene, verifies release of the
newly owned host, and restores the original profile in finally.
It is not a substitute for a clean-machine installer test.

For the complete Electron journey, run
`electron/scripts/probe-windows-wallpaper.cjs` using the Electron executable,
with stdout/stderr redirected to persistent files. Do not detach a GUI Electron
process from short-lived terminal pipes: subsequent console writes can raise
`EPIPE`. The probe launches the real backend and verifies the real before-quit
hook; it writes results to `build/windows-wallpaper/electron-experiment.json`.
Add `--host-exit` to terminate only the helper owned by the probe, verify backend
wallpaper shutdown and authenticated/origin-checked cleanup, and then restore the
desktop with the documented recovery command. No microphone is captured by this
probe; active ASR cleanup is covered by the synthetic lifecycle contract.

Validated locally: Windows x64, Lively 2.2.1.0, one active monitor. Multi-monitor
restoration and user changes are covered by contract tests; physical multi-
monitor and a clean-machine install still need release qualification.
