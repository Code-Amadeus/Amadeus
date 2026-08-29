"""Adapter for Amadeus's embedded, v3-only GPT-SoVITS inference rewrite."""

from __future__ import annotations

from tts.backend import BaseTTSBackend, TTSAudioChunk, TTSSynthesisRequest


class GPTSoVITSBackend(BaseTTSBackend):
    """Run GPT-SoVITS v3 checkpoints through the Amadeus low-latency pipeline."""

    backend_id = "gpt_sovits"
    deployment = "embedded"
    supports_streaming = True

    def __init__(self) -> None:
        self._inferencer = None

    def load(self) -> None:
        if self._inferencer is not None:
            return
        from config import settings
        from local_tts_infer import TTSInferencer

        self._inferencer = TTSInferencer(
            device=settings.TTS_DEVICE,
            gpt_path=settings.TTS_GPT_MODEL_PATH or None,
            sovits_path=settings.TTS_SOVITS_MODEL_PATH or None,
        )

    def _ready(self):
        self.load()
        if self._inferencer is None:
            raise RuntimeError("GPT-SoVITS inferencer is unavailable")
        return self._inferencer

    @staticmethod
    def _kwargs(request: TTSSynthesisRequest, *, streaming: bool) -> dict:
        options = dict(request.options)
        options.pop("text_language", None)
        options.pop("prompt_language", None)
        options.pop("speed", None)
        if not streaming:
            options.pop("collect_t2s_stats", None)
        return {
            "ref_audio_path": request.reference_audio,
            "prompt_text": request.reference_text,
            "text_language": request.options.get("text_language", request.language),
            "prompt_language": request.options.get(
                "prompt_language",
                request.reference_language or request.language,
            ),
            "speed": request.speed,
            **options,
        }

    def synthesize(self, request: TTSSynthesisRequest) -> TTSAudioChunk:
        sample_rate, audio = self._ready().infer(
            text=request.text,
            **self._kwargs(request, streaming=False),
        )
        return TTSAudioChunk(int(sample_rate), audio, request.text)

    def synthesize_stream(self, request: TTSSynthesisRequest):
        kwargs = self._kwargs(request, streaming=True)
        kwargs["chunk_size_seconds"] = request.chunk_size_seconds
        for item in self._ready().infer_stream(text=request.text, **kwargs):
            if len(item) == 2:
                sample_rate, audio = item
                text = ""
            else:
                sample_rate, audio, text = item
            yield TTSAudioChunk(int(sample_rate), audio, str(text or ""))

    def close(self) -> None:
        inferencer = self._inferencer
        self._inferencer = None
        close = getattr(inferencer, "close", None)
        if callable(close):
            close()
