"""投机 LLM 启动器（所有权迁移·切片 D2）。

把两段式投机端点的第二级接到 pending-turn 机制上：

  投机转写完成（短静音 + sidecar 出文本，通常在端点确认前后）
      → launch()：策略检查通过则以 pending 轮发起 LLM
  端点确认、正式识别文本返回
      → resolve(final_text)：
          文本一致 → confirm_turn（TTS 门控放行，LLM 早跑的部分全是净赚）
          文本不一致 / 轮已被作废 → discard + 返回 False（调用方按原路径正常发送）
  监听停止 / 打断
      → abandon()（打断路径由 TurnCoordinator 自动作废 pending 轮，
        abandon 只负责清理本地槽位与流任务）

策略门（全部通过才 launch）：
  - ASR_SPECULATIVE_LLM_START 开关
  - provider ∈ {hybrid, hybrid2, hybrid3}（仅本地首句链路；远程单链
    有 prompt 计费与重发幂等成本，且首句延迟收益本就被 hybrid 吃掉）
  - 当前 ASR 会话来源为 wake（免手语音流；手动 ASR 输入不投机）
  - chat 不忙（正在流式/播放时不抢跑）
  - 通过 ASR prompt 泄漏过滤

单槽设计：同一时刻至多一个投机轮；新 launch 抢占旧槽（旧轮静默作废）。
所有方法绝不抛异常；启动器失效退化为无投机的旧流程。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Awaitable, Callable

from config.log_privacy import protected_text

logger = logging.getLogger("speculative_turn")

_HYBRID_PROVIDERS = {"hybrid", "hybrid2", "hybrid3"}
# 槽位最长存活：超龄的投机轮视为陈旧（正常决议在数百 ms 内到达）
_SLOT_MAX_AGE_S = 12.0


class SpeculativeTurnLauncher:
    def __init__(self) -> None:
        self._send_pending: Callable[..., Awaitable[dict]] | None = None
        self._confirm: Callable[..., Awaitable[bool]] | None = None
        self._discard: Callable[..., Awaitable[bool]] | None = None
        self._provider_getter: Callable[[], str] | None = None
        self._asr_source_getter: Callable[[], str] | None = None
        self._chat_busy_fn: Callable[[], bool] | None = None
        self._voice_allowed_fn: Callable[[], Awaitable[bool]] | None = None
        self._session_id_factory: Callable[[], str] | None = None
        # 单槽：当前投机轮
        self._slot_turn_id = ""
        self._slot_text = ""
        self._slot_at = 0.0

    def configure(
        self,
        *,
        send_pending: Callable[..., Awaitable[dict]] | None = None,
        confirm: Callable[..., Awaitable[bool]] | None = None,
        discard: Callable[..., Awaitable[bool]] | None = None,
        provider_getter: Callable[[], str] | None = None,
        asr_source_getter: Callable[[], str] | None = None,
        chat_busy_fn: Callable[[], bool] | None = None,
        voice_allowed_fn: Callable[[], Awaitable[bool]] | None = None,
        session_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if send_pending is not None:
            self._send_pending = send_pending
        if confirm is not None:
            self._confirm = confirm
        if discard is not None:
            self._discard = discard
        if provider_getter is not None:
            self._provider_getter = provider_getter
        if asr_source_getter is not None:
            self._asr_source_getter = asr_source_getter
        if chat_busy_fn is not None:
            self._chat_busy_fn = chat_busy_fn
        if voice_allowed_fn is not None:
            self._voice_allowed_fn = voice_allowed_fn
        if session_id_factory is not None:
            self._session_id_factory = session_id_factory

    # ── 策略 ─────────────────────────────────────────────────────────────────

    def _policy_blocked_reason(self, text: str) -> str:
        try:
            from config.settings import ASR_SPECULATIVE_LLM_START

            if not ASR_SPECULATIVE_LLM_START:
                return "disabled"
            if self._send_pending is None or self._confirm is None or self._discard is None:
                return "not_configured"
            provider = str(self._provider_getter() if self._provider_getter else "").strip().lower()
            if provider not in _HYBRID_PROVIDERS:
                return f"provider_not_hybrid:{provider or 'unknown'}"
            source = str(self._asr_source_getter() if self._asr_source_getter else "")
            if source != "wake":
                return f"asr_source_not_wake:{source or 'none'}"
            if self._chat_busy_fn is not None and self._chat_busy_fn():
                return "chat_busy"
            try:
                from asr.text_filter import is_asr_prompt_leak
                from config.settings import ASR_CONTEXT

                if is_asr_prompt_leak(text, context=ASR_CONTEXT):
                    return "prompt_leak"
            except Exception:
                pass
            return ""
        except Exception:
            logger.debug("speculative policy check failed", exc_info=True)
            return "policy_error"

    # ── 生命周期 ─────────────────────────────────────────────────────────────

    async def launch(self, text: str) -> bool:
        """投机文本就绪：策略通过则开 pending 轮发起 LLM。绝不抛异常。"""
        try:
            text = str(text or "").strip()
            if not text:
                return False
            reason = self._policy_blocked_reason(text)
            if reason:
                logger.debug("[SPEC-LLM] launch skipped: %s", reason)
                return False
            if self._voice_allowed_fn is not None and not await self._voice_allowed_fn():
                logger.debug("[SPEC-LLM] launch skipped: voice_not_allowed")
                return False

            # 抢占旧槽（旧投机轮静默作废）
            await self._drop_slot("superseded")

            turn_id = f"spec_{uuid.uuid4().hex[:12]}"
            session_id = ""
            if self._session_id_factory is not None:
                try:
                    session_id = self._session_id_factory() or ""
                except Exception:
                    logger.exception("speculative session id factory failed")
            await self._send_pending(
                text,
                turn_id=turn_id,
                session_id=session_id,
                source="wake",
            )
            self._slot_turn_id = turn_id
            self._slot_text = text
            self._slot_at = time.monotonic()
            logger.info(
                "[SPEC-LLM] pending turn launched turn=%s text=%s",
                turn_id,
                protected_text(text, limit=40),
            )
            return True
        except Exception:
            logger.exception("[SPEC-LLM] launch failed")
            return False

    async def resolve(self, final_text: str) -> bool:
        """正式识别文本到达。返回 True 表示投机轮已确认，调用方不要再发送。"""
        try:
            turn_id, spec_text = self._slot_turn_id, self._slot_text
            if not turn_id:
                return False
            age = time.monotonic() - self._slot_at
            self._clear_slot()
            if age > _SLOT_MAX_AGE_S:
                logger.info("[SPEC-LLM] stale slot (%.1fs); discarding turn=%s", age, turn_id)
                await self._safe_discard(turn_id, "stale_slot")
                return False
            if str(final_text or "").strip() != spec_text:
                logger.info(
                    "[SPEC-LLM] text mismatch; discarding turn=%s spec=%r final=%r",
                    turn_id, spec_text[:40], str(final_text or "")[:40],
                )
                await self._safe_discard(turn_id, "text_mismatch")
                return False
            confirmed = bool(await self._confirm(turn_id, reason="asr_final_match"))
            if confirmed:
                logger.info("[SPEC-LLM] confirmed turn=%s (LLM head start banked)", turn_id)
            else:
                # 轮已被作废（打断 / 门控超时）：按未投机处理，正常发送
                logger.info("[SPEC-LLM] confirm no-op (turn already decided) turn=%s", turn_id)
            return confirmed
        except Exception:
            logger.exception("[SPEC-LLM] resolve failed")
            return False

    async def abandon(self, reason: str = "") -> None:
        """监听停止 / 会话收尾：清理未决议的投机槽。绝不抛异常。"""
        await self._drop_slot(reason or "abandoned")

    async def _drop_slot(self, reason: str) -> None:
        try:
            turn_id = self._slot_turn_id
            if not turn_id:
                return
            self._clear_slot()
            logger.info("[SPEC-LLM] dropping pending turn=%s reason=%s", turn_id, reason)
            await self._safe_discard(turn_id, reason)
        except Exception:
            logger.debug("speculative slot drop failed", exc_info=True)

    async def _safe_discard(self, turn_id: str, reason: str) -> None:
        try:
            if self._discard is not None:
                await self._discard(turn_id, reason=reason)
        except Exception:
            logger.exception("[SPEC-LLM] discard failed turn=%s", turn_id)

    def _clear_slot(self) -> None:
        self._slot_turn_id = ""
        self._slot_text = ""
        self._slot_at = 0.0

    @property
    def has_pending(self) -> bool:
        return bool(self._slot_turn_id)


# 进程级单例（bootstrap 时 configure）
launcher = SpeculativeTurnLauncher()


def get_speculative_launcher() -> SpeculativeTurnLauncher:
    return launcher
