# T2S Nested KV 实现与实测

日期：2026-10-14。分支：`codex/tts-nested-kv-evaluation`。

建议在 Amadeus 当前默认预捕获、多 Graph、单用户 TTS 配置中采纳 `nested`。保留各桶计算形状，把 K/V 物理存储合并为最大容量根缓冲区。当前权重和默认 17 图配置下，KV 常驻 **561 → 48 MiB**，稳定减少 **513 MiB**。主要收益是显存；交替 A/B 的 AR 耗时变化为 −2.15%～+3.52%，没有把它作为普遍加速方案。

有一个明确的适用边界：关闭预捕获、只使用一张 448 图时，KV 反而从 **21 → 48 MiB**。这类部署可以选择 `graph`。`bucket` 适合保留不同桶的独立工作区，默认预捕获的 KV 为 129 MiB。全局共享工作区的 TTS AR 请求必须独占；ASR 使用自己的模型状态，仍可并行执行。

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

问题归类为 decoder 的存储分配与请求所有权边界。原锁只覆盖一次 replay，prompt 重置和相邻 token 之间仍可能被另一请求覆盖。一个存储 owner 在完整 AR 请求期间独占，是本次的核心不变量。

- `nested` 使用 `(layers, 1, max_bucket, hidden)` 的稳定 K/V 根张量；各层和各桶是视图。`bucket`、`graph` 分别按桶或 Graph key 分配根张量，用于明确的对照与部署选择。已经捕获的地址不会因新 key 被替换。
- 请求在 prefill 前获得相应 owner 的锁，覆盖 prompt 写入、捕获、所有 replay、采样和转动态 KV。锁释放时记录 CUDA event，下一个 stream 等待该 event，保护尚未完成的 GPU 操作，不增加逐 token 全设备同步。
- Graph 的输入、输出、mask、position 和 CUDA 私有内存池仍各自保留。KV 分配没有把 448/512/768 的 SDPA 计算形状扩至 1024。
- 请求直接将真实 prompt 写入 Graph 的 KV 视图，清空当前桶的未来位，保留真实连续写入位置。省去原先额外的请求级桶 KV 和捕获阶段的重复分配。
- Graph 被捕获协调器推迟时，使用请求私有静态 KV；桶满时复制完整有效历史，接续现有动态 KV。batch>1 使用私有 KV，避免写入 batch=1 Graph 缓冲区。
- 普通 ASR/TTS 工作继续使用 `speech_cuda` 的共享工作租约；仅捕获需要已有的独占窗口。TTS 工作区锁不覆盖 ASR 模型。

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

180 对交替实验的 token 全部一致。笔记本 SDPA 中句保留约 3.5% 的局部耗时代价，采纳建议考虑的是 513 MiB 常驻节省与当前单用户配置，不是所有硬件/场景零回退。

回归阶段另观察到，hidden=32/head=2、48/96 槽的极小型 FA2 图在两个相同布局的实例之间也不能稳定逐位重复。实验开始时保存的未修改 decoder 对照同样出现首步约 0.1006 的 logits 差异，排除了 nested 作为这个现象的必要触发条件。最终 GPU 回归采用当前权重的 head dimension=32 和对齐桶，固定双方每步输入并逐步核对 logits；真实权重的采样和完整波形另外保持严格 hash 对照。小模型后端现象的底层原因未确定，临时 FA2 调试改动全部撤回，没有添加兼容分支、扩大数值容差或据此提交上游 bug。

笔记本 FA2 初轮分进程有 4/28 个 token hash 差异。未修改的原实现以同样源码、权重和 seed 再跑，也在相同四项出现差异；nested 与这次重复原基线 28/28 一致。交替实验另外验证了 60/60 对 FA2 token 一致。因此不把初轮跨进程差异归因于共享 KV；底层数值差异原因没有进一步确定，保留在[证据](evidence/tts_nested_kv_validation_2026-10-14.json)中。

## 共享物理容量与最大计算桶的消融

另将 nested 的计算桶强制为 `[1024]`，比较同样五个场景。KV 仍是 48 MiB；短桶路由发生改变，预捕获图数为 19。两种 attention 各 35 对 token 与保留短桶的 nested 一致。

SDPA 的 448/512 短场景 wall time 增加约 **29%–30%**，768 中场景增加 **15%**；本就路由 1024 的长场景基本持平。FA2 valid 本轮没有出现最大桶惩罚，但分进程结果含运行噪声，不能推广为容量完全不影响 FA2 的 kernel 选择/性能。保留短桶对 SDPA 有明确价值，正是当前实现保留多桶 Graph 的原因。

## 真实语义与同卡 ASR/TTS

使用现有日语稳定性 probe，覆盖弱尾逗号、省略号、强句末、衔接句和普通句共 11 类，每类 5 个相同 seed，不启用语义重试。`graph` 与 `nested` 的 **55 对 token、停止条件和异常标记全部一致**。各布局都保留了 2 个既有异常样本；本次存储优化没有改变这项既有语义问题。

完整语音链路使用当前 GPT/SoVITS、CNHubert、BigVGAN，并让 Qwen3-ASR-0.6B 与 TTS 在同一个 `cuda:0` 进程运行。ASR 音频 FA2、文本 SDPA，实际使用 384 Graph，有 replay 且无 fallback。短句/中句 × 串行/重叠 × 5 次，两种布局 **20 对完整 waveform hash 和 ASR text hash 全部一致**，math flags 未改变。这里的首段时间是首个有声的生成 PCM chunk，不包含实体扬声器播放延迟。

| 场景 | graph 首段有声中位 ms | nested 首段有声中位 ms |
| --- | ---: | ---: |
| 短句、串行 | 335.73 | 334.27 |
| 短句、重叠 | 509.75 | 514.12 |
| 中句、串行 | 423.03 | 423.48 |
| 中句、重叠 | 635.43 | 630.62 |

加载 ASR/TTS 权重、准备 TTS 17 图并完成真实 TTS 预热后，联合进程 allocated 为 **3430.68 → 2917.68 MiB**，减少 513 MiB。这个 snapshot 在显式 ASR Graph 捕获前；随后完成 ASR 捕获/预热并测量的全场景 peak allocated 为 **3579.18 → 3068.06 MiB**。每格只有 5 次，不能据此保证所有尾延迟改善。

## 使用与验证入口

模型加载前配置 `TTS_KV_CACHE_LAYOUT=nested|bucket|graph`，默认 `nested`。环境解析位于 `config/tts_acceleration.py`；无效值会明确报错。变更布局、设备、dtype 或容量应创建新的 decoder，不在已经捕获的地址上热切换。

- 容量/多场景 probe：`tools/probes/benchmark_t2s_nested_kv.py`，支持 attention/device/layout、`--buckets 1024` 和 `--skip-precapture`。
- 交替对照：`tools/probes/benchmark_t2s_nested_kv_paired.py`。`--baseline-source` 指向本地实验开始时保存的未修改快照。
- ASR/TTS probe：`tools/asr/benchmark_qwen_tts_coexistence.py --tts-precapture`；报告增加 layout、去重的 TTS KV、常驻和峰值数据。
- 回归测试：`tests/test_t2s_nested_kv.py` 验证 storage/view 形状、整个请求的独占、异常释放、独立 owner 并行、真实 CUDA Graph 的跨桶/跨请求/溢出一致性、跨 stream 未完成写入交接以及 batch=2 私有 KV；同时运行原 mask、EOS、history、捕获协调、设备配置和语音线程回归。

最终该组回归 **168 passed、1 skipped**。跳过的是需要显式 opt-in 的旧 NVIDIA kernel probe；本次新增 CUDA 测试及真实权重 FA2/Graph/BigVGAN 实验均已执行。语音线程测试在 Windows 沙箱内的 asyncio 本地 socketpair 初始化处阻塞，定位后在沙箱外完成该组回归，没有为测试改变应用代码。

实验基线包含开始工作前已有的 semantic-history/EOS 优化，双方均保留，没有将这些改动的收益计入 nested KV。原始 JSON 位于忽略的本地 `output/diagnostics/nested-kv-20261014/`；[可审阅证据汇总](evidence/tts_nested_kv_validation_2026-10-14.json)保留 source/checkpoint/raw-result hash、容量、时延和一致性检查，去掉个人路径和原始语音内容。

同时准备了 [Aqua-TTS 请求所有权问题的上游报告草稿](evidence/aqua_t2s_request_ownership_upstream_draft_2026-10-14.md)，使用已核验上游 commit 的直接代码证据；尚未发布。实验结论限定在当前 v3hc 权重、两张 Ada GPU 和这组本地 runtime。多用户 workspace slot、其它权重/语言，以及独立的 CUDA Graph 内存池共享不属于本次实现。
