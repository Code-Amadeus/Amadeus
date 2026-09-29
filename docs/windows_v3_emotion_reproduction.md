# Windows V3 mixed-reference experiment / 混合参考实验

## Status / 状态

Experimental branch: `codex/windows-v3-emotion-streaming` in `Code-Amadeus/Amadeus`.
The downloadable local bundle records an exact source commit in `SOURCE.json`.
This is not a mainline PR or a production-quality emotion system.

实验分支为上述分支，资源包 `SOURCE.json` 记录准确提交。它保留当前试听环境的
切句与日语读音底座，不是只包含情绪功能的独立 PR，也不代表情绪系统已成熟。
`d9cee90` 是底座快照；`dce381c` 开始接入情绪路由；`5f16f26` 修正初始化预热顺序。
后续分享提交解除 hc3 文件名限制并增加可移植复现工具。

The experiment is **off by default**. Set
`ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING=1` in your own `.env` and restart the desktop
to enable it; set `0` and restart to disable it. This is a startup switch, not a
UI toggle or a promise of live reconfiguration. Disabling restores the default
reference behavior of this branch's TTS baseline; it does not revert that
baseline's sentence planning or pronunciation changes.

**默认关闭**。在自己的 `.env` 设置上述开关为 `1`，重启启用；设为 `0`，重启关闭。
当前没有 UI 勾选框，也不是热切换。关闭恢复本分支底座的默认参考行为，
不会撤销此前切句、语速、停顿与读音修改。

Activation requires Windows, embedded GPT-SoVITS, NVIDIA CUDA, loaded model
version V3, and Japanese output. CPU, ROCm, V2, other platforms, remote providers,
and other output languages do not route references. V3 checkpoint filenames
are not an activation condition. Default `kurisu_v3` and custom V3 weights are
eligible; **the archived listening evidence is hc3 only**. Default V3 was not
run in the sharing pass, at the maintainer's request. Eligibility is not evidence
of equal emotional quality across checkpoints or speakers.

作用域为 Windows 内嵌 GPT-SoVITS、NVIDIA CUDA、V3 模型、日语输出。
权重文件名不再限制启用，因此默认 `kurisu_v3` 和自定义 V3 均可尝试。
**已有试听证据来自 hc3**；本次按维护者要求只整理分享，未补跑默认 V3 合成实验。
换权重、换说话人后的情绪和音色仍需试听，不能由接口兼容推定效果相同。

## Mechanism / 机制

The existing LLM EMO tag is the semantic source. No second classifier/model call
or keyword inference is added. Each turn starts `normal`; tags apply to following
text until the next tag. `dur` still belongs to the expression animation.
An emotion change flushes preceding unfinished text; different emotions cannot
merge in the scheduler. The producer thread owns reference conditioning through
a ContextVar which resets on completion, error, or generator close.

沿用 LLM 输出的 EMO 标签，每轮从 `normal` 开始，标签向后生效到下个标签。
不增加分类器或额外模型调用。`dur` 仍控制表情，不决定声音情绪的持续时长。
切换时先提交此前的文本，不让新情绪追溯影响旧文本；调度器不跨情绪合并。
条件绑定于合成线程作用域，结束、异常、关闭生成器时都会复位。

T2S uses the selected reference's `prompt`, `phones1`, `bert1`. The original
default acoustic reference supplies `refer_spec`, `prompt_fea_ref`, `prompt_ge`,
and `mel2_norm` as a group. Shared caches are not modified. BigVGAN consumes the
generated mel; it does not select a separate reference waveform.

T2S 使用情绪参考的语义和文本条件；四项声学条件成组保留默认参考。
共享缓存不被原地修改，BigVGAN 接收生成的 mel，不独立读取另一份参考音。

| EMO | Reference / 参考 | Selection / 来源 |
|---|---|---|
| angry | angry.wav | B03 |
| sad | sad.wav | round 1, crs_2773 |
| shy, blush | shy_b.ogg | continuity B, crs_1002 |
| surprised | surprised.ogg | round 2 candidate 3, crs_2679 |
| disappointed | disappointed_a.ogg | continuity A, crs_2408 |
| normal, thinking, smile, happy, serious_speaking | default | configured default pair |

`disappointed_b.ogg` (crs_1976) and `exasperated_candidate.wav` (crs_2603)
are retained alternatives, not active routes or new EMO labels.
Every reference must retain its paired transcript. Some candidate transcripts
are automatic ASR/training transcriptions and have not been independently checked.

两份备用参考只保留试听，不新增“无语”等标签。不要把参考原文替换成待合成台词。
部分原文来自 ASR 或训练清单，尚未独立人工核验。

## Bundle and setup / 资源包与安装

The resource ZIP is an **add-on for an existing working Windows CUDA V3 installation**.
It contains only new emotion reference audio/transcripts, selected listening
results, example text, bilingual instructions and SHA-256 manifests. It does not
include model weights, environments, dependencies, dictionaries, `.env`, credentials,
chat databases or raw logs. Keep your existing matched GPT/SoVITS pair and default
acoustic reference/transcript. The baseline reference under `listening/reference-baseline`
is only a record of the hc3 audition; the installer never overwrites your default.

资源 ZIP 是**已有正常 Windows CUDA V3 管线的增量包**，只包含新增情绪参考音频、
配对原文、选定试听、示例文本、双语说明和 SHA-256 校验。没有权重、环境、依赖、
字典、配置凭据或聊天日志。保留你已有的配对 GPT/SoVITS 和默认声学参考及其原文。
`listening/reference-baseline` 仅记录本次 hc3 试听的默认参考，安装器不会覆盖默认参考。

```powershell
# Preserve local changes before switching; do not reset your existing work.
# 切换前保存自己的修改，不要 reset 掉现有工作。
git fetch origin codex/windows-v3-emotion-streaming
git switch --track origin/codex/windows-v3-emotion-streaming
# If the branch already exists: git switch codex/windows-v3-emotion-streaming
# SOURCE.json in the ZIP records the exact code commit used for this package.

# Run from the repository root using your existing Python environment.
# 在仓库根目录使用现有 Python 环境；不需要再创建一套环境。
.venv/Scripts/python.exe tools/probes/install_emotion_bundle.py --bundle-dir D:/emotion-bundle --verify-only
.venv/Scripts/python.exe tools/probes/install_emotion_bundle.py --bundle-dir D:/emotion-bundle
```

The installer verifies all hashes before copying and installs only the new
`assets/audio/reference/emotions` pairs. It does not modify `.env`, models or
base references. Different existing emotion files are refused; back them up
before intentionally using `--overwrite`. Reusing a working V3 environment is
expected; the branch's existing baseline includes `e2k`, so check
`python -c "import e2k"` if your environment predates that baseline. If absent,
follow `docs/install_profiles.md` using your complete existing capability profile;
never run a core-only sync over a working GPU environment.

安装器先校验所有文件再复制，仅安装新增情绪音频与原文；不改 `.env`、模型或
默认参考。同名不同内容的情绪文件会拒绝覆盖，需先备份再显式 `--overwrite`。
预期直接复用现有 V3 环境。本分支底座已有 `e2k` 读音依赖，较旧环境可用
`python -c "import e2k"` 检查；若缺失，按安装文档保留现有完整 GPU 能力组合更新，
不要用只含 core 的同步命令把 GPU 依赖移除。

Set `ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING=1` in your own `.env`, retain your
existing V3 profile/paths, and restart your normal launcher. Set `0` and restart
to disable. Run `python -m pytest tests/test_experimental_v3_emotion.py -q` for
the model-less contract checks if your environment includes pytest.

在自己的 `.env` 只增加／修改上述开关，保留原 V3 配置，正常重启即可；`0` 重启关闭。
有 pytest 的环境可运行上述不加载模型的契约测试。

## Reproduce synthesis / 复现合成

Run from the repository root, with enough free GPU memory. The replay does not
launch an LLM or modify a chat session. It feeds tagged text through the real
ChatRuntime parser/queue, utterance scheduler, synthesis backend and
PlaybackManager, replacing the audio device with a WAV sink that advances in
real time. Default input pacing is 8 characters per 100 ms; it is not an exact
replay of original network token arrival or a measurement of physical speakers.

在仓库根目录执行，确保显存足够。脚本不调用 LLM，也不写聊天会话。
使用真实解析、切句、调度、合成和 PlaybackManager；仅把声卡输出替换为按音频时长
推进的文件输出。默认每 100 ms 输入 8 个字符，不声称还原原网络时序或扬声器延迟。

```powershell
# Keep your installed V3 weights / 复用现有 V3 权重
python tools/probes/replay_emotion_stream.py --voice-profile configured --emotion-routing on --text-file D:/emotion-bundle/examples/dialogue.txt --output-dir .cache/emotion-v3-on
python tools/probes/replay_emotion_stream.py --voice-profile configured --emotion-routing off --text-file D:/emotion-bundle/examples/dialogue.txt --output-dir .cache/emotion-v3-off

# Optional: only if hc3 weights are already installed at the conventional paths
# 可选：仅当你已在约定路径安装 hc3；资源包不提供权重
python tools/probes/replay_emotion_stream.py --voice-profile hc3 --emotion-routing on --text-file D:/emotion-bundle/examples/dialogue.txt --output-dir .cache/emotion-hc3-on
```

Each output directory must be empty. `full-turn.wav`, individual WAV blocks,
`requests.json`, `source-with-emotions.txt` and `manifest.json` record the run.
The manifest is written only after every queued span has matching start/end
events. No seed search, silent reference substitution or added retry is used.
Repeated runs can differ due to stochastic generation and adaptive scheduling.
Compare naturalness, identity continuity, missing/repeated speech, pauses and
producer time; do not assume an EMO label guarantees audible emotion.

输出目录必须为空。整轮音频、分块、标签原文和清单会保留；只有全部片段完成才写
成功清单。不增加选种子重试、静默换参考或修音。随机生成和自适应调度意味着结果
并非逐样本一致。应检查自然度、音色连续性、漏字／重复、停顿和生成速度。

For real LLM chat, use the normal desktop launcher and your own LLM configuration.
Set the routing switch and a matching voice profile in `.env`; for hc3 use
`TTS_VOICE_PROFILE=custom` and your own installed checkpoint paths. Keep your
default reference WAV paired with its **exact** transcript. Request
natural speech with existing EMO tags, not a fixed sequence of artificial moods.
The real desktop currently does not persist whole-turn audio automatically.

真实聊天用正常启动器和自己的 LLM 配置。设置开关、匹配权重、默认参考及其准确
原文；让模型自然使用已有 EMO 标签。桌面管线目前不会自动永久保存每轮音频，
需要导出时用上述复现脚本；历史音频无法事后恢复。

## Evidence, limitations / 证据与限制

- Real hc3 chat: 45 queued spans, 45 starts, 45 ends; 27 synthesis jobs;
  all observed merges stayed within one emotion. No audio was retained for that turn.
- Saved hc3 replay: 74.509 seconds, 32/32 spans completed, 24 kHz mono.
- In that replay, time from producer start to reference composition was about
  95–101 ms on first use and 0–3 ms on reuse. This is a log interval, not an
  isolated switch benchmark or cross-hardware guarantee.
- Routed first sentences bypass the default opening-audio cache. Changing
  emotion limits merging; excessive tags can create more jobs and pauses.
  Longer semantic references and outputs can increase T2S work. Acoustic
  conditioning stays fixed, but output mel length still changes CFM/vocoder work.
- Cold reference construction currently also computes acoustic features that
  the mixed view later replaces. This is bounded one-time work, not yet optimized.
- Older pre-rendered long-dialogue evidence includes one explicitly documented
  semantic-collapse retake for both A/B variants. It is not a production retry
  policy or evidence that every long utterance is stable.

已有 hc3 真实聊天 45 个片段全部完成，但该轮没有留存 WAV；已保存的重放为
74.509 秒、32 个片段、24 kHz。首次参考准备约 95–101 ms，复用约 0–3 ms，
仅为该机日志阶段耗时。情绪首句不命中默认开场音频缓存；标签过密会拆散合并。
首次构建参考缓存还计算了随后被替换的声学特征，可后续优化，但不影响条件来源。
旧长文试听包含一次明确记录的 A/B 同步换种子重做，不代表生产重试策略。

The architecture is suitable for an isolated opt-in experiment. It is not yet a
general multi-platform emotion API, nor a controlled proof of subjective improvement.
No new default-V3 GPU experiment was run while preparing this share.

实现适合作为可关闭的隔离实验；尚不是通用跨平台情绪 API，也没有完成严格听感
对照。本次整理分享未补跑默认 V3 GPU 实验。最新检查结果见资源包 `VALIDATION.md`。
