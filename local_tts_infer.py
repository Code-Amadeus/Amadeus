import os
import sys
import time
import threading
import torch
import soundfile as sf
import logging
import tempfile
import traceback
import numpy as np
from contextlib import nullcontext
from pathlib import Path
import string
from string import punctuation

from tts.optional_ap_bwe import APBWEUnavailable, create_ap_bwe
from tts.semantic_stability import (
    SemanticGenerationError,
    assess_semantic_candidate,
)


def _iter_segment_text_lang(text: str):
    """Yield (segment_text, segment_lang) from both old dict and v3 tuple formats."""
    for item in LangSegmenter.getTexts(text):
        if isinstance(item, dict):
            yield item.get("text", ""), item.get("lang", "other")
        elif isinstance(item, tuple) and len(item) >= 2:
            yield item[0], item[1]
        else:
            yield str(item), "other"

# TTS 语言配置（通过环境变量 TTS_OUTPUT_LANGUAGE 切换，默认日文）
try:
    from config.settings import TTS_OUTPUT_LANGUAGE as _TTS_OUTPUT_LANGUAGE
    from config.settings import TTS_REF_AUDIO_JA as _TTS_REF_AUDIO_JA
    from config.settings import TTS_REF_AUDIO_EN as _TTS_REF_AUDIO_EN
    from config.settings import TTS_REF_TEXT_JA as _TTS_REF_TEXT_JA
    from config.settings import TTS_REF_TEXT_EN as _TTS_REF_TEXT_EN
except ImportError:
    _TTS_OUTPUT_LANGUAGE = "日文"
    _TTS_REF_AUDIO_JA = "./assets/audio/reference/kurisu_reference.wav"
    _TTS_REF_AUDIO_EN = "./assets/audio/reference/english_recording.wav"
    _TTS_REF_TEXT_JA = "そうやって全部私に頼るのね……まったく"
    _TTS_REF_TEXT_EN = ""

def _default_lang_code() -> str:
    """返回当前语言对应的 GPT-SoVITS 内部语言代码（用于 fallback）。"""
    return "en" if _TTS_OUTPUT_LANGUAGE == "英文" else "ja"

def _default_ref_free_prompt() -> str:
    """v3 ref_free 兜底 prompt 文本。"""
    if _TTS_OUTPUT_LANGUAGE == "英文":
        return _TTS_REF_TEXT_EN or "I see, let me think about that."
    return _TTS_REF_TEXT_JA or "そうやって全部私に頼るのね……まったく"


# 设置日志记录
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('tts_inference')

# 获取当前项目根目录
root_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, root_dir)
sys.path.insert(0, os.path.join(root_dir, "GPT_SoVITS"))


# 导入必要的模块
try:
    import librosa
    import re
    from tools.i18n import I18nAuto
    from GPT_SoVITS.text.LangSegmenter import LangSegmenter
    from GPT_SoVITS.text import cleaned_text_to_sequence
    from GPT_SoVITS.text.cleaner import clean_text
    from GPT_SoVITS.module.mel_processing import spectrogram_torch, mel_spectrogram_torch
    from GPT_SoVITS.text import chinese
    from GPT_SoVITS.feature_extractor import cnhubert
    from GPT_SoVITS.TTS_infer_pack.TTS import TTS
    from GPT_SoVITS.AR.models.t2s_lightning_module import Text2SemanticLightningModule

    """加载SoVITS模型"""
    from GPT_SoVITS.module.models import SynthesizerTrn, SynthesizerTrnV3
    from GPT_SoVITS.process_ckpt import load_sovits_new
    from peft import LoraConfig, get_peft_model

    from GPT_SoVITS.text.LangSegmenter import LangSegmenter
    from GPT_SoVITS.text import cleaned_text_to_sequence
    from GPT_SoVITS.text.cleaner import clean_text

    from GPT_SoVITS.BigVGAN import bigvgan

    from GPT_SoVITS.text import chinese


except ImportError as e:
    logger.error(f"module import failed: {str(e)}")
    raise


class TTSInferencer:
    def __init__(self,
                 device="cuda",
                 gpt_path=None,
                 sovits_path=None,
                 bert_path=None,
                 cnhubert_path=None,
                 language="Auto"):
        """
        初始化TTS推理器

        Args:
            device: 推理设备，默认为"cuda"
            gpt_path: GPT模型路径，如果为None则使用默认路径
            sovits_path: SoVITS模型路径，如果为None则使用默认路径
            bert_path: BERT模型路径，如果为None则使用默认路径
            cnhubert_path: CNHuBERT模型路径，如果为None则使用默认路径
            language: 默认语言，可选"Auto"、"中文"、"英文"、"日文"等
        """
        try:
            # 确定模型路径
            base_dir = root_dir
            self.device = device
            device_name = str(device).lower()
            self._is_cuda = torch.cuda.is_available() and device_name.startswith("cuda")
            if device_name.startswith("cuda") and not self._is_cuda:
                raise RuntimeError(
                    f"TTS device {device!r} requires CUDA, but this PyTorch build has no usable CUDA device"
                )
            if device_name.startswith("mps") and not (
                hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
            ):
                raise RuntimeError(
                    f"TTS device {device!r} requires an available PyTorch MPS backend"
                )
            self.is_half = self._is_cuda

            # CUDA device index — used to set the current CUDA device before custom
            # CUDA kernel calls (BigVGAN's fused anti-alias activation), which rely on
            # at::cuda::getCurrentCUDAStream() internally.  Without explicitly setting
            # the device, that function defaults to device 0 even when all tensors live
            # on a different device, causing CUDNN_STATUS_MAPPING_ERROR.
            try:
                self._tts_device_idx = int(device_name.split(":")[-1])
            except (ValueError, IndexError):
                self._tts_device_idx = 0

            # 默认模型路径
            default_gpt_path = os.path.join(base_dir, "assets/models/gpt-sovits/weights/gpt/v3", "xxx-e15.ckpt")
            default_sovits_path = os.path.join(base_dir, "assets/models/gpt-sovits/weights/sovits/v3", "xxx_e2_s174_l32.pth")
            default_sovits_pretrain_path = os.path.join(base_dir, "assets", "models", "gpt-sovits", "pretrained", "s2Gv3.pth")
            default_bert_path = os.path.join(base_dir, "assets", "models", "gpt-sovits", "pretrained",
                                             "chinese-roberta-wwm-ext-large")
            default_cnhubert_path = os.path.join(base_dir, "assets", "models", "gpt-sovits", "pretrained", "chinese-hubert-base")

            # 使用传入参数或默认路径
            self.gpt_path = gpt_path or default_gpt_path
            self.sovits_path = sovits_path or default_sovits_path
            self.bert_path = bert_path or default_bert_path
            self.cnhubert_path = cnhubert_path or default_cnhubert_path
            self.sovits_pretrain_path = default_sovits_pretrain_path

            # 检查必要文件是否存在
            for path, desc in [
                (self.gpt_path, "GPT权重"),
                (self.sovits_path, "SoVITS权重"),
                (self.sovits_pretrain_path, "SoVITS pretrained weights"),
                (self.bert_path, "BERT model"),
                (self.cnhubert_path, "CNHuBERT model")
            ]:
                if not os.path.exists(path):
                    logger.warning(f"Required file is missing: {path} ({desc}); please check the configured path")

            # 初始化国际化
            self.i18n = I18nAuto(language=language)

            # 初始化语言字典
            self._init_language_dict()

            # 初始化BERT和SSL模型
            self._init_bert_model()
            self._init_ssl_model()

            # 加载GPT和SoVITS模型
            self._load_models()

            logger.info("TTS inferencer initialized")

            # 会话级缓存：按(ref_audio_path, prompt_text, prompt_language_code, model_version, is_half)键控
            self._session_cache = {}
            self._sovits_decode_lock = threading.Lock()
            # 仅在需要做性能剖析时开启；默认关闭以避免流式首句每块都强制同步 GPU。
            self._stream_sync_timing_enabled = os.environ.get("TTS_STREAM_SYNC_TIMING", "0") == "1"
            self._sovits_sync_timing_enabled = (
                os.environ.get("TTS_SOVITS_SYNC_TIMING", "0") == "1"
                or self._stream_sync_timing_enabled
            )
            self.t2s_stats = []
            if os.environ.get("TTS_RUNTIME_WARMUP", "1").strip().lower() not in {"0", "false", "off", "no"}:
                self._warmup_runtime()

        except Exception as e:
            logger.error(f"Failed to initialize TTS inferencer: {str(e)}")
            logger.error(traceback.format_exc())
            raise

    def _get_effective_max_sec(self, override_value):
        """计算当前推理应当使用的最大时长限制（秒）"""
        effective = float(self.max_sec)
        if override_value is not None:
            try:
                override = float(override_value)
                if override > 0:
                    effective = min(effective, override)
            except (TypeError, ValueError):
                pass
        return effective

    def _sync_t2s_timing(self):
        self._synchronize_device()

    def _sync_sovits_timing(self):
        self._synchronize_device()

    def _device_context(self):
        if self._is_cuda:
            return torch.cuda.device(self._tts_device_idx)
        return nullcontext()

    def _synchronize_device(self):
        if self._is_cuda:
            with torch.cuda.device(self._tts_device_idx):
                torch.cuda.synchronize()
        elif str(self.device).lower().startswith("mps"):
            torch.mps.synchronize()

    def _record_t2s_stat(
        self,
        text_item: str,
        tokens: int,
        elapsed_sec: float,
        attempts: int = 1,
    ):
        rate = float(tokens) / elapsed_sec if elapsed_sec > 0 else 0.0
        stat = {
            "text": text_item,
            "tokens": int(tokens),
            "elapsed_sec": float(elapsed_sec),
            "tokens_per_sec": rate,
            "semantic_attempts": int(attempts),
        }
        self.t2s_stats.append(stat)
        logger.info(
            f"[t2s] tokens={stat['tokens']} elapsed={stat['elapsed_sec']:.3f}s "
            f"speed={stat['tokens_per_sec']:.0f} it/s attempts={stat['semantic_attempts']}"
        )
        return stat

    @staticmethod
    def _semantic_guard_enabled() -> bool:
        raw = os.environ.get("TTS_SEMANTIC_GUARD", "1")
        return raw.strip().lower() not in {"0", "false", "off", "no"}

    def _infer_semantic_with_guard(
        self,
        *,
        text_item: str,
        target_phone_count: int,
        all_phoneme_ids,
        all_phoneme_len,
        prompt,
        bert,
        top_k,
        top_p,
        temperature,
        effective_max_sec: float,
        enable_cuda_graph: bool,
        enable_static_kv: bool,
    ):
        """Generate one admissible semantic candidate before SoVITS decoding.

        The normal path performs exactly one unchanged 1.35-penalty decode.  A
        structurally collapsed candidate is logged, discarded, and sampled one
        more time with a modestly stronger repetition penalty.  If both bounded
        attempts collapse, no vocoder work is performed and the caller fails
        closed instead of emitting a long corrupt waveform.
        """

        max_generation_tokens = int(self.hz * effective_max_sec)
        guard_enabled = self._semantic_guard_enabled()
        penalties = (1.35, 1.5) if guard_enabled else (1.35,)
        last_assessment = None

        for attempt, repetition_penalty in enumerate(penalties, start=1):
            prediction, idx = self.t2s_model.model.infer_panel(
                all_phoneme_ids,
                all_phoneme_len,
                prompt,
                bert,
                top_k=top_k,
                top_p=top_p,
                temperature=temperature,
                repetition_penalty=repetition_penalty,
                early_stop_num=max_generation_tokens,
                enable_cuda_graph=enable_cuda_graph,
                enable_static_kv=enable_static_kv,
            )
            generated_count = max(0, int(idx))
            if generated_count:
                candidate = prediction[:, -generated_count:]
            else:
                candidate = prediction[:, :0]

            if not guard_enabled:
                return candidate.unsqueeze(0), generated_count, attempt

            tokens = candidate.detach().reshape(-1).cpu().long().tolist()
            assessment = assess_semantic_candidate(
                tokens,
                target_phone_count=target_phone_count,
                max_generation_tokens=max_generation_tokens,
            )
            last_assessment = assessment
            if assessment.accepted:
                if attempt > 1:
                    logger.info(
                        "[T2S guard] retry recovered text=%r tokens=%d penalty=%.2f",
                        text_item[:80],
                        assessment.token_count,
                        repetition_penalty,
                    )
                return candidate.unsqueeze(0), generated_count, attempt

            logger.warning(
                "[T2S guard] rejected attempt=%d/%d text=%r reasons=%s "
                "tokens=%d budget=%d equal_run=%d periodic_run=%d top_ratio=%.3f",
                attempt,
                len(penalties),
                text_item[:80],
                ",".join(assessment.reasons),
                assessment.token_count,
                assessment.soft_token_budget,
                assessment.longest_equal_run,
                assessment.longest_periodic_run,
                assessment.top_token_ratio,
            )

        detail = "unknown"
        if last_assessment is not None:
            detail = ",".join(last_assessment.reasons) or "rejected"
        raise SemanticGenerationError(
            f"semantic generation collapsed after {len(penalties)} attempts: {detail}"
        )

    def _resolve_runtime_warmup_ref(self):
        lang = _default_lang_code()
        if lang == "en":
            ref_audio = _TTS_REF_AUDIO_EN
            ref_text = _TTS_REF_TEXT_EN
        else:
            ref_audio = _TTS_REF_AUDIO_JA
            ref_text = _TTS_REF_TEXT_JA
        ref_audio_path = Path(ref_audio)
        if not ref_audio_path.is_absolute():
            ref_audio_path = Path(root_dir) / ref_audio_path
        return str(ref_audio_path), ref_text, lang

    def _warmup_bigvgan_runtime(self):
        if self.model_version != "v3" or self.bigvgan_model is None:
            return
        raw_buckets = os.environ.get(
            "TTS_BIGVGAN_WARMUP_MELS",
            "64,70,73,80,90,100,106,110,120,128,178,256,298,512,598",
        )
        buckets = []
        for item in raw_buckets.split(","):
            try:
                value = int(item.strip())
            except ValueError:
                continue
            if value > 0 and value not in buckets:
                buckets.append(value)
        if not buckets:
            return

        try:
            param = next(self.bigvgan_model.parameters())
            dtype = param.dtype
            device = param.device
            num_mels = int(getattr(self.bigvgan_model, "h", {}).get("num_mels", 100))
            logger.info("[warmup] BigVGAN mel buckets: %s" % ",".join(str(v) for v in buckets))
            if device.type == "cuda":
                with torch.cuda.device(self._tts_device_idx):
                    with torch.inference_mode():
                        for mel_t in buckets:
                            dummy = torch.zeros((1, num_mels, mel_t), device=device, dtype=dtype)
                            _ = self.bigvgan_model(dummy)
                    torch.cuda.synchronize(device)
            else:
                with torch.inference_mode():
                    for mel_t in buckets:
                        dummy = torch.zeros((1, num_mels, mel_t), device=device, dtype=dtype)
                        _ = self.bigvgan_model(dummy)
        except Exception:
            logger.warning("[warmup] BigVGAN warmup failed; continuing without it")
            logger.warning(traceback.format_exc())

    def _warmup_session_cache_runtime(self):
        if os.environ.get("TTS_SESSION_WARMUP", "1").strip().lower() in {"0", "false", "off", "no"}:
            return
        ref_audio_path, ref_text, lang = self._resolve_runtime_warmup_ref()
        if not os.path.exists(ref_audio_path):
            logger.info(f"[warmup] skip session cache: ref audio not found: {ref_audio_path}")
            return
        try:
            logger.info(f"[warmup] building session cache: {ref_audio_path}")
            self._build_session_cache(ref_audio_path, ref_text, lang)
            if str(self.device).lower().startswith("cuda") and torch.cuda.is_available():
                self._sync_sovits_timing()
        except Exception:
            logger.warning("[warmup] session cache warmup failed; continuing without it")
            logger.warning(traceback.format_exc())

    def _warmup_runtime(self):
        if getattr(self, "_runtime_warmed", False):
            return
        self._runtime_warmed = True
        if self.model_version != "v3":
            return
        t0 = time.perf_counter()
        logger.info("[warmup] TTS runtime warmup start")
        self._warmup_session_cache_runtime()
        self._warmup_bigvgan_runtime()
        logger.info("[warmup] TTS runtime warmup done in %.2fs" % (time.perf_counter() - t0))

    def _init_language_dict(self):
        """初始化语言字典"""
        # 检测模型版本，根据版本确定语言字典
        self.model_version = self._detect_model_version()

        dict_language_v1 = {
            self.i18n("中文"): "all_zh",
            self.i18n("英文"): "en",
            self.i18n("日文"): "all_ja",
            self.i18n("中英混合"): "zh",
            self.i18n("日英混合"): "ja",
            self.i18n("多语种混合"): "auto",
        }

        dict_language_v2 = {
            self.i18n("中文"): "all_zh",
            self.i18n("英文"): "en",
            self.i18n("日文"): "all_ja",
            self.i18n("粤语"): "all_yue",
            self.i18n("韩文"): "all_ko",
            self.i18n("中英混合"): "zh",
            self.i18n("日英混合"): "ja",
            self.i18n("粤英混合"): "yue",
            self.i18n("韩英混合"): "ko",
            self.i18n("多语种混合"): "auto",
            self.i18n("多语种混合(粤语)"): "auto_yue",
        }

        self.dict_language = dict_language_v2 if self.model_version in ["v2", "v3"] else dict_language_v1
        self.splits = {"，", "。", "？", "！", ",", ".", "?", "!", "~", ":", "：", "—", "…"}

    def _detect_model_version(self):
        """检测模型版本"""
        # 简单版本检测，可根据文件名或其他特征判断
        if "v3" in self.sovits_path or "v3" in self.gpt_path:
            return "v3"
        elif "v2" in self.sovits_path or "v2" in self.gpt_path:
            return "v2"
        else:
            return "v1"

    def _init_bert_model(self):
        """初始化BERT模型"""
        from transformers import AutoModelForMaskedLM, AutoTokenizer

        logger.info(f"Loading BERT model: {self.bert_path}")
        self.tokenizer = AutoTokenizer.from_pretrained(self.bert_path)
        self.bert_model = AutoModelForMaskedLM.from_pretrained(self.bert_path)

        if self.is_half:
            self.bert_model = self.bert_model.half().to(self.device)
        else:
            self.bert_model = self.bert_model.to(self.device)

    def _init_ssl_model(self):
        """初始化SSL模型"""


        logger.info(f"Loading CNHubert model: {self.cnhubert_path}")
        cnhubert.cnhubert_base_path = self.cnhubert_path
        self.ssl_model = cnhubert.get_model()

        if self.is_half:
            self.ssl_model = self.ssl_model.half().to(self.device)
        else:
            self.ssl_model = self.ssl_model.to(self.device)

    def _load_models(self):
        """加载GPT和SoVITS模型"""
        # 加载GPT模型
        self._load_gpt_model()

        # 加载SoVITS模型
        self._load_sovits_model()

        # 如果是v3模型，还需加载BigVGAN
        if self.model_version == "v3":
            self._load_bigvgan_model()

    def _load_gpt_model(self):
        """加载GPT模型"""

        logger.info(f"Loading GPT model: {self.gpt_path}")
        dict_s1 = torch.load(self.gpt_path, map_location="cpu", weights_only=True)
        self.gpt_config = dict_s1["config"]
        self.hz = 50  # 默认值
        self.max_sec = self.gpt_config["data"]["max_sec"]

        self.t2s_model = Text2SemanticLightningModule(self.gpt_config, "****", is_train=False)
        self.t2s_model.load_state_dict(dict_s1["weight"])

        if self.is_half:
            self.t2s_model = self.t2s_model.half()
        self.t2s_model = self.t2s_model.to(self.device)
        self.t2s_model.eval()

        self._maybe_precapture_t2s_graph()

    def _maybe_precapture_t2s_graph(self):
        """根据环境变量可选地预捕获 T2S 阶段的 CUDA Graph"""
        try:
            enable_precapture = os.environ.get("ENABLE_CUDA_GRAPH_PRECAPTURE", "1") == "1"
            decoder = getattr(self.t2s_model, "model", None)
            if not enable_precapture or decoder is None:
                return
            # cuda_graph_enabled 由 ENABLE_CUDA_GRAPH 环境变量控制，
            # 但 force_graph=True 路径在运行时可以绕过该标志直接使用 graph。
            # 因此，只要 use_static_kv_cache=True（CUDA 可用即成立），就应该预捕获。
            can_use_graph = (getattr(decoder, "cuda_graph_enabled", False)
                             or getattr(decoder, "use_static_kv_cache", False))
            if not can_use_graph:
                return

            bucket_env = os.environ.get("CUDA_GRAPH_PRECAPTURE_BUCKETS", "")
            buckets = []
            if bucket_env.strip():
                for token in bucket_env.split(","):
                    token = token.strip()
                    if not token:
                        continue
                    try:
                        buckets.append(int(token))
                    except ValueError:
                        logger.warning(f"Skipping invalid CUDA Graph bucket config: {token}")
            available_buckets = list(getattr(decoder, "kv_cache_buckets", []) or [])
            if not available_buckets:
                # 没有定义桶就直接返回
                return

            # 首句实测高频落在 448/512；较长 prompt 需要 768 留出生成余量。
            # 无论环境变量是否显式指定，都优先补上这些常用桶，避免真实运行临时捕获。
            preferred_buckets = [448, 512, 768]
            preferred_buckets = [b for b in preferred_buckets if b in available_buckets]

            # 默认：预热所有可用的 KV Cache 桶，避免在首句推理时临时捕获造成额外延迟
            if not buckets:
                buckets = list(available_buckets)
            else:
                buckets = [b for b in buckets if b in available_buckets]

            merged_buckets = []
            for bucket in preferred_buckets + buckets:
                if bucket not in merged_buckets:
                    merged_buckets.append(bucket)
            buckets = merged_buckets

            logger.info(f"Starting CUDA Graph precapture; target buckets: {buckets}")
            results = decoder.precapture_cuda_graph(buckets)
            logger.info(f"CUDA Graph precapture results: {results}")
        except Exception as exc:
            logger.warning(f"CUDA Graph precapture failed: {exc}")

    def _load_sovits_model(self):


        # 加载SoVITS配置
        dict_s2 = load_sovits_new(self.sovits_path)
        self.hps = DictToAttrRecursive(dict_s2["config"])
        self.hps.model.semantic_frame_rate = "25hz"

        # 确定SoVITS版本
        if 'enc_p.text_embedding.weight' not in dict_s2['weight']:
            self.hps.model.version = "v2"  # v3model,v2symbols
        elif dict_s2['weight']['enc_p.text_embedding.weight'].shape[0] == 322:
            self.hps.model.version = "v1"
        else:
            self.hps.model.version = "v2"

        self.sovits_version = self.hps.model.version
        logger.info(f"SoVITS version: {self.sovits_version}, model version: {self.model_version}")

        # 根据模型版本创建模型
        if self.model_version != "v3":
            self.vq_model = SynthesizerTrn(
                self.hps.data.filter_length // 2 + 1,
                self.hps.train.segment_size // self.hps.data.hop_length,
                n_speakers=self.hps.data.n_speakers,
                **self.hps.model
            )
        else:
            self.vq_model = SynthesizerTrnV3(
                self.hps.data.filter_length // 2 + 1,
                self.hps.train.segment_size // self.hps.data.hop_length,
                n_speakers=self.hps.data.n_speakers,
                **self.hps.model
            )

        # 处理预训练模型
        if "pretrained" not in self.sovits_path:
            try:
                del self.vq_model.enc_q
            except:
                pass

        # 转换模型类型并加载到设备
        if self.is_half:
            self.vq_model = self.vq_model.half().to(self.device)
        else:
            self.vq_model = self.vq_model.to(self.device)

        self.vq_model.eval()

        # 检查是否是LoRA模型
        # 根据后缀或文件大小判断
        self.if_lora_v3 = False
        if self.model_version == "v3" and ".pth" in self.sovits_path.lower():
            file_size = os.path.getsize(self.sovits_path) / (1024 * 1024)  # MB
            if file_size < 100:  # 假设小于100MB的是LoRA权重
                self.if_lora_v3 = True
                logger.info(f"LoRA model detected: {self.sovits_path}")

        # 加载权重
        if not self.if_lora_v3:
            logger.info(f"Loading sovits_{self.model_version} model weights")
            self.vq_model.load_state_dict(dict_s2["weight"], strict=False)
        else:
            # 加载预训练模型和LoRA权重
            if not os.path.exists(self.sovits_pretrain_path):
                raise FileNotFoundError(f"SoVITS V3底模不存在: {self.sovits_pretrain_path}")

            logger.info(f"Loading sovits_v3 pretrained weights: {self.sovits_pretrain_path}")
            self.vq_model.load_state_dict(load_sovits_new(self.sovits_pretrain_path)["weight"], strict=False)

            # 应用LoRA
            lora_rank = dict_s2.get("lora_rank", 32)  # 默认值，应该从模型中读取
            lora_config = LoraConfig(
                target_modules=["to_k", "to_q", "to_v", "to_out.0"],
                r=lora_rank,
                lora_alpha=lora_rank,
                init_lora_weights=True,
            )
            logger.info(f"Applying LoRA config, rank={lora_rank}")
            self.vq_model.cfm = get_peft_model(self.vq_model.cfm, lora_config)

            # 加载LoRA权重
            self.vq_model.load_state_dict(dict_s2["weight"], strict=False)

            # 合并LoRA权重
            self.vq_model.cfm = self.vq_model.cfm.merge_and_unload()
            self.vq_model.eval()


    def _load_bigvgan_model(self):
        """加载BigVGAN模型（v3模型需要）"""
        if self.model_version != "v3":
            self.bigvgan_model = None
            return

        try:


            bigvgan_path = os.path.join(root_dir, "assets", "models", "gpt-sovits", "pretrained",
                                        "models--nvidia--bigvgan_v2_24khz_100band_256x")
            logger.info(f"Loading BigVGAN model: {bigvgan_path}")

            # use_cuda_kernel=True：编译 anti-aliased activation 的融合 CUDA kernel，
            # 减少内存读写，对 CUDA Graph 之后的 L2 cache miss 不敏感。
            # 优先检查预编译 .pyd 缓存（无需 cl.exe/nvcc），否则尝试现场编译。
            import pathlib as _pathlib
            # Use the device index that was resolved at __init__ time.
            _tts_device_idx = self._tts_device_idx
            _cache_id = os.environ.get("BIGVGAN_CACHE_ID", "").strip()
            if _cache_id:
                import re as _re
                _cache_suffix = _re.sub(r"[^A-Za-z0-9_.-]+", "_", _cache_id.lower()).strip("_") or "unknown"
            elif torch.cuda.is_available():
                try:
                    import re as _re
                    import sys as _sys
                    _props = torch.cuda.get_device_properties(_tts_device_idx)
                    _name = _re.sub(r"[^A-Za-z0-9_.-]+", "_", _props.name.lower()).strip("_") or "unknown"
                    _mem_gb = int(round(_props.total_memory / (1024 ** 3)))
                    _py_tag = f"py{_sys.version_info.major}{_sys.version_info.minor}"
                    _torch_tag = _re.sub(
                        r"[^A-Za-z0-9_.-]+",
                        "_",
                        f"torch{torch.__version__.split('+', 1)[0]}".lower(),
                    ).strip("_")
                    _cuda_tag = _re.sub(
                        r"[^A-Za-z0-9_.-]+",
                        "_",
                        f"cu{torch.version.cuda or 'cpu'}".lower(),
                    ).strip("_")
                    _cache_suffix = f"sm{_props.major}{_props.minor}_{_mem_gb}gb_{_name}_{_py_tag}_{_torch_tag}_{_cuda_tag}"
                except Exception:
                    _cache_suffix = f"device{_tts_device_idx}"
            else:
                _cache_suffix = f"device{_tts_device_idx}"
            _cuda_pyd = (
                _pathlib.Path(__file__).parent
                / "GPT_SoVITS/BigVGAN/alias_free_activation/cuda"
                / f"build_{_cache_suffix}"
                / "anti_alias_activation_cuda.pyd"
            )
            _use_cuda_kernel = False
            if self._is_cuda and _cuda_pyd.exists():
                _use_cuda_kernel = True
                logger.info("[BigVGAN] compiled CUDA kernel cache found; loading directly")
            elif self._is_cuda:
                try:
                    import subprocess as _sp
                    _nvcc = _sp.run(["nvcc", "--version"], capture_output=True, timeout=5)
                    if _nvcc.returncode == 0:
                        _use_cuda_kernel = True
                        logger.info("[BigVGAN] nvcc available; trying to compile the CUDA kernel")
                except Exception:
                    logger.info("[BigVGAN] nvcc unavailable; using the PyTorch implementation")
            else:
                logger.info("[BigVGAN] non-CUDA device; using the PyTorch implementation")

            kernel_override = os.environ.get("BIGVGAN_USE_CUDA_KERNEL", "").strip().lower()
            if kernel_override in {"0", "false", "off", "no"}:
                _use_cuda_kernel = False
                logger.info("[BigVGAN] BIGVGAN_USE_CUDA_KERNEL=0, forcing PyTorch path")
            elif kernel_override in {"1", "true", "on", "yes"}:
                if self._is_cuda:
                    _use_cuda_kernel = True
                    logger.info("[BigVGAN] BIGVGAN_USE_CUDA_KERNEL=1, forcing CUDA kernel path")
                else:
                    _use_cuda_kernel = False
                    logger.warning("[BigVGAN] CUDA kernels are unavailable on this TTS device; using PyTorch")

            # activation1d.py 在首次 import 时执行模块级 load.load()，
            # 若此时无设备上下文则 CUDA kernel 内部状态绑定到 cuda:0，
            # 之后模型移到 cuda:1 会触发 CUDNN_STATUS_MAPPING_ERROR。
            # 用 torch.cuda.device() 确保 kernel 初始化在正确设备上进行。
            try:
                with self._device_context():
                    self.bigvgan_model = bigvgan.BigVGAN.from_pretrained(bigvgan_path, use_cuda_kernel=_use_cuda_kernel)
            except Exception as _kernel_err:
                if _use_cuda_kernel:
                    logger.warning(f"[BigVGAN] CUDA kernel compilation failed; falling back to PyTorch implementation: {_kernel_err}")
                    with self._device_context():
                        self.bigvgan_model = bigvgan.BigVGAN.from_pretrained(bigvgan_path, use_cuda_kernel=False)
                else:
                    raise
            self.bigvgan_model.remove_weight_norm()
            self.bigvgan_model = self.bigvgan_model.eval()

            if self.is_half:
                self.bigvgan_model = self.bigvgan_model.half().to(self.device)
            else:
                self.bigvgan_model = self.bigvgan_model.to(self.device)
        except Exception as e:
            logger.error(f"Failed to load BigVGAN model: {str(e)}")
            logger.error(traceback.format_exc())
            raise

    def _build_session_cache(self, ref_audio_path: str, prompt_text: str, prompt_language_code: str):
        """构建会话级参考缓存。"""
        key = (ref_audio_path, prompt_text or "", prompt_language_code or "", self.model_version, self.is_half)
        if key in self._session_cache:
            return self._session_cache[key]

        cache_item = {}
        try:
            # 1) Prompt 相关（SSL → prompt）
            with torch.no_grad():
                wav16k, _ = librosa.load(ref_audio_path, sr=16000)
                wav16k = torch.from_numpy(wav16k)
                if self.is_half:
                    wav16k = wav16k.half().to(self.device)
                else:
                    wav16k = wav16k.float().to(self.device)
                # 追加极短静音，避免边界截断
                tail = torch.zeros(int(1600), dtype=wav16k.dtype, device=wav16k.device)
                wav16k = torch.cat([wav16k, tail])
                ssl_content = self.ssl_model.model(wav16k.unsqueeze(0))["last_hidden_state"].transpose(1, 2)
                codes = self.vq_model.extract_latent(ssl_content)
                prompt_semantic = codes[0, 0]
                cache_item["prompt"] = prompt_semantic.unsqueeze(0).to(self.device)

            # 2) Prompt 文本 phones/bert
            phones1, bert1, _ = self.get_phones_and_bert(prompt_text or "", prompt_language_code or _default_lang_code())
            cache_item["phones1"] = phones1
            cache_item["bert1"] = bert1

            # 3) 参考频谱（所有版本可用）
            refer = self.get_spepc(ref_audio_path).to(self.device)
            if self.is_half:
                refer = refer.half()
            else:
                refer = refer.float()
            cache_item["refer_spec"] = refer

            # 4) v3 额外缓存：ref_audio 24k 的 mel2（归一化后）
            if self.model_version == "v3":
                import torchaudio
                ref_audio, ref_sr = torchaudio.load(ref_audio_path)
                ref_audio = ref_audio.to(self.device)
                if self.is_half:
                    ref_audio = ref_audio.half()
                else:
                    ref_audio = ref_audio.float()
                if ref_audio.shape[0] == 2:
                    ref_audio = ref_audio.mean(0).unsqueeze(0)
                if ref_sr != 24000:
                    ref_audio = self._resample(ref_audio, ref_sr)

                mel_fn = lambda x: mel_spectrogram_torch(x, **{
                    "n_fft": 1024,
                    "win_size": 1024,
                    "hop_size": 256,
                    "num_mels": 100,
                    "sampling_rate": 24000,
                    "fmin": 0,
                    "fmax": None,
                    "center": False
                })
                spec_min, spec_max = -12, 2
                norm_spec = lambda x: (x - spec_min) / (spec_max - spec_min) * 2 - 1
                mel2 = mel_fn(ref_audio)
                mel2 = norm_spec(mel2)
                cache_item["mel2_norm"] = mel2

                # 5) v3 额外缓存：prompt 侧 decode_encp 结果，避免每句重复做同一份参考编码
                phoneme_ids0 = torch.LongTensor(phones1).to(self.device).unsqueeze(0)
                with torch.no_grad():
                    prompt_fea_ref, prompt_ge = self.vq_model.decode_encp(
                        cache_item["prompt"].unsqueeze(0),
                        phoneme_ids0,
                        refer,
                    )
                cache_item["prompt_fea_ref"] = prompt_fea_ref
                cache_item["prompt_ge"] = prompt_ge

            self._session_cache[key] = cache_item
            return cache_item
        except Exception:
            logger.warning("Failed to build session cache; falling back to per-sentence computation")
            logger.warning(traceback.format_exc())
            return {}

    def _clone_cached_value(self, value):
        """避免读取会话缓存后被后续推理路径原地复用/污染。"""
        if torch.is_tensor(value):
            return value.clone()
        if isinstance(value, list):
            return [self._clone_cached_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._clone_cached_value(item) for item in value)
        if isinstance(value, dict):
            return {k: self._clone_cached_value(v) for k, v in value.items()}
        return value

    def get_bert_feature(self, text, word2ph):
        """获取BERT特征"""
        with torch.no_grad():
            inputs = self.tokenizer(text, return_tensors="pt")
            for i in inputs:
                inputs[i] = inputs[i].to(self.device)
            res = self.bert_model(**inputs, output_hidden_states=True)
            res = torch.cat(res["hidden_states"][-3:-2], -1)[0].cpu()[1:-1]

        assert len(word2ph) == len(text)
        phone_level_feature = []
        for i in range(len(word2ph)):
            repeat_feature = res[i].repeat(word2ph[i], 1)
            phone_level_feature.append(repeat_feature)
        phone_level_feature = torch.cat(phone_level_feature, dim=0)

        return phone_level_feature.T

    def get_phones_and_bert(self, text, language, final=False):
        """获取音素和BERT特征"""
        if language in {"en", "all_zh", "all_ja", "all_ko", "all_yue"}:
            formattext = text
            while "  " in formattext:
                formattext = formattext.replace("  ", " ")

            # 处理中文中的英文字符
            if language == "all_zh" and re.search(r'[A-Za-z]', formattext):

                formattext = re.sub(r'[a-z]', lambda x: x.group(0).upper(), formattext)
                formattext = chinese.mix_text_normalize(formattext)
                return self.get_phones_and_bert(formattext, "zh")

            phones, word2ph, norm_text = self.clean_text_inf(formattext, language)

            if language == "all_zh":
                bert = self.get_bert_feature(norm_text, word2ph).to(self.device)
            else:
                bert = torch.zeros(
                    (1024, len(phones)),
                    dtype=torch.float16 if self.is_half else torch.float32,
                ).to(self.device)

        elif language in {"zh", "ja", "ko", "yue", "auto", "auto_yue"}:
            textlist = []
            langlist = []

            # 处理多语言混合
            if language == "auto":
                for seg_text, seg_lang in _iter_segment_text_lang(text):
                    langlist.append(seg_lang)
                    textlist.append(seg_text)
            elif language == "auto_yue":
                for seg_text, seg_lang in _iter_segment_text_lang(text):
                    if seg_lang == "zh":
                        seg_lang = "yue"
                    langlist.append(seg_lang)
                    textlist.append(seg_text)
            else:
                for seg_text, seg_lang in _iter_segment_text_lang(text):
                    if seg_lang == "en":
                        langlist.append(seg_lang)
                    else:
                        # 因无法区别中日韩文汉字,以用户输入为准
                        langlist.append(language)
                    textlist.append(seg_text)

            logger.debug(f"text segments: {textlist}")
            logger.debug(f"language segments: {langlist}")

            phones_list = []
            bert_list = []
            norm_text_list = []

            for i in range(len(textlist)):
                lang = langlist[i]
                phones, word2ph, norm_text = self.clean_text_inf(textlist[i], lang)
                bert = self.get_bert_inf(phones, word2ph, norm_text, lang)
                phones_list.append(phones)
                norm_text_list.append(norm_text)
                bert_list.append(bert)

            bert = torch.cat(bert_list, dim=1)
            phones = sum(phones_list, [])
            norm_text = ''.join(norm_text_list)

        # 处理过短的内容
        dtype = torch.float16 if self.is_half else torch.float32
        if not final and len(phones) < 6:
            return self.get_phones_and_bert("." + text, language, final=True)

        return phones, bert.to(dtype), norm_text

    def clean_text_inf(self, text, language):
        """清理文本并转换为音素"""
        language = language.replace("all_", "")
        phones, word2ph, norm_text = clean_text(text, language, self.sovits_version)
        phones = cleaned_text_to_sequence(phones, self.sovits_version)
        return phones, word2ph, norm_text

    def get_bert_inf(self, phones, word2ph, norm_text, language):
        """根据语言获取BERT特征"""
        language = language.replace("all_", "")
        if language == "zh":
            bert = self.get_bert_feature(norm_text, word2ph).to(self.device)
        else:
            bert = torch.zeros(
                (1024, len(phones)),
                dtype=torch.float16 if self.is_half else torch.float32,
            ).to(self.device)

        return bert

    def _audio_sr(self, audio, sr):
        """音频超分辨率处理"""
        try:

            sr_model = create_ap_bwe(self.device, DictToAttrRecursive)
            return sr_model(audio, sr)
        except (ImportError, APBWEUnavailable):
            logger.warning("audio super-resolution module not found; skipping super-resolution")
            return audio.cpu().detach().numpy(), sr
        except FileNotFoundError:
            logger.warning("audio super-resolution model parameters not found; skipping super-resolution")
            return audio.cpu().detach().numpy(), sr

    def get_spepc(self, filename):
        """获取频谱特征"""
        audio, sampling_rate = librosa.load(filename, sr=int(self.hps.data.sampling_rate))
        audio = torch.FloatTensor(audio)
        maxx = audio.abs().max()
        if maxx > 1:
            audio /= min(2, maxx)

        audio_norm = audio.unsqueeze(0)
        spec = spectrogram_torch(
            audio_norm,
            self.hps.data.filter_length,
            self.hps.data.sampling_rate,
            self.hps.data.hop_length,
            self.hps.data.win_length,
            center=False,
        )

        return spec

    def infer(self,
              text,
              ref_audio_path,
              prompt_text=None,
              text_language="日文",
              prompt_language="日文",
              how_to_cut="不切",
              top_k=20,
              top_p=0.6,
              temperature=0.6,
              speed=1.0,
              sample_steps=16,
              ref_free=False,
              pause_second=0.3,
              if_freeze=False,
              inp_refs=None,
              if_sr=False,
              enable_cuda_graph=False,
              enable_static_kv=True,
              max_sec_override=None):
        """
        执行TTS推理

        Args:
            text: 要合成的目标文本
            ref_audio_path: 参考音频路径
            prompt_text: 参考文本，如果为None则使用ref_free模式
            text_language: 目标文本的语言
            prompt_language: 参考文本的语言
            how_to_cut: 文本切分方式，可选"不切"、"凑四句一切"、"凑50字一切"、"按中文句号。切"、"按英文句号.切"、"按标点符号切"
            top_k, top_p, temperature: GPT采样参数
            speed: 语速控制
            sample_steps: v3模型的采样步数
            ref_free: 是否使用无参考模式
            pause_second: 句间停顿秒数
            if_freeze: 是否重用上次的缓存(防止随机性)
            inp_refs: 额外的参考音频列表(用于混合音色)
            if_sr: 是否使用音频超分辨率(仅v3模型支持)

        Returns:
            tuple: (采样率, 音频数据)
        """
        try:
            # 准备输入
            text = text.strip()
            if not text:
                raise ValueError("推理文本不能为空")

            logger.info(f"starting inference: '{text[:30]}...'")

            # 统一定义 v3 CFM 解码所需的反归一化函数，避免某些分支下未定义
            spec_min, spec_max = -12, 2
            denorm_spec = lambda x: (x + 1) / 2 * (spec_max - spec_min) + spec_min

            # 转换语言代码
            if text_language in self.dict_language:
                text_language_code = self.dict_language[text_language]
            else:
                text_language_code = _default_lang_code()
                logger.warning(f"unknown language: {text_language}; using default language: {_TTS_OUTPUT_LANGUAGE}")

            # 如果没有提供参考文本，则使用无参考模式
            if prompt_text is None or prompt_text.strip() == "":
                ref_free = True
                logger.info("no prompt text provided; using reference-free mode")
            else:
                prompt_text = prompt_text.strip()
                # 确保参考文本以标点符号结尾
                if prompt_text and prompt_text[-1] not in self.splits:
                    prompt_text += "。" if prompt_language != "英文" else "."

                if prompt_language in self.dict_language:
                    prompt_language_code = self.dict_language[prompt_language]
                else:
                    prompt_language_code = _default_lang_code()

                logger.info(f"prompt text: '{prompt_text}'")

            # v3模型不支持ref_free模式
            if self.model_version == "v3" and ref_free:
                logger.warning("v3 model does not support reference-free mode; forcing reference mode")
                ref_free = False

                # 如果没有参考文本，使用当前语言的默认文本
                if not prompt_text:
                    prompt_text = _default_ref_free_prompt()
                    prompt_language_code = "en" if _TTS_OUTPUT_LANGUAGE == "英文" else "all_ja"

            # 根据选择的切分方式处理文本
            logger.info(f"text segmentation mode: {how_to_cut}")
            if how_to_cut == "凑四句一切":
                text = cut1(text)
            elif how_to_cut == "凑50字一切":
                text = cut2(text)
            elif how_to_cut == "按中文句号。切":
                text = cut3(text)
            elif how_to_cut == "按英文句号.切":
                text = cut4(text)
            elif how_to_cut == "按标点符号切":
                text = cut5(text)

            # 按行切分
            while "\n\n" in text:
                text = text.replace("\n\n", "\n")
            texts = text.split("\n")
            texts = process_text(texts)

            # 初始化结果
            audio_outputs = []
            sr = self.hps.data.sampling_rate if self.model_version != "v3" else 24000

            # 创建句间停顿的静音
            zero_wav = torch.zeros(
                int(sr * pause_second),
                dtype=torch.float16 if self.is_half else torch.float32  # 根据is_half决定类型
            ).to(self.device)

            # 处理参考音频（会话级缓存优先）
            sess_lang = prompt_language_code if not ref_free else _default_lang_code()
            sess = self._build_session_cache(ref_audio_path, prompt_text, sess_lang)
            prompt = sess.get("prompt")

            # 获取参考音频的音素和BERT特征
            if not ref_free:
                if "phones1" in sess and "bert1" in sess:
                    phones1, bert1 = sess["phones1"], sess["bert1"]
                else:
                    phones1, bert1, norm_text1 = self.get_phones_and_bert(prompt_text, prompt_language_code)

            # 初始化缓存
            cache = {}

            effective_max_sec = self._get_effective_max_sec(max_sec_override)
            # 分句合成
            logger.info(f"split into {len(texts)} sentence(s) for synthesis")
            for i_text, text_item in enumerate(texts):
                # 跳过空句
                if len(text_item.strip()) == 0:
                    continue

                # 确保句子以标点符号结尾
                if text_item and text_item[-1] not in self.splits:
                    text_item += "。" if text_language != "英文" else "."

                logger.info(f"processing sentence {i_text + 1}: '{text_item}'")

                # 获取目标文本的音素和BERT特征
                phones2, bert2, norm_text2 = self.get_phones_and_bert(text_item, text_language_code)
                logger.info(f"normalized target text: {norm_text2}")

                # 合并音素和BERT特征
                if not ref_free:
                    bert = torch.cat([bert1, bert2], 1)
                    all_phoneme_ids = torch.LongTensor(phones1 + phones2).to(self.device).unsqueeze(0)
                else:
                    bert = bert2
                    all_phoneme_ids = torch.LongTensor(phones2).to(self.device).unsqueeze(0)

                bert = bert.to(self.device).unsqueeze(0)
                all_phoneme_len = torch.tensor([all_phoneme_ids.shape[-1]]).to(self.device)

                # 处理缓存
                if i_text in cache and if_freeze:
                    logger.info("using cached GPT output")
                    pred_semantic = cache[i_text]
                else:
                    # GPT推理
                    logger.info("running GPT inference...")
                    with torch.no_grad():
                        pred_semantic, idx, _semantic_attempts = self._infer_semantic_with_guard(
                            text_item=text_item,
                            target_phone_count=len(phones2),
                            all_phoneme_ids=all_phoneme_ids,
                            all_phoneme_len=all_phoneme_len,
                            prompt=None if ref_free else prompt,
                            bert=bert,
                            top_k=top_k,
                            top_p=top_p,
                            temperature=temperature,
                            effective_max_sec=effective_max_sec,
                            enable_cuda_graph=enable_cuda_graph,
                            enable_static_kv=enable_static_kv,
                        )
                        cache[i_text] = pred_semantic

                # SoVITS推理
                logger.info("running SoVITS decoding...")

                if self.model_version != "v3":
                    # v1/v2模型解码
                    # 处理多个参考音频
                    refers = []
                    if inp_refs:
                        for ref_path in inp_refs:
                            try:
                                ref_path = ref_path if isinstance(ref_path, str) else ref_path.name
                                # 根据is_half决定是否使用half
                                refer = self.get_spepc(ref_path).to(self.device)
                                if self.is_half:
                                    refer = refer.half()
                                else:
                                    refer = refer.float()
                                refers.append(refer)
                                logger.info(f"loading extra reference audio: {ref_path}")
                            except Exception as e:
                                logger.warning(f"failed to load extra reference audio: {e}")

                    # 如果没有额外参考音频，使用主参考音频
                    if len(refers) == 0:
                        refer = sess.get("refer_spec")
                        if refer is None:
                            refer = self.get_spepc(ref_audio_path).to(self.device)
                            refer = refer.half() if self.is_half else refer.float()
                        refers = [refer]

                    # 解码
                    audio = self.vq_model.decode(
                        pred_semantic,
                        torch.LongTensor(phones2).to(self.device).unsqueeze(0),
                        refers,
                        speed=speed
                    )[0][0]

                    # 防止爆音
                    max_audio = torch.abs(audio).max()
                    if max_audio > 1:
                        audio = audio / max_audio

                    # 添加到输出列表
                    audio_outputs.append(audio)
                    audio_outputs.append(zero_wav)  # 句间停顿

                else:
                    # v3模型解码
                    import torchaudio

                    # 根据is_half决定是否使用half
                    # 优先使用会话缓存的参考频谱
                    refer = sess.get("refer_spec")
                    if refer is None:
                        refer = self.get_spepc(ref_audio_path).to(self.device)
                        refer = refer.half() if self.is_half else refer.float()

                    phoneme_ids0 = torch.LongTensor(phones1).to(self.device).unsqueeze(0)
                    phoneme_ids1 = torch.LongTensor(phones2).to(self.device).unsqueeze(0)

                    # 提取参考音频特征（优先使用会话缓存）
                    fea_ref = self._clone_cached_value(sess.get("prompt_fea_ref"))
                    ge = self._clone_cached_value(sess.get("prompt_ge"))
                    if fea_ref is None or ge is None:
                        fea_ref, ge = self.vq_model.decode_encp(prompt.unsqueeze(0), phoneme_ids0, refer)

                    # 加载并处理参考音频
                    ref_audio, ref_sr = torchaudio.load(ref_audio_path)
                    ref_audio = ref_audio.to(self.device)
                    # 根据is_half决定是否使用half
                    if self.is_half:
                        ref_audio = ref_audio.half()
                    else:
                        ref_audio = ref_audio.float()

                    if ref_audio.shape[0] == 2:  # 转单声道
                        ref_audio = ref_audio.mean(0).unsqueeze(0)

                    # 重采样到24kHz
                    if ref_sr != 24000:
                        ref_audio = self._resample(ref_audio, ref_sr)

                    # 提取mel特征（优先使用会话缓存）
                    mel2 = sess.get("mel2_norm")
                    if mel2 is None:
                        mel_fn = lambda x: mel_spectrogram_torch(x, **{
                            "n_fft": 1024,
                            "win_size": 1024,
                            "hop_size": 256,
                            "num_mels": 100,
                            "sampling_rate": 24000,
                            "fmin": 0,
                            "fmax": None,
                            "center": False
                        })
                        spec_min, spec_max = -12, 2
                        norm_spec = lambda x: (x - spec_min) / (spec_max - spec_min) * 2 - 1
                        mel2 = mel_fn(ref_audio)
                        mel2 = norm_spec(mel2)

                    # 调整长度
                    T_min = min(mel2.shape[2], fea_ref.shape[2])
                    mel2 = mel2[:, :, :T_min]
                    fea_ref = fea_ref[:, :, :T_min]
                    if (T_min > 468):
                        mel2 = mel2[:, :, -468:]
                        fea_ref = fea_ref[:, :, -468:]
                        T_min = 468

                    # 设置块长度
                    chunk_len = 934 - T_min
                    # 根据is_half决定是否使用half
                    if self.is_half:
                        mel2 = mel2.half()
                    else:
                        mel2 = mel2.float()

                    # 解码目标特征
                    fea_todo, ge = self.vq_model.decode_encp(pred_semantic, phoneme_ids1, refer, ge, speed)

                    # 分块处理
                    cfm_resss = []
                    idx = 0
                    while True:
                        fea_todo_chunk = fea_todo[:, :, idx:idx + chunk_len]
                        if fea_todo_chunk.shape[-1] == 0:
                            break

                        idx += chunk_len
                        fea = torch.cat([fea_ref, fea_todo_chunk], 2).transpose(2, 1)

                        # CFM推理
                        cfm_res = self.vq_model.cfm.inference(
                            fea,
                            torch.LongTensor([fea.size(1)]).to(fea.device),
                            mel2,
                            sample_steps,
                            inference_cfg_rate=0
                        )

                        cfm_res = cfm_res[:, :, mel2.shape[2]:]
                        mel2 = cfm_res[:, :, -T_min:]
                        fea_ref = fea_todo_chunk[:, :, -T_min:]
                        cfm_resss.append(cfm_res)

                    # 合并结果
                    cmf_res = torch.cat(cfm_resss, 2)
                    cmf_res = denorm_spec(cmf_res)

                    # BigVGAN生成波形
                    # torch.cuda.device() ensures at::cuda::getCurrentCUDAStream()
                    # inside the fused CUDA kernel uses the correct device stream,
                    # preventing CUDNN_STATUS_MAPPING_ERROR on non-default GPUs.
                    with self._device_context():
                        with torch.inference_mode():
                            wav_gen = self.bigvgan_model(cmf_res)
                            audio = wav_gen[0][0]

                    # 防止爆音
                    max_audio = torch.abs(audio).max()
                    if max_audio > 1:
                        audio = audio / max_audio

                    # 添加到输出列表
                    audio_outputs.append(audio)
                    audio_outputs.append(zero_wav)  # 句间停顿

            # 合并所有音频片段
            if audio_outputs:
                final_audio = torch.cat(audio_outputs, 0)

                # 音频超分(仅v3模型支持)
                if if_sr and self.model_version == "v3":
                    try:
                        logger.info("running audio super-resolution...")
                        # 初始化超分模型（如果未初始化）
                        if not hasattr(self, 'sr_model') or self.sr_model is None:
                            self.sr_model = create_ap_bwe(self.device, DictToAttrRecursive)

                        # 进行音频超分
                        final_audio, sr = self.sr_model(final_audio.unsqueeze(0), sr)

                        # 再次防止爆音
                        max_audio = np.abs(final_audio).max()
                        if max_audio > 1:
                            final_audio = final_audio / max_audio
                    except Exception as e:
                        logger.warning(f"audio super-resolution failed: {e}")
                        logger.warning(traceback.format_exc())
                        final_audio = final_audio.cpu().detach().numpy()
                else:
                    final_audio = final_audio.cpu().detach().numpy()

                # 确保音频数据是float32类型（只针对numpy数组，不处理tensor）
                if isinstance(final_audio, np.ndarray):
                    if 'float16' in str(final_audio.dtype):
                        final_audio = final_audio.astype(np.float32)

                # 返回结果
                logger.info(f"inference completed, generated audio length: {len(final_audio) / sr:.2f}s")
                return sr, final_audio
            else:
                raise ValueError("未能生成有效音频")

        except Exception as e:
            logger.error(f"inference failed: {str(e)}")
            logger.error(traceback.format_exc())
            raise

    def infer_stream(self,
                     text,
                     ref_audio_path,
                     prompt_text=None,
                     text_language="日文",
                     prompt_language="日文",
                     how_to_cut="按标点符号切",  # 默认使用按标点符号切，更适合流式处理
                     top_k=20,
                     top_p=0.6,
                     temperature=0.6,
                     speed=1.0,
                     sample_steps=16,
                     ref_free=False,
                     pause_second=0.3,
                     if_freeze=False,
                     inp_refs=None,
                     if_sr=False,
                     enable_cuda_graph=False,
                     enable_static_kv=True,
                     chunk_size_seconds: float = None,
                     max_sec_override: float = None,
                     collect_t2s_stats: bool = False):
        """
        流式执行TTS推理，逐步返回音频块

        与infer函数相比，该函数是一个生成器，会逐块返回处理后的音频

        Args:
            text: 要合成的目标文本
            ref_audio_path: 参考音频路径
            prompt_text: 参考文本，如果为None则使用ref_free模式
            text_language: 目标文本的语言
            prompt_language: 参考文本的语言
            how_to_cut: 文本切分方式，可选"不切"、"凑四句一切"、"凑50字一切"、"按中文句号。切"、"按英文句号.切"、"按标点符号切"
            top_k, top_p, temperature: GPT采样参数
            speed: 语速控制
            sample_steps: v3模型的采样步数
            ref_free: 是否使用无参考模式
            pause_second: 句间停顿秒数
            if_freeze: 是否重用上次的缓存(防止随机性)
            inp_refs: 额外的参考音频列表(用于混合音色)
            if_sr: 是否使用音频超分辨率(仅v3模型支持)
            chunk_size_seconds: 若大于0，则按指定秒数对输出音频进行分块，提升首句流式体验

        Returns:
            生成器：每次生成 (采样率, 音频数据片段)
        """
        try:
            # 准备输入
            text = text.strip()
            if not text:
                raise ValueError("推理文本不能为空")

            logger.info(f"starting streaming inference: '{text[:30]}...'")

            # 统一定义 v3 CFM 解码所需的反归一化函数，避免某些分支下未定义
            spec_min, spec_max = -12, 2
            denorm_spec = lambda x: (x + 1) / 2 * (spec_max - spec_min) + spec_min

            # 转换语言代码
            if text_language in self.dict_language:
                text_language_code = self.dict_language[text_language]
            else:
                text_language_code = _default_lang_code()
                logger.warning(f"unknown language: {text_language}; using default language: {_TTS_OUTPUT_LANGUAGE}")

            # 如果没有提供参考文本，则使用无参考模式
            if prompt_text is None or prompt_text.strip() == "":
                ref_free = True
                logger.info("no prompt text provided; using reference-free mode")
            else:
                prompt_text = prompt_text.strip()
                # 确保参考文本以标点符号结尾
                if prompt_text and prompt_text[-1] not in self.splits:
                    prompt_text += "。" if prompt_language != "英文" else "."

                if prompt_language in self.dict_language:
                    prompt_language_code = self.dict_language[prompt_language]
                else:
                    prompt_language_code = _default_lang_code()

                logger.info(f"prompt text: '{prompt_text}'")

            # v3模型不支持ref_free模式
            if self.model_version == "v3" and ref_free:
                logger.warning("v3 model does not support reference-free mode; forcing reference mode")
                ref_free = False

                # 如果没有参考文本，使用当前语言的默认文本
                if not prompt_text:
                    prompt_text = _default_ref_free_prompt()
                    prompt_language_code = "en" if _TTS_OUTPUT_LANGUAGE == "英文" else "all_ja"

            # 根据选择的切分方式处理文本
            logger.info(f"text segmentation mode: {how_to_cut}")
            if how_to_cut == "凑四句一切":
                text = cut1(text)
            elif how_to_cut == "凑50字一切":
                text = cut2(text)
            elif how_to_cut == "按中文句号。切":
                text = cut3(text)
            elif how_to_cut == "按英文句号.切":
                text = cut4(text)
            elif how_to_cut == "按标点符号切":
                text = cut5(text)

            # 按行切分
            while "\n\n" in text:
                text = text.replace("\n\n", "\n")
            texts = text.split("\n")
            texts = process_text(texts)

            # 初始化结果
            sr = self.hps.data.sampling_rate if self.model_version != "v3" else 24000

            # 首先返回采样率
            yield sr, None, ""

            # 创建句间停顿的静音
            zero_wav = torch.zeros(
                int(sr * pause_second),
                dtype=torch.float16 if self.is_half else torch.float32  # 根据is_half决定类型
            ).to(self.device)

            chunk_samples = None
            if chunk_size_seconds is not None and chunk_size_seconds > 0:
                chunk_samples = max(1, int(sr * chunk_size_seconds))
                logger.info(f"chunked output enabled: {chunk_size_seconds:.2f}s -> {chunk_samples} samples")

            def _yield_audio_segments(audio_np: np.ndarray, text_payload: str):
                if chunk_samples is None or audio_np is None:
                    yield sr, audio_np, text_payload
                    return
                start = 0
                total_len = audio_np.shape[-1]
                first_chunk = True
                while start < total_len:
                    end = min(total_len, start + chunk_samples)
                    sub_chunk = audio_np[start:end]
                    if sub_chunk.size == 0:
                        break
                    yield sr, sub_chunk, text_payload if first_chunk else ""
                    first_chunk = False
                    start = end

            # 处理参考音频（会话级缓存优先）
            sess_lang = prompt_language_code if not ref_free else _default_lang_code()
            sess = self._build_session_cache(ref_audio_path, prompt_text, sess_lang)
            prompt = sess.get("prompt")

            # 获取参考音频的音素和BERT特征（会话级缓存优先）
            if not ref_free:
                if "phones1" in sess and "bert1" in sess:
                    phones1, bert1 = sess["phones1"], sess["bert1"]
                else:
                    phones1, bert1, norm_text1 = self.get_phones_and_bert(prompt_text, prompt_language_code)

            # 初始化缓存
            cache = {}

            effective_max_sec = self._get_effective_max_sec(max_sec_override)
            if max_sec_override is not None:
                logger.info(f"applying max duration limit: {effective_max_sec:.2f}s (base {self.max_sec:.2f}s)")
            # 分句合成
            logger.info(f"split into {len(texts)} sentence(s) for streaming synthesis")
            for i_text, text_item in enumerate(texts):
                # 跳过空句
                if len(text_item.strip()) == 0:
                    continue

                # 确保句子以标点符号结尾
                if text_item and text_item[-1] not in self.splits:
                    text_item += "。" if text_language != "英文" else "."

                logger.info(f"processing sentence {i_text + 1}: '{text_item}'")

                # 获取目标文本的音素和BERT特征
                phones2, bert2, norm_text2 = self.get_phones_and_bert(text_item, text_language_code)
                logger.info(f"normalized target text: {norm_text2}")

                # 合并音素和BERT特征
                if not ref_free:
                    bert = torch.cat([bert1, bert2], 1)
                    all_phoneme_ids = torch.LongTensor(phones1 + phones2).to(self.device).unsqueeze(0)
                else:
                    bert = bert2
                    all_phoneme_ids = torch.LongTensor(phones2).to(self.device).unsqueeze(0)

                bert = bert.to(self.device).unsqueeze(0)
                all_phoneme_len = torch.tensor([all_phoneme_ids.shape[-1]]).to(self.device)

                # 处理缓存
                if i_text in cache and if_freeze:
                    logger.info("using cached GPT output")
                    pred_semantic = cache[i_text]
                else:
                    # GPT推理
                    logger.info("running GPT inference...")
                    with torch.no_grad():
                        if collect_t2s_stats:
                            self._sync_t2s_timing()
                            t2s_start = time.perf_counter()
                        pred_semantic, idx, semantic_attempts = self._infer_semantic_with_guard(
                            text_item=text_item,
                            target_phone_count=len(phones2),
                            all_phoneme_ids=all_phoneme_ids,
                            all_phoneme_len=all_phoneme_len,
                            prompt=None if ref_free else prompt,
                            bert=bert,
                            top_k=top_k,
                            top_p=top_p,
                            temperature=temperature,
                            effective_max_sec=effective_max_sec,
                            enable_cuda_graph=enable_cuda_graph,
                            enable_static_kv=enable_static_kv,
                        )
                        if collect_t2s_stats:
                            self._sync_t2s_timing()
                            self._record_t2s_stat(
                                text_item,
                                idx,
                                time.perf_counter() - t2s_start,
                                attempts=semantic_attempts,
                            )
                        cache[i_text] = pred_semantic

                # SoVITS推理
                logger.info("running SoVITS decoding...")

                if self.model_version != "v3":
                    # v1/v2模型解码
                    # 处理多个参考音频
                    refers = []
                    if inp_refs:
                        for ref_path in inp_refs:
                            try:
                                ref_path = ref_path if isinstance(ref_path, str) else ref_path.name
                                # 根据is_half决定是否使用half
                                refer = self.get_spepc(ref_path).to(self.device)
                                if self.is_half:
                                    refer = refer.half()
                                else:
                                    refer = refer.float()
                                refers.append(refer)
                                logger.info(f"loading extra reference audio: {ref_path}")
                            except Exception as e:
                                logger.warning(f"failed to load extra reference audio: {e}")

                    # 如果没有额外参考音频，使用主参考音频
                    if len(refers) == 0:
                        refer = sess.get("refer_spec")
                        if refer is None:
                            refer = self.get_spepc(ref_audio_path).to(self.device)
                            refer = refer.half() if self.is_half else refer.float()
                        refers = [refer]

                    # 解码
                    audio = self.vq_model.decode(
                        pred_semantic,
                        torch.LongTensor(phones2).to(self.device).unsqueeze(0),
                        refers,
                        speed=speed
                    )[0][0]

                    # 防止爆音
                    max_audio = torch.abs(audio).max()
                    if max_audio > 1:
                        audio = audio / max_audio

                    # 转换为numpy并流式返回
                    audio_chunk = audio.cpu().detach().numpy()

                    # 确保音频数据是float32类型
                    if hasattr(audio_chunk, 'dtype') and 'float16' in str(audio_chunk.dtype):
                        audio_chunk = audio_chunk.astype(np.float32)

                    # 句尾淡出，消除突然截断的爆音感
                    audio_chunk = self._apply_fade_out(audio_chunk, sr)

                    # 流式返回当前句子的音频和对应的文本（可分块）
                    for _sr, _chunk, _text in _yield_audio_segments(audio_chunk, text_item):
                        yield _sr, _chunk, _text

                    # 返回句间停顿
                    pause_chunk = zero_wav.cpu().detach().numpy()
                    if hasattr(pause_chunk, 'dtype') and 'float16' in str(pause_chunk.dtype):
                        pause_chunk = pause_chunk.astype(np.float32)
                    yield sr, pause_chunk, ""  # 停顿不需要文本

                else:
                    # v3模型解码
                    import torchaudio

                    # audio saved tossplit into graph audio saved tosynthesis failed v3 synthesis failed
                    # audio saved tossplit into GPTsynthesis faileds SoVITS / BigVGAN synthesis faileds
                    with self._sovits_decode_lock:
                        # 根据is_half决定是否使用half
                        refer = sess.get("refer_spec")
                        if refer is None:
                            refer = self.get_spepc(ref_audio_path).to(self.device)
                            refer = refer.half() if self.is_half else refer.float()

                        phoneme_ids0 = torch.LongTensor(phones1).to(self.device).unsqueeze(0)
                        phoneme_ids1 = torch.LongTensor(phones2).to(self.device).unsqueeze(0)

                        # 提取参考音频特征（优先使用会话缓存）
                        fea_ref = self._clone_cached_value(sess.get("prompt_fea_ref"))
                        ge = self._clone_cached_value(sess.get("prompt_ge"))
                        if fea_ref is None or ge is None:
                            fea_ref, ge = self.vq_model.decode_encp(prompt.unsqueeze(0), phoneme_ids0, refer)

                        # 加载并处理参考音频
                        # 提取mel特征（缓存优先）
                        mel2 = sess.get("mel2_norm")
                        # 归一化/反归一化参数与函数（无论是否命中缓存，都需要用于后续 denorm）
                        spec_min, spec_max = -12, 2
                        denorm_spec = lambda x: (x + 1) / 2 * (spec_max - spec_min) + spec_min
                        if mel2 is None:
                            ref_audio, ref_sr = torchaudio.load(ref_audio_path)
                            ref_audio = ref_audio.to(self.device)
                            ref_audio = ref_audio.half() if self.is_half else ref_audio.float()
                            if ref_audio.shape[0] == 2:  # 转单声道
                                ref_audio = ref_audio.mean(0).unsqueeze(0)
                            if ref_sr != 24000:
                                ref_audio = self._resample(ref_audio, ref_sr)
                            mel_fn = lambda x: mel_spectrogram_torch(x, **{
                                "n_fft": 1024,
                                "win_size": 1024,
                                "hop_size": 256,
                                "num_mels": 100,
                                "sampling_rate": 24000,
                                "fmin": 0,
                                "fmax": None,
                                "center": False
                            })
                            norm_spec = lambda x: (x - spec_min) / (spec_max - spec_min) * 2 - 1
                            mel2 = mel_fn(ref_audio)
                            mel2 = norm_spec(mel2)

                        # 调整长度
                        T_min = min(mel2.shape[2], fea_ref.shape[2])
                        mel2 = mel2[:, :, :T_min]
                        fea_ref = fea_ref[:, :, :T_min]
                        if (T_min > 468):
                            mel2 = mel2[:, :, -468:]
                            fea_ref = fea_ref[:, :, -468:]
                            T_min = 468

                        # 设置块长度
                        default_chunk_len = 934 - T_min
                        chunk_len = default_chunk_len
                        stream_v3_chunks = chunk_samples is not None and chunk_samples > 0
                        if stream_v3_chunks:
                            target_chunk_frames = max(
                                8, int(round((chunk_samples / float(sr)) * (sr / 256.0)))
                            )
                            chunk_len = max(8, min(default_chunk_len, target_chunk_frames))
                            logger.info(
                                "[v3-stream] enable true streaming: mel_chunk=%s (default=%s, target_frames=%s)"
                                % (chunk_len, default_chunk_len, target_chunk_frames)
                            )
                        #sis_halfaudio saved tohalf
                        if self.is_half:
                            mel2 = mel2.half()
                        else:
                            mel2 = mel2.float()

                        # audio saved to
                        fea_todo, ge = self.vq_model.decode_encp(pred_semantic, phoneme_ids1, refer, ge, speed)

                        # synthesis failed
                        cfm_resss = []
                        _t_cfm_total = 0.0
                        idx = 0
                        total_todo_frames = fea_todo.shape[2]
                        stream_chunk_index = 0
                        while True:
                            chunk_end = min(total_todo_frames, idx + chunk_len)
                            fea_todo_chunk = fea_todo[:, :, idx:chunk_end]
                            if fea_todo_chunk.shape[-1] == 0:
                                break

                            idx = chunk_end
                            fea = torch.cat([fea_ref, fea_todo_chunk], 2).transpose(2, 1)

                            # CFMss
                            if self._sovits_sync_timing_enabled:
                                self._sync_sovits_timing()
                            _t0 = time.perf_counter()
                            cfm_res = self.vq_model.cfm.inference(
                                fea,
                                torch.LongTensor([fea.size(1)]).to(fea.device),
                                mel2,
                                sample_steps,
                                inference_cfg_rate=0
                            )
                            if self._sovits_sync_timing_enabled:
                                self._sync_sovits_timing()
                            _t_cfm_chunk = (
                                time.perf_counter() - _t0
                                if self._sovits_sync_timing_enabled
                                else None
                            )

                            cfm_res = cfm_res[:, :, mel2.shape[2]:]
                            mel2 = cfm_res[:, :, -T_min:]
                            fea_ref = fea_todo_chunk[:, :, -T_min:]
                            if _t_cfm_chunk is not None:
                                _t_cfm_total = _t_cfm_total + _t_cfm_chunk

                            if stream_v3_chunks:
                                stream_chunk_index += 1
                                is_last_stream_chunk = idx >= total_todo_frames
                                if self._sovits_sync_timing_enabled:
                                    self._sync_sovits_timing()
                                _t_denorm0 = time.perf_counter()
                                chunk_mel = denorm_spec(cfm_res)
                                if self._sovits_sync_timing_enabled:
                                    self._sync_sovits_timing()
                                _t_denorm_chunk = (
                                    time.perf_counter() - _t_denorm0
                                    if self._sovits_sync_timing_enabled
                                    else None
                                )
                                if self._sovits_sync_timing_enabled:
                                    self._sync_sovits_timing()
                                _t1 = time.perf_counter()
                                with self._device_context():
                                    with torch.inference_mode():
                                        wav_gen = self.bigvgan_model(chunk_mel)
                                        audio = wav_gen[0][0]
                                if self._sovits_sync_timing_enabled:
                                    self._sync_sovits_timing()
                                if self._sovits_sync_timing_enabled:
                                    logger.info(
                                        "[v3-stream] chunk=%s cfm=%.1fms denorm=%.1fms bigvgan=%.1fms mel_T=%s"
                                        % (
                                            stream_chunk_index,
                                            _t_cfm_chunk * 1000.0,
                                            _t_denorm_chunk * 1000.0,
                                            (time.perf_counter() - _t1) * 1000.0,
                                            chunk_mel.shape[2],
                                        )
                                    )
                                else:
                                    logger.info(
                                        "[v3-stream] chunk=%s mel_T=%s"
                                        % (stream_chunk_index, chunk_mel.shape[2])
                                    )

                                max_audio = torch.abs(audio).max()
                                if max_audio > 1:
                                    audio = audio / max_audio

                                audio_chunk = self._finalize_stream_chunk(
                                    audio,
                                    sr,
                                    if_sr=if_sr,
                                    is_last_chunk=is_last_stream_chunk,
                                    apply_fade_in=(stream_chunk_index > 1),
                                )
                                yield sr, audio_chunk, text_item if stream_chunk_index == 1 else ""
                            else:
                                cfm_resss.append(cfm_res)

                        if not stream_v3_chunks:
                            # synthesis failed
                            cmf_res = torch.cat(cfm_resss, 2)
                            if self._sovits_sync_timing_enabled:
                                self._sync_sovits_timing()
                            _t_denorm0 = time.perf_counter()
                            cmf_res = denorm_spec(cmf_res)
                            if self._sovits_sync_timing_enabled:
                                self._sync_sovits_timing()
                            _t_denorm_total = (
                                time.perf_counter() - _t_denorm0
                                if self._sovits_sync_timing_enabled
                                else None
                            )

                            # BigVGANsynthesis failed
                            if self._sovits_sync_timing_enabled:
                                self._sync_sovits_timing()
                            _t1 = time.perf_counter()
                            with self._device_context():
                                with torch.inference_mode():
                                    wav_gen = self.bigvgan_model(cmf_res)
                                    audio = wav_gen[0][0]
                            if self._sovits_sync_timing_enabled:
                                self._sync_sovits_timing()
                                print("[sovits-timing] cfm=%.1fms  denorm=%.1fms  bigvgan=%.1fms  mel_T=%s" % (
                                    _t_cfm_total * 1000.0,
                                    _t_denorm_total * 1000.0,
                                    (time.perf_counter() - _t1) * 1000.0,
                                    cmf_res.shape[2],
                                ))
                            else:
                                self._synchronize_device()
                                print("[sovits-timing] cfm=%.1fms  bigvgan=%.1fms  mel_T=%s" % (
                                    _t_cfm_total * 1000.0,
                                    (time.perf_counter() - _t1) * 1000.0,
                                    cmf_res.shape[2],
                                ))

                            # synthesis failed
                            max_audio = torch.abs(audio).max()
                            if max_audio > 1:
                                audio = audio / max_audio

                            audio_chunk = self._finalize_stream_chunk(
                                audio,
                                sr,
                                if_sr=if_sr,
                                is_last_chunk=True,
                            )

                            # audio saved toaudio saved toaudio saved tosynthesis failed
                            for _sr, _chunk, _text in _yield_audio_segments(audio_chunk, text_item):
                                yield _sr, _chunk, _text

                        pause_chunk = zero_wav.cpu().detach().numpy()
                        if hasattr(pause_chunk, 'dtype') and 'float16' in str(pause_chunk.dtype):
                            pause_chunk = pause_chunk.astype(np.float32)
                        yield sr, pause_chunk, ""  # 停顿不需要文本

        except Exception as e:
            logger.error(f"streaming inference failed: {str(e)}")
            logger.error(traceback.format_exc())
            # 返回一个空音频块，避免生成器中断
            yield sr if 'sr' in locals() else 24000, np.zeros(16000, dtype=np.float32), ""

    def _apply_fade_out(self, audio: np.ndarray, sr: int, duration_ms: int = 15) -> np.ndarray:
        """对音频末尾做线性淡出，避免句尾突然截断产生的爆音感。
        duration_ms: 淡出持续时间（毫秒），默认 15ms
        """
        fade_samples = min(int(sr * duration_ms / 1000), len(audio) // 4)
        if fade_samples > 0:
            audio = audio.copy()
            audio[-fade_samples:] *= np.linspace(1.0, 0.0, fade_samples, dtype=np.float32)
        return audio

    def _apply_fade_in(self, audio: np.ndarray, sr: int, duration_ms: int = 8) -> np.ndarray:
        fade_samples = min(int(sr * duration_ms / 1000), len(audio) // 4)
        if fade_samples > 0:
            audio = audio.copy()
            audio[:fade_samples] *= np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
        return audio

    def _finalize_stream_chunk(
        self,
        audio_chunk,
        sr: int,
        *,
        if_sr: bool = False,
        is_last_chunk: bool = False,
        apply_fade_in: bool = False,
    ) -> np.ndarray:
        if torch.is_tensor(audio_chunk):
            if if_sr and self.model_version == "v3":
                try:
                    logger.info("running audio super-resolution...")
                    if not hasattr(self, 'sr_model') or self.sr_model is None:
                        self.sr_model = create_ap_bwe(self.device, DictToAttrRecursive)
                    audio_chunk, sr = self.sr_model(audio_chunk.unsqueeze(0), sr)
                    max_audio = np.abs(audio_chunk).max()
                    if max_audio > 1:
                        audio_chunk = audio_chunk / max_audio
                except Exception as e:
                    logger.warning(f"audio super-resolution failed: {e}")
                    logger.warning(traceback.format_exc())
                    audio_chunk = audio_chunk.cpu().detach().numpy()
            else:
                audio_chunk = audio_chunk.cpu().detach().numpy()

        if hasattr(audio_chunk, 'dtype') and 'float16' in str(audio_chunk.dtype):
            audio_chunk = audio_chunk.astype(np.float32)
        elif getattr(audio_chunk, "dtype", None) != np.float32:
            audio_chunk = np.asarray(audio_chunk, dtype=np.float32)

        if apply_fade_in:
            audio_chunk = self._apply_fade_in(audio_chunk, sr)
        if is_last_chunk:
            audio_chunk = self._apply_fade_out(audio_chunk, sr)
        return audio_chunk

    def _resample(self, audio_tensor, sr0):
        """重采样音频"""
        import torchaudio

        # 确保输入类型与权重类型匹配
        if self.is_half:
            # 如果模型是半精度，则强制将音频转为半精度
            audio_tensor = audio_tensor.half()
            # 创建半精度的重采样器
            resample_fn = torchaudio.transforms.Resample(sr0, 24000).to(self.device).half()
        else:
            # 如果模型是全精度，则强制将音频转为全精度
            audio_tensor = audio_tensor.float()
            # 创建全精度的重采样器
            resample_fn = torchaudio.transforms.Resample(sr0, 24000).to(self.device)

        return resample_fn(audio_tensor)

class DictToAttrRecursive(dict):
    """将字典转换为可属性访问的对象"""

    def __init__(self, input_dict):
        super().__init__(input_dict)
        for key, value in input_dict.items():
            if isinstance(value, dict):
                value = DictToAttrRecursive(value)
            self[key] = value
            setattr(self, key, value)

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            raise AttributeError(f"Attribute {item} not found")

    def __setattr__(self, key, value):
        if isinstance(value, dict):
            value = DictToAttrRecursive(value)
        super(DictToAttrRecursive, self).__setitem__(key, value)
        super().__setattr__(key, value)

    def __delattr__(self, item):
        try:
            del self[item]
        except KeyError:
            raise AttributeError(f"Attribute {item} not found")


def cut1(inp):
    """凑四句一切 - 每四个句子作为一个段落"""
    inp = inp.strip("\n")
    inps = split(inp)
    split_idx = list(range(0, len(inps), 4))
    split_idx[-1] = None
    if len(split_idx) > 1:
        opts = []
        for idx in range(len(split_idx) - 1):
            opts.append("".join(inps[split_idx[idx]: split_idx[idx + 1]]))
    else:
        opts = [inp]
    opts = [item for item in opts if not set(item).issubset(punctuation)]
    return "\n".join(opts)


def cut2(inp):
    """凑50字一切 - 大约每50个字符作为一个段落"""
    inp = inp.strip("\n")
    inps = split(inp)
    if len(inps) < 2:
        return inp
    opts = []
    summ = 0
    tmp_str = ""
    for i in range(len(inps)):
        summ += len(inps[i])
        tmp_str += inps[i]
        if summ > 50:
            summ = 0
            opts.append(tmp_str)
            tmp_str = ""
    if tmp_str != "":
        opts.append(tmp_str)
    # 如果最后一个太短了，和前一个合一起
    if len(opts) > 1 and len(opts[-1]) < 50:
        opts[-2] = opts[-2] + opts[-1]
        opts = opts[:-1]
    opts = [item for item in opts if not set(item).issubset(punctuation)]
    return "\n".join(opts)


def cut3(inp):
    """按中文句号切 - 按中文句号'。'分割"""
    inp = inp.strip("\n")
    opts = ["%s" % item for item in inp.strip("。").split("。")]
    opts = [item for item in opts if not set(item).issubset(punctuation)]
    return "\n".join(opts)


def cut4(inp):
    """按英文句号切 - 按英文句号'.'分割"""
    import re
    inp = inp.strip("\n")
    opts = re.split(r'(s<!\d)\.(s!\d)', inp.strip("."))
    opts = [item for item in opts if not set(item).issubset(punctuation)]
    return "\n".join(opts)


def cut5(inp):
    """按标点符号切 - 按各种标点符号分割"""
    import re
    inp = inp.strip("\n")
    punds = {',', '.', ';', 's', '!', '、', '，', '。', '？', '！', ';', '：', '…'}
    mergeitems = []
    items = []

    for i, char in enumerate(inp):
        if char in punds:
            if char == '.' and i > 0 and i < len(inp) - 1 and inp[i - 1].isdigit() and inp[i + 1].isdigit():
                items.append(char)
            else:
                items.append(char)
                mergeitems.append("".join(items))
                items = []
        else:
            items.append(char)

    if items:
        mergeitems.append("".join(items))

    opt = [item for item in mergeitems if not set(item).issubset(punds)]
    return "\n".join(opt)


def split(todo_text):
    """将文本按标点符号分割成句子列表"""
    splits = {"，", "。", "？", "！", ",", ".", "s", "!", "~", ":", "：", "—", "…"}
    punctuation = set(['!', 's', '…', ',', '.', '-', " "])

    todo_text = todo_text.replace("……", "。").replace("——", "，")
    if todo_text[-1] not in splits:
        todo_text += "。"
    i_split_head = i_split_tail = 0
    len_text = len(todo_text)
    todo_texts = []
    while 1:
        if i_split_head >= len_text:
            break  # 结尾一定有标点，所以直接跳出即可，最后一段在上次已加入
        if todo_text[i_split_head] in splits:
            i_split_head += 1
            todo_texts.append(todo_text[i_split_tail:i_split_head])
            i_split_tail = i_split_head
        else:
            i_split_head += 1
    return todo_texts


def process_text(texts):
    """处理文本，过滤空行并检查是否有有效内容"""
    _text = []
    if all(text in [None, " ", "\n", ""] for text in texts):
        raise ValueError("请输入有效文本")
    for text in texts:
        if text in [None, " ", ""]:
            pass
        else:
            _text.append(text)
    return _text


def synthesize(gpt_model_path, sovits_model_path, ref_audio_path, ref_text_path, ref_language,
               target_text_path, target_language, output_path, sample_steps=16, top_p=0.6,
               temperature=0.6, speed=1.0, how_to_cut="不切", if_sr=False, pause_second=0.3):
    """
    合成语音的封装函数，符合原始CLI工具的接口

    Args:
        gpt_model_path: GPT模型路径
        sovits_model_path: SoVITS模型路径
        ref_audio_path: 参考音频路径
        ref_text_path: 参考文本路径
        ref_language: 参考文本语言
        target_text_path: 目标文本路径
        target_language: 目标文本语言
        output_path: 输出路径
        sample_steps: 采样步数
        top_p: GPT采样参数
        temperature: GPT采样参数
        speed: 语速控制
        how_to_cut: 文本切分方式
        if_sr: 是否使用音频超分
        pause_second: 句间停顿秒数
    """
    try:
        # 初始化TTS推理器
        inferencer = TTSInferencer(
            gpt_path=gpt_model_path,
            sovits_path=sovits_model_path
        )

        # 读取参考文本
        with open(ref_text_path, 'r', encoding='utf-8') as file:
            ref_text = file.read().strip()

        # 读取目标文本
        with open(target_text_path, 'r', encoding='utf-8') as file:
            target_text = file.read().strip()

        # 执行推理
        sampling_rate, audio_data = inferencer.infer(
            text=target_text,
            ref_audio_path=ref_audio_path,
            prompt_text=ref_text,
            text_language=target_language,
            prompt_language=ref_language,
            how_to_cut=how_to_cut,
            top_p=top_p,
            temperature=temperature,
            sample_steps=sample_steps,
            speed=speed,
            if_sr=if_sr,
            pause_second=pause_second
        )

        # 保存结果
        if hasattr(audio_data, 'dtype') and 'float16' in str(audio_data.dtype):
            # 转换为float32
            audio_data = audio_data.astype(np.float32)

            # 保存结果
        os.makedirs(output_path, exist_ok=True)
        output_wav_path = os.path.join(output_path, "output.wav")
        sf.write(output_wav_path, audio_data, sampling_rate)

        logger.info(f"audio saved to {output_wav_path}")
        return output_wav_path

    except Exception as e:
        logger.error(f"synthesis failed: {str(e)}")
        logger.error(traceback.format_exc())
        raise


def main():
    """命令行入口函数"""
    import argparse

    parser = argparse.ArgumentParser(description="GPT-SoVITS TTS 推理工具")
    parser.add_argument('--gpt_model', required=True, help="GPT模型路径")
    parser.add_argument('--sovits_model', required=True, help="SoVITS模型路径")
    parser.add_argument('--ref_audio', required=True, help="参考音频路径")
    parser.add_argument('--ref_text', required=True, help="参考文本路径")
    parser.add_argument('--ref_language', required=True, choices=["中文", "英文", "日文"], help="参考音频语言")
    parser.add_argument('--target_text', required=True, help="目标文本路径")
    parser.add_argument('--target_language', required=True,
                        choices=["中文", "英文", "日文", "中英混合", "日英混合", "多语种混合", "粤语", "韩文",
                                 "粤英混合", "韩英混合", "多语种混合(粤语)"],
                        help="目标文本语言")
    parser.add_argument('--output_path', required=True, help="输出目录")
    parser.add_argument('--how_to_cut', default="不切",
                        choices=["不切", "凑四句一切", "凑50字一切", "按中文句号。切", "按英文句号.切", "按标点符号切"],
                        help="文本切分方式")
    parser.add_argument('--sample_steps', type=int, default=16, help="仅V3模型：采样步数")
    parser.add_argument('--top_k', type=int, default=20, help="GPT采样参数 top_k")
    parser.add_argument('--top_p', type=float, default=0.6, help="GPT采样参数 top_p")
    parser.add_argument('--temperature', type=float, default=0.6, help="GPT采样参数 temperature")
    parser.add_argument('--speed', type=float, default=1.0, help="语速控制")
    parser.add_argument('--pause_second', type=float, default=0.3, help="句间停顿秒数")
    parser.add_argument('--if_sr', action='store_true', help="是否使用音频超分(仅V3模型)")

    args = parser.parse_args()

    synthesize(
        gpt_model_path=args.gpt_model,
        sovits_model_path=args.sovits_model,
        ref_audio_path=args.ref_audio,
        ref_text_path=args.ref_text,
        ref_language=args.ref_language,
        target_text_path=args.target_text,
        target_language=args.target_language,
        output_path=args.output_path,
        how_to_cut=args.how_to_cut,
        sample_steps=args.sample_steps,
        top_p=args.top_p,
        temperature=args.temperature,
        speed=args.speed,
        if_sr=args.if_sr,
        pause_second=args.pause_second
    )


if __name__ == "__main__":
    main()
