# ASR 第一、二阶段实施与审计（2026-10-14）

后续：本页保留第一、二阶段的历史选型与数据。当前 CUDA Graph 默认策略和 5–10 秒补测见 [CUDA Graph 实施与审计](asr_cuda_graph_audit_2026-10-14.md)。

状态：正式性能比较已完成；分段计时、同音频回放、prefill 和 greedy 候选已实现。选定的生产路径是 **音频 FA2＋文本 SDPA＋官方生成循环（inference_mode）**，prefill/greedy 保留离线候选。此前存在并行合成负载的时间数据全部排除出最终延迟收益结论。按用户最新要求，默认选型只依据日常短句及同卡 TTS，已有长音频数据放入附录。

## 已实现的边界

- `asr/backend.py` 提供通用请求上下文；Manager 标记 speculative、final、recovery，并把推测提交、作废、命中的 request ID 与推理指标关联。该上下文不依赖 Qwen，也不改变识别结果的提交权限。
- `asr/qwen_inference.py` 保留官方音频归一化、重采样、分块、语言处理、提示词构建和解析。每次请求创建轻量 wrapper，复用同一个已加载模型和 processor，不跨请求保留 KV。
- `official` 使用官方生成循环并启用 `torch.inference_mode()`；`prefill` 只对 vocabulary projection 保留最后一个位置；`greedy` 另使用 Transformers 的 callable `custom_generate` 接口。
- greedy 的首次前向保留官方 multimodal embedding、audio scatter、mask 与 M-RoPE；后续直接调用同一个文本 decoder，并使用官方 continuation position 公式。官方 generation config、logits processors、stopping criteria 和 KV 更新仍参与执行，去掉重复的 CUDA 标量首步判断。
- greedy 的支持域明确限制为 batch 1、普通贪心、dynamic cache；不支持的生成模式显式报错。没有异常后悄悄重跑、全局 monkey patch、依赖替换、量化、编译或 ASR CUDA Graph。
- in-process 和 sidecar 共用推理实现；生命周期锁覆盖推理及临时 hooks，异常时也移除 hooks。模型 park/restore 仍由原后端拥有。
- 生产默认 `official`；NVIDIA 且 FA2 可用时，在加载后显式验证音频 FA2、文本 SDPA。CPU、既有 ROCm eager workaround 和未安装 FA2 的加载选择保持原有行为。in-process 与 sidecar 共用组件策略；没有新增用户配置项。prefill/greedy 通过 Python 构造参数及离线 CLI 选择。

## 计时解释

| 字段 | 边界与含义 |
| --- | --- |
| lock_wait_ms | 等待后端生命周期锁 |
| restore_exposed_ms | 当前请求实际承担的 RAM → GPU 恢复；提前恢复已隐藏的时间不重复计入 |
| frontend | prompt、tokenization、特征构建及 context token 统计 |
| h2d | 输入 tensor 移到模型设备和 dtype |
| generation_prepare | 官方生成准备至第一次模型前向 |
| encoder | 音频 tower 前向 |
| prefill | 第一次 thinker 前向减去 encoder，包含音频嵌入合并、位置、文本计算和 head |
| decode_forward | continuation 模型前向累计时间 |
| decode | generation 减准备及第一次前向，包含选 token 和停止检查；decode_forward 是其子项 |
| detokenize | 生成 token 一次复制到 CPU，并解码 |
| total_wall_ms | 后端/回放请求外层的总耗时，包含锁、恢复及官方外层预后处理 |

普通模式是 host elapsed time，不把 CUDA 异步提交时间假称为纯 GPU 计算时间。显式 `--profile` 才记录 CUDA events，结束时统一同步一次；event elapsed 仍可能包含 stream 空隙，不等于 kernel busy time。没有新增逐 token 计时同步。逐 token 的官方停止判定同步仍存在，也是下一阶段优化需要处理的边界。

指标包含请求类型、关联 ID、实际设备、音频长度、context token 数、生成 token 数、EOS/budget/criteria、各组件实际 attention、warm/restore 状态。生产日志不包含转写文本或 token ID；本地 `--include-text` 回放可保留它们以审查差异。

## FlashAttention 的实质发现

原启动日志的 `attn=flash_attention_2` 只证明 loader 收到了该参数。在本机安装的 qwen-asr 0.0.6 / Transformers 4.57.6 中，实际配置为外层 FA2，thinker、audio tower 和 text decoder 都是 SDPA。现在日志区分 `loader_attention_requested` 与实际组件配置。

离线比较三种明确路由：全 SDPA、全 FA2、audio FA2 + text SDPA。反复切换时对各拥有组件调用公开 setter 并检查实际配置，防止深层配置没有跟随切换。不能把外层参数改变当成已经测到 FA2。

这与上游已有 issue 201 相符；PR 202 关闭但未合并。已保存[脱敏上游核对记录](evidence/qwen_attention_upstream_note_2026-10-14.md)，没有发布新 issue。另一个长音频 audio window mask 问题与性能路由有关系，但须与逐 token 优化区分；本次没有改写该 mask 实现。

## 验证环境与语料

- 单进程、单卡 RTX 4070 Ti SUPER 16 GB，逻辑设备 `cuda:0`。不实施分卡；nvidia-smi 的物理索引与 PyTorch 序号不同。
- Windows 11、Python 3.12.10、PyTorch 2.6.0+cu124、Transformers 4.57.6、qwen-asr 0.0.6、flash-attn 2.7.4.post1；BF16，Qwen3-ASR-0.6B，权重 revision `5eb144179a02acc5e5ba31e748d22b0cf3e303b0`。
- 保留现有 auto language、context 和按时长 token budget。每组比较使用同一个 PCM、模型实例及配置，按样本和重复轮次轮换 attention × engine 顺序。
- 9 个原始来源构成 67 个回放用例：中、英、日语，原声、低音量、前后静音、20/10 dB 合成噪声、1/3/6/15/30 秒长度、中英顺序拼接和纯静音。
- 变换用例不是 67 段独立真人录音；中英拼接也不代表自然 code-switching。24 个日语派生用例有 TTS 合成目标文本，另外 2 个纯静音有空参考；它们都不能替代人工听写标注。
- 无人工标注的用例只能验证转写一致性。CER 以原始 Unicode 字符计算，含标点；CJK 的空格分词 WER 没有可靠意义，不用于质量结论。

## 质量和功能结果

完整 SDPA/FA2 质量矩阵已完成：67 用例 × 2 attention × 4 engines，共 536 次回放。同 attention 内三个候选与 raw official 的 402 组文本比较全部一致；prefill/greedy 与 instrumented official 的 token 比较也全部一致。1 秒与 3 秒纯静音在两种 attention 下均为空文本。

SDPA 与 FA2 之间，所有 engine 都在同样 9 个用例出现文本变化。24 个有非空合成参考的日语派生用例，共 1,086 个参考字符，SDPA 为 82 次字符编辑（7.55%），FA2 为 73 次（6.72%）；这个小型合成参考集未出现单例 CER 增加。有标点和空白差异，也有日语词语变化，不能把 attention 切换当成仅改变速度。两段无标注英语的变化涉及断句/大小写及数字书写。该结果不是人工标注准确率结论。

混合 audio FA2 + text SDPA 另完成相同 67 用例 × 4 engines 的 268 次回放，同 attention 内 201 组文本比较和 prefill/greedy token 比较均无差异。该配置相对 SDPA 有 9 个用例变化，相对全 FA2 有 5 个；合成参考集字符编辑为 72/1,086（6.63%），相对 SDPA 无单例 CER 增加，但相对全 FA2 有一例多一个标点编辑。不能据这组小样本宣布普遍准确率优势。

三种 attention 的完整质量矩阵共 **804 次回放、603 组同 attention 文本对比、402 组 prefill/greedy 对 official 的 token 对比，均无引擎间差异**。所有 instrumented official 用例均以 EOS 结束。

混合 attention 下另完成官方／greedy 两种引擎、英日两段音频的 4 次 warm、**20 次 RAM → GPU restore** 和 **4 次重新加载模型**。每段音频跨引擎、warm、restore、cold 的文本和 token 序列全部一致；四个组件的实际 attention 配置也保持正确。未观察到旧 KV、mask、RoPE 或设备引用污染，恢复性能另在正式数据中记录。

首轮同卡 TTS 功能验证使用生产 GPT-SoVITS checkpoints、参考声音、现有 CUDA Graph 路径，先预热，再分别顺序及并发执行 ASR/TTS。固定两个日语句子、配对随机种子；16 次有效调用中，同一句 TTS 波形 hash 一致，全局 math flags 不变。该结果只支持这两个用例的波形一致性；该阶段的延迟未用于最终选型；正式重测见后文。首个 voiced chunk 指生成出的语音片段，未测物理扬声器播放延迟。

后续混合 audio FA2 + text SDPA 配置下，raw official / official / prefill / greedy 四个 ASR engine × 两句 TTS × serial/overlap × 两轮，共 **32 次同卡调用**全部完成。每句仅有一个 TTS 波形 hash，并与此前 SDPA 测试完全一致；ASR 文本 hash 也一致。TTS CUDA Graph 保持开启，全局 flags 未改变。这个阶段确认功能与已测波形没有回退，不对时延作结论。

同卡 TTS 工具还支持在一个进程内轮换 attention × engine，避免正式测量时以两个独立运行混淆配置收益与负载变化。新增 8 次 current/mixed × raw official/greedy × 两句的并发功能 smoke 完成，实际配置切换正确、波形与前述基线一致、全局 flags 不变。三轮 TTS 功能验证共 56 次有效调用；这 56 次只作功能证据；用户确认动画结束后的正式测速见后文。

最初 TTS 测量脚本误把流头部的 `None` payload 转成数组；已按生产协议跳过流头后重新运行。那次失败属于测试工具问题，不是 TTS 生成回归。

## 代码与测试审计

针对最终补丁的推理语义、resident lifecycle、启动、handoff/endpointing、sidecar bootstrap 共 **67 项测试通过，0 skipped / 0 failed**：Qwen 推理 25、停驻生命周期 24、启动生命周期 6、handoff/endpointing 11、sidecar bootstrap 1。沿用此前 63 项的五个文件，新增 4 个加载 attention 策略用例；逐文件隔离运行，正式延迟测量期间不运行 CI。检查包括双 EOS、长度预算、long/short 交替的语义契约、真实小型上游 audio encoder/placeholder、M-RoPE/动态 KV、generation config、规范化和解析、异常移除 hook、拒绝不支持的采样模式、推测作废不提交及 mixed attention 重复切换。

最终生产入口另外完成 **10 次短音频功能调用**：in-process 五段（中/英/日短句、1 秒片段、静音）、三次英日 RAM restore，以及真实 sidecar 两次英日调用。不施加离线 attention override，核实加载后的实际混合配置、默认 official、请求 ID/类型、分段指标、恢复状态；转写及 token 序列均与已归档的 mixed official 结果一致。该功能检查不计入性能统计，没有再测 15/30 秒音频。

全量按文件隔离运行 450 个 Python suite。两项失败单列：

1. `test_texture_cache.py` 一次 localhost HTTP 连接收到 WinError 10053；整文件单独重跑 25/25 通过。未改动渲染服务器或该测试。
2. `test_system_settings.py:340` 断言 TTS 描述含旧短语 `checkpoint header selects`。工作区在本任务之前已有 `config/catalog/tts/gpt_sovits.json` 文案改动，旧短语被移除，与失败实得文本对应。保留用户的既有改动；这项全量回归失败仍存在，不能报告全量全绿。

Windows sandbox 下部分 asyncio 测试在 loopback socketpair 建立时阻塞；允许本地 socket 后按文件运行通过。未在产品代码中加入事件循环绕过以掩盖测试环境问题。

## 正式性能判定

此前用户在另一任务中做动画合成，因此首轮 ASR/TTS 时间只用于排查，全部排除出最终选型收益表。用户明确通知结束后，重新运行正式测量，记录了开跑时的源码指纹与 GPU 状态；没有并行执行 CI 或其它本任务 GPU benchmark。现有应用保持运行，开跑前目标 GPU 利用率为 0%；没有锁定频率或完全隔离 Windows 系统活动，因此仍保留尾部波动的限制。

正式重测使用固定中/英/日三段短音频，三种 attention × 四种 engine，各样本各组合 30 次热态回放；warmup 单列，报告配对中位数、p95、文本/token 一致性与 n。另进行了 RAM restore、同进程模型重新加载及独立 event profile。用户随后明确日常交流应聚焦短句，因此已测 15/30 秒数据只作附录，不参与默认选型，也不再追加该类测试。避免 CI、其它本任务 GPU benchmark 与正式测速并行。

同卡 TTS 复测覆盖 baseline 与入选候选、顺序/重叠执行、配对种子，报告生成首 voiced chunk 的 p50/p95、总耗时、波形一致性及全局 flags。必须把 ASR 独占收益与 ASR/TTS 竞争时的变化分开。

候选只有在对应质量和 TTS 检查无已知退化、且重复测量支持收益时才提升为生产默认。若 prefill/greedy/FA2 没有稳定收益，保留官方循环并说明结果，不为了兑现预期而启用候选。

## 正式热态短句矩阵（已完成）

每个组合每段音频 30 次，共 1,080 次计入统计的热态调用；另有 72 次预热。以下为 p50 / p95，单位 ms。所有同 attention 文本/token 比较及这三段短音频的跨 attention 文本比较均无差异。

| Attention | Engine | 中文 5.55 s | 英文 3.82 s | 日文 4.98 s |
| --- | --- | ---: | ---: | ---: |
| SDPA | raw_official | 597 / 801 | 539 / 659 | 826 / 1029 |
| SDPA | official | 559 / 802 | 501 / 593 | 821 / 1034 |
| SDPA | prefill | 548 / 687 | 489 / 560 | 815 / 1013 |
| SDPA | greedy | 537 / 646 | 487 / 591 | 779 / 996 |
| FA2 | raw_official | 630 / 837 | 557 / 692 | 925 / 1174 |
| FA2 | official | 592 / 760 | 516 / 615 | 832 / 995 |
| FA2 | prefill | 589 / 678 | 527 / 656 | 842 / 1018 |
| FA2 | greedy | 610 / 733 | 532 / 640 | 820 / 965 |
| 音频 FA2 / 文本 SDPA | raw_official | 623 / 795 | 532 / 650 | 843 / 1108 |
| 音频 FA2 / 文本 SDPA | official | 578 / 687 | 499 / 630 | 777 / 1119 |
| 音频 FA2 / 文本 SDPA | prefill | 555 / 660 | 510 / 730 | 787 / 1198 |
| 音频 FA2 / 文本 SDPA | greedy | 553 / 705 | 488 / 616 | 761 / 1113 |

相对原始有效 SDPA + raw official 的配对中位延迟下降（每个方案 90 对调用，按 30 个 repeat blocks 做 5,000 次 bootstrap，括号为 95% 区间）：

- 音频 FA2 / 文本 SDPA + greedy：8.28%（7.22%–9.18%）。
- 音频 FA2 / 文本 SDPA + official：5.64%（4.02%–7.00%）。
- 音频 FA2 / 文本 SDPA + prefill：5.88%（4.49%–7.04%）。
- FA2 + greedy：2.39%（0.57%–4.47%）。
- FA2 + official：2.26%（-0.17%–3.69%）。
- FA2 + prefill：0.92%（-0.39%–3.24%）。
- SDPA + greedy：8.16%（6.98%–10.19%）。
- SDPA + official：6.07%（5.04%–7.81%）。
- SDPA + prefill：6.57%（5.30%–8.46%）。

固定同一种 attention 后，prefill 相对 measured official 的配对中位变化均接近零，95% 区间跨零。greedy 相对 mixed prefill 的配对中位改善为 2.64%（2.03%–3.40%），相对 mixed official 为 2.27%（1.21%–4.11%）。所以不能把全部约 8% 的改善归功于 greedy；原始实现到 inference_mode/计时版官方循环已贡献主要部分。

全 FA2 + raw official 相对 SDPA + raw official 中位慢 6.38%（4.61%–8.96%）。这只适用于当前 Qwen/Hugging Face/Windows/batch=1 路径，不代表 FlashAttention 算法在所有模型与实现中较慢。

mixed greedy 的日语 p95 在此矩阵为 1,113 ms，原始 SDPA baseline 为 1,029 ms。另做 30 次/组合日语定点复测后，SDPA raw / greedy 的 p50/p95 为 817/929、746/1002 ms，mixed raw / greedy 为 845/1127、759/981 ms。约 8% 的配对中位收益可重复，尾部排序却发生变化，故不宣布 p95 已稳定改善，也不舍弃较差的一轮。约 8% 的热态总收益也尚未达到原先建议的 25% 工程目标。

独立 CUDA event/allocator 诊断不计入上表：短句 prefill 路径相对 official 的 PyTorch allocated 峰值降低约 5.4 / 11.3 MiB；30 秒样本的峰值均约 1,732.6 MiB，说明其它阶段决定其总峰值。这里是本进程 PyTorch allocator 口径，不是全系统显存。

混合 official 的两次短句诊断，中文/英文分别生成 15/13 个 token；host elapsed 的前处理为 2.27/2.30 ms、音频编码 16.30/14.09 ms、prefill 50.21/48.57 ms、decode 549.58/442.56 ms。该小样本仅定位阶段，不作为正式收益统计；event 结果同样显示主要间隔在 decode，但不等于已证明 GPU kernel 饱和或已量化 CPU launch 成本。

本机 Qwen 使用 Transformers 通用 FA2 mask/forward 路径；Aqua-TTS 则直接调用 flash_attn_with_kvcache。这两条实现不能据相同“FA2”名字预设收益相同。通用路径有 mask 布尔判断；有 padding 时还包含 unpad、nonzero 和取长度标量。当前单请求无 padding 的路径会跳过后者，不能把其成本当作这次变慢的已证实原因。未做 kernel-level profiler 归因。

独立 CPU operator trace 用 3.82 秒英文短句、13 个生成 token 检查实际调用，文本 mask 为 `[1, 111]` 且全有效。SDPA official / greedy 的 `aten::_local_scalar_dense` 计数为 79 / 43，全 FA2 则为 479 / 443；四者 `aten::nonzero` 都只有 1 次。全 FA2 确实进入 18 次 audio varlen flash 和 364 次 text flash 前向；SDPA 则出现 18 次 efficient attention 和 364 次 math attention。它确认了 FA2 切换真实生效，以及 greedy 减少标量读取，但 CPU trace 不区分所有标量的设备，也未量化每个读取的同步成本，不能据计数单独解释全部性能差异。SDPA 这个名称也不等于实际已使用原生 flash kernel。

补充读取当前安装的 PyTorch 能力：`torch.backends.cuda.is_flash_attention_available()` 为 `False`，而 flash/efficient/math 的启用开关均为 `True`。所以该 Windows 构建没有可用的 PyTorch 原生 flash SDP；外部 flash-attn 包的 FA2 是另一套可用实现。本次没有为追求某个 kernel 切换 PyTorch、修改依赖或全局 SDP 开关。

## 同卡 TTS 正式复测与候选淘汰

ASR 使用同一段 3.82 秒英文短句；TTS 使用现有生产 checkpoint、参考声音和 CUDA Graph，固定短句与中句、配对随机种子。每次运行都轮换 attention × engine，首 voiced chunk 指生成完成的首个有声片段，不是扬声器播放时间。第一次正式矩阵 320 次调用，第二次顺序执行确认 360 次；均保留全部样本，未按结果剔除尖峰。

第一轮 mixed greedy 在中句 serial 条件下，TTS 首段 p95 从 523 增至 689 ms，总耗时 p95 从 1,071 增至 1,358 ms；mixed raw official 也出现尖峰，不能把问题归因于 greedy 本身。第二轮这些尖峰没有以同样方式重现。合并两轮相同 serial 配置各 50 次后，中句首段 p95 为 baseline 525 ms、SDPA greedy 553 ms、mixed greedy 577 ms，分别仍有约 5.2% / 9.8% 上升。因此没有选择较好的一轮来宣布 greedy 已通过 TTS 门槛。

第二轮增加 official 对照。下表是 **mixed official** 对原始有效 SDPA raw official，各 case/config 30 次的 serial 结果，单位 ms（p50 / p95）：

| TTS 用例 | 指标 | 原始基线 | Mixed official | p95 变化 |
| --- | --- | ---: | ---: | ---: |
| 短句 | 首有声片段 | 366 / 508 | 370 / 424 | -16.4% |
| 短句 | 总生成 | 741 / 1,044 | 745 / 938 | -10.2% |
| 中句 | 首有声片段 | 463 / 525 | 466 / 533 | +1.5% |
| 中句 | 总生成 | 964 / 1,189 | 977 / 1,171 | -1.5% |

这四项观测 p95 均在事先采用的 +5% 容差内。这是有限样本的实测门槛，不是统计上证明所有文本、所有竞争负载都不存在退化。

最终 official 重叠矩阵另完成 **160 次调用**，explicit SDPA/mixed × raw official/official × 两句 × 20 轮。新生产加载策略已生效到该独立测试进程，故使用明确的 `sdpa` 作为旧基线，未把此时已代表混合策略的 `current` 误当作旧基线。以下仍是 mixed official 对 SDPA raw official：

| TTS 用例 | 指标 | 原始基线 p50 / p95 | Mixed official p50 / p95 | p95 变化 |
| --- | --- | ---: | ---: | ---: |
| 短句 | 首有声片段 | 557 / 659 | 561 / 662 | +0.3% |
| 短句 | 总生成 | 1,049 / 1,281 | 1,037 / 1,242 | -3.1% |
| 中句 | 首有声片段 | 709 / 824 | 729 / 845 | +2.6% |
| 中句 | 总生成 | 1,246 / 1,477 | 1,232 / 1,508 | +2.0% |

首段配对中位变化为短句慢 0.52%（95% 区间：快 0.97% 至慢 2.14%），中句慢 1.72%（慢 0.39%–6.12%）。因此应描述为“观测 p95 在 5% 容差内”，不能描述为严格零退化或统计上已证明 5% 非劣效。对应的 ASR 重叠配对中位延迟下降约 2.19% / 4.06%。

三轮正式 TTS 共 **840 次有效调用**，两句分别只有一个波形 SHA-256，且与此前功能基线一致；全部全局 math flags 不变，TTS CUDA Graph 一直开启。没有麦克风、扬声器或完整聊天端到端测量；这次覆盖的是实际模型推理链路。

## 最终默认路径与取舍

采用音频 FA2＋文本 SDPA＋官方生成循环。短句 ASR 相对原始路径的配对中位下降为 **5.64%（95% 区间 4.02%–7.00%）**；SDPA official 的 6.07% 与之接近，不能宣称混合方案是纯延迟的唯一赢家。选择混合方案还考虑了音频窗口语义及现有合成参考集未见质量退化；通用全 FA2 在本机更慢，文本端保留 SDPA。

prefill 的独立时间收益不稳定，显存节省仅几 MiB；greedy 在 mixed official 之上的额外中位改善约 2.27%，但 ASR p95 未稳定改善、同卡 TTS 两轮 pooled 尾部未通过既定观察门槛。两者实现与回放入口保留，暂不加入默认链路。原先建议的 25% 总收益目标未达到，不将其包装为已经完成的性能提升。

加载策略通过同一个 owning-component setter 应用并校验，失败显式报错；未增加全局 monkey patch、重试、线程数/TF32 修改、第二张显卡或 ASR CUDA Graph。当前应用没有被重启，生产策略在下次正常启动后生效。

## CUDA Graph 下一阶段

Aqua-TTS 的同卡实践使 Graph 成为有依据的优先候选，而不是只能最后考虑的方向。但尚未在本次 Qwen 路径里启用。

进入该阶段时复用成熟的工程经验：单请求静态 KV、少量长度 buckets、有效长度 mask、稳定 tensor 地址、每 bucket capture/replay 锁、捕获预热与 TTS 协调；保留 Qwen 自身 M-RoPE/多模态语义。park、设备迁移或模型替换必须使对应 graph 失效。测量 capture 开销和常驻显存，保留普通执行回退的明确条件，不在异常之后悄悄重跑识别。

下一步应分别测试 SDPA Graph 与可用的 FA2/KV 接口，不能把 Aqua-TTS 的 `flash_attn_with_kvcache` 收益等同于 Qwen 当前通用 HF FA2 的收益。本次短句基线可用于量化 Graph 的增量收益；后续继续以日常短句、同卡 TTS 首段响应为主，不再追加 15/30 秒基准。

## 复现入口

见 [回放说明](../tools/asr/README_qwen_replay.md)。本地原始音频、转写及逐调用记录保留在被忽略的 `output/diagnostics/asr-phase12-20261014/`；原始语料不提交或上传。配置、依赖、线上服务和 TTS 模型未被替换；当前运行的应用未重启，本次生产代码改动将在下次正常启动后生效。

汇总计数、实际 attention 路由、原始报告 SHA-256 与最终审计源码指纹见[脱敏验证证据](evidence/asr_phase12_validation_2026-10-14.json)。

## 附录：已完成但不参与对话场景选型的长音频检查

用户明确后续聚焦正常短句交流，本节只归档已有结果，不追加该类性能测试。1/15/30 秒补充矩阵各配置只有 5 次热态调用，另外各 1 次 restore 和同进程权重重新加载；计入全部 attention/engine 为 90 次 warm、18 次 warmup、18 次 restore、18 次 reload。所有文本/token 对比一致。

15/30 秒的原始 SDPA raw official p50 为 2,030/3,762 ms，mixed official 为 1,753/3,444 ms。这些数字不参与默认路径收益判断。mixed official 在三个时长各一次 RAM 恢复暴露约 539–563 ms；同进程模型重载约 1.74–1.97 秒，另加识别本身的耗时。重载保留进程、依赖及 OS 文件缓存，不能称为完整应用冷启动，也没有足够样本推断其 p95。
