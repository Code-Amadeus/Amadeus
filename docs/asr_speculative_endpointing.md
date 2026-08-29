# ASR 两段式投机端点（Speculative Endpointing）

Date: 2026-07-02

## 问题

Qwen3-ASR 是整段式识别：必须等端点检测（VAD 静音 `ASR_VAD_SILENCE_MS`=350ms
或能量静音 `ASR_ENERGY_END_MS`=450ms）确认"说完了"，才开始转写。
用户停止说话后的感知延迟 = 尾静音等待 + 整段转写耗时（约 300–600ms）串联。

## 方案

把端点分成两段，让转写与尾静音等待并行：

1. **第一段（投机）**：捕获循环里 RMS 连续静音达到 `ASR_SPECULATIVE_END_MS`
   （默认 160ms）时，把当前已捕获音频提交给 ASR 后端在后台线程转写。
2. **作废**：说话恢复（RMS 回升）或该段被 VAD/能量端点丢弃时，投机结果作废；
   下一次停顿可重新武装（前一次投机仍在 sidecar 中执行时不排队新任务）。
3. **第二段（确认）**：正常端点（350/450ms）确认后，若投机未作废，
   直接等待并复用投机文本（`[ASR-SPEC] hit`）；否则按原路径重新转写完整音频。

理论收益：`min(转写耗时, 端点静音 - 投机静音)` ≈ 190–290ms/轮。
转写结果安全性不变：复用条件是"投机之后没有再出现语音"，此时投机音频与
最终音频只差尾部静音，转写文本一致；`is_asr_prompt_leak` 检查两条路径都过。

## 实现位置

- `config/settings.py`：`ASR_SPECULATIVE_TRANSCRIBE`（默认开）、
  `ASR_SPECULATIVE_END_MS`（默认 160）
- `asr/mic_input_service.py`：`capture_utterance` 新增
  `probable_end_silence_ms` / `on_probable_end` / `on_probable_end_cancelled`，
  复用既有 RMS `silence_chunks` 计数通道
- `asr/manager.py`：`_SpeculativeTranscription`（submit / invalidate / consume）
  + `_listen_once` 接线。后端 `transcribe` 自带 IO 锁，投机与正式调用天然串行。

## 日志标记

- `[ASR-SPEC] probable end: speculative transcribe submitted (...)`
- `[ASR-SPEC] speculation invalidated: speech_resumed`
- `[ASR-SPEC] hit: reused speculative text (transcribe total Xms, post-endpoint wait Yms)`

`post-endpoint wait` 就是端点确认后的真实剩余等待——对比无投机时的整段转写
耗时即为本方案的实测收益。

## 后续扩展（未实现）

- **投机 LLM 启动**：投机转写完成后即发起 LLM 请求（甚至 hybrid 首句流），
  端点确认且文本一致时直接接上。需要 chat 侧支持"缓冲输出、确认前不进 TTS"
  的 pending-turn 语义，建议等 TurnCoordinator（收敛计划 Phase 2）落地后做。
- **远端 prompt cache 预热**：投机触发时预热远端连接/前缀缓存，成本低，
  可与上一项分开先做。
