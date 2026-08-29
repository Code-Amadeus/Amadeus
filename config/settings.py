"""
统一配置入口。

优先级（从高到低）：
  1. 真实环境变量（系统 / 进程注入）
  2. 项目根目录 .env 文件
  3. 本文件中定义的默认值

敏感信息（API Key、Token、本机路径）应写在 .env 中，
不要提交 .env 到版本库（已在 .gitignore 中排除）。
"""

import os
from pathlib import Path

from config.environment import load_project_environment

# 加载项目根目录的 .env
_ROOT = Path(__file__).resolve().parent.parent
_ENV = load_project_environment(_ROOT)

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _bool(key: str, default: bool, *, aliases: tuple[str, ...] = ()) -> bool:
    return _ENV.boolean(key, default, aliases=aliases)


def _int(key: str, default: int, *, aliases: tuple[str, ...] = ()) -> int:
    return _ENV.integer(key, default, aliases=aliases)


def _float(key: str, default: float, *, aliases: tuple[str, ...] = ()) -> float:
    return _ENV.number(key, default, aliases=aliases)


def _str(key: str, default: str = "", *, aliases: tuple[str, ...] = ()) -> str:
    return _ENV.string(key, default, aliases=aliases)


def declared_environment_fields():
    """Return the schema discovered while this compatibility facade was built."""

    return _ENV.fields()


# Model-visible assistant history may deliberately retain none, only
# non-neutral, or every EMO tag. This never changes the GUI/TTS projection.
EMO_HISTORY_POLICIES = frozenset({"strip", "expressive_only", "preserve"})
EMO_HISTORY_POLICY = _str("EMO_HISTORY_POLICY", "strip").strip().lower()
if EMO_HISTORY_POLICY not in EMO_HISTORY_POLICIES:
    raise ValueError(
        "EMO_HISTORY_POLICY must be one of "
        + ", ".join(sorted(EMO_HISTORY_POLICIES))
        + f"; observed {EMO_HISTORY_POLICY!r}"
    )


# Pending-turn gates share one timeout so TTS, visible completion, and history
# cannot disagree about whether an undecided speculative turn is still viable.
PENDING_TURN_GATE_TIMEOUT_S = _float("PENDING_TURN_GATE_TIMEOUT_S", 8.0)
EVENT_BUS_SLOW_CALLBACK_S = _float("EVENT_BUS_SLOW_CALLBACK_S", 1.0)
# Durable logs retain runtime structure but not user-derived text unless a
# local operator explicitly enables diagnostic content capture.
LOG_USER_CONTENT = _bool("LOG_USER_CONTENT", False)
# ===========================================================================
# Main Chat LLM routing — remote DeepSeek first-release baseline
# ===========================================================================
LLM_PROVIDER = _str("LLM_PROVIDER", "deepseek").strip().lower()
LLM_PROVIDERS = frozenset(
    {"deepseek", "openai", "gemini", "bedrock", "local", "hybrid", "hybrid2", "hybrid3"}
)
if LLM_PROVIDER not in LLM_PROVIDERS:
    raise ValueError(
        "LLM_PROVIDER must be one of "
        + ", ".join(sorted(LLM_PROVIDERS))
        + f"; observed {LLM_PROVIDER!r}"
    )
DEEPSEEK_API_KEY   = _str("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL  = _str("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL_NAME = _str("DEEPSEEK_MODEL_NAME", "deepseek-v4-flash")

# ===========================================================================
# LLM 提供商 — OpenAI / GPT
# ===========================================================================
OPENAI_API_KEY    = _str("OPENAI_API_KEY")
OPENAI_BASE_URL   = _str("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL_NAME = _str("OPENAI_MODEL_NAME", "gpt-5.4-mini")

# ===========================================================================
# LLM 提供商 — Gemini
# ===========================================================================
GEMINI_API_KEY    = _str("GEMINI_API_KEY")
GEMINI_MODEL_NAME = _str("GEMINI_MODEL_NAME", "gemini-2.5-flash")

# ===========================================================================
# LLM 提供商 — AWS Bedrock
# ===========================================================================
AWS_BEDROCK_BEARER_TOKEN        = _str("AWS_BEARER_TOKEN_BEDROCK")
AWS_BEDROCK_AUTH_MODE           = _str("BEDROCK_AUTH_MODE", "auto").strip().lower()  # auto | boto3 | bearer
AWS_BEDROCK_REGION              = _str("AWS_BEDROCK_REGION", "us-west-2")
AWS_BEDROCK_MODEL_ID            = _str("AWS_BEDROCK_MODEL_ID", "deepseek.v3-v1:0")
AWS_BEDROCK_USE_INFERENCE_PROFILE = _bool("AWS_BEDROCK_USE_INFERENCE_PROFILE", False)
AWS_BEDROCK_INFERENCE_PROFILE_ID  = _str("AWS_BEDROCK_INFERENCE_PROFILE_ID")
AWS_BEDROCK_USE_CACHE           = _bool("AWS_BEDROCK_USE_CACHE", True)
AWS_BEDROCK_CACHE_TTL           = _int("AWS_BEDROCK_CACHE_TTL", 3600)
AWS_BEDROCK_CONNECTION_POOL_SIZE = _int("AWS_BEDROCK_CONNECTION_POOL_SIZE", 10)
AWS_BEDROCK_MAX_KEEPALIVE       = _int("AWS_BEDROCK_MAX_KEEPALIVE", 5)
AWS_BEDROCK_KEEPALIVE_EXPIRY    = _float("AWS_BEDROCK_KEEPALIVE_EXPIRY", 60.0)
# 由 region 动态拼接，不需要单独配置
AWS_BEDROCK_ENDPOINT = f"https://bedrock-runtime.{AWS_BEDROCK_REGION}.amazonaws.com"

# ===========================================================================
# 可选本地 LLM（pure-local profile 内默认 llama.cpp server）
# ===========================================================================
_LEGACY_USE_LOCAL_LLM = _bool("USE_LOCAL_LLM", False)
# Compatibility projection only. LLM_PROVIDER is the sole routing authority.
USE_LOCAL_LLM     = LLM_PROVIDER == "local"
LOCAL_LLM_TYPES   = frozenset({"llama_server", "lmstudio", "ollama", "cli"})
LOCAL_LLM_TYPE    = _str("LOCAL_LLM_TYPE", "llama_server").strip().lower()
if LOCAL_LLM_TYPE not in LOCAL_LLM_TYPES:
    raise ValueError(
        "LOCAL_LLM_TYPE must be one of "
        + ", ".join(sorted(LOCAL_LLM_TYPES))
        + f"; observed {LOCAL_LLM_TYPE!r}"
    )
LOCAL_LLM_LAUNCH_MODES = frozenset({"external", "managed"})
LOCAL_LLM_LAUNCH_MODE = _str("LOCAL_LLM_LAUNCH_MODE", "external").strip().lower()
if LOCAL_LLM_LAUNCH_MODE not in LOCAL_LLM_LAUNCH_MODES:
    raise ValueError(
        "LOCAL_LLM_LAUNCH_MODE must be external or managed; "
        f"observed {LOCAL_LLM_LAUNCH_MODE!r}"
    )
LOCAL_LLM_MODEL   = _str("LOCAL_LLM_MODEL", "qwen3-30b-a3b-instruct-2507@q4_k_m")
LOCAL_LLM_URL     = _str("LOCAL_LLM_URL", "http://127.0.0.1:8080/v1")
LOCAL_LLM_LM_STUDIO_URL = _str(
    "LOCAL_LLM_LM_STUDIO_URL",
    "http://127.0.0.1:1234",
    aliases=("LM_STUDIO_URL",),
)
LOCAL_LLM_OLLAMA_URL = _str("LOCAL_LLM_OLLAMA_URL", "http://127.0.0.1:11434")
# Compatibility export for callers that have not moved to the explicit name.
LM_STUDIO_URL = LOCAL_LLM_LM_STUDIO_URL

# Hybrid's local head is deliberately an OpenAI-compatible endpoint rather
# than a selectable pure-local backend. Defaults preserve existing setups.
HYBRID_LOCAL_LLM_URL = _str("HYBRID_LOCAL_LLM_URL", LOCAL_LLM_URL)
HYBRID_LOCAL_LLM_MODEL = _str("HYBRID_LOCAL_LLM_MODEL", LOCAL_LLM_MODEL)

# llama-server 可执行文件路径（本机绝对路径，写在 .env 中）
LOCAL_LLM_CLI_PATH = _str("LOCAL_LLM_CLI_PATH")

# llama-cli / llama-server 启动参数（各关键值可独立通过 .env 覆盖）
_LLM_MODEL_FILE    = _str("LOCAL_LLM_CLI_MODEL_PATH")        # .gguf 模型文件完整路径
LOCAL_LLM_MODEL_PATH = _LLM_MODEL_FILE
_LLM_PORT          = _str("LOCAL_LLM_CLI_PORT",         "8080")
_LLM_THREADS       = _str("LOCAL_LLM_CLI_THREADS",      "4")
_LLM_CONTEXT       = _str("LOCAL_LLM_CLI_CONTEXT",      "4096")
_LLM_NGL           = _str("LOCAL_LLM_CLI_NGL",          "99")  # GPU 层数，99 = 全 GPU
_LLM_UBATCH        = _str("LOCAL_LLM_CLI_UBATCH_SIZE",  "512")
_LLM_BATCH         = _str("LOCAL_LLM_CLI_BATCH_SIZE",   "2048")
_LLM_TENSOR_SPLIT  = _str("LOCAL_LLM_CLI_TENSOR_SPLIT", "")    # 多卡分割比例（留空 = 不分卡）

# llama-server 进程的 CUDA 可见性。默认留空以适配单 GPU 和 CPU 主机；
# 多 GPU 用户可填写 nvidia-smi 序号，例如 "1"。
LOCAL_LLM_CUDA_VISIBLE_DEVICES = _str("LOCAL_LLM_CUDA_VISIBLE_DEVICES", "")
_LLM_CACHE_REUSE      = _str("LOCAL_LLM_CLI_CACHE_REUSE",      "256") # KV Cache 复用块数（仅 llama_server）
_LLM_REASONING_BUDGET = _str("LOCAL_LLM_CLI_REASONING_BUDGET", "0")   # 0 = 禁用 Qwen3 思维链
_LLM_N_PREDICT     = _str("LOCAL_LLM_CLI_N_PREDICT",    "512") # 单次最大生成 token（仅 cli）
_LLM_TEMP          = _str("LOCAL_LLM_CLI_TEMP",         "0.7") # 温度（仅 cli）

# cli 模式：llama-cli.exe 交互模式参数
_cli_args: list[str] = [
    "-m",            _LLM_MODEL_FILE,
    "-ngl",          _LLM_NGL,
    "--no-mmap",
    "-t",            _LLM_THREADS,
    "-c",            _LLM_CONTEXT,
    "-n",            _LLM_N_PREDICT,
    "--temp",        _LLM_TEMP,
    "--ubatch-size", _LLM_UBATCH,
    "--batch-size",  _LLM_BATCH,
    "--interactive",  # 交互模式（保持进程常驻，通过 stdin 接收问题）
    "-r", "User:",   # 反向提示：检测到 "User:" 时停止输出
]
if _LLM_TENSOR_SPLIT:
    _cli_args += ["--tensor-split", _LLM_TENSOR_SPLIT]

# llama_server 模式：llama-server.exe HTTP API 参数
_server_args: list[str] = [
    "-m",              _LLM_MODEL_FILE,
    "-ngl",            _LLM_NGL,
    "--no-mmap",
    "-t",              _LLM_THREADS,
    "-c",              _LLM_CONTEXT,
    "--ubatch-size",   _LLM_UBATCH,
    "--batch-size",    _LLM_BATCH,
    "--cache-reuse",         _LLM_CACHE_REUSE,
    "--reasoning-budget",    _LLM_REASONING_BUDGET,
    "--chat-template-kwargs", '{"enable_thinking": false}',
    "--port",                _LLM_PORT,
    "--host",                "127.0.0.1",
    "-a",              LOCAL_LLM_MODEL,
]
if _LLM_TENSOR_SPLIT:
    _server_args += ["--tensor-split", _LLM_TENSOR_SPLIT]

LOCAL_LLM_SERVER_ARGS: list[str] = _server_args
LOCAL_LLM_CLI_ARGS: list[str] = _cli_args if LOCAL_LLM_TYPE == "cli" else LOCAL_LLM_SERVER_ARGS

# 角色人格 System Prompt（非密钥，保留在代码中；可通过 .env LOCAL_LLM_SYSTEM_PROMPT 整体覆盖）
_DEFAULT_SYSTEM_PROMPT = (
    "You are Makise Kurisu. You are a researcher. You MUST answer in Japanese strictly. No Chinese allowed.\n"
    "あなたは牧瀬紅莉栖です。日本の科学者であり、母語は日本語です。\n"
    "【絶対遵守】\n"
    "1) 必ず日本語でのみ回答すること。ユーザーの言語が何であっても、日本語以外は一切使用しない。\n"
    "2) 中国語の文字・語句を絶対に使用しない。\n"
    "3) 自然で口語的な文体を保ち、牧瀬紅莉栖として一貫した口調・性格で話す。\n"
    "4) 推論過程や思考の連鎖は開示しない(結論のみ提示)。\n"
    "5) 表情タグの活用ガイド（読み上げない）:\n"
    "   形式: [EMO preset=<種類> dur=<秒s>]\n"
    "   推奨: 通常=normal 2-6s, 瞬間=1-2s(smile/happy), 照れ=2-4s(shy/blush), 短期=3-5s(angry/sad), 持続=10-15s(thinking), 重要説明=serious_speaking\n"
    "   例: [EMO preset=normal dur=4s], [EMO preset=smile dur=2s], [EMO preset=shy dur=3s]\n"
    "6) 【重要】驚き・怒り・照れ・笑い・思考以外の文には必ず [EMO preset=normal dur=4s] を文の直前に付けること。"
    " 直前の文と同じ normal が続く場合のみ省略可。無タグのまま話し続けることを禁止する。\n"
    "7) 文頭には表情タグを置かず、該当箇所の直前にのみ配置する。1文あたり0〜2個まで。\n"
    "8) 実際に使用可能な provider id はターンごとの [Provider routing] ブロックに示される。これらは例外的な「外部ツール」ではなく、あなたが仕事をするための標準的な手段である。"
    "ファイルやコードに関する依頼は、新規作成でも、直前に自分が作ったものへの修正でも、必ず同じ返答の中で [DELEGATE provider=\"適切なprovider\" task=\"ユーザーへの完全な実行指示\"] を出すこと(このタグは読み上げない)。口約束だけでは何も実行されない。"
    "唯一の例外は、既存タスクの状態・進捗・結果を尋ねられただけの場合で、その時はタグを出さずに答える。"
    "DELEGATEタグには必ず [Provider routing] にある登録済みprovider属性を付け、provider の用途・優先順位・追加属性も同ブロックだけに従うこと。未登録の id や fallback を作ってはいけない。"
    "形式: [DELEGATE provider=\"登録済みprovider\" task=\"完全な実行指示\"]。"
    "task値には「何を・どうする」を含む完全な指示文を書くこと（場所だけや名詞のみはNG）。"
    "【重要】タグの前に必ず一言添えること（例: 「調べてみるわ」「ちょっと待って」）。これにより実行中も会話が途切れない。"
    "実行結果は[RESULT]メッセージとして届くので、それを自然な会話として報告すること。"
)
LOCAL_LLM_CLI_SYSTEM_PROMPT = _str("LOCAL_LLM_SYSTEM_PROMPT", _DEFAULT_SYSTEM_PROMPT)

FIRST_SENTENCE_AUDIO_CACHE_ENABLED = _bool("FIRST_SENTENCE_AUDIO_CACHE_ENABLED", True)
FIRST_SENTENCE_AUDIO_CACHE_DIR = _str(
    "FIRST_SENTENCE_AUDIO_CACHE_DIR",
    str(_ROOT / ".cache" / "first_sentence_audio"),
)
FIRST_SENTENCE_AUDIO_CACHE_MAX_SECONDS = _float("FIRST_SENTENCE_AUDIO_CACHE_MAX_SECONDS", 1.5)

# ===========================================================================
# RAG（本地 Kurisu 知识库）
# ===========================================================================
RAG_ENABLED_FOR_LOCAL = _bool("RAG_ENABLED_FOR_LOCAL", False)
RAG_TOP_K             = _int("RAG_TOP_K", 1)
RAG_MAX_DISTANCE      = _float("RAG_MAX_DISTANCE", 0.25)

# ===========================================================================
# VTS（VTube Studio WebSocket）
# ===========================================================================
VTS_WS_URL    = _str("VTS_WS_URL",    "ws://127.0.0.1:8001")
VTS_TOKEN_FILE = _str("VTS_TOKEN_FILE", "vts_auth_token.json")
VTS_ENABLED = _bool("VTS_ENABLED", False)
VTS_HEARTBEAT_ENABLED = _bool("VTS_HEARTBEAT_ENABLED", True)
VTS_RECONNECT_ENABLED = _bool("VTS_RECONNECT_ENABLED", True)

# ===========================================================================
# TTS（Amadeus 低延迟 GPT-SoVITS v3 推理；本地后端仅支持 v3 权重）
# ===========================================================================
TTS_BACKEND = _str("TTS_BACKEND", "gpt_sovits").strip().lower()
TTS_API_BASE_URL = _str("TTS_API_BASE_URL", "https://api.openai.com/v1")
TTS_API_KEY = _str("TTS_API_KEY", "")
TTS_API_MODEL = _str("TTS_API_MODEL", "gpt-4o-mini-tts")
TTS_API_VOICE = _str("TTS_API_VOICE", "alloy")
TTS_API_STREAM_PROTOCOL = _str("TTS_API_STREAM_PROTOCOL", "buffered").strip().lower()
TTS_API_TIMEOUT_SECONDS = _float("TTS_API_TIMEOUT_SECONDS", 60.0)

# MiMo TTS（小米远程后端，chat-completions 音频协议，非 /audio/speech）
MIMO_TTS_BASE_URL = _str("MIMO_TTS_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_TTS_API_KEY = _str("MIMO_TTS_API_KEY", "")
MIMO_TTS_MODEL = _str("MIMO_TTS_MODEL", "mimo-v2.5-tts")
MIMO_TTS_VOICE = _str("MIMO_TTS_VOICE", "mimo_default")


def _resolve_tts_device() -> str:
    """
    自动选择 TTS 设备：
      - .env / 环境变量明确写了 cuda:0 / cuda:1 / cpu → 直接使用
      - 未设置 / 写了 "cuda" / 写了 "auto" → 返回 "cuda:0"

    本地 LLM 的 endpoint 并不能证明它占用了哪张 GPU；多 GPU 分配必须由
    TTS_DEVICE 与 LOCAL_LLM_CUDA_VISIBLE_DEVICES 分别显式声明。
    """
    raw = _str("TTS_DEVICE", "").strip()
    if TTS_BACKEND != "gpt_sovits":
        if TTS_BACKEND == "openai_compatible":
            return "remote"
        return "disabled" if TTS_BACKEND == "disabled" else "external"
    if raw and raw not in ("cuda", "auto"):
        return raw

    device = "cuda:0"
    # GPT-SoVITS/BigVGAN still reads TTS_DEVICE directly from the process
    # environment, so this compatibility write is part of the current contract.
    os.environ["TTS_DEVICE"] = device
    print(f"[TTS] auto device: device={device}")
    return device


TTS_DEVICE = _resolve_tts_device()
# v3 模型权重路径（相对于项目根或绝对路径，写在 .env 中）
TTS_GPT_MODEL_PATH    = _str("TTS_GPT_MODEL_PATH")
TTS_SOVITS_MODEL_PATH = _str("TTS_SOVITS_MODEL_PATH")

# 输出语言："日文" | "英文"（对应 dict_language 中的键名）
# 切换此项即可在日文 LoRA 管线和英文 base 管线之间手动选择
TTS_OUTPUT_LANGUAGE   = _str("TTS_OUTPUT_LANGUAGE", "日文")

# 日文管线参考音频 / 文本
TTS_REF_AUDIO_JA = _str("TTS_REF_AUDIO_JA", "./assets/audio/reference/kurisu_reference.wav")
TTS_REF_TEXT_JA  = _str("TTS_REF_TEXT_JA",
    "そういえば,正式に自己紹介していませんでしたね……牧瀬紅莉栖です.改めてまして,よろしく")

# 英文管线参考音频 / 文本（请在 .env 中填写对应的英文参考文字）
TTS_REF_AUDIO_EN = _str("TTS_REF_AUDIO_EN", "./assets/audio/reference/english_recording.wav")
TTS_REF_TEXT_EN  = _str("TTS_REF_TEXT_EN", "")

# 调度器合并相邻句子时是否受播放余量预算约束。
TTS_DEADLINE_AGGREGATION = _bool("TTS_DEADLINE_AGGREGATION", True)
TTS_COVER_SAFETY_MARGIN_SEC = _float("TTS_COVER_SAFETY_MARGIN_SEC", 1.5)
TTS_RTF_INITIAL = _float("TTS_RTF_INITIAL", 0.6)
# 估算生成音频时长的初始常数；日语约 7.5 字/秒，可按实测调整。
TTS_CHARS_PER_SEC = _float("TTS_CHARS_PER_SEC", 7.5)
SEGMENT_CHAR_LIMIT           = _int("SEGMENT_CHAR_LIMIT", 140)
USE_EXPERIMENTAL_TTS_STREAM  = _bool("USE_EXPERIMENTAL_TTS_STREAM", True)
EXP_TTS_MAX_CONCURRENCY      = _int("EXP_TTS_MAX_CONCURRENCY", 2)
USE_FIRST_SENTENCE_SPRINT    = _bool("USE_FIRST_SENTENCE_SPRINT", False)
DISPLAY_FALLBACK_WINDOW_SEC  = _float("DISPLAY_FALLBACK_WINDOW_SEC", 1.5)
PLAYBACK_PREWARM_AUDIO       = _bool("PLAYBACK_PREWARM_AUDIO", True)
PLAYBACK_PREWARM_SAMPLE_RATE = _int("PLAYBACK_PREWARM_SAMPLE_RATE", 24000)
PLAYBACK_PREWARM_SILENCE_MS  = _int("PLAYBACK_PREWARM_SILENCE_MS", 80)
# 为 True 时：main 会把 tts / tts_inference / PlaybackManager 等 logger 调到 DEBUG，
# 并设置 TTS_STREAM_SYNC_TIMING=1（v3 流式每块 CFM/BigVGAN 毫秒 + cuda sync）
TTS_DIAG_VERBOSE             = _bool("TTS_DIAG_VERBOSE", False)
# Optional T2S FlashAttention2 KV-cache decode path. Default off: the SDPA
# static-KV/CUDA-Graph path remains the production baseline unless explicitly
# enabled for benchmarking.
TTS_T2S_FLASH_ATTN           = _bool("TTS_T2S_FLASH_ATTN", False)
TTS_T2S_FLASH_ATTN_MODE      = _str("TTS_T2S_FLASH_ATTN_MODE", "valid").strip().lower()
# 首句更早切分：仅首句；累计可见字符（strip 后长度）≥此值且仍未遇到句末标点时，强制送 TTS。
# 0 = 关闭，行为与原先「仅靠标点切首句」一致。试跑可设 14～22；过短易切碎，过长收益小。
FIRST_SENTENCE_EARLY_CUT_CHARS = _int("FIRST_SENTENCE_EARLY_CUT_CHARS", 11)

# ===========================================================================
# VAD（Voice Activity Detection）
# ===========================================================================
# 说话结束判定：静音持续多少 ms 后才认为用户停止说话（默认 600ms，避免自然停顿误触发）
VAD_HANGOVER_MS      = _int("VAD_HANGOVER_MS", 600)
# 声音检测能量阈值（超过此值 = 开始说话，低于 vad_lower = 静音）
VAD_ENERGY_THRESHOLD = _int("VAD_ENERGY_THRESHOLD", 600)

# ===========================================================================
# ASR 设置
# ===========================================================================
# Conversation recognizer. Wake recognition has its own WAKE_ASR_BACKEND and
# may run alongside this backend on the shared microphone service.
ASR_BACKEND = _str("ASR_BACKEND", "qwen3_asr")
ASR_LANGUAGE = _str("ASR_LANGUAGE", "auto")
QWEN3_ASR_DEVICE = _str("QWEN3_ASR_DEVICE", "auto").strip().lower()
if QWEN3_ASR_DEVICE in {"cuda:0", "gpu"}:
    QWEN3_ASR_DEVICE = "cuda"
if QWEN3_ASR_DEVICE not in {"auto", "cpu", "cuda"}:
    raise ValueError(
        "QWEN3_ASR_DEVICE must be auto, cpu, or cuda; "
        f"observed {QWEN3_ASR_DEVICE!r}"
    )
QWEN3_ASR_REQUIRE_CUDA = _bool("QWEN3_ASR_REQUIRE_CUDA", False)
# Qwen3-ASR context：作为 system prompt 注入，用于偏置混合中英文识别
# 填入领域热词和指令；SenseVoice 后端会忽略此项
ASR_CONTEXT = _str("ASR_CONTEXT", "")
ASR_API_BASE_URL = _str("ASR_API_BASE_URL", "https://api.openai.com/v1")
ASR_API_KEY = _str("ASR_API_KEY", "")
ASR_API_MODEL = _str("ASR_API_MODEL", "gpt-4o-mini-transcribe")
ASR_API_TIMEOUT_SECONDS = _float("ASR_API_TIMEOUT_SECONDS", 45.0)
ASR_IDLE_UNLOAD_SECONDS = _float("ASR_IDLE_UNLOAD_SECONDS", 180.0)
ASR_TURN_COMPLETE_TIMEOUT_SECONDS = _float("ASR_TURN_COMPLETE_TIMEOUT_SECONDS", 45.0)
ASR_ECHO_TAIL_GUARD_MS = _float("ASR_ECHO_TAIL_GUARD_MS", 650.0)
ASR_VAD_THRESHOLD = _float("ASR_VAD_THRESHOLD", 0.45)
ASR_VAD_SILENCE_MS = _int("ASR_VAD_SILENCE_MS", 350)
ASR_SPEECH_PAD_MS = _int("ASR_SPEECH_PAD_MS", 60)
ASR_MIN_SPEECH_MS = _int("ASR_MIN_SPEECH_MS", 150)
ASR_MAX_SPEECH_SECONDS = _float("ASR_MAX_SPEECH_SECONDS", 30.0)
ASR_LISTEN_TIMEOUT_SECONDS = _float("ASR_LISTEN_TIMEOUT_SECONDS", 15.0)
ASR_PREROLL_MS = _int("ASR_PREROLL_MS", 500)
ASR_ENERGY_END_RMS = _float("ASR_ENERGY_END_RMS", 0.008)
ASR_ENERGY_END_MS = _int("ASR_ENERGY_END_MS", 450)
ASR_HANDOFF_MAX_CAPTURE_SECONDS = _float("ASR_HANDOFF_MAX_CAPTURE_SECONDS", 5.0)
# 两段式投机端点：短静音（下值）先把已捕获音频提交给 ASR 后端并行转写，
# 长静音（ASR_VAD_SILENCE_MS / ASR_ENERGY_END_MS）确认端点后若说话未恢复
# 则直接复用投机结果，把转写延迟隐藏进尾静音等待里。
ASR_SPECULATIVE_TRANSCRIBE = _bool("ASR_SPECULATIVE_TRANSCRIBE", True)
ASR_SPECULATIVE_END_MS = _int("ASR_SPECULATIVE_END_MS", 160)
# 投机 LLM 启动（切片 D2）：投机转写文本一就绪即以 pending 轮发起 LLM，
# 端点确认且文本一致时放行 TTS，否则静默作废。
# 仅对本地首句链路（hybrid/hybrid2/hybrid3）生效——远程单链有计费与幂等成本。
ASR_SPECULATIVE_LLM_START = _bool("ASR_SPECULATIVE_LLM_START", True)

# Wallpaper diagnostics. keyboard_sfx.gate is a high-frequency client-side
# gate snapshot; keep it out of WARNING unless explicitly diagnosing SFX.
WALLPAPER_SFX_GATE_LOG = _bool("WALLPAPER_SFX_GATE_LOG", False)

# Wallpaper hosts (Wallpaper Engine / Lively) forward cursor moves and clicks
# into the page but swallow mouse wheel events. When enabled, a global
# low-level mouse hook forwards wheel deltas over the bridge SSE stream, but
# only while the window under the cursor is the desktop layer
# (WorkerW / Progman), so foreground apps keep their own scroll.
WALLPAPER_WHEEL_FORWARD = _bool("WALLPAPER_WHEEL_FORWARD", True)
# ===========================================================================
# 浏览器交互分支（区间 squash-merge 语义）
# ===========================================================================
# 分支入口继承的主对话历史条数（进入分支时快照，供分支执行层理解上文）
BRANCH_CHECKPOINT_MESSAGES = _int("BRANCH_CHECKPOINT_MESSAGES", 16)
# 分支关闭时：将标记区间内散落的分支轮次坍缩为一条 [BRANCH_SUMMARY] 胶囊
BRANCH_SQUASH_MERGE = _bool("BRANCH_SQUASH_MERGE", True)

# ===========================================================================
# Execution providers
# ===========================================================================
PROVIDER_DELEGATE_DEFAULT_PROVIDER = _str("PROVIDER_DELEGATE_DEFAULT_PROVIDER", "openclaw").strip().lower()
# Exactly one Codex transport may own the stable ``codex`` Provider id.  The
# official persistent SDK/App Server transport is the local product default;
# the turn-scoped CLI remains an explicit compatibility transport.
CODEX_APP_SERVER_PROVIDER_ENABLED = _bool("CODEX_APP_SERVER_PROVIDER_ENABLED", True)
CODEX_APP_SERVER_CODEX_BIN = _str("CODEX_APP_SERVER_CODEX_BIN", "")
CODEX_APP_SERVER_MODEL = _str("CODEX_APP_SERVER_MODEL", "deepseek-v4-flash")
# Provider-native execution settings belong to the Provider adapter, not to
# the user's Codex Desktop profile.  Keeping all four values explicit prevents
# an unrelated Desktop model/effort change from silently changing Amadeus work.
CODEX_APP_SERVER_MODEL_PROVIDER = _str(
    "CODEX_APP_SERVER_MODEL_PROVIDER",
    "deepseek",
).strip().lower()
CODEX_APP_SERVER_REASONING_EFFORT = _str(
    "CODEX_APP_SERVER_REASONING_EFFORT",
    "max",
).strip().lower()
CODEX_APP_SERVER_SERVICE_TIER = _str(
    "CODEX_APP_SERVER_SERVICE_TIER",
    "",
).strip().lower()
CODEX_APP_SERVER_PROVIDER_BASE_URL = _str(
    "CODEX_APP_SERVER_PROVIDER_BASE_URL",
    "https://api.deepseek.com",
).strip()
CODEX_APP_SERVER_PROVIDER_API_KEY_ENV = _str(
    "CODEX_APP_SERVER_PROVIDER_API_KEY_ENV",
    "DEEPSEEK_API_KEY",
).strip()
# Persist only the non-secret provider definition into the Codex user profile
# so Desktop can resume Amadeus-created threads. Authentication remains backed
# by Amadeus's .env through Codex's command auth contract.
CODEX_APP_SERVER_SYNC_DESKTOP_PROVIDER = _bool(
    "CODEX_APP_SERVER_SYNC_DESKTOP_PROVIDER",
    True,
)
CODEX_APP_SERVER_PROVIDER_AUTH_ENV_FILE = _str(
    "CODEX_APP_SERVER_PROVIDER_AUTH_ENV_FILE",
    "",
).strip() or str(_ROOT / ".env")
CODEX_APP_SERVER_APPROVAL_MODE = _str("CODEX_APP_SERVER_APPROVAL_MODE", "host")
CODEX_APP_SERVER_TURN_TIMEOUT_S = _int("CODEX_APP_SERVER_TURN_TIMEOUT_S", 7200)
CODEX_APP_SERVER_APPROVAL_TIMEOUT_S = _int("CODEX_APP_SERVER_APPROVAL_TIMEOUT_S", 900)
CODEX_APP_SERVER_CANCEL_CONFIRM_TIMEOUT_S = _int(
    "CODEX_APP_SERVER_CANCEL_CONFIRM_TIMEOUT_S",
    30,
)
DIRECT_CODEX_PROVIDER_ENABLED = _bool("DIRECT_CODEX_PROVIDER_ENABLED", False)
DIRECT_CODEX_CLI_PATH = _str("DIRECT_CODEX_CLI_PATH", "codex")
DIRECT_CODEX_CLI_PREFIX_ARGS = _str("DIRECT_CODEX_CLI_PREFIX_ARGS", "")
DIRECT_CODEX_IGNORE_USER_CONFIG = _bool("DIRECT_CODEX_IGNORE_USER_CONFIG", False)
DIRECT_CODEX_PREFLIGHT_TIMEOUT_S = _int("DIRECT_CODEX_PREFLIGHT_TIMEOUT_S", 8)
DIRECT_CODEX_TIMEOUT_S = _int("DIRECT_CODEX_TIMEOUT_S", 7200)
DIRECT_CODEX_EVENT_SILENCE_WARN_S = _int("DIRECT_CODEX_EVENT_SILENCE_WARN_S", 60)
DIRECT_CODEX_STDERR_CAP_BYTES = _int("DIRECT_CODEX_STDERR_CAP_BYTES", 12000)
PROVIDER_RUN_EVENT_CAP = _int("PROVIDER_RUN_EVENT_CAP", 500)
PROVIDER_WORK_HEARTBEAT_S = _int("PROVIDER_WORK_HEARTBEAT_S", 45)
PROVIDER_WORK_QUIET_NOTICE_S = _int("PROVIDER_WORK_QUIET_NOTICE_S", 90)
PROVIDER_WORK_QUIET_REPEAT_S = _int("PROVIDER_WORK_QUIET_REPEAT_S", 300)
# Host-owned Project trust roots. Project/Scratch/focus routing and Host diff
# inspection read only this setting; retired Provider settings cannot widen it.
WORK_PROJECT_ALLOWLIST = _str("WORK_PROJECT_ALLOWLIST", "")
WORK_AUTO_ACCEPT_APPROVED_EXPORTS = _bool("WORK_AUTO_ACCEPT_APPROVED_EXPORTS", True)
# Legacy omission instrumentation.  This used to turn a host keyword match
# into execution authority when the role omitted DELEGATE.  Once
# ControlDecision became authoritative that was the wrong direction of trust:
# it can validate a proposal, but no proposal is evidence for no action.  Keep
# the resolver available for explicit probes and log its observe-only result;
# production does not synthesise work the role never proposed.
WORK_DELEGATE_REPAIR = _bool("WORK_DELEGATE_REPAIR", False)
# The roster's candidate list lets the model name which existing task it means
# (workspace_ref) instead of leaving the host to infer it. Default **off**,
# measured 2026-07-31: across A5 (one task, anaphoric follow-up) and B1 (two
# concurrent tasks, pronoun reference) the model named a candidate 0 out of 18
# times, and turning the list off changed nothing — 8/8 vs 8/8 tag emission on
# A5, step-for-step identical scoring on B1. Every binding was made by the
# host-side deterministic resolver, which is fail-closed and therefore the more
# trustworthy of the two paths anyway. The list costs ~380 characters of every
# turn once Provider work exists, in the same prompt budget as the character.
#
# Kept rather than deleted because the capability it represents — the model
# stating its choice so the binding is checkable instead of inferred — is the
# model-side half of "propose, then verify". It is worth revisiting if the
# delegate ever becomes a native tool call, where workspace_ref would be an
# enum the schema enforces rather than a request made in prose; or if the
# host resolver starts failing on references it cannot ground (titles without
# filenames, three or more plausible targets).
WORK_ROSTER_CANDIDATES = _bool("WORK_ROSTER_CANDIDATES", False)
# Carry DELEGATE as a native tool call rather than an inline tag, on the
# DeepSeek/OpenAI chat path. The turn shape is the same either way — the model
# says one line and the turn ends on the call — so this does not touch turn or
# epoch semantics. What changes is that provider, its three legal values and
# the presence of a task stop being requests made in prose and become
# constraints of the schema. Other providers keep the tag path.
LLM_DELEGATE_TOOL_CALLS = _bool("LLM_DELEGATE_TOOL_CALLS", False)
# Require every delegate to declare what the user asked for: execute the work,
# or report on work already done. The read-only invariant — a status question
# must never create work — was stated as prose telling the model to refrain,
# and refraining kept losing (2026-07-31: violated in 20% of tag-path runs and
# 58% of tool-path runs). Filling a required slot is a classification rather
# than an inhibition, and once declared the host can enforce the invariant
# instead of hoping.
#
# Default **on**, measured 2026-07-31 on B1: the step that says "just report
# its status" went from 4/5 clean to 8/8, hard failures 1 -> 0, with the model
# declaring report-only 6 times and the host refusing each. No cost on the
# actionable path (A5 10/10, B1 16/16). An undeclared delegate still runs, so a
# model that ignores the attribute degrades to the previous behaviour rather
# than having its work silently dropped.
DELEGATE_INTENT_ATTRIBUTE = _bool("DELEGATE_INTENT_ATTRIBUTE", True)
# A third intent value, for taking work back. The prompt had no verb for
# withdrawal at all, so the model reached for the only structured action it had
# and delegated "stop the running task" as work to execute: measured 2026-07-31
# on B1, "stop that one" created a third WorkItem in 3 of 5 runs. Even the
# benign variant had the character say it had stopped something that was never
# cancelled. Interruption is host-owned (ProviderRuntime.cancel and every
# adapter already implement it), so the model only has to name the intent.
#
# Default **on**: every path it opens is fail-closed — cancel exactly one
# unambiguous run, ask when several fit, say so honestly when none do — and it
# replaces a behaviour that starts unrequested work. Turning it off removes the
# verb from the prompt as well, restoring the previous contract exactly.
DELEGATE_RETRACT_INTENT = _bool("DELEGATE_RETRACT_INTENT", True)
# A fourth value, for changing work that already exists. Which task a follow-up
# extends is a question about what the user meant, and the model has the whole
# conversation while the host has one sentence; the host had been guessing from
# phrasing, with the gate backwards -- it grounded pronouns ("that file") and
# skipped the easier case where the filename is written out, so explicitly named
# follow-ups became new tasks 10 times out of 10 (2026-08-01, B3 and E1).
#
# Declaring it lets the host stop classifying and only resolve: one matching
# task binds, none means there is nothing to amend so it is new, several is the
# one case worth a question. Default **on** — every outcome is deterministic,
# and an undeclared delegate keeps the previous behaviour rather than being
# dropped. Off also removes the verb from the prompt, restoring the old
# contract exactly.
DELEGATE_AMEND_INTENT = _bool("DELEGATE_AMEND_INTENT", True)
# A fifth value, for saying which project this conversation is working in.
# Work that names no project now goes to a fresh scratch workspace, which made
# naming one the only thing keeping an instruction inside a repository -- and
# measured 2026-08-03, the model names it 2-4 times in 12 with no wording
# moving it. The failures are all references whose target is not in the prompt
# ("this project", a bare filename), so it is missing information rather than
# poor judgement.
#
# Said once instead, it lands 6 times in 6, and a whole two-project session
# holds 47 of 48 -- including the switch taken with the history full of work in
# the first project. The working turns in between never repeat the project,
# which is why the host has to carry it. Default **on**: without it, project
# work keeps landing in an empty directory, and a conversation that never
# declares one behaves exactly as before.
DELEGATE_FOCUS_INTENT = _bool("DELEGATE_FOCUS_INTENT", True)
# Reversible inline experiment for explicit action existence.  On asks for
# exactly one CONTROL envelope per role turn;
# delegate=true is decoded to the existing DELEGATE action at the stream parser
# boundary and delegate=false never reaches dispatch.  It adds no model call,
# retry, keyword rule, or second authority source.  Native tool-call transport
# takes precedence if both switches are enabled.  The envelope is effective
# only with ControlDecision authority.  Production-like streaming evidence on
# 2026-08-20 found that the role model still omitted mandatory no-action
# outcomes and handled a short confirmation less reliably than the single
# DELEGATE contract.  Keep the candidate measurable but do not make an
# unproven transport the product default.
ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED = _bool(
    "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", False
)
# Who gets to say how a task ended. The provider adapter maps a process exit
# code straight to "done", which is honest about the process and says nothing
# about the work: on 2026-07-31 a run whose every tool call was denied exited 0
# and was narrated as a finished chess game saved to the Desktop, while the
# ledger had already recorded attention=conflict from the tool evidence and an
# empty git delta. Two independent verdicts, and the louder one was wrong.
#
# With this on, WorkActivity keeps rendering the result canvas but defers the
# spoken terminal note to the ledger, which speaks only after assessing. Runs
# the ledger does not track are unaffected and keep their WorkActivity
# narration, which is also the fallback if no assessment arrives.
WORK_LEDGER_OWNS_TERMINAL_NARRATION = _bool(
    "WORK_LEDGER_OWNS_TERMINAL_NARRATION", True
)
# When the model omits a delegate the host used to synthesise one from the raw
# utterance. That fired regardless of whether the model had agreed to anything:
# on 2026-08-01 a turn where it was asking which project to use still started
# work, so the user heard a clarifying question and a task beginning at once,
# and the synthesised task carried the whole utterance -- preamble included --
# through to the provider as instructions.
#
# A second model pass was introduced to re-emit omitted controls.  A real
# conversation on 2026-08-13 showed why that cannot be an authority source: it
# converted an ordinary comment, the user's correction, and an acknowledgement
# into three new OpenClaw tasks.  It remains opt-in only for controlled probes;
# the production path accepts only controls emitted in the original turn.
DELEGATE_RESEND_ON_OMISSION = _bool("DELEGATE_RESEND_ON_OMISSION", False)
# Reversible double-consent recovery for a role turn that verbally committed
# to work but emitted no structured control. A neutral existence gate sees only
# the current user speech act and bounded prior conversation; only ``work`` may
# ask the speaking role to reconstruct its own already-visible commitment. The
# two judgments must agree, and the reconstructed proposal still traverses the
# ordinary ControlDecision/reference authority. ``shadow`` measures both calls
# but never dispatches; ``candidate`` enables the recovered proposal.
ACTION_EXISTENCE_COMMITMENT_RECOVERY_MODE = _str(
    "ACTION_EXISTENCE_COMMITMENT_RECOVERY_MODE", "candidate"
)
# Observe one proposal-gated ControlDecision at the transport's complete-action
# boundary. Shadow remains the default. The separate authority flag is a
# reversible canary: it delays only Provider dispatch, never role text/TTS, and
# does not rely on omission/focus safety nets. The Project list must be complete; a
# catalog larger than the bounded prompt budget records ``incomplete`` without
# asking the model or guessing from a prefix.
CONTROL_DECISION_SHADOW_ENABLED = _bool("CONTROL_DECISION_SHADOW_ENABLED", True)
CONTROL_DECISION_AUTHORITY_ENABLED = _bool(
    # The authority path has a reversible environment escape hatch, but it is
    # now the product path.  Keeping the default on prevents desktop launches
    # from silently testing the legacy dispatcher while the real-machine
    # journey tests the canonical decision layer.
    "CONTROL_DECISION_AUTHORITY_ENABLED", True
)
# Overall deadline for the authority callback, including bounded per-candidate
# evidence and its single protocol retry. The query client's own timeout is a
# lower-level transport bound; this deadline guarantees dispatch recovery.
CONTROL_DECISION_AUTHORITY_TIMEOUT_S = _float(
    "CONTROL_DECISION_AUTHORITY_TIMEOUT_S", 30.0
)
CONTROL_DECISION_PROJECT_LIMIT = _int("CONTROL_DECISION_PROJECT_LIMIT", 200)
CONTROL_DECISION_WORK_ITEM_LIMIT = _int("CONTROL_DECISION_WORK_ITEM_LIMIT", 200)
CONTROL_DECISION_EXHAUSTIVE_CANDIDATE_LIMIT = _int(
    "CONTROL_DECISION_EXHAUSTIVE_CANDIDATE_LIMIT", 64
)
CONTROL_DECISION_MAX_TOKENS = _int("CONTROL_DECISION_MAX_TOKENS", 900)
CONTROL_DECISION_TIMEOUT_S = _int("CONTROL_DECISION_TIMEOUT_S", 45)

# Exact-clause expansion for one proposal-gated turn that contains several
# independently actionable clauses. The production resolver preserves A for
# zero/one clause and uses the ordered B plan only for genuine multi-operation
# turns. The authority flag is the reversible product switch; the shadow flag
# retains an independent telemetry-only arm when authority is disabled.
COMPOUND_CONTROL_AUTHORITY_ENABLED = _bool(
    "COMPOUND_CONTROL_AUTHORITY_ENABLED", True
)
COMPOUND_CONTROL_SHADOW_ENABLED = _bool(
    "COMPOUND_CONTROL_SHADOW_ENABLED", False
)

# AUIP action existence has a source-local decision axis because an AppSession
# is neither Provider Work nor a Project/WorkItem.  When enabled, the role
# prompt no longer carries a duplicate AUIP tag contract: the role speaks
# naturally while this bounded pass selects the requested transition in
# parallel.  The role-routing matrix and assembled visible-runtime journey
# proved the no-Work launch, real AppSession attach, mode change, and leave
# boundaries without delaying first speech.  It is therefore the default
# authority; setting the flag false restores the legacy inline-role proposal
# for bounded rollback.
AUIP_CONTROL_DECISION_ENABLED = _bool("AUIP_CONTROL_DECISION_ENABLED", True)

# Resolve the entity of an already-proposed Project focus against complete
# host catalogs.  Genuine ambiguity becomes a one-shot Slice selection before
# any destination mutation or Provider start; it does not make the experimental
# ControlDecision shadow authoritative.
REFERENCE_CLARIFICATION_ENABLED = _bool("REFERENCE_CLARIFICATION_ENABLED", True)
# Task lookup: retrieval replaces injection as the way the main chat knows
# about past work (task_lookup_work_order.md). The host resolves which task
# the user means BEFORE the model speaks -- an exact-filename index first, a
# literal-overlap prefilter plus one side-channel pick only when that misses
# -- because asking the model to notice a task is absent from its list fails
# 3 times in 9 by confidently answering about the wrong task. Also gives
# intent="report" the answering half it never had: facts from the ledger,
# spoken through a second pass on the base prompt. One switch by design
# (rule R7).
#
# On by default since 2026-08-02, when every acceptance item in the work
# order's section 7 passed, including on a real machine: four status questions
# resolved to the task actually asked about and were answered from the ledger
# rather than the provider's self-report, and an amendment bound back to its
# task through the index. In the shipping configuration an ordinary turn pays
# nothing for this -- the pre-turn pass has no consumer while roster candidate
# rows are off, so it does not run -- and the index is only queried when a tag
# carrying a filename arrives. The switch itself retires once the [TASK-LOOKUP]
# counters have enough real usage to say whether the second rung earns its
# second, which is the one thing no test could produce.
TASK_LOOKUP_ENABLED = _bool("TASK_LOOKUP_ENABLED", True)
# P1 worktree isolation: when enabled, the Host allocates an isolated Git
# worktree for every new Project write WorkItem. Providers only consume the
# resulting cwd and never own the workspace identity.
WORK_WORKTREE_ISOLATION = _bool("WORK_WORKTREE_ISOLATION", False)
_LOCAL_APP_STATE = Path(os.getenv("LOCALAPPDATA") or (Path.home() / ".amadeus"))
_DEFAULT_WORKTREE_ROOT = (
    _LOCAL_APP_STATE / "Amadeus" / "worktrees"
    if os.getenv("LOCALAPPDATA")
    else _LOCAL_APP_STATE / "worktrees"
)
WORK_WORKTREE_ROOT = (
    _str("WORK_WORKTREE_ROOT", str(_DEFAULT_WORKTREE_ROOT)).strip()
    or str(_DEFAULT_WORKTREE_ROOT)
)
WORK_WORKTREE_ENSURE_TIMEOUT_S = _float("WORK_WORKTREE_ENSURE_TIMEOUT_S", 30.0)
# Where work that belongs to no known project goes. Every such task gets its own
# git repository under this root, so one-off creation never writes into a real
# project. See docs/work_destination_work_order.md.
WORK_SCRATCH_ROOT = _str("WORK_SCRATCH_ROOT", str(_ROOT / "runtime" / "scratch"))
WORK_NARRATION_MIN_INTERVAL_S = _float("WORK_NARRATION_MIN_INTERVAL_S", 20.0)
WORK_NARRATION_DIAGNOSTIC_FIRST_N = _int("WORK_NARRATION_DIAGNOSTIC_FIRST_N", 5)
WORK_NARRATION_DIAGNOSTIC_EVERY_N = _int("WORK_NARRATION_DIAGNOSTIC_EVERY_N", 25)
AUIP_NARRATION_ENABLED = _bool("AUIP_NARRATION_ENABLED", True)
AUIP_NARRATION_NORMAL_BEAT_STRIDE = _int("AUIP_NARRATION_NORMAL_BEAT_STRIDE", 3)
# ``structured`` keeps Host admission/cadence but combines semantic selection
# and role rendering into one fact-id-bound presentation call. ``split`` is the
# reversible Observer -> Narrator baseline.
AUIP_PRESENTATION_MODE = _str("AUIP_PRESENTATION_MODE", "structured").strip().lower()
# AppSession-local dialogue mode. ``b2`` is the promoted Alpha default and
# enables the candidate-locked, receipt-before-delivery foreground action
# handoff. ``off`` preserves the legacy split path and ``a1`` changes memory
# placement only; both remain explicit rollback modes.
AUIP_APPSESSION_ROLE_BRANCH_MODE = _str(
    "AUIP_APPSESSION_ROLE_BRANCH_MODE", "b2"
).strip().lower()
if AUIP_APPSESSION_ROLE_BRANCH_MODE not in {"off", "a1", "b2"}:
    raise ValueError("AUIP_APPSESSION_ROLE_BRANCH_MODE must be off, a1, or b2")
# Reversible extension for action families whose payload cannot be enumerated
# into B2 candidates. ``candidate`` keeps the same receipt-held role owner but
# lets that one call fill only a manifest-declared open action schema. Reactor
# and reactive-defense attached Journeys passed the Alpha promotion gate; off
# remains the rollback to the former split Participant path.
AUIP_B2_OPEN_PAYLOAD_MODE = _str(
    "AUIP_B2_OPEN_PAYLOAD_MODE", "candidate"
).strip().lower()
if AUIP_B2_OPEN_PAYLOAD_MODE not in {"off", "candidate"}:
    raise ValueError("AUIP_B2_OPEN_PAYLOAD_MODE must be off or candidate")
AUIP_NARRATION_PROVIDER = _str("AUIP_NARRATION_PROVIDER", "")
AUIP_NARRATION_MODEL = _str("AUIP_NARRATION_MODEL", "")
WORK_OBSERVER_PROVIDER = _str("WORK_OBSERVER_PROVIDER", "")
WORK_OBSERVER_MODEL = _str("WORK_OBSERVER_MODEL", "")
AUIP_NARRATION_TIMEOUT_S = _float("AUIP_NARRATION_TIMEOUT_S", 12.0)
# Participant proposal and silent main-role authorization are decision-quality
# lanes, not narration. They reuse the typed tool transport but may select a
# stronger model profile without making routine commentary equally expensive.
AUIP_ACTION_PROVIDER = _str("AUIP_ACTION_PROVIDER", "")
AUIP_ACTION_MODEL = _str("AUIP_ACTION_MODEL", "")
AUIP_ACTION_REASONING_EFFORT = _str("AUIP_ACTION_REASONING_EFFORT", "none")
AUIP_ACTION_SERVICE_TIER = _str("AUIP_ACTION_SERVICE_TIER", "auto").strip().lower()
if AUIP_ACTION_SERVICE_TIER not in {"auto", "default", "fast", "priority"}:
    raise ValueError(
        "AUIP_ACTION_SERVICE_TIER must be auto, default, fast, or priority"
    )
AUIP_ACTION_TIMEOUT_S = _float("AUIP_ACTION_TIMEOUT_S", 8.0)
# Material Provider events may arrive in bursts.  The live event channel still
# carries every event; this only coalesces the much heavier Work projection.
WORK_PROVIDER_SNAPSHOT_MIN_INTERVAL_S = _float(
    "WORK_PROVIDER_SNAPSHOT_MIN_INTERVAL_S", 1.0
)
# A stuck chat/TTS busy flag must not erase a business-terminal report.  The
# Observer waits this long for the shared voice lane, then publishes the same
# truthful report as text and leaves the voice channel alone.
WORK_TERMINAL_NARRATION_MAX_WAIT_S = _float(
    "WORK_TERMINAL_NARRATION_MAX_WAIT_S", 20.0
)

# Wake word settings. SenseVoice is intended to be the lightweight always-on
# recognizer; Qwen-ASR remains the lazy-loaded full recognizer.
WAKE_ENABLED = _bool("WAKE_ENABLED", False)
WAKE_ASR_BACKEND = _str("WAKE_ASR_BACKEND", "sense_voice")
WAKE_PHRASES = _str(
    "WAKE_PHRASES",
    "hi amadeus,hey amadeus,hello amadeus,high amadeus,"
    "hi amadues,hey amadues,hello amadues,high amadues,"
    "hi amadius,hey amadius,hello amadius,"
    "hi i'm as,hi im as,hi ims,hi i'ms,hi i am as,"
    "嗨阿玛迪斯,嘿阿玛迪斯,你好阿玛迪斯,"
    "嗨阿马迪斯,嘿阿马迪斯,你好阿马迪斯,"
    "ハイアマデウス,ヘイアマデウス,アマデウス",
)
WAKE_MATCH_THRESHOLD = _float("WAKE_MATCH_THRESHOLD", 0.10)
WAKE_AUTO_START_WITH_WALLPAPER = _bool("WAKE_AUTO_START_WITH_WALLPAPER", True)
WAKE_AUTO_SEND_TO_CHAT = _bool("WAKE_AUTO_SEND_TO_CHAT", True)
WAKE_AWAKE_SECONDS = _float("WAKE_AWAKE_SECONDS", 60.0)
WAKE_BRIDGE_MAX_SECONDS = _float("WAKE_BRIDGE_MAX_SECONDS", 45.0)
WAKE_BRIDGE_AUTO_SEND = _bool("WAKE_BRIDGE_AUTO_SEND", False)
WAKE_VAD_THRESHOLD = _float("WAKE_VAD_THRESHOLD", 0.45)
WAKE_MIN_SEGMENT_RMS = _float("WAKE_MIN_SEGMENT_RMS", 0.003)
WAKE_SENSEVOICE_LANGUAGES = _str("WAKE_SENSEVOICE_LANGUAGES", "en")
SENSEVOICE_LANGUAGE = _str("SENSEVOICE_LANGUAGE", "en")
SENSEVOICE_MODEL_PATH = _str("SENSEVOICE_MODEL_PATH", "")
WAKE_TEMPLATE_CACHE_ENABLED = _bool("WAKE_TEMPLATE_CACHE_ENABLED", True)
WAKE_TEMPLATE_CACHE_DIR = _str("WAKE_TEMPLATE_CACHE_DIR", str(_ROOT / "runtime" / "wake_templates"))
WAKE_TEMPLATE_CACHE_THRESHOLD = _float("WAKE_TEMPLATE_CACHE_THRESHOLD", 0.68)
WAKE_TEMPLATE_CACHE_LEARN_THRESHOLD = _float("WAKE_TEMPLATE_CACHE_LEARN_THRESHOLD", 0.80)
WAKE_TEMPLATE_CACHE_MAX_PER_DEVICE = _int("WAKE_TEMPLATE_CACHE_MAX_PER_DEVICE", 8)
WAKE_TEMPLATE_CACHE_MIN_MS = _float("WAKE_TEMPLATE_CACHE_MIN_MS", 500.0)
WAKE_TEMPLATE_CACHE_MAX_MS = _float("WAKE_TEMPLATE_CACHE_MAX_MS", 2600.0)
WAKE_ENERGY_FALLBACK = _bool("WAKE_ENERGY_FALLBACK", False)
WAKE_ENERGY_START_RMS = _float("WAKE_ENERGY_START_RMS", 0.020)
WAKE_ENERGY_END_RMS = _float("WAKE_ENERGY_END_RMS", 0.010)
WAKE_DEBUG_AUDIO = _bool("WAKE_DEBUG_AUDIO", False)

# ===========================================================================
# Realtime acoustic echo cancellation
# ===========================================================================
AEC_REALTIME_ENABLED = _bool("AEC_REALTIME_ENABLED", False)
AEC_REALTIME_DELAY_MS = _float("AEC_REALTIME_DELAY_MS", 280.0)
# 初始猜测值，后续按真机实测调整；显式 AEC_REALTIME_DELAY_MS 会覆盖这些分类默认值。
AEC_DELAY_MS_BLUETOOTH = _float("AEC_DELAY_MS_BLUETOOTH", 220.0)
AEC_DELAY_MS_INTERNAL = _float("AEC_DELAY_MS_INTERNAL", 80.0)
AEC_DELAY_MS_USB = _float("AEC_DELAY_MS_USB", 120.0)
AEC_REALTIME_ENABLE_NS = _bool("AEC_REALTIME_ENABLE_NS", False)
AEC_REALTIME_ENABLE_AGC = _bool("AEC_REALTIME_ENABLE_AGC", False)
AEC_REALTIME_BARGE_IN = _bool("AEC_REALTIME_BARGE_IN", False)
AEC_REALTIME_DEBUG = _bool("AEC_REALTIME_DEBUG", False)
ASR_ECHO_GUARD_ENABLED = _bool("ASR_ECHO_GUARD_ENABLED", True)
ASR_ECHO_GUARD_CORR_THRESHOLD = _float("ASR_ECHO_GUARD_CORR_THRESHOLD", 0.74)
ASR_ECHO_GUARD_RESIDUAL_CORR_THRESHOLD = _float("ASR_ECHO_GUARD_RESIDUAL_CORR_THRESHOLD", 0.58)
ASR_ECHO_GUARD_RESIDUAL_RATIO_THRESHOLD = _float("ASR_ECHO_GUARD_RESIDUAL_RATIO_THRESHOLD", 0.80)
ASR_ECHO_GUARD_MIN_REF_RMS = _float("ASR_ECHO_GUARD_MIN_REF_RMS", 0.004)
ASR_ECHO_GUARD_REFERENCE_PAD_MS = _float("ASR_ECHO_GUARD_REFERENCE_PAD_MS", 900.0)
ASR_ECHO_GUARD_BARGE_IN_RAW_CORR_THRESHOLD = _float("ASR_ECHO_GUARD_BARGE_IN_RAW_CORR_THRESHOLD", 0.82)
ASR_ECHO_GUARD_BARGE_IN_RESIDUAL_CORR_THRESHOLD = _float("ASR_ECHO_GUARD_BARGE_IN_RESIDUAL_CORR_THRESHOLD", 0.50)
ASR_ECHO_GUARD_BARGE_IN_RATIO_THRESHOLD = _float("ASR_ECHO_GUARD_BARGE_IN_RATIO_THRESHOLD", 0.90)
ASR_ECHO_GUARD_BARGE_IN_STRONG_RAW_CORR_THRESHOLD = _float("ASR_ECHO_GUARD_BARGE_IN_STRONG_RAW_CORR_THRESHOLD", 0.92)
BARGE_IN_VAD_THRESHOLD = _float("BARGE_IN_VAD_THRESHOLD", 0.55)
BARGE_IN_MIN_RMS = _float("BARGE_IN_MIN_RMS", 0.012)
BARGE_IN_START_DELAY_MS = _float("BARGE_IN_START_DELAY_MS", 350.0)
BARGE_IN_ECHO_CONFIRM_MS = _float("BARGE_IN_ECHO_CONFIRM_MS", 96.0)

# ===========================================================================
# 麦克风选择
# ===========================================================================
# 优先匹配的设备名称关键词（部分匹配，不区分大小写），留空则纯靠 RMS 竞争
MICROPHONE_PREFERRED_NAME = _str("MICROPHONE_PREFERRED_NAME")
MICROPHONE_FALLBACK_DEVICE_INDEX = _int("MICROPHONE_FALLBACK_DEVICE_INDEX", -1)
MICROPHONE_FALLBACK_NAME = _str("MICROPHONE_FALLBACK_NAME", "")
# 直接指定设备索引（-1 = 不强制，使用自动选择）
MICROPHONE_DEVICE_INDEX   = _int("MICROPHONE_DEVICE_INDEX", -1)

# ===========================================================================
# OpenClaw
# ===========================================================================
OPENCLAW_BASE_URL    = _str("OPENCLAW_BASE_URL",      "http://127.0.0.1:18789")
OPENCLAW_TOKEN       = _str("OPENCLAW_GATEWAY_TOKEN")
OPENCLAW_PROJECT_DIR = _str("OPENCLAW_PROJECT_DIR")   # Node.js 项目根目录（本机路径）
