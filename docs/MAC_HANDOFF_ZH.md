# Apple Silicon：MLX T2S 一键测试

本说明按 2026-09-30 对 `1c169e7` 的复审更新。目标是在同一台 Mac 上比较
Torch MPS 与 MLX Metal 的实际收益，并决定注意力补零是否保留。Windows CPU
只运行缩减模式检查流程，不用于性能排名或决定优化去留。原交接第 3 节
#5“图节点减半”和 #6“CPU 无回归”已作废。

## 准备

使用 `codex/gsv-mlx-t2s` 的最新完整检出或对应源码包、Apple Silicon Mac、
Python 3.12.10。接通电源、关闭低电量模式，并停止其他重负载。
在项目根目录安装已锁定的依赖：

```bash
uv sync --locked --extra voice --extra vad --extra local-mps --extra mlx-t2s --extra dev
```

本机需已有以下资产；工具不自动下载，也不上传它们：

- GPT-SoVITS v3 GPT checkpoint，例如公开的 `s1v3.ckpt`。
- 对应 v3 SoVITS checkpoint，例如公开的 `s2Gv3.pth`；使用 LoRA 音色时还需基础 `s2Gv3.pth`。
- 一段获准使用的参考 WAV，以及准确的日文转写。
- 现有安装根目录下 `assets/models/gpt-sovits/pretrained/` 中的
  `chinese-roberta-wwm-ext-large`、`chinese-hubert-base`、`s2Gv3.pth` 和
  `models--nvidia--bigvgan_v2_24khz_100band_256x`。

源码包不含模型、参考音频或私人音色。`--assets-root` 指向持有上述资产的
**安装根目录**，不是 `pretrained` 子目录。速度测试可使用公开的 v3 权重；
真人音质验收应使用获准测试的目标音色。

## 一条测试命令

将路径和参考转写替换成本机实际值，在项目根目录执行：

```bash
uv run --locked --no-sync python tools/probes/gsv_mac_qualification.py \
  --device metal \
  --checkpoint /path/to/s1v3.ckpt \
  --sovits /path/to/s2Gv3.pth \
  --reference-audio /path/to/reference.wav \
  --reference-text '这里填写参考音频的准确日文转写。' \
  --assets-root /path/to/existing-amadeus \
  --output output/diagnostics/mac-mlx-qualification
```

如需导出供盲听的合成音频，增加 `--include-listening-wavs`。请只对获准使用和
分享的音色开启此项。速度与数值结果不能代替真人听测；人耳验收保持待定，
直到听过 Mac 生成的样本。

中断或失败后，保持相同代码、资产、配置和命令参数，在原命令末尾添加
`--resume`。已完成且未被修改的步骤会复用，失败步骤重新执行；遇到错误会
停止，不会用旧成功结果掩盖本次失败。修改代码或参数后应换一个输出目录。
正常模式的时长取决于机型和模型，目标约 45 分钟内，尚无实机时长保证。

## 自动执行的流程

1. 检查 Metal/MPS，转换所选 GPT 权重，生成同一份真实前端固定历史输入；
   FP32 数值检查失败即停止，FP16 记录误差和排名，不放宽 FP32 容差冒充通过。
2. 测以下七个变体，每个独立进程，正序/反序交替，分别预热后累计至少
   20 次正式测量。固定循环逐步采样、判断 EOS、观察完成，并使用相同历史
   token 保持工作量一致；最后计算完成也计入时间。

   | 变体 | 模型与循环 |
   |---|---|
   | Torch FP32 | 现有生产 T2S，MPS |
   | Torch FP16 | 仅探针将 T2S 转为 FP16，MPS |
   | MLX 审计原版 FP32 | `a1873b7` 的模型、采样器和原同步循环 |
   | MLX 不补零 FP32 / FP16 | `184543d` 的模型，当前前瞻循环 |
   | MLX 补零 FP32 / FP16 | 当前模型，当前前瞻循环 |

   历史快照随源码附带并记录来源与哈希；无需切换 Git 提交或在产品中增加开关。
   prefill、固定解码总耗时、每步位置及每轮原始值分别保存。
3. 对 Torch FP32 做首句、中句、长句的阶段拆分，各至少 10 次，使用生产
   4/16/32 步 CFM 配置。首个冷请求与预热后的测量分开；阶段同步只在本步开启。
4. 以固定循环解码 p50 选择供后续测试的 MLX 候选，再测自然执行的首块音频
   时延：Torch FP32 对该候选，独立进程、正反顺序、每种至少 20 次。
   同时包含判断补零所需的同 dtype 补零/不补零对照；有竞争力的 Torch FP16
   也纳入。所有比较统一关闭完整首句音频缓存，声学链精度相同。
5. 对选定 MLX 候选做至少 100 次持续请求，覆盖工作线程中的语义与声学链，
   记录 RSS、MLX active/cache/peak 和 Torch MPS 分配量。
6. 汇总数据、生成可分享 ZIP；可选生成匿名盲听样本。报告不自动批准合并，
   不改变应用的 Torch 默认设置或 MLX 精度。

## 结果与补零决策

检查输出中的汇总报告及可分享 ZIP。`private/` 保存本机排错日志、状态、
参考衍生 fixture 等，不属于分享包。默认分享包不包含权重、参考音频、
转写、绝对路径、用户名或主机名；开启听测选项时才附加匿名合成 WAV。

主要看同机 Torch MPS 对照、自然首块时延、失败情况和内存趋势。固定 token
的 T2S 加速不能直接当作首块或整句加速，自由生成的 token 数与重试次数也
必须一起看。首块时间是软件输出首块，尚非扬声器出声时间。

对拟采用的精度，补零版必须同时满足：**解码 p50 比同 dtype 不补零版更低，
且自然首块时延更低**。任一项不满足就删除补零；数据缺失时保持未判定。
该规则不撤回其余改动：fast LN、addmm、分块 KV、前瞻、FP16 选项、首句
缓存共享和启动优化全部保留。最终是否采用 MLX 仍取决于相对 Torch MPS
的速度收益及真人音质验收。

## Windows 缩减模式

开发环境安装 CPU 对应依赖，在同一条命令中改用 `--device cpu --smoke`。
该模式会缩短历史、文本和生成预算，并减少次数，但仍实际经过七变体、
数值、三种 CFM 步数、首块比较、持续请求、打包与恢复检查所需的流程。
CPU 模式使用固定候选覆盖流程，不按 CPU 耗时选优，也不作补零性能判定。
其输出明确为功能空跑，不能当成 Mac 验收结果。禁止为性能比较再次增加
Windows CPU 测量轮数。
