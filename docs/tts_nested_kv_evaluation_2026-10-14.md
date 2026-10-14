# T2S Nested KV 实现与实测

日期：2026-10-14。实验分支：`codex/tts-nested-kv-evaluation`；草稿 PR 分支：`codex/tts-nested-kv-pr`（基于已包含 history/EOS 优化的最新 main）。

建议在 Amadeus 当前默认预捕获、多 Graph、单用户 TTS 配置中采纳 `nested`。保留各桶计算形状，把 K/V 物理存储合并为最大容量根缓冲区。当前权重和默认 17 图配置下，KV 常驻 **561 → 48 MiB**，稳定减少 **513 MiB**。主要收益是显存；交替 A/B 的 AR 耗时变化为 −2.15%～+3.52%，没有把它作为普遍加速方案。

**当前 Amadeus 的 CUDA Graph 模式原本就按完整 TTS 合成请求串行执行；`nested` 没有新增这条产品路径的并发代价。** 原有调度器选择 `cuda_graph_serial`，并在合成开始至完成期间持有 `graph_tts_lock`；Graph 模式选择也将合成并发度设为 1。decoder 的请求级锁把共享 KV 的所有权落实到直接调用边界，不会在这条已串行路径中减少可同时执行的 TTS 请求。普通 ASR 推理和音频播放仍可重叠。

有一个明确的适用边界：关闭预捕获、只使用一张 448 图时，KV 反而从 **21 → 48 MiB**。这类部署可以选择 `graph`。`bucket` 保留不同桶的独立工作区，默认预捕获的 KV 为 129 MiB；它在当前 Graph 串行管线中不会自动增加并发度。未来若绕过该管线直接并发调用同一 decoder，`nested` 的全局 owner 才会限制不同 Graph key 的请求交错；多 workspace slot 属于未来扩展范围。

## 核验的实现与真实容量

- Aqua-TTS：[`591fbb5f82052b6965612a3f50dbc201fbc7e261`](https://github.com/Lucas1479/Aqua-TTS/blob/591fbb5f82052b6965612a3f50dbc201fbc7e261/aquatts/modeling/t2s_streaming.py#L347)。该版本按 `(bucket, initial_len)` 为 Graph 分配独立 KV。其 [FA2 valid 路径](https://github.com/Lucas1479/Aqua-TTS/blob/591fbb5f82052b6965612a3f50dbc201fbc7e261/aquatts/modeling/t2s_flash_attn.py) 使用实际写入位置构造 `cache_seqlens`。
- GSV-TTS-Lite：[`6c049397142f4c9147a85f86b6ba37546e93a188`](https://github.com/chinokikiss/GSV-TTS-Lite/blob/6c049397142f4c9147a85f86b6ba37546e93a188/gsv_tts/GPT_SoVITS/GPT/t2s_model.py#L244)。SDPA 使用最大根缓冲区和短桶切片；[FA2 实现](https://github.com/chinokikiss/GSV-TTS-Lite/blob/6c049397142f4c9147a85f86b6ba37546e93a188/gsv_tts/GPT_SoVITS/GPT/t2s_model_flash_attn.py#L236) 同样保留各桶的视图。这里采用存储共享的思路，保留 Amadeus 现有 decoder、采样和路由。

本地实际权重配置为 **24 层、hidden=512、16 heads、FP16**。因此 K 和 V 各占 24 MiB，合计：

```text
24 layers × 1024 slots × 512 hidden × 2 bytes × 2 (K,V)
= 50,331,648 bytes = 48 MiB
```

外部审计的 24 MiB 合计不适用于当前 24 层权重。当前默认预捕获也实际得到 17 张图：448 桶 5 张、512 桶 2 张、768 桶 6 张、1024 桶 4 张。128/256 没有默认预捕获 key；短输入可以懒捕获。原实现的容量计算为 `(5×448 + 2×512 + 6×768 + 4×1024) × 24 × 512 × 4 = 561 MiB`。

## 实现边界

问题归类为 decoder 的存储分配与请求所有权边界。原 decoder 的锁只覆盖一次 replay；若外部调用者绕过已有 Graph 串行管线，prompt 重置和相邻 token 之间仍可能被另一请求覆盖。这个触发条件不出现在受 `graph_tts_lock` 保护的正常产品路径中。一个存储 owner 在完整 AR 请求期间独占，是本次落实在 decoder 层的核心不变量。

已有串行策略的代码依据（实验前 main `ef3a54b` 已包含这些行为）：

- [`tts/synthesis_backend.py`](../tts/synthesis_backend.py)：`select_synthesis()` 在 CUDA Graph 启用时选择 `cuda_graph_serial`。
- [`tts/pipeline.py`](../tts/pipeline.py)：`speak_stream_graph_serial()` 通过 `on_synthesis_start` / `on_synthesis_done` 获取/释放合成级锁；`reconfigure_tts_mode_name()` 为 `auto` / `cuda_graph` 设置并发度 1，为 `parallel2` 关闭 Graph 并设置并发度 2。
- 因此合成完成后播放与下一次合成仍可重叠；`Parallel ×2` 走 Graph 关闭后的请求私有 KV，不应把 nested Graph owner 的独占当作该模式新增的串行限制。

- `nested` 使用 `(layers, 1, max_bucket, hidden)` 的稳定 K/V 根张量；各层和各桶是视图。`bucket`、`graph` 分别按桶或 Graph key 分配根张量，用于明确的对照与部署选择。已经捕获的地址不会因新 key 被替换。
- 请求在 prefill 前获得相应 owner 的锁，覆盖 prompt 写入、捕获、所有 replay、采样和转动态 KV。锁释放时记录 CUDA event，下一个 stream 等待该 event，保护尚未完成的 GPU 操作，不增加逐 token 全设备同步。
- Graph 的输入、输出、mask、position 和 CUDA 私有内存池仍各自保留。KV 分配没有把 448/512/768 的 SDPA 计算形状扩至 1024。
- 请求直接将真实 prompt 写入 Graph 的 KV 视图，清空当前桶的未来位，保留真实连续写入位置。省去原先额外的请求级桶 KV 和捕获阶段的重复分配。
- Graph 被捕获协调器推迟时，使用请求私有静态 KV；桶满时复制完整有效历史，接续现有动态 KV。batch>1 使用私有 KV，避免写入 batch=1 Graph 缓冲区。
- 普通 ASR/TTS 工作继续使用 `speech_cuda` 的共享工作租约；仅捕获需要已有的独占窗口。TTS 工作区锁不覆盖 ASR 模型。

## 多片段聚合与分桶的适用范围

启用 utterance 聚合后，应区分首句与后续任务，而不能把每个排队片段都当成一个独立短句 AR 请求：

- [`TTSUtteranceScheduler._can_start_merge()`](../tts/utterance_scheduler.py) 明确排除 `is_first` 或 `stream_tts` 的片段。首句不为聚合等待 lookahead；但它的 bucket 仍包含参考文本音素与参考音频 semantic prompt，不能仅凭目标句字数认定一定命中 448。
- 后续允许聚合的连续片段按可播放缓冲、合成预算、字符上限、lookahead 超时及强句末等边界组成 `job.text`。[`get_sovits_params()`](../tts/pipeline.py) 对非首句且长度达到 45 的文本使用 32 步合成参数与“凑四句一切”；[`infer_stream()`](../local_tts_infer.py) 会根据该切分及换行得到实际 AR 输入片段。聚合 job 不保证永远只有一个 AR 请求，也不会因为播放侧保留多个 segment 就逐个重新选短桶。
- decoder 对每个实际 AR 输入按 `参考音频 semantic prompt + 参考文本/目标文本音素` 的 KV 长度选桶，保持 `ceil(KV/32)*32 + 96 < bucket` 的原规则。例如初始 KV 288 / 335 / 432 / 704 分别选 448 / 512 / 768 / 1024。聚合变长通常增加大桶或动态路径的机会；实际命中分布未记录，不能断言后续任务仍以短桶为主，也不能断言聚合就一定不能走短桶。

**此前完整语音/ASR 共存 probe 直接调用 `infer.infer_stream()`，没有经过 utterance scheduler、队列等待或播放覆盖预算。** 它对 short 和 medium 两类文本都调用 `get_sovits_params(..., is_first_sentence=True)`，使用首句 profile（4 步、`how_to_cut=不切`、首句速度/时长限制）。因此 20 对 waveform/ASR hash 一致与首段数据只证明这个预热后的首句 profile；medium 文本不能作为后续非首句聚合、32 步 SoVITS profile 的端到端性能证据。

固定工作量 AR 的 768/1024、长解码和溢出实验仍验证了这些 KV 长度下的存储、Attention 和续接行为；它们没有测量真实聚合调度的等待、实际桶命中率、聚合任务首个有声 chunk 或连续播放缺口。采用结论不依赖“聚合后仍大量命中短桶”，但后续聚合路径的端到端延迟仍是明确的验证空白。Graph/KV 分桶与 nested 存储布局独立，聚合增加长度时仍选择相应计算桶。

本次聚合范围复核运行现有 adaptive aggregation、deadline aggregation 和 TTS cost profile 合约测试，**62 passed**；未增加新的 GPU 性能采样。

## 显存实测

环境：Windows 11 x86-64、Python 3.12.10、PyTorch 2.6.0+cu124、CUDA 12.4、FlashAttention 2.7.4.post1、驱动 581.57。主测 RTX 4070 Ti SUPER 16GB，复测 RTX 4070 Laptop GPU 8GB。没有锁定 GPU 频率；每个独立布局、attention、device 的容量测试都在新进程中执行。

下表为主卡默认 17 图预捕获后的 MiB。KV 通过 storage 地址去重计数，不把视图重复计入。allocated/reserved 为该进程的 PyTorch allocator 指标；不是整卡 `nvidia-smi` 占用，也不是仅 KV。

| 布局 | KV 常驻 | allocated | reserved | 捕获阶段 peak allocated |
| --- | ---: | ---: | ---: | ---: |
| 原实现 | 561.0 | 727.49 | 880.0 | 751.49 |
| 新实现 `graph` | 561.0 | 727.49 | 796.0 | 728.50 |
| `bucket` | 129.0 | 295.49 | 364.0 | 296.50 |
| `nested` | 48.0 | 214.49 | 280.0 | 215.50 |

FA2 和 SDPA 的这些常驻指标相同。每图输入/输出合计约 0.14 MiB；其余分配包括模型权重、Graph 中间张量和算子工作区，没有宣称整段 runtime 只占 48 MiB。上述值在两张显卡上得到相同的 storage/allocated 结果。

关闭预捕获，仅懒捕获 `(448,288)` 的单图实验：原实现 KV 21 MiB、allocated 195.25 MiB；nested KV 48 MiB、allocated 222.25 MiB。稳态推理 peak 分别约 238.94 / 249.02 MiB。Graph 数量少时不能引用默认 17 图的节省数字。

## AR 场景与耗时

固定工作量实验使用相同权重、合成 phoneme/semantic token、零 BERT、相同 seed、原采样参数。EOS 仍执行检查，但诊断脚本忽略停止结果，固定运行指定步数；这些序列不作为语音质量样本。每场景预热两次，正式 7 次，记录 wall time、CUDA stream time、token hash 和 peak allocated。

主卡中位 wall time（ms）：

| 场景 | 实际初始 KV / 步数 | FA2 原实现 | FA2 nested | SDPA 原实现 | SDPA nested |
| --- | --- | ---: | ---: | ---: | ---: |
| 短桶 448 | 288 / 64 | 93.94 | 96.61 | 121.58 | 118.93 |
| 未对齐 512 | 335 / 96 | 133.82 | 132.08 | 183.06 | 177.48 |
| 对齐 512 | 336 / 96 | 133.66 | 132.41 | 180.72 | 175.16 |
| 中桶 768 | 432 / 192 | 262.70 | 260.28 | 402.91 | 393.31 |
| 长桶 1024 | 704 / 192 | 267.72 | 263.93 | 464.93 | 453.70 |
| 512 装满后动态续接 | 335 / 224 | 506.11 | 508.00 | 591.63 | 586.87 |
| prompt 超出桶路由 | 960 / 32 | 223.25 | 220.51 | 230.13 | 223.10 |
| 无参考输入 | 64 / 32 | 48.08 | 46.43 | 58.15 | 55.49 |

主卡每种新布局与原实现的 112 个 FA2/SDPA 对照全部 token 一致，包含桶满后的完整历史续接。batch=2 的原实现因 `[1,288,512]` 与 `[2,288,512]` 广播形状不符而失败；新实现三种布局的 batch=2 都实际执行私有 KV，Graph replay 数为 0。

笔记本卡初轮分进程耗时为 nested 增加约 2%–6%。为控制运行顺序/频率漂移，随后在同一进程同时保留两个 decoder，按 AB/BA 交替，每场景 20 对。此实验只评估耗时与 token；两个模型同时常驻时的显存不能代表单个布局。

| GPU / attention | 448 短句配对耗时变化 | 未对齐 512 | 768 中句 |
| --- | ---: | ---: | ---: |
| Ti SUPER / FA2 | −2.15% | −1.21% | −0.11% |
| Laptop / FA2 | −1.62% | −0.76% | +1.74% |
| Laptop / SDPA | −0.19% | +1.74% | +3.52% |

180 对交替实验的 token 全部一致。笔记本 SDPA 中句观察到约 3.5% 的配对耗时增加，不能据此认定 nested 导致了稳定回退。脚本固定先构建/预捕获 original，再构建 nested；交替执行顺序控制了部分运行漂移，却没有分离布局与显存放置/分配顺序。采纳建议考虑的是 513 MiB 常驻节省与当前单用户配置，不把这些观测差异当成已定位的因果成本，也不据此添加性能 workaround。

回归阶段另观察到，hidden=32/head=2、48/96 槽的极小型 FA2 图在两个相同布局的实例之间也不能稳定逐位重复。实验开始时保存的未修改 decoder 对照同样出现首步约 0.1006 的 logits 差异，排除了 nested 作为这个现象的必要触发条件。最终 GPU 回归采用当前权重的 head dimension=32 和对齐桶，固定双方每步输入并逐步核对 logits；真实权重的采样和完整波形另外保持严格 hash 对照。小模型后端现象的底层原因未确定，临时 FA2 调试改动全部撤回，没有添加兼容分支、扩大数值容差或据此提交上游 bug。

笔记本 FA2 初轮分进程有 4/28 个 token hash 差异。未修改的原实现以同样源码、权重和 seed 再跑，也在相同四项出现差异；nested 与这次重复原基线 28/28 一致。交替实验另外验证了 60/60 对 FA2 token 一致。因此不把初轮跨进程差异归因于共享 KV；底层数值差异原因没有进一步确定，保留在[证据](evidence/tts_nested_kv_validation_2026-10-14.json)中。

## 共享物理容量与最大计算桶的消融

另将 nested 的计算桶强制为 `[1024]`，比较同样五个场景。KV 仍是 48 MiB；短桶路由发生改变，预捕获图数为 19。两种 attention 各 35 对 token 与保留短桶的 nested 一致。

SDPA 的 448/512 短场景 wall time 增加约 **29%–30%**，768 中场景增加 **15%**；本就路由 1024 的长场景基本持平。FA2 valid 本轮没有出现最大桶惩罚，但分进程结果含运行噪声，不能推广为容量完全不影响 FA2 的 kernel 选择/性能。保留短桶对 SDPA 有明确价值，正是当前实现保留多桶 Graph 的原因。

## 真实语义与同卡 ASR/TTS

使用现有日语稳定性 probe，覆盖弱尾逗号、省略号、强句末、衔接句和普通句共 11 类，每类 5 个相同 seed，不启用语义重试。`graph` 与 `nested` 的 **55 对 token、停止条件和异常标记全部一致**。各布局都保留了 2 个既有异常样本；本次存储优化没有改变这项既有语义问题。

完整语音链路在首句 profile 下直接调用 inferencer（未经过聚合 scheduler），使用当前 GPT/SoVITS、CNHubert、BigVGAN，并让 Qwen3-ASR-0.6B 与 TTS 在同一个 `cuda:0` 进程运行。ASR 音频 FA2、文本 SDPA，实际使用 384 Graph，有 replay 且无 fallback。短句/中句 × 串行/重叠 × 5 次，两种布局 **20 对完整 waveform hash 和 ASR text hash 全部一致**，math flags 未改变。这里的首段时间是首个有声的生成 PCM chunk，不包含实体扬声器播放延迟。

| 场景 | graph 首段有声中位 ms | nested 首段有声中位 ms |
| --- | ---: | ---: |
| 短句、串行 | 335.73 | 334.27 |
| 短句、重叠 | 509.75 | 514.12 |
| 中句、串行 | 423.03 | 423.48 |
| 中句、重叠 | 635.43 | 630.62 |

加载 ASR/TTS 权重、准备 TTS 17 图并完成真实 TTS 预热后，联合进程 allocated 为 **3430.68 → 2917.68 MiB**，减少 513 MiB。这个 snapshot 在显式 ASR Graph 捕获前；随后完成 ASR 捕获/预热并测量的全场景 peak allocated 为 **3579.18 → 3068.06 MiB**。每格只有 5 次，不能据此保证所有尾延迟改善。

## 取舍修正与性能差异复核

本节复核现有原始数据与调用路径，没有新增性能采样。当前 Graph 串行调度在变更前后相同，额外请求排队不属于本次默认产品路径的性能代价。此前将共享 KV 的独占泛化成当前并发损失的表述已修正；该限制仅涉及将来直接并发调用 decoder 或增加 workspace slot 的设计。

### 已观察到的局部耗时差异与显存回退

交替 A/B 的每个场景有 20 对样本。下表的耗时变化是每对比例变化的中位数；样本 p95 分别取各布局排序后的第 19 个值（nearest rank），不等同于配对比例的 p95，也不作为稳定尾延迟保证。

| 耗时增加的观测场景 | 配对中位耗时变化 | 原实现样本 p95 ms | nested 样本 p95 ms |
| --- | ---: | ---: | ---: |
| Laptop / FA2 / 768 中句 | +1.74% | 325.18 | 330.91 |
| Laptop / SDPA / 未对齐 512 | +1.74% | 215.99 | 220.78 |
| Laptop / SDPA / 768 中句 | +3.52% | 480.70 | 498.54 |

这些差异也出现在相应样本的尾部，但不是已经定位到 nested 的内核回退。固定构建/预捕获顺序使布局与基地址/显存放置混在一起；反转构建顺序的对照尚未执行。不能只用全场景中位数接近来宣称零回退，也不能由这几格数字为 nested 增加未经证实的修补代码。主卡 FA2 三个配对场景的样本 p95 均下降；笔记本短句等其它场景未显示同样的 p95 增加。未锁定 GPU 频率，20 对样本也不能确定底层原因或其它硬件的长期表现。新核算的 p95 和方法已加入[证据汇总](evidence/tts_nested_kv_validation_2026-10-14.json)的 `paired_latency`。

只懒捕获一张 448 图时，nested 最大根缓冲区增加 **27 MiB 常驻 KV/allocated**；该实验稳态 peak allocated 从约 **238.94 → 249.02 MiB**。这是已测到的显存回退，接近显存上限的小图部署应选择 `graph`，不能套用默认 17 图节省 513 MiB 的结论。

### ASR 重叠与首段尾部样本

已有完整语音实验每格仅 5 次，nearest-rank p95 实际等于该格最大样本。它们记录小幅局部差异，但不能确定是 nested 的因果成本，也不足以证明长期 p95/p99 无回退或改善：

| 场景 | graph 首段有声中位 / 样本 p95 ms | nested 首段有声中位 / 样本 p95 ms |
| --- | ---: | ---: |
| 短句、串行 | 335.73 / 340.63 | 334.27 / 340.11 |
| 短句、ASR 重叠 | 509.75 / 571.23 | 514.12 / 554.35 |
| 中句、串行 | 423.03 / 425.28 | 423.48 / 431.82 |
| 中句、ASR 重叠 | 635.43 / 678.78 | 630.62 / 680.32 |

短句重叠的首段中位数增加约 4.36 ms；中句串行的最大首段样本增加约 6.54 ms，中句重叠增加约 1.53 ms。ASR 中句重叠的样本 p95 也从 324.02 → 325.37 ms。ASR 仍实际 replay 且无 fallback，这些变化不能归因于新增串行化，也不能用显存下降推导出延迟必然改善。

### 调用路径与未覆盖的测量

- **请求级 CPU 锁与 CUDA event 有非零开销。** event record/wait 位于每个 AR 请求的交接处，默认没有增加逐 token 全设备同步；交替实验已包含这些操作的整体耗时，但没有单独分解其占比。对极短请求不能据此保证开销完全可忽略。
- **首次根分配和懒捕获的冷启动尚未单独测量。** 单短图实验先预热再计时；`skip_precapture=true` 下的 `precapture_seconds` 只记录跳过预捕获，不能作为冷请求时延。默认 17 图的单次准备记录未出现 nested 更慢（主卡 FA2 3.369 → 3.171 s、SDPA 3.255 → 3.133 s；笔记本 FA2 3.055 → 3.041 s、SDPA 3.106 → 3.062 s），但这些独立进程记录没有证明冷启动或长期启动时延零回退。
- **Graph miss/deferred capture、桶满和超大 prompt 仍有已有的路径切换成本。** 路由和桶容量未改变，桶满会复制完整有效历史并转动态 KV，而不会因根容量为 1024 自动换到更大 Graph。已测主卡 FA2 溢出场景中位数 506.11 → 508.00 ms；没有发现新增的提前溢出或计算桶扩大。冷 miss、捕获推迟及长期尾延迟没有专门做性能对照。
- **FA2 可用性与模式回退不是 nested 新增的限制。** FA2 `valid` 与 nested 同时工作；`TTS_T2S_FLASH_ATTN_MODE=bucket` 的 masked SDPA 回退和扩展不可用回退属于已有行为。FA2 配对 probe 强制直接执行内核，未用自动 SDPA 回退掩盖测量；`TTS_KV_CACHE_LAYOUT=bucket` 是独立的存储设置。当前实现没有把短桶统一扩到 1024，也没有关闭 FA2。
- **验证边界保留。** 当前只覆盖这份 v3hc 权重、两张 Ada GPU 和本地 runtime。小型 FA2/笔记本跨进程重复性异常同样出现在原实现，对应底层原因未确定；它们不是已经定位的 nested 回退。更长时间的首段/ASR p95/p99、冷懒捕获以及其它硬件仍未验证。

采纳依据仍是当前已串行、默认多图路径的 513 MiB 常驻节省，同时保留短桶与 FA2。保留上述耗时观测差异、稀疏图结构性显存回退和测量空白，不以未来并发扩展边界替代当前性能取舍。没有定位稳定的热路径算子回退原因；需要独立处理的是下面已经复现的启动捕获缺陷。

本次说明复核另运行现有 `tests/test_tts_acceleration.py`、`tests/test_tts_cache_backend_boundary.py`、`tests/test_tts_threading.py`，**93 passed**。确认模式选择、Graph 合成锁与语音线程合约；未改动推理实现，也未重跑 GPU 性能实验。

## 审查后修复：SDPA 启动预捕获的 grad 边界

审查指出并独立复现了 PR 的 P1：产品构造阶段在 grad 开启、参数 `requires_grad=True` 时调用预捕获。`unbind` 产生的共享根视图在 SDPA 预热时被需要梯度的 K/V `scatter_` 原地写入，触发 PyTorch 多输出视图限制。捕获层返回失败，导致启动本应准备好的键改到真实请求里懒捕获，或在 ASR 租约活跃时退回 eager。

Torch 2.6.0+cu124 / CUDA 12.4 的两层合成模型独立对照：未修改基线在 grad 模式下成功捕获默认 17 键；修复前 `graph`、`bucket`、`nested` 都失败 17 键（零键保存），相同布局在 `no_grad` 下都成功。这是 PR 的 grad-mode 集成回归，原先包在 `inference_mode` 中的 SDPA benchmark/GPU 测试漏掉了构造阶段边界。因而此前预热后的时延不能证明产品 SDPA 启动捕获可用，`graph` 存储退路在修复前也受影响。

修复在 `_capture_bucket()` 添加 `@torch.no_grad()`，使该组件自己拥有推理捕获的 grad 边界。普通 grad 调用下分配的是可在后续 `no_grad` 请求中写入的正常张量；不使用 `inference_mode` 替代，也不改 `unbind` 或参数的 `requires_grad`。另删除 replay 失败后 KV/mask 向自身 `copy_` 的空操作，直接从已有工作区有效前缀继续 eager。

新增 CPU 合约记录预热/捕获中的 grad 和 inference 状态，三种布局均断言 `(False, False)`、调用者 grad 状态恢复且根是普通非 autograd 张量。这三项在修复前均失败。新增 CUDA 合约在外层 grad 开启时执行真实 SDPA，覆盖三种布局、FP16/FP32、默认 17 键，再验证 `no_grad` / `inference_mode` 请求命中四个计算桶且不产生新键；另验证模拟 replay launch 前失败后的 eager token/logits 与正常图路径一致。

模块共 **26 passed**；扩大后的 CUDA/设备/语音线程回归（显式 GPU probe 开启）共 **183 passed**。另在 `CUDA_VISIBLE_DEVICES=-1` 的进程确认 **11 passed、15 CUDA cases skipped**，其中新增三项 CPU 合约实际执行。使用当前真实 checkpoint、SDPA 和普通 grad 调用另做功能验证：三种布局均捕获 17 键，后续两种请求模式均直接 replay，12 个短/中输入请求不新增键且 token hash 一致。去重 KV 仍为 graph 561 / bucket 129 / nested 48 MiB。本轮验证只检查功能与容量，没有计时；不能把 17 图的旧总准备时间除以 17 当作实测单键首声成本。

本轮完整 model-less 三分片也已执行：前两个 PASS；第三个在 `test_skill_codex_runtime.py` 的原生 Skill fixture teardown 出现子进程清理期限错误，测试正文通过。runner 汇总 5782 passed / 43 skipped，另有这一个 teardown error，不能将本轮写成全部分片 PASS。其它分片结束后隔离复跑该套件得到 **2 passed / 2 optional ACP cases skipped**，未修改相关 Provider/fixture 代码。Ruff、架构生成与 README 生成检查通过。

PR 继续保持 draft。新的公开配置与默认值仍需要关联 Amadeus Issue，才能进入 merge-ready 审阅；此 P1 不作为 Aqua-TTS 上游缺陷报告。

## 使用与验证入口

模型加载前配置 `TTS_KV_CACHE_LAYOUT=nested|bucket|graph`，默认 `nested`。环境解析位于 `config/tts_acceleration.py`；无效值会明确报错。变更布局、设备、dtype 或容量应创建新的 decoder，不在已经捕获的地址上热切换。

- 容量/多场景 probe：`tools/probes/benchmark_t2s_nested_kv.py`，支持 attention/device/layout、`--buckets 1024` 和 `--skip-precapture`。
- 交替对照：`tools/probes/benchmark_t2s_nested_kv_paired.py`。`--baseline-source` 指向本地实验开始时保存的未修改快照。
- ASR/TTS probe：`tools/asr/benchmark_qwen_tts_coexistence.py --tts-precapture`；报告增加 layout、去重的 TTS KV、常驻和峰值数据。
- 回归测试：`tests/test_t2s_nested_kv.py` 验证 storage/view 形状、整个请求的独占、异常释放、独立 owner 并行、真实 CUDA Graph 的跨桶/跨请求/溢出一致性、跨 stream 未完成写入交接以及 batch=2 私有 KV；同时运行原 mask、EOS、history、捕获协调、设备配置和语音线程回归。

最终该组回归 **168 passed、1 skipped**。跳过的是需要显式 opt-in 的旧 NVIDIA kernel probe；本次新增 CUDA 测试及真实权重 FA2/Graph/BigVGAN 实验均已执行。语音线程测试在 Windows 沙箱内的 asyncio 本地 socketpair 初始化处阻塞，定位后在沙箱外完成该组回归，没有为测试改变应用代码。

实验基线包含开始工作前已有的 semantic-history/EOS 优化，双方均保留，没有将这些改动的收益计入 nested KV。原始 JSON 位于忽略的本地 `output/diagnostics/nested-kv-20261014/`；[可审阅证据汇总](evidence/tts_nested_kv_validation_2026-10-14.json)保留 source/checkpoint/raw-result hash、容量、时延和一致性检查，去掉个人路径和原始语音内容。

同时准备了 [Aqua-TTS 请求所有权问题的上游报告草稿](evidence/aqua_t2s_request_ownership_upstream_draft_2026-10-14.md)，使用已核验上游 commit 的直接代码证据；它已随 Amadeus PR #206 的分支公开供审阅，尚未向 Aqua-TTS 提交为 Issue。实验结论限定在当前 v3hc 权重、两张 Ada GPU 和这组本地 runtime。多用户 workspace slot、其它权重/语言，以及独立的 CUDA Graph 内存池共享不属于本次实现。


## 草稿 PR checkout 的补充验证

PR 分支从 `ef3a54b` main 建立，只移入 nested KV，排除原实验分支的 ambient 提交。当时的 decoder、FA2 与 acceleration resolver 与原实验工作区源码一致，原始实验 JSON 的 SHA-256 全部复核通过。后续审计修复的捕获 grad 边界与回退自拷贝清理属于这些性能采样之后的变更，另列功能验证，不将旧采样声明为修复后重新计时。

干净 checkout 的本地 CUDA 回归为 **172 passed、1 skipped**；随后开启 `AMADEUS_RUN_GPU_TESTS=1`，此前可选的 NVIDIA 自动配置/捕获/replay probe **1 passed**。新增真实 CUDA 请求竞争测试覆盖 SDPA/FA2、同/异 Graph key，两个不同 stream 的结果与串行 logits 对照一致。Windows cu124 安装梯级及 cu128/MPS candidate 的 CI 命令已加入新模块，保证安装模型依赖的 hosted runner 也执行其 CPU 合约。

隔离 checkout 执行 `uv sync --locked --extra dev` 创建独立 model-less 环境，`--profile ci` 校验通过。完整 `tools/run_tests.py` 三个分片均 PASS，runner 汇总为 **5782 passed、43 skipped**；可选依赖缺失的跳过不作为 CUDA 实验的替代。Ruff 与两项架构/README 生成检查通过。新环境没有同步或修改原语音 `.venv`。

修复 benchmark 删除闭包变量导致的 F821 后，FA2/SDPA 短桶及动态续接 CLI 再次实际运行，仍得到 17 图、48 MiB KV。此次 smoke 与 CPU suite 工作同时进行，只用于正确性/计量验证，不将其时延混入前述采用建议。

公开 main `ef3a54b` 的 `GPT_SoVITS/AR/models/t2s_model.py` 与原实验快照具有相同 Git blob（`d76d9f664e51bad72143b49886b7bbec7e7fbb44`），可从该提交导出 `--baseline-source`；原始对照无需依赖未公开代码。
