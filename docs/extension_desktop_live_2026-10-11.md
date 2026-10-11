# 插件桌面真机验收与下一步

2026-10-11，基于实验分支 `51e9baa`。本轮使用未修改的 production Electron renderer/main、真实 Python 后端、DeepSeek 主对话与 DSH ACP。
通过 computer-use 操作桌面窗口，任务确实从 Chat 进入 Host Work。设置、会话、Work 账本、DSH home 和插件安装均使用 `build/desktop-live` 下的隔离目录；关闭 TTS、唤醒与 VTS。
不是组件 mock，也不是直接调用 Adapter 代替用户入口；没有验证语音链路。

## 已完成的用户流程

1. 主界面的 Chat 提交 CSV 审核任务：在 Draft 工作区创建给定的 5 行 CSV，使用已安装的审核 Skill，输出 `audit.json`。
2. 主角色把任务交给 `deepseek_live`；Work 自动获得 Draft 工作区。真实 Agent 读取安装目录中的 `SKILL.md` 与 `references/rules.json`，写出文件。
3. Chat 显示执行进度、`Completed` 和角色结果。实际 JSON 精确包含负金额、重复 ID、两个缺值，共 4 项；源 CSV 与输入一致。
4. 从 Settings 的真实按钮重启后端，返回原 Chat。历史、Work 身份和文件均恢复。
5. 从同一 Chat 继续原任务，要求读取 `audit.json` 并生成 `issues.txt`。续接成功，账本中第二次 attempt 明确 `provider_session_attach.state = attached`；两次 attempt 使用同一 DSH session ID 和同一 Skill revision。
6. 从 Extensions 的安装按钮和系统文件选择框安装 handoff 0.2.0 wheel。界面先显示 `Restart required`，旧版本目录仍存在；点击 `Restart backend to apply` 后，新版本 Active，旧目录回收，pending revision 清空。

以上执行完成不等于用户已经验收：Host Work 仍是 `review_ready`，`completion = partial`；Chat 的 `Completed` 表示 Provider 已完成执行。
结构化证据和产物校验结果见 [桌面验收 JSON](evidence/extension_desktop_live_2026-10-11.json)。

## 未通过：普通文件的产物交付入口

通过主界面打开 **Artifacts → Draft artifacts**，页面显示 **No verified AUIP artifacts yet**。
当时 Host 已登记 `orders.csv` 和 `audit.json` 两个 business artifacts；续接后又登记了 `issues.txt`。
文件确实存在，执行记录中也可以看到内容，但用户不能在这个产物列表直接打开这些文件。

代码边界与现象一致：

- `ChatSessionRail.tsx` 的 Draft 产物入口调用 `onOpenDraftApps`。
- 当前产物页面消费经过验证的 AUIP 应用，而不是 Work 账本全部普通文件。
- `ChatWorkActivityCard.tsx` 仅展开过程证据，不提供普通文件交付操作。
- Work 账本已有归属、路径、状态与文件指纹，不缺第二套插件产物数据库。

这是 **Host 产物展示/交付层的缺口**，不是 CSV Skill、DSH ACP 或安装器失败。因此不能把本轮写成“完整桌面用户流程全部通过”。

另观察到一次重启成功提示与 `reconnecting` alert 同时存在；后端、插件和续接均正常。这条提示状态需要定向复现及修复，不能据它推断重启失败。

## 下一步实施顺序

1. **补普通文件交付入口。** 复用既有 Work/artifact 身份，在 Chat/产物列表暴露可打开的已登记文件；先覆盖 JSON、CSV、Markdown/文本。
   Host 按已登记 artifact ID、所属 Work/工作区及实际文件状态提供内容，界面不能把模型给出的任意路径当访问权限。
   展示执行完成、待用户验收及产物可用性各自的真实状态，不把 Agent 说明当验收事实。
2. **用同一桌面流程验收交付。** 从 Chat 发起，核对产物列表、内容打开、后台重启后的同一归属、文件缺失提示；同时复核重连提示。
3. **完善 Agent 原生 Skill 发现。** 通用 ACP 文件目录与 DSH 原生 registry 的差异仍按上一轮报告处理；先定清适配边界，再做自动配置，不在通用 ACP 中伪造安装 RPC。
4. **再进入记忆。** 保持 M0 → M2 → M1，先完成上下文位置、失败降级与 Host 显式记忆规则；本轮没有实施记忆模块。

## 本轮范围与保留状态

初次窗口访问授权超时，用户授权后继续。后续工具两次提示 `user input was detected in this window; call get_window_state before continuing`，因此停止进一步鼠标/键盘输入，避免覆盖用户操作。
本轮没有继续做“更新后向旧 Agent 会话提交任务”和桌面卸载两项；它们已有上一轮真实 Adapter 生命周期证据，但不能算成本轮 UI 验收。

隔离测试窗口保留供用户查看，没有继续发起模型任务。原始日志、安装记录、界面文字证据和合成产物位于 `build/desktop-live`；认证文件仅保存在该本地忽略目录，未进入报告或提交。
用户生产设置未修改。此次新增内容为验收记录与实施优先级，没有用产品补丁掩盖观察到的缺口。
