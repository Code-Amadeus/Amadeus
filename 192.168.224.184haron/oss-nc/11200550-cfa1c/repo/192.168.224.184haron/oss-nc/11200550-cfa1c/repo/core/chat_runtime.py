"""Chat role presentation: filtering, sentence splitting, expression and TTS delivery.

Cooperative owns role generation and whole-turn orchestration. This runtime accepts
role text and never parses executable control decisions or dispatches Host actions.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from config.settings import FIRST_SENTENCE_EARLY_CUT_CHARS
from config.log_privacy import protected_text
from core.chat_history_projection import project_inline_role_history
from core.chat_stream_consumption import consume_role_stream_text
from llm.sentence_splitter import split_stream_buffer_for_first_sentence
from llm.stream_parser import StreamTagParser, clean_presentation_sentence, presentation_parts
from tools.text_utils import STRONG_SENTENCE_ENDINGS
from tts.contract import TTSRequest
from tts.latency_clock import log_latency_marker
from tts.sentence_state import sentence_state_manager, pre_translation_cache
from tts.pre_translation_runtime import runtime as pre_translation_runtime
from vts.expression_controller import get_controller as _get_expr_ctrl

logger = logging.getLogger("chat_runtime")

_WEAK_ENDINGS = {"，", ",", "、", "；", ";"}
_SENTENCE_ENDINGS = STRONG_SENTENCE_ENDINGS | _WEAK_ENDINGS

def _observe_turn_event(
    turn_id: str,
    *,
    stage: str,
    origin_kind: str,
    origin_id: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    """Best-effort experiment telemetry; never participates in routing."""

    try:
        from server.turn_decision_shadow import (
            get_enabled_turn_decision_shadow_observer,
        )

        observer = get_enabled_turn_decision_shadow_observer()
        if observer is None:
            return
        observer.record_event(
            turn_id,
            stage=stage,
            origin_kind=origin_kind,
            origin_id=origin_id,
            payload=payload,
        )
    except Exception:
        logger.debug("turn decision event observation failed", exc_info=True)


def _pre_translation_enabled() -> bool:
    return pre_translation_runtime.is_enabled()


class _RoleTextState:
    """Mutable presentation state for exactly one reply."""

    __slots__ = (
        "full_response", "history_response", "current_sentence", "is_first",
        "pending_expr_acts", "last_sentence_id", "next_stream_tts",
        "tts_emotion_routing", "tts_emotion", "tts_ordered_parts", "parser",
        "gui_callback", "turn_id", "sentence_count", "auip_background_capture_release",
    )

    def __init__(self, *, gui_callback, turn_id="", auip_background_capture_release=None):
        from tts.pipeline import emotion_references_enabled

        self.full_response = ""
        self.history_response = ""
        self.current_sentence = ""
        self.is_first = True
        self.pending_expr_acts = []
        self.last_sentence_id = None
        self.next_stream_tts = None
        self.tts_emotion_routing = emotion_references_enabled()
        self.tts_emotion = ""
        self.tts_ordered_parts = ()
        self.parser = StreamTagParser(control_envelope_enabled=True, stop_after_control=False)
        self.gui_callback = gui_callback
        self.turn_id = str(turn_id or "")
        self.sentence_count = 0
        self.auip_background_capture_release = auip_background_capture_release


class _RoleTextStream:
    """One reply's presentation state; queued speech retains exact Chat turn identity."""

    def __init__(self, runtime, *, turn_id, speech, gui_callback=None,
                 auip_background_capture_release=None):
        self.runtime = runtime
        self.state = _RoleTextState(gui_callback=gui_callback,
            turn_id=turn_id,
            auip_background_capture_release=auip_background_capture_release)
        self.speech = speech and runtime._pending_sentence_items is not None
        self.releases_auip_on_first_sentence = bool(
            self.speech and auip_background_capture_release is not None)
        self.closed = False

    def _parse_chunk(self, text):
        state = self.state
        cleaned, parts = presentation_parts(state.parser, text)
        state.history_response += project_inline_role_history(parts)
        if state.tts_emotion_routing:
            state.tts_ordered_parts = parts
        else:
            state.pending_expr_acts.extend(value for kind, value in parts if kind == "action")
        return cleaned

    async def _process_sentence(self, text):
        safe_text, expressions = clean_presentation_sentence(text)
        if safe_text:
            await self.runtime._enqueue_sentence(self.state, safe_text, expressions)

    async def prepare(self):
        if self.speech:
            await self.runtime.prepare_role_audio(turn_id=self.state.turn_id)

    async def begin_remote(self):
        """Finish the local span before arming the remote first sentence's TTS."""
        if self.speech and self.state.current_sentence.strip():
            await self._process_sentence(self.state.current_sentence)
        self.state.current_sentence = ""
        self.state.next_stream_tts = True

    async def feed(self, text):
        if self.closed:
            raise RuntimeError("role stream is closed")

        async def silent(_text):
            pass

        await self.runtime._accept_role_stream_text(
            self.state, text, dispatch_text=None if self.speech else silent,
            parse_control=self._parse_chunk, process_sentence=self._process_sentence)
        return self.state.full_response

    async def finish(self):
        if self.closed:
            raise RuntimeError("role stream is closed")
        state = self.state
        if self.speech and state.current_sentence.strip():
            await self._process_sentence(state.current_sentence)
        state.current_sentence = ""
        self.closed = True
        if state.last_sentence_id and self.runtime._playback_manager is not None:
            self.runtime._playback_manager.mark_turn_last_sentence(
                state.last_sentence_id, str(state.turn_id or "") or None)
        if not state.last_sentence_id:
            return {"status":"skipped", "reason":"no_speakable_sentence"}
        return {"status":"queued", "sentence_id":state.last_sentence_id,
            "last_sentence_id":state.last_sentence_id, "sentence_count":state.sentence_count}

    def abort(self):
        # Never touch another turn's queue/playback. Existing Chat interruption
        # and epoch ownership decide whether already queued requests may play.
        self.closed = True
        self.state.current_sentence = ""
        self.state.pending_expr_acts.clear()


class ChatRuntime:
    """Process-shared presentation dependencies; each reply has its own state."""

    def __init__(self) -> None:
        self._playback_manager = None
        self._pending_sentence_items = None

    def configure(self, *, playback_manager=None, pending_sentence_items=None) -> None:
        if playback_manager is not None:
            self._playback_manager = playback_manager
        if pending_sentence_items is not None:
            self._pending_sentence_items = pending_sentence_items

    async def _safe_start_translation(self, sentence_id: str, safe_text: str) -> None:
        try:
            await asyncio.wait_for(
                pre_translation_cache.start_translation(sentence_id, safe_text),
                timeout=3.0,
            )
            logger.debug(f"pre-translation started successfully: {sentence_id}")
        except asyncio.TimeoutError:
            logger.warning(f"pre-translation startup timed out: {sentence_id}")
        except Exception as e:
            logger.error(f"pre-translation startup failed: {sentence_id}, error: {e}")


    def begin_role_text_stream(self, *, turn_id: str, speech: bool = True,
                               gui_callback=None,
                               auip_background_capture_release=None):
        """Create one delivery-only reply using Main Chat's existing parser/queue."""
        return _RoleTextStream(self, turn_id=turn_id, speech=speech,
            gui_callback=gui_callback,
            auip_background_capture_release=auip_background_capture_release)


    async def prepare_role_audio(self, *, turn_id: str = "") -> None:
        """Prepare the shared output device off the event loop before role generation."""
        player = getattr(self._playback_manager, "player", None)
        if (
            player is not None
            and hasattr(player, "initialize")
            and str(os.environ.get("AMADEUS_E2E_NO_TTS") or "").strip().lower()
            not in {"1", "true", "yes", "on"}
        ):
            try:
                started = time.perf_counter()
                await asyncio.to_thread(player.initialize, 24000)
                logger.info(
                    "pyaudio stream warmup completed (24000 Hz) turn=%s elapsed_ms=%.1f",
                    turn_id, (time.perf_counter() - started) * 1000,
                )
            except asyncio.CancelledError:
                raise
            except Exception as _e:
                logger.warning(f"pyaudio warmup failed (non-fatal): {_e}")


    async def enqueue_completed_role_text(
        self,
        text: str,
        *,
        turn_id: str,
    ) -> dict[str, object]:
        """Send a completed role line through the established Main Chat TTS path.

        Structured role decisions arrive as one completed string rather than a
        token stream. Presentation tags may still be retained for expression
        timing, but action tags from this delivery-only port are never routed.
        Sentence splitting, first-sentence streaming, later-utterance scheduling,
        translation and playback identity remain owned by the normal Chat path.
        """

        if self._pending_sentence_items is None:
            return {"status": "unavailable", "reason": "tts_queue_unavailable"}
        raw_text = str(text or "")
        if not raw_text.strip():
            return {"status": "skipped", "reason": "empty_text"}
        stream = self.begin_role_text_stream(turn_id=turn_id)
        try:
            await stream.prepare()
            await stream.feed(raw_text)
            return await stream.finish()
        finally:
            stream.abort()


    async def _enqueue_sentence(
        self, st: _RoleTextState, safe_text: str, inline_expr_acts: list,
    ) -> None:
        """Queue already-clean presentation text; this port cannot dispatch Work."""
        start_time = time.time()
        sentence_id = sentence_state_manager.create_sentence(safe_text)
        st.last_sentence_id = sentence_id
        st.sentence_count += 1
        # The experiment queues only actions encountered before this span.
        # Keep the established one-action-per-sentence policy when disabled.
        if st.tts_emotion_routing:
            buffered = list(st.pending_expr_acts)
            st.pending_expr_acts.clear()
        else:
            buffered = [st.pending_expr_acts.pop(0)] if st.pending_expr_acts else []
        all_expr_acts = buffered + inline_expr_acts
        if all_expr_acts:
            _get_expr_ctrl().register_sentence_actions(sentence_id, all_expr_acts)
        if st.tts_emotion_routing:
            logger.info("[TTS-EMOTION] queued turn=%s id=%s reference=%s chars=%s",
                        st.turn_id, sentence_id, st.tts_emotion, len(safe_text))

        # 并行启动预翻译，不阻塞 TTS（CLI 本地路径关闭翻译，字幕在播放时显示）
        if _pre_translation_enabled():
            asyncio.create_task(self._safe_start_translation(sentence_id, safe_text))

        # stream_tts 标志供 hybrid 路线使用：远端首 token 到达后下一句走流式 TTS
        _stream_tts_flag = st.next_stream_tts if st.next_stream_tts is not None else st.is_first
        st.next_stream_tts = None  # 消费一次后重置
        logger.info("adding sentence to queue: %s", protected_text(safe_text, limit=30))
        await self._pending_sentence_items.put(TTSRequest(
            sentence_id=sentence_id,
            text=safe_text,
            is_first=st.is_first,
            stream_tts=_stream_tts_flag,
            source="chat",
            turn_id=st.turn_id,
            emotion=st.tts_emotion,
        ))

        if st.is_first:
            log_latency_marker(
                logger,
                "first_sentence_enqueued",
                id=sentence_id,
                chars=len(safe_text.strip()),
                turn_id=st.turn_id,
            )
            _observe_turn_event(
                st.turn_id,
                stage="first_sentence_enqueued",
                origin_kind="presentation_lane",
                origin_id=sentence_id,
                payload={"chars": len(safe_text.strip())},
            )
            try:
                from core.turn_coordinator import get_turn_coordinator

                get_turn_coordinator().on_first_sentence_enqueued(
                    turn_id=st.turn_id,
                    sentence_id=sentence_id,
                )
            except Exception:
                logger.debug("first sentence identity observation failed", exc_info=True)
            self._release_auip_background_capture(
                st,
                reason="first_sentence_enqueued",
            )
        st.is_first = False

        processing_time = time.time() - start_time
        logger.info(f"sentence processing time: {processing_time:.3f}s (ID: {sentence_id})")
        if processing_time > 8.0:
            logger.warning(f"sentence processing timed out: {processing_time:.3f}s (ID: {sentence_id})")


    def _should_dispatch(self, st: _RoleTextState, ch: str) -> bool:
        sentence_len = len(st.current_sentence.strip())
        if st.is_first:
            return True
        return not (ch in _WEAK_ENDINGS and sentence_len < 5)


    async def _append_and_dispatch(self, st: _RoleTextState, text_piece: str, dispatch) -> None:
        """将新增文本逐字符注入 current_sentence，遇到终止符立即触发分句。"""
        if not text_piece:
            return
        from tts.pipeline import current_tts_language_code

        tts_language_code = current_tts_language_code()
        for ch in text_piece:
            st.current_sentence += ch
            if ch in _SENTENCE_ENDINGS:
                # 非英文：句点前若为字母/_/- 则暂不当作边界，减少 file.txt / URL 误切。
                if (
                    ch == "."
                    and tts_language_code != "en"
                    and len(st.current_sentence) >= 2
                ):
                    prev_ch = st.current_sentence[-2]
                    if prev_ch.isalnum() or prev_ch in "_-":
                        continue

                if self._should_dispatch(st, ch):
                    await dispatch(st.current_sentence)
                    st.current_sentence = ""
            elif (
                st.is_first
                and FIRST_SENTENCE_EARLY_CUT_CHARS > 0
                and len(st.current_sentence.strip()) >= FIRST_SENTENCE_EARLY_CUT_CHARS
            ):
                _buf = st.current_sentence
                _head, _tail, _reason = split_stream_buffer_for_first_sentence(
                    _buf,
                    FIRST_SENTENCE_EARLY_CUT_CHARS,
                    "英文" if tts_language_code == "en" else "日文",
                )
                if _head and (_tail or _reason != "japanese_wait_boundary"):
                    logger.info(
                        f"⚡ [首句早切] 已达 {FIRST_SENTENCE_EARLY_CUT_CHARS} 字（尚无句末标点），"
                        f"在安全边界切开 reason={_reason}（remainder={len(_tail)} 字符）"
                    )
                    log_latency_marker(
                        logger,
                        "first_sentence_early_cut",
                        chars=len(_head.strip()),
                    )
                    await dispatch(_head)
                    st.current_sentence = _tail
                else:
                    logger.info(
                        f"⚡ [首句早切] 已达 {FIRST_SENTENCE_EARLY_CUT_CHARS} 字（尚无句末标点），"
                        f"等待日语安全边界 reason={_reason}"
                    )


    async def _accept_role_stream_text(
        self,
        st: _RoleTextState,
        raw_content: str,
        *,
        dispatch_text=None,
        pace_s: float = 0.0,
        parse_control,
        process_sentence,
    ) -> str:
        """Consume one presentation text fragment through the shared stream port."""

        async def dispatch(content: str) -> None:
            async def text_piece(text):
                if dispatch_text is not None:
                    await dispatch_text(text)
                else:
                    await self._append_and_dispatch(st, text, dispatch=process_sentence)

            if not st.tts_emotion_routing:
                await text_piece(content)
                return
            from llm.emo_presets import EMOTION_DURATION_RANGES
            from tts.pipeline import emotion_reference_key
            for kind, value in st.tts_ordered_parts:
                if kind == "text":
                    await text_piece(str(value))
                elif kind == "action":
                    if value.get("type") == "EMO":
                        preset = str(value.get("attrs", {}).get("preset", "normal")).lower()
                        preset = preset if preset in EMOTION_DURATION_RANGES else "normal"
                        reference_key = emotion_reference_key(preset)
                        if reference_key != st.tts_emotion and st.current_sentence.strip():
                            # A model-emitted boundary must not recolor text
                            # already buffered before the tag or cross a job.
                            await process_sentence(st.current_sentence)
                            st.current_sentence = ""
                        st.tts_emotion = reference_key
                    st.pending_expr_acts.append(value)
            st.tts_ordered_parts = ()

        return await consume_role_stream_text(
            st,
            raw_content,
            parse_control=parse_control,
            dispatch_text=dispatch,
            pace_s=pace_s,
        )


    @staticmethod
    def _release_auip_background_capture(
        st: _RoleTextState,
        *,
        reason: str,
    ) -> None:
        release = st.auip_background_capture_release
        if release is None or release.is_set():
            return
        release.set()
        logger.info(
            "[AUIP-CONTROL] background capture released turn_id=%s reason=%s",
            st.turn_id,
            reason,
        )


# Process-shared presentation runtime.
runtime = ChatRuntime()


def get_chat_runtime() -> ChatRuntime:
    return runtime
