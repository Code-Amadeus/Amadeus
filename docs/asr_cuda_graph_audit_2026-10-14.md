# Qwen ASR CUDA Graph 短句实施与验证

日期：2026-10-14。范围为现有单卡 Windows Qwen3-ASR-0.6B 链路，音频 FA2、文本 SDPA；只测日常短句，按用户补充覆盖到 10 秒。本阶段承接[第一二阶段基线](asr_phase12_audit_2026-10-14.md)。已实现固定容量预捕获、捕获协调、可观测回退与生命周期释放。按实测结果默认启用 CUDA Graph，单独保留 384 桶；以下区分最终配置的正式测量与早期消融实验。

环境：RTX 4070 Ti SUPER 16GB，同一逻辑 `cuda:0` 承担 ASR/TTS；Windows 11 x86-64、Python 3.12.10、PyTorch 2.6.0+cu124、Transformers 4.57.6、qwen-asr 0.0.6、flash-attn 2.7.4.post1、驱动 581.57。Qwen 权重 revision 为 `5eb144179a02acc5e5ba31e748d22b0cf3e303b0`，BF16，现有 46-token context，自动语言和原有按时长计算的输出预算。当前应用保持运行，没有锁定 GPU 频率或宣称完全隔离系统噪声；结论限于本机这组配置。

## 固定容量预捕获

模型实例持有固定 KV 容量桶，默认仅保留 **384**。容量是 prompt token 加本次最大输出预算，不是秒数。57 个 ≤10 秒用例最大需求为 285，7 个性能样本中 8.57/9.29/9.57 秒分别需要 257/274/280，证明仅保留 256 无法覆盖这组日常输入。容量不足时在生成前明确选择 official，并记录原因。每张图捕获单 token 文本 decoder 与 vocabulary head，token、位置、M-RoPE delta、有效长度 mask 及 KV 地址固定，内容可更新；同一张图可服务不同长度短句。音频编码和 multimodal prefill 保留原有执行方式，随后将真实 prefill KV 写入选定桶。未写位置和 padding 被精确屏蔽，不把补零 KV 当作有效历史。

预捕获采用独立 stream、三轮预热、明确的 stream 等待和全局 capture error 检查；模拟数据不使用随机数，捕获后清空状态。graph replay 之外继续使用官方生成准备、logits processors、EOS/长度停止条件和解析。每个新请求覆盖真实 KV/有效位/位置；不捕获整段固定长度输出，也不预设每句话会生成多少 token。

参考 Aqua-TTS 的固定缓冲区、容量分桶和模型所有权设计，保留 Qwen 自身的多模态与 M-RoPE 语义。Aqua 的按 prompt 对齐长度建图、随机模拟数据以及失败后继续另一条推理路径没有照搬。本实现的桶 key 只取固定容量，避免随 prompt 长度增加图数量。

## 捕获与同卡 TTS 的协调

PyTorch 2.6 要求同进程同时只有一个捕获，而且捕获期间其它线程不得执行普通 CUDA 工作；热态 replay 可以在其它 stream 执行。[PyTorch CUDA Graph 约束](https://docs.pytorch.org/docs/2.6/notes/cuda.html#constraints)

当前 ASR 在后台懒加载，TTS 在独立线程执行，播放 idle 检查并不覆盖模型加载与恢复。因此新增一个共享的 speech CUDA 工作登记：普通 Qwen/TTS 推理可并发，捕获只能在没有其它 owner 工作时获得独占窗口。申请不等待已登记的其它 owner，忙时推迟捕获；捕获已经开始后，新工作等待该窗口结束。这样避免持模型锁等待另一推理线程造成死锁，也不会串行化整个 ASR/TTS 推理。

Qwen 的 load/prepare 在所选引擎需要 Graph 时尝试固定桶预捕获。已有全部桶时，prepare 是纯 CPU 返回，不再申请独占或同步 GPU。ASR manager 在开口准备阶段调用后端已有 prepare 钩子，允许重试此前因 TTS 活动而推迟的准备。真正识别时若桶未就绪、容量不足或设备/生成配置不支持，提前选官方路径并记录原因；没有识别异常后悄悄重跑。

停驻到 RAM、最后一个模型使用者关闭、fatal cleanup 或实际 attention 配置变化之前释放 graph、静态 KV 和对应引用，恢复后重新准备。重复设置相同 attention 保留已有图。sidecar 通过父进程明确传入的 engine 初始化同一预捕获策略，仍使用同一张显卡。

## 384 桶选择

在同一已加载模型上比较单 384 与按需选 256/384，7 个样本、每方案 10 轮、共 140 次正式调用和 14 次预热。70 对 token 全部一致。单 384 的配对中位耗时仅增加 **1.39%（95% 区间 0.08%–3.19%）**；较短样本的 p50 差距约 3–8 ms。额外保留 256 会增加约 36.4 MiB allocated，并增加一次预热/捕获，因此默认保留单 384。离线参数仍允许显式比较其它容量。

最终正式测量中，384 增加约 **50.4 MiB allocated、100 MiB reserved**，静态缓冲区与输出约 42.3 MiB。该轮预热 293 ms、capture 192 ms；reserved 包含 allocator 缓存，不是每请求新增显存。完整启动与 RAM 恢复成本单列，不能用这些局部数字代表首次响应。

## 短句质量

现有语料筛选为 57 个不超过 10 秒的用例，最大 9.79 秒，包含中英日短句、低音量、噪声、静音和片段。最终 official/384 Graph 共 114 次调用，57 个 Graph 用例均实际重放，没有 fallback。

**53/57 个用例文本和 token 完全一致**。四项差异逐项审阅如下，不能称为逐字零变化：

| 用例 | Graph 相对 official 的变化 | 对现有合成参考的影响 |
| --- | --- | --- |
| 日语 question 尾部静音 | 一处句号变逗号 | 少 1 个字符编辑 |
| 日语 continuity 原音 | 一处新增空格 | 字符编辑数相同 |
| 日语 continuity 低音量 | 少一个逗号 | 多 1 个字符编辑 |
| 日语 explanation 低音量 | 「違憲」变为「危険」 | 纠正词语，少 2 个字符编辑 |

24 个具有非空合成参考的用例共 1,086 字符，official 为 72 次字符编辑（CER 6.63%），Graph 为 70 次（6.45%）；两个纯静音用例均输出空文本。当前样本未观察到词义退化，但标点有一项局部退化。参考主要来自四句日语合成目标及其扰动，不是 24 句独立人工听写金标。中文、英文及拼接样本主要验证一致性；没有据此宣称真实中文或自然中英混说准确率提高。

最初 31 个 ≤6 秒用例还比较了 greedy，greedy 与 official 全部一致。最终对上述四个差异样本、英文和静音，分别执行 official／未捕获静态解码／Graph，各重复三轮，共 54 次功能诊断。**未捕获静态与 Graph 的 18 对 token 全部一致**，各方案内部重复也稳定。差异与静态计算路径相关，没有发现 capture/replay 或跨请求残留导致的额外变化；静态 4D mask 会影响 SDPA 的 kernel 选择，因此另设静态对照，避免把所有收益都归于 Graph replay。

## 最终短句性能

最终单 384、修复后的捕获边界：7 段 3.82–9.57 秒音频，official 与 Graph 各预热两轮、正式回放 20 轮，共 **280 次正式调用、28 次预热**。顺序逐轮轮换，图准备与热态统计分开；本任务正式测量期间不运行 CI 或其它 GPU 实验。对照是上一阶段选定的音频 FA2＋文本 SDPA＋official。

下表均为每方案每音频 20 次的 p50 / p95，单位 ms。

| 音频 | Official | 384 Graph |
| --- | ---: | ---: |
| 英文 3.82 秒 | 465 / 505 | **130 / 144** |
| 日文 4.98 秒 | 711 / 738 | **171 / 179** |
| 中文 5.55 秒 | 526 / 569 | **140 / 149** |
| 日文 7.56 秒 | 1,128 / 1,228 | **247 / 258** |
| 日文 8.57 秒 | 947 / 1,032 | **221 / 282** |
| 日文 9.29 秒 | 1,226 / 1,400 | **269 / 323** |
| 中英顺序拼接 9.57 秒 | 522 / 570 | **141 / 167** |

按同音频、同 repeat 配对，140 对的中位延迟下降 **75.17%（95% 区间 74.81%–75.57%）**；仅取 5–10 秒部分为 100 对，下降 **76.44%（75.34%–76.86%）**。区间由 20 个 repeat blocks 做 5,000 次 bootstrap 得出，只描述这些固定样本上的重复波动，不是整个真实语音人群的置信范围。中英拼接不等同自然 code-switching，且其输出 token 较少，不能只按音频时长解释耗时。

140 个正式 Graph 请求均实际 replay，共 3,140 步，无 fallback。两个引擎内部每个样本的 20 次 token 均稳定；跨引擎只有上述 8.57 秒 continuity 样本始终多一个空格，另外六段完全一致。

9.29 秒句子的 host 分段中位数：encoder 14.1→14.3 ms、prefill 39.6→38.6 ms、decode 1,164.8→208.6 ms；两者都生成 37 token，主要收益确实来自逐 token 解码。host decode_forward 只是提交/等待区间，不能把它当作纯 kernel 忙碌时间；完整 decode 仍包含选 token、停止条件等开销。指标截止于 ASR 返回，不包括 VAD 等待、LLM 或扬声器播放。

### 早期消融：收益并非全来自静态 KV

最终容量选择前，以 256/512 两桶对三段 3.82–5.55 秒音频比较 official、greedy、未捕获 static decode、Graph，各预热 2 轮、正式 30 轮，共 360 次正式调用。全部文本/token 一致。相对 official 的配对中位改善：greedy **2.58%**，未捕获静态解码 **11.66%**，Graph **74.01%**；Graph 相对相同静态实现未捕获时仍下降 **70.63%（69.82%–71.24%）**。这些数值用于解释归因，不与最终单 384 的正式数据混算。prefill 与 attention 的独立比较保留在上一阶段报告。

## 同卡 TTS：最终 9.29 秒输入

使用当前生产 GPT-SoVITS checkpoint、参考声音及原有 TTS CUDA Graph，ASR 固定为 9.29 秒自然连续日语句子。official/384 Graph × serial/overlap × 两句 TTS × 20 轮，共 **160 次调用**。每句 TTS 只有一个波形 hash，且与上一阶段基线相同；全局 math flags 不变，80 个 Graph ASR 请求全部实际使用 Graph。ASR 文本在这组测试中也完全一致。

首有声片段 p50 / p95，单位 ms，每格 20 次：

| TTS 用例与调度 | ASR official | ASR 384 Graph | p95 变化 |
| --- | ---: | ---: | ---: |
| 短句 顺序 | 379 / 509 | 376 / 438 | -14.0% |
| 中句 顺序 | 498 / 592 | 483 / 516 | -12.9% |
| 短句 重叠 | 687 / 739 | 592 / 637 | **-13.7%** |
| 中句 重叠 | 858 / 970 | 741 / 772 | **-20.4%** |

TTS 总生成 p95：短句顺序 979→869 ms，中句顺序 1,201→1,039 ms，短句重叠 1,559→1,099 ms，中句重叠 2,037→1,337 ms。本轮所有观测 p95 都未退化。顺序条件的配对中位变化较小，部分区间跨零，不能把尾部下降解释为修改了 TTS 算法或保证所有场景更快。

重叠时，TTS 首段配对中位延迟分别下降 **11.22%（95% 区间 6.05%–14.37%）**、**14.63%（9.68%–15.18%）**；总生成下降 **29.54% / 33.31%**。同一轮 ASR p50 分别从 2,194→398 ms、2,437→366 ms，配对中位延迟下降 **80.87% / 84.49%**。TTS 的首段还包含自身 frontend、prefill、语义生成和声码器，减少 ASR 干扰不会等比例缩短这些固有工作。

早期 3.82 秒 ASR＋两句 TTS 的 160 次实验同样波形一致，顺序条件观测 p95 在 +5% 容差内，重叠首段 p95 下降 18%–23%。它作为较短输入的补充证据单独保留，不与本轮 9.29 秒结果混合统计。

首段指标截止于生成有声波形，不是扬声器播放或完整聊天端到端延迟。模型常驻、图已准备和热参考 cache 的正式测试，与冷启动/捕获争用的契约测试分别处理；不会拿缓存未热的第一句和热态句直接比较波形。

## 默认行为、恢复成本与模块边界

按最终测量，`Qwen3ASRBackend()` 默认选择 Graph、预捕获单 384；保留 `inference_engine="official"` 作为显式对照入口。CPU、ROCm、不支持的生成配置、未准备或容量不足的请求在生成前选择 official，并记录实际引擎和原因。没有修改用户配置、重启当前应用或热替换其模型；默认策略在下次正常启动后生效。

最终 384 生命周期实机验证共 13 次识别，包含默认构造的进程内后端与真实 sidecar。文本/token 与基线相同；park 释放图及其模型引用、恢复后重新准备、共享模型在最后一个使用者关闭时释放、未经准备的恢复先走 official、随后 prepare 恢复 Graph 均通过。sidecar 由父后端明确传入 engine，启动时捕获；删除了没有真实调用方的额外 prepare 协议分支。

三次 RAM 恢复：official prepare 为 **537–560 ms**；384 Graph 的恢复加预捕获为 **823–871 ms**，增加约 286–313 ms。把 prepare 和随后识别相加、完全不隐藏准备成本时，三段 official 为 1,011–1,445 ms，Graph 为 953–1,089 ms，逐样本仍较快。这只是三次生命周期诊断，不当作 p95 或稳定收益保证。现有开口准备可与录音重叠，本轮准备均小于 1 秒，但没有把公式估算当作真实麦克风端到端测试。

共享已热模型追加单桶的完整 prepare 实测 323 ms。首次进程内 official load API 为 18.74 秒、独立 sidecar 启动含 Graph 为 26.49 秒，含导入和权重初始化；两者部署不同，不拿差值归因于 Graph。park 后该基准进程仅余 512 bytes allocated / 2 MiB reserved；最后关闭后还有约 16.25 MiB allocated 和 allocator reserved，不能把 reserved 缓存描述为模型泄漏或声称显存绝对清零。

职责归属维持明确：Manager 管任务和准备时机；Backend 管模型生命周期；`qwen_inference.py` 管 Qwen 生成语义、路径选择及测量；`qwen_cuda_graph.py` 管固定地址状态与捕获重放；`core/cuda_execution.py` 是不依赖 Torch 的跨 ASR/TTS 捕获协调器。TTS 只登记工作及申请捕获窗口。没有把 Qwen 解码规则放到 Manager，也没有让 ASR 管理 TTS 模型。Qwen 适配层仍依赖当前 Transformers 的生成/cache 接口，升级这些依赖需要运行语义与实机回归。

## 冷启动顺序修复

补充测试发现，PyTorch 2.6 中首次 Graph 在 inference mode 内捕获，会使后续 TTS 的 no_grad 捕获因共享 RNG seed/offset tensor 类型而失败。纯 PyTorch 的两个四元素确定性图即可复现，无 Qwen/TTS 模型或用户数据；首图改在 no_grad 下捕获则通过。该问题已对应上游 [issue 166792](https://github.com/pytorch/pytorch/issues/166792) 和[已合入修复 172706](https://github.com/pytorch/pytorch/pull/172706)，不重复提交。[本地核验记录](evidence/pytorch_cuda_graph_inference_mode_2026-10-14.md)保存了最小复现、已核验提交和未测试范围。

本地将静态缓冲区初始化和捕获限定在 `inference_mode(False) + no_grad()`，即使调用者外层启用了 inference mode，也不会把跨模型共享状态建成受限 tensor。运行时 ASR 推理继续使用 inference mode；不重置全局随机种子、不修改 TTS 算法或升级依赖。

修复后的真实模型验证覆盖 ASR 先捕获、TTS 后首次捕获；双方捕获请求争用时提前 defer；TTS 的 static 路径和随后重新捕获；ASR 未准备图时明确走 official，再准备后恢复 replay；ASR park 清理后 TTS 已有图继续可用。相同参考/session cache 条件下三种 TTS 路径波形 hash 与原基线完全一致。测试中的人为会合只验证并发契约，不纳入延迟统计。

## 回归与审计

最终相关 14 个 Python 文件逐文件隔离执行，**271 passed、1 skipped、0 failed**。唯一跳过项是需显式 opt-in 的 NVIDIA kernel probe；CUDA 功能与性能由上述真实模型实验另行验证。覆盖生成语义、padding/M-RoPE、EOS/预算、请求复用、固定 buffer 地址、捕获模式、模型停驻/共享、sidecar bootstrap、普通工作并发、捕获互斥、异常/stream close 释放、TTS reference cache 和线程消费。TTS AST 测试夹具补充了新增轻量 decorator 的真实依赖，未绕开该边界。

独立 PR 工作树按锁文件安装无 Torch 的 CI profile 后，相同 14 文件为 **147 passed、6 个可选模型模块跳过**。全仓 Ruff、7 份架构视图、README overview 和 CI 环境检查均通过。静态检查收尾仅补了 continuation token 的显式初始绑定及 pytest fixture 再导出，两文件再跑 **38/38 通过**。重建本地 CUDA profile 后另行复核：114 次 ASR 文本/token 与原运行相同，TTS 冷捕获契约报告和波形 hash 也相同；这轮是功能复核，不替代正式性能数据。

当前最终补丁未重新执行整套 CI。上一阶段 450 个 suite 的历史结果保留原记录：一次 localhost 临时连接失败已单独通过；工作区既有 TTS catalog 文案与旧断言不一致的失败未混入本次修复，也不能声称历史全量全绿。结构化汇总和源码/原始结果 SHA-256 见[脱敏验证证据](evidence/asr_cuda_graph_validation_2026-10-14.json)。

## 复现

实现入口是 `asr/qwen_cuda_graph.py`、`asr/qwen_inference.py`，生命周期由现有后端锁保护，捕获协调位于 `core/cuda_execution.py`。回放命令和指标说明见 [README](../tools/asr/README_qwen_replay.md)。原始音频、转写和逐调用结果保留在忽略目录 `output/diagnostics/asr-cudagraph-20261014/`，不上传。
