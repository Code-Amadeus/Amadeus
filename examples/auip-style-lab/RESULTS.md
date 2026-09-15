# Artifact 风格实验结果 · 2026-09-13

后续集成说明：本记录保留三轮隔离实验及其验收边界。用户随后批准将准则与
基础 CSS 接入默认创作路径，并要求默认开启；正式资源、开关位置及校验范围
见 [运行说明](README.md)。这项后续集成不会改变下述历史运行的 Host verdict。

## 结论

“准则 + 小型基础 CSS”可以把现有 Amadeus 的视觉语言传递给 Provider，
同时保留应用自己的布局与内容色。但 v0 对“大面积暖色内容区”的许可过宽：
用户看到汉诺塔后指出，暖色面积和饱和度压过了既有风格。**v0.1 因此把自由
限定在深色/中性基调之内：大面积表面克制，暖色用于小面积内容和点缀。**

最终样例采用深灰绿棋盘、低饱和陶土/旧黄铜/灰青盘片。盘片靠大小和位置保持
语义，青色仍用于操作与状态。它没有成为光谱页面的布局复制品，也没有依靠
整页 CRT 扫描线维持辨识度。

这不是一次生成成功的证明：实际做了 1 次独立生成、1 次功能纠正、1 次用户
指导的配色修订。后两次给出了具体反馈；只能说明该方向和反馈流程可行。

## 实际运行

全部使用当前配置 `gpt-5.6-terra / medium`，经 Amadeus 的真实
`ProviderRuntime → WorkLedgerCoordinator → CodexAppServerAdapter` 执行。
每次使用独立 workspace 和 ledger，不创建生产 Chat 任务、不启动/Attach 应用。

| 运行 | 本地目录（相对仓库根） | 输入与结果 |
| --- | --- | --- |
| 生成首版 | `runtime/auip-style-lab/direct-full-host-145ea10c36` | 冻结 v0 准则、base.css 和任务；未提供光谱源码。风格部分迁移，但圆盘显示顺序反了，浅棋盘焦点弱，离线启动有未处理的连接错误。 |
| 功能修订 | `runtime/auip-style-lab/direct-full-host-f5c97ae295` | 首版应用 + REVIEW.md；纠正上述问题，同时消除重复 manifest 和不真实的固定序列投影。暖色方案保留。 |
| 用户配色修订 | `runtime/auip-style-lab/direct-full-host-1ec34be939` | 已修正应用 + v0.1 准则 + PALETTE_REVIEW.md；改变 CSS 配色与材质。应用 JS、manifest、base.css 与前一版字节一致；HTML 的变化仅为同步工具在生成 manifest 插槽加入空白。 |

每个运行目录的 `report.json` 包含模型配置、输入 SHA-256、Provider 结果、
Host verdict、Work/Attempt 身份与输入未修改检查；`events.jsonl` 保留原始事件。
每个 `project/` 保留当次输入与完整产物。后续没有覆盖前次产物。

## 独立检查

参考光谱页：1280px / 390px、参数变化、记录观察、重置、键盘 range、可见
焦点、reduced motion 和页面错误检查通过。曲线标注为示意，并不声称物理测量。

最终汉诺塔的 [独立检查数据](../../runtime/auip-style-lab/palette-final/checks.json)
记录 16 项通过，包括：

- 初始、移动后、完成时的真实 DOM 位置与棋局快照一致。
- 本地移盘、非法移动不改棋局/步数、键盘七步完成、完成后重置。
- 加载真实 Managed Web 应用后，用同一份真实 snapshot/action handlers
  构造隔离 Managed Core，验证七次接受回执、陈旧 revision 拒绝和完成后重置。
- 390px 无横向溢出、reduced motion、键盘焦点可见。
- 有 SDK 但离线时没有未处理页面错误；完全阻断 SDK 时原始玩法仍然可用。

焦点线与棋盘渐变端点的对比度：首版最低 **1.10:1**，功能修订后最低
**3.14:1**，深色配色版最低 **11.58:1**。这是对相关颜色的检查，不是完整
WCAG/辅助技术审计。

三轮的 entry/bundle 启动检查均通过；前两轮 Host 应用交付 verdict 通过。
**第三轮 Host 应用交付 verdict 没有通过**：`bundle_validation_verified=true`，
但 `current_attempt_contributed=false`，因此 `application_verified=false`。
本实验工具将纯 CSS 修订作为另一个新建 `prepare` Work 运行，输入里已存在且
未改动的 manifest/entry 没有成为该新 Work 的已验证应用贡献。页面和 CSS 是
可检查的输出，但不能据此声称第三轮获得了 Host 的应用交付/启动资格。
这是实验设置与贡献验证边界的限制；没有通过改写 manifest 版本或生产代码来
绕过它。若后续验证真实交付，应走同一已绑定 Work 的 amendment/贡献路径。

功能修订的独立
preflight 在输出 `ok: true / diagnostics: []` 后，Playwright 关闭阶段另有
`TargetClosedError` 的未取回 Future 警告；它属于测试工具清理诊断，不能写成
整个工具进程完全无诊断。本任务没有改动生产验证器。独立应用页面检查没有该
页面错误，真实玩法也经过了单独检查。

对照页复用现有 Preview CSS，可显示/隐藏外框且不重置当前应用；支持参考、
首版、暖色修订版、低饱和版切换。预览服务器的 CSP 禁止网络连接，所以这页
不建立生产 AUIP WebSocket；这不是实际 Electron/Host Attach 验收。

## 设计判断

按 DESIGN.md 的 0–2 五项量表，主持者对 v0.1 的判断为 **9/10**：辨识度 2，
内容自由度 2，可读与可操作 2，装饰与层级 1，交互真实 2。层级扣分来自大屏
棋盘留白与柱体比例仍可精修。评分反映本次目视判断，不替代用户的审美判断。

v0 虽然形成了不同布局，但用户反馈说明其色彩面积规则不足，不能再将暖色版
称为视觉意图完整达成。v0.1 的提升来自明确的面积/饱和度规则及具体反馈，
尚未用多个新任务或多次无指导生成验证泛化能力。

## 产物与下一步边界

- [中文准则与运行说明](README.md)
- [Provider 当前设计准则](DESIGN.md)
- [基础 CSS](base.css)
- [交互对照页](index.html)
- [参考页](reference.html)

当前全部是隔离实验，生产 authoring skill、Host 注入、AUIP 协议、默认路径和
语义快照没有改变。若继续验证，适合用阅读型和数据型内容检查 v0.1 能否稳定
保留差异；有多个独立样本后，再考虑将准则与 CSS 接入默认 authoring 资源。
