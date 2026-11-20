# Optional V3 emotion references / 可选 V3 情绪参考

The experimental `ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING` setting is off by
default. The Voice settings page exposes a boolean switch and requires a backend
restart. It uses the ordinary configuration parser, so existing `1`/`0` values
and GUI `true`/`false` values agree.

实验开关默认关闭，在设置 → 语音中启用并重启后端后生效。
原有 `.env` 的 `1`/`0` 和 GUI 保存的 `true`/`false` 均由统一配置入口解析。

Supported scope is Windows CUDA, embedded GPT-SoVITS V3, Japanese output. V3
checkpoint filenames do not select eligibility. Other platforms, CPU, ROCm,
V2, sidecars and remote providers keep default reference synthesis. The Settings
page reports whether the feature is disabled, unsupported, missing its pack,
invalid, or ready. A missing or invalid optional pack does not fail the base
TTS model's initialization.

当前范围为 Windows CUDA、内嵌 GPT-SoVITS V3、日语输出；不限制 V3 权重文件名。
设置页显示关闭、不支持、未安装、无效或就绪状态。可选包缺失或无效时，
情绪参考不可用，基础 TTS 初始化仍可继续。

## Separate asset pack / 独立资源包

`voice-kurisu-emotions` is registered in `assets/index.json` and uses the existing
external bundle installer. It contains no model weights or default references.
The original experiment's listening ZIP remains separate evidence, not a
runtime pack. Reference recording distribution terms remain independent of
the source code's license.

情绪包沿用已有资源包格式与安装器，不包含模型或默认参考。之前的试听 ZIP
保留为实验资料，不作为运行时 pack。参考录音的分发权利不由代码许可证覆盖。

```powershell
python tools/external_assets.py verify amadeus-voice-kurisu-emotions.zip
python tools/external_assets.py install amadeus-voice-kurisu-emotions.zip
python tools/external_assets.py status
```

Installation verifies file hashes and pack contents. Different existing files
are refused unless the caller explicitly chooses `--overwrite`. Installation
does not activate the setting or change model paths and default reference pairs.

安装会校验文件哈希与包内容。同名不同文件需显式选择 `--overwrite` 才能替换。
安装不会自动启用开关，不修改模型路径和默认参考配对。

The pack manifest is `assets/audio/reference/emotions/references.json`:

```json
{
  "format": "amadeus.tts-reference-pack.v1",
  "language": "ja",
  "references": {
    "shy": {"audio": "shy_b.ogg", "transcript": "shy_b.txt"},
    "blush": {"audio": "shy_b.ogg", "transcript": "shy_b.txt"}
  }
}
```

Members are relative to the pack directory. Audio and UTF-8 TXT files must exist,
and transcripts must be non-empty. Paths cannot escape the directory. `normal`
is reserved for the configured default pair. Unmapped EMO presets also use the
default. Aliases of one audio/TXT pair share a single conditioning identity.
Member paths use normalized forward slashes; backslashes are rejected.
Mapping keys must be recognized EMO presets; misspelled or unknown keys are rejected.
The builder includes only the manifest and referenced members, excluding saved
alternative candidates or listening artifacts in the same local directory.

文件路径相对于情绪包目录；音频和非空 UTF-8 原文必须配对存在，路径不能越界。
成员路径使用规范化的正斜杠格式，不接受反斜杠。
`normal` 始终使用配置的默认参考，未映射情绪也使用默认参考。
同一音频／原文的别名共用一个条件标识。打包器仅打包清单引用的文件，
不夹带本地备用候选和试听资料。
映射键必须是有效的 EMO 预设；拼写错误或未知键会使资源包校验失败。

## Startup warmup / 启动预热

When the switch is enabled and the loaded profile is supported, startup first
validates the pack. It warms the configured default acoustic cache and every
distinct emotional reference cache, synchronizes queued GPU work, and only then
reports ready. Aliases are warmed once. This happens even if optional general
runtime warmup was disabled: a ready emotion route promises warm reference
features. No target speech is generated or played during this step.

开关启用且模型配置受支持时，启动先验证情绪包，预热默认声学参考和每一份不同的
情绪参考缓存，等待 GPU 工作完成后才报告就绪。别名只预热一次。
即使一般运行时预热被关闭，情绪参考就绪仍意味着其参考特征已预热。
这一步不生成或播放目标语音。

The same normalization and cache key are used for startup and synthesis. Live
requests reuse the warmed cache rather than first extracting a new reference.
The earlier hc3 replay observed about 95–101 ms from producer start to reference
composition on first use, versus 0–3 ms on reuse. That interval is not a controlled
benchmark. Warmup moves reference preparation to startup; it does not eliminate
T2S decoding, CFM/vocoder work, new Graph bucket capture, or effects of output
length. Startup time and retained reference tensors increase accordingly.

预热与正式合成共用文本规范化和缓存键。此前 hc3 重放中首次参考准备阶段约
95–101 ms，复用约 0–3 ms；这不是严格对照基准。预热把参考准备移到启动阶段，
不会消除语义解码、CFM／声码器计算、新 Graph 桶捕获或输出长度的影响。
代价是启动时间和参考特征缓存占用有所增加。

## Shared speech pipeline / 共用语音管线

The existing LLM EMO parser is the source of semantics. Every turn starts with
the default reference. A changed effective reference flushes preceding buffered
text and prevents merging across that reference boundary. Expression actions
retain their original presets; `normal` → `thinking` and `shy` → `blush` do not
create extra TTS boundaries when their references are identical. `dur` belongs
to expression animation, not reference lifetime.
When enabled, local LLMs use the same first-sentence splitting as remote providers,
with 10 ms stream pacing and without the local first-sentence 50 ms read pause.

沿用 LLM 的 EMO 解析，每轮从默认参考开始。实际参考改变时才提交此前缓冲文本、
禁止跨参考合并。表情仍保留原标签，因此共用参考的表情变化不会额外切分 TTS。
`dur` 控制表情，不控制参考的持续时间。
启用后，本地 LLM 使用与远程提供方相同的首句切分，每块读流让出 10 ms，
不再执行本地首句提交后的 50 ms 读流暂停。

The resolved preset travels through the existing TTS request and backend options.
GPT-SoVITS passes an explicit request-local semantic audio/text pair into inference.
The semantic `prompt`, `phones1`, `bert1` are selected from that pair; `refer_spec`,
`prompt_fea_ref`, `prompt_ge`, `mel2_norm` stay together from the configured default.
Neither shared cache is mutated. BigVGAN consumes the resulting mel.
No dynamic inferencer subclass or ContextVar injection is used.

条件经现有 TTS 请求与后端参数传递，在推理调用中显式指定语义音频与原文。
语义条件来自所选情绪参考，四项声学条件成组取自默认参考；共享缓存不被改写。
BigVGAN 接收最终 mel；不再通过动态子类或 ContextVar 注入条件。

Mixed first sentences bypass the default opening-audio cache. Default first
sentences retain their cache behavior. The existing scheduling budget, playback,
inference permit and interruption ownership still apply. Disabling the setting
does not load an emotion pack, prewarm its references, or change ordinary EMO
presentation behavior. Existing semantic failure checks remain; no seed retry
or automatic audio editing is added.

情绪首句绕过默认开场音频缓存，默认首句继续复用原缓存。调度预算、播放、推理许可和
打断所有权沿用现有管线。关闭时不加载或预热情绪包，保留原有表情处理行为。
不增加换种子重试或自动修音。

## Acceptance / 验收

Model-less tests exercise disabled and unsupported profiles, pack validation and
installation, distinct-reference warmup, warm-key reuse, request isolation,
default acoustic cache composition, tag ordering, default cache behavior and
GUI persistence/restart. These tests do not measure real GPU warmup duration.
Existing listening evidence is hc3; default V3 and the explicit mainline
implementation still require real continuous-playback/listening acceptance
before promotion beyond an opt-in experiment.

无模型测试验证关闭路径、配置隔离、包安装与校验、去重预热、缓存键复用、请求隔离、
默认声学条件、标签顺序与 GUI 保存／重启。不宣称测量了真实 GPU 的预热耗时。
现有听感证据来自 hc3；默认 V3 及此显式实现仍需连续播放／试听验收。
