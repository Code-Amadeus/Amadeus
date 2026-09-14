# Amadeus artifact 风格实验 v0.1

状态：2026-09-13 的隔离实验记录。后续已将收敛后的指导与基础 CSS 接入
生产 authoring 资源；本目录继续保留实验原样与比较材料。

正式开关位于 Settings → Work Providers → Artifact appearance → Use Amadeus style，
默认开启，保存后重启后端应用于后续 AUIP 创作。生产指导位于
`skills/auip-authoring/references/artifact-style.md`，基础资产为
`skills/auip-authoring/assets/amadeus-v1.css`。它不直接引用本实验目录。
现有应用和明确的用户设计要求优先；样式随应用交付，不由 Host 动态注入。
入口预检会拒绝缺失/加载失败的 CSS；布局与焦点观察只作为质量提示。

## 这版设计的准则

**越靠近系统操作，越统一；越靠近应用内容，越自由。**

目标是“安静的实验室终端”：用现有 CRT Slice / Preview 的深蓝绿底、
薄荷青交互、等宽信息标签和细分隔线建立辨识度。

- 系统外框仍由 Host 提供，应用不重复绘制窗口标题栏，不制造连接或参与事实。
- 操作区共享颜色变量、按钮、输入控件、焦点和文字层级，但不规定布局模板。
- 棋盘、图表、画布和正文保留领域配色与排版；大面积界面表面使用深色或低饱和
  中性色，暖色主要用于小面积盘片、标记和点缀。
- 同时控制面积、饱和度和亮度：覆盖面积越大，色彩越克制。约 80–90% 深色/
  中性色结构可作为构图参考，暖色点缀通常控制在主画面的 10–15% 以内；这是
  设计建议，不是代码配额。照片、文档、数据等真实内容保留自己的颜色。
- 青色用于交互与少量重点，不要求所有图形都变成青色。
- 正文保持可读；不照搬 Slice 的微型字号，不给整页文字加扫描线与发光。
- CRT 效果向内容区减弱，主要通过线条、字体与交互细节延续风格。
- 应用自己的视觉调整必须保留清晰的焦点、状态和真实的内容含义。

给 Provider 的当前准则在 [DESIGN.md](DESIGN.md)，基础样式在
[base.css](base.css)。这份 CSS 仅在 `.am-app` 下按需启用，不控制页面布局。
最初的 v0 准则仍保留在前两次运行的隔离 workspace；v0.1 根据用户对大面积暖色
的反馈修订。[PALETTE_REVIEW.md](PALETTE_REVIEW.md) 记录了具体视觉修改要求。

## 实验结构

1. 主持者手工制作光谱实验台，检查桌面、390px、键盘、记录与重置。
2. 冻结 DESIGN.md / base.css / provider-task.md，并记录 SHA-256。
3. 通过真实 `ProviderRuntime → WorkLedgerCoordinator → CodexAppServerAdapter`
   在独立 workspace、独立 ledger 中生成汉诺塔；使用当前配置模型。
4. Provider 得到设计准则与 CSS，没有得到光谱页面源码。
5. 保留首版，独立检查真实页面和真实 Managed-Core handlers。
6. 对首版观察到的问题做一次独立的 Provider 修订，分别记录结果。
7. 用户指出大面积暖色压过既有风格；主持者先做深灰绿、低饱和盘片试样，
   再给 Provider 具体视觉决定，只调整应用 CSS，保留已验证的玩法与接入。

对照页 [index.html](index.html) 可以切换样例并显示/隐藏现有 Preview 样式。
外框复用仓库 CSS，但这只是视觉对照页，不等于真实 Electron 窗口、Attach、
角色参与或用户验收。

## 本地重现

从仓库根目录运行（此机器已具备的环境）：

```powershell
.venv_cu124/Scripts/python.exe -X utf8 examples/auip-style-lab/check_reference.py
.venv_cu124/Scripts/python.exe -X utf8 examples/auip-style-lab/run_provider.py --live --timeout 900
.venv_cu124/Scripts/python.exe examples/auip-style-lab/serve_lab.py --port 8767
```

浏览 `http://127.0.0.1:8767/examples/auip-style-lab/`。
服务仅监听本机，响应头通过 CSP 禁止网络连接，保证视觉实验不会建立生产
AUIP WebSocket。停止该 Python 服务即可关闭预览。

`run_provider.py` 不传 `--live` 只打印说明。新运行路径记录在
`runtime/auip-style-lab/latest-run.json`；每个 run 保存报告、原始事件与完整
workspace，不改生产 ledger。自动运行只验证创作与入口，不自动启动/Attach。

对输出独立检查：

```powershell
.venv_cu124/Scripts/python.exe -X utf8 examples/auip-style-lab/check_provider.py <workspace> <evidence-output>
```

修订实验使用 `--seed <首版workspace> --feedback examples/auip-style-lab/REVIEW.md`，
创建另一个隔离目录并拷入应用源码；不覆盖首版。

本次生成内容、完整 SDK bundle、截图与运行日志保留在被 Git 忽略的 `runtime/`
目录；它们是本机实验输出。准则、样例源文件和实验工具留在 `examples/`。
对照页中的生成样例链接对应本次本地输出，换机器需要重新运行并更新 result.js。

## 判断方式

分别判断辨识度、内容自由度、可读与可操作、装饰克制、交互真实五项。
主观设计评分不替代功能证据；Provider 完成状态和 Host entry 校验也不等于
视觉、领域行为或完整产品验收。单个玩法的一次生成、一次功能修订和一次用户
指导的配色修订，只能证明当前流程可行，不能声称一次生成便满足风格要求。
