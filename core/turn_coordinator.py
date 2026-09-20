"""Turn lifecycle coordinator — runtime convergence plan Phase 2 (observation).

一个小状态账本，不是 kernel：

- 记录 会话/轮次/ASR 模式/输出模式 的状态转换，镜像各子系统上报的 epoch；
- 校验一组具体的不变量，违规只告警和计数，**从不抛异常、从不阻塞热路径**；
- 提供 snapshot() 给 runtime.status 和调试取证（含最近转换环形缓冲）。

Phase 2 边界（重要）：
- 本模块**不拥有**任何 epoch 或状态——chat_epoch 仍属 chat_handler，
  TTS interrupt epoch 仍属 tts.pipeline，playback epoch 仍属 PlaybackManager。
  各所有者在状态变化点调用 on_* 上报；账本只记录与校验。
- 所有 on_* 方法是同步、线程安全、O(1) 的，可以从任意线程（playback 线程、
  asyncio 事件循环、ASR 监听线程）直接调用。
- 位置在 core/ 而非 server/：tts/、asr/ 模块需要上报，不能反向依赖 server 层。

所有权迁移（epoch 由账本发放）是后续阶段的工作。
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque
from threading import Event, Lock
from typing import Any

from config.settings import PENDING_TURN_GATE_TIMEOUT_S

logger = logging.getLogger("turn_coordinator")

# ── 模式枚举（与收敛计划一致）──────────────────────────────────────────────

ASR_SLEEPING = "sleeping"
ASR_WAKE_LISTENING = "wake_listening"
ASR_QWEN_HOT = "qwen_hot"
ASR_LISTENING = "listening"          # 非唤醒来源的主动监听（手动 / VN）

OUT_IDLE = "idle"
OUT_LLM_STREAMING = "llm_streaming"
OUT_PLAYING = "playing"
OUT_INTERRUPTED = "interrupted"

# 轮次决议状态（pending-turn）
TURN_PENDING = "pending"
TURN_CONFIRMED = "confirmed"
TURN_DISCARDED = "discarded"

# TTS 出队门控裁决
GATE_PROCEED = "proceed"
GATE_WAIT = "wait"
GATE_DROP = "drop"

_TURN_STATE_CAP = 64


class TurnCoordinator:
    def __init__(self) -> None:
        self._lock = Lock()
        # 轮次身份
        self._session_id = ""
        self._active_turn_id = ""
        self._turn_source = ""
        # 镜像 epoch（所有者上报，账本不发放）
        self._chat_epoch = -1
        self._tts_epoch = -1
        self._playback_epoch = -1
        # 模式
        self._asr_mode = ASR_SLEEPING
        self._output_mode = OUT_IDLE
        self._wake_running = False
        # 复合打断括号深度：>0 时子事件（chat_aborted / tts_interrupted）
        # 不重复累加 interrupts 计数——整个括号算一次打断
        self._interrupt_depth = 0
        # 轮次决议状态（pending-turn 语义，切片 D1）：
        # turn_id -> "pending" | "confirmed" | "discarded"
        # 有界（_TURN_STATE_CAP），插入序淘汰；被淘汰的旧轮按 confirmed 对待。
        self._turn_states: OrderedDict[str, str] = OrderedDict()
        # pending 轮的决议事件：confirm/discard 时置位，供 TTS 门控线程等待
        self._turn_events: dict[str, Event] = {}
        # per-turn 播放完成事件：最后一句播完、打断、作废或淘汰时置位。
        self._playback_events: OrderedDict[str, Event] = OrderedDict()
        # provider
        self._provider_run_id = ""
        # 取证
        self._transitions: deque[dict[str, Any]] = deque(maxlen=80)
        self._violations: deque[dict[str, Any]] = deque(maxlen=30)
        self._counters: dict[str, int] = {
            "turns_started": 0,
            "turns_completed": 0,
            "turns_failed": 0,
            "turns_discarded": 0,
            "interrupts": 0,
            "stale_drops": 0,
            "violations": 0,
        }
        self._seen_any_event = False

    # ── 内部记录 ─────────────────────────────────────────────────────────────

    def _record(self, event: str, **fields: Any) -> None:
        entry = {
            "ts": round(time.time(), 3),
            "event": event,
            "asr_mode": self._asr_mode,
            "output_mode": self._output_mode,
            **{k: v for k, v in fields.items() if v not in (None, "")},
        }
        self._transitions.append(entry)
        self._seen_any_event = True
        logger.debug("[TURN-COORD] %s %s", event, fields)

    def _violation(self, rule: str, detail: str, **fields: Any) -> None:
        entry = {
            "ts": round(time.time(), 3),
            "rule": rule,
            "detail": detail,
            "asr_mode": self._asr_mode,
            "output_mode": self._output_mode,
            **fields,
        }
        self._violations.append(entry)
        self._counters["violations"] += 1
        logger.warning("[TURN-COORD] invariant violation rule=%s %s %s", rule, detail, fields)

    # ── chat 轮次生命周期 ────────────────────────────────────────────────────

    def open_turn(
        self,
        *,
        turn_id: str,
        local_next_epoch: int,
        session_id: str = "",
        source: str = "",
        pending: bool = False,
    ) -> dict[str, Any]:
        """申领一个 chat 轮次（所有权迁移·切片 C / D1）。

        原子完成：重叠轮次校验 → 发放 chat epoch → 登记轮次身份 →
        output_mode 进入 llm_streaming。返回 {"turn_id", "chat_epoch", "pending"}。

        pending=True 开出投机轮次：LLM 流照常运行、句子照常入队，但该轮的
        TTS 条目会被出队门控（turn_gate）扣住，直到 confirm_turn / discard_turn
        决议。绝不抛异常；账本不可用时调用方回退本地自增（旧行为）。
        """
        try:
            with self._lock:
                if (
                    self._active_turn_id
                    and self._active_turn_id != turn_id
                    and self._output_mode in (OUT_LLM_STREAMING, OUT_PLAYING)
                ):
                    self._violation(
                        "overlapping_turns",
                        "new chat turn opened while previous turn still active",
                        prev_turn=self._active_turn_id, new_turn=turn_id,
                    )
                issued = self._issue_epoch("chat", self._chat_epoch, local_next_epoch, source)
                self._chat_epoch = issued
                self._active_turn_id = str(turn_id or "")
                self._turn_source = str(source or "")
                if session_id:
                    self._session_id = str(session_id)
                self._output_mode = OUT_LLM_STREAMING
                self._counters["turns_started"] += 1
                if turn_id:
                    self._set_turn_state(
                        str(turn_id), TURN_PENDING if pending else TURN_CONFIRMED
                    )
                    self._open_playback_event_locked(str(turn_id))
                self._record(
                    "turn_opened", turn_id=turn_id, source=source,
                    chat_epoch=issued, pending=pending or None,
                )
                return {"turn_id": str(turn_id or ""), "chat_epoch": issued, "pending": bool(pending)}
        except Exception:
            logger.debug("open_turn failed; caller falls back to local epoch", exc_info=True)
            return {"turn_id": str(turn_id or ""), "chat_epoch": int(local_next_epoch), "pending": False}

    # ── 轮次决议（pending-turn 语义，切片 D1）────────────────────────────────

    def _set_turn_state(self, turn_id: str, state: str) -> None:
        """内部：必须已持有 self._lock。"""
        self._turn_states[turn_id] = state
        self._turn_states.move_to_end(turn_id)
        if state == TURN_PENDING:
            self._turn_events.setdefault(turn_id, Event())
        while len(self._turn_states) > _TURN_STATE_CAP:
            evicted, _ = self._turn_states.popitem(last=False)
            ev = self._turn_events.pop(evicted, None)
            if ev is not None:
                ev.set()  # 兜底：绝不让等待者悬死在被淘汰的轮上
            playback_ev = self._playback_events.pop(evicted, None)
            if playback_ev is not None:
                playback_ev.set()  # 兜底：播放完成等待者同样不能悬死

    def _open_playback_event_locked(self, turn_id: str) -> None:
        """内部：必须已持有 self._lock。为新轮登记未完成的播放事件。"""
        if not turn_id:
            return
        old = self._playback_events.get(turn_id)
        if old is not None:
            old.set()
        self._playback_events[turn_id] = Event()
        self._playback_events.move_to_end(turn_id)
        while len(self._playback_events) > _TURN_STATE_CAP:
            _, evicted_event = self._playback_events.popitem(last=False)
            evicted_event.set()

    def _set_playback_event_locked(self, turn_id: str) -> None:
        """内部：必须已持有 self._lock。未知轮次按已完成处理。"""
        if not turn_id:
            return
        ev = self._playback_events.get(turn_id)
        if ev is not None:
            ev.set()

    def _set_all_playback_events_locked(self) -> None:
        """内部：必须已持有 self._lock。打断后旧轮不会再产生播放完成回调。"""
        for ev in self._playback_events.values():
            ev.set()

    def _discard_all_pending_locked(self, reason: str) -> None:
        """内部：必须已持有 self._lock。打断时作废全部 pending 轮，
        使"打断后无旧作业"不变量覆盖投机轮次。"""
        for tid, state in list(self._turn_states.items()):
            if state != TURN_PENDING:
                continue
            self._turn_states[tid] = TURN_DISCARDED
            self._counters["turns_discarded"] = self._counters.get("turns_discarded", 0) + 1
            ev = self._turn_events.get(tid)
            if ev is not None:
                ev.set()
            self._set_playback_event_locked(tid)
            self._record("turn_discarded", turn_id=tid, reason=reason)

    def _decide_turn(self, turn_id: str, state: str, reason: str) -> bool:
        try:
            with self._lock:
                current = self._turn_states.get(turn_id)
                if current != TURN_PENDING:
                    # 幂等：重复决议 / 对非 pending 轮决议均为 no-op
                    self._record(
                        "turn_decision_ignored", turn_id=turn_id,
                        requested=state, current=current or "unknown",
                    )
                    return False
                self._set_turn_state(turn_id, state)
                if state == TURN_DISCARDED:
                    self._counters["turns_discarded"] = (
                        self._counters.get("turns_discarded", 0) + 1
                    )
                    self._set_playback_event_locked(turn_id)
                    if self._active_turn_id == turn_id:
                        self._active_turn_id = ""
                        if self._output_mode == OUT_LLM_STREAMING:
                            self._output_mode = OUT_IDLE
                ev = self._turn_events.get(turn_id)
                if ev is not None:
                    ev.set()
                self._record(f"turn_{state}", turn_id=turn_id, reason=reason)
                return True
        except Exception:
            logger.debug("turn decision failed", exc_info=True)
            return False

    def confirm_turn(self, turn_id: str, *, reason: str = "") -> bool:
        """pending 轮确认：TTS 门控放行该轮全部条目。"""
        return self._decide_turn(str(turn_id or ""), TURN_CONFIRMED, reason)

    def discard_turn(self, turn_id: str, *, reason: str = "") -> bool:
        """pending 轮作废：该轮全部 TTS 条目在出队点被丢弃（静默，无打断标注）。"""
        return self._decide_turn(str(turn_id or ""), TURN_DISCARDED, reason)

    def wait_turn_playback_complete(self, turn_id: str, timeout: float = 30.0) -> bool:
        """阻塞等待指定轮次的账本播放完成事件。

        同步、线程安全；ChatRuntime 经 asyncio.to_thread 调用，不阻塞事件循环。
        未知 / 空 turn_id 视为已经完成，避免旧调用方因缺少轮次账本而悬死。
        """
        try:
            turn_id = str(turn_id or "")
            if not turn_id:
                return True
            with self._lock:
                ev = self._playback_events.get(turn_id)
            if ev is None:
                return True
            return bool(ev.wait(timeout=max(0.0, float(timeout))))
        except Exception:
            logger.debug("wait_turn_playback_complete failed; treating as complete", exc_info=True)
            return True

    def turn_gate(self, turn_id: str) -> str:
        """TTS 出队门控裁决。

        未知 / 空 turn_id 一律放行——VN、live、legacy 条目没有轮次概念，
        绝不能被 pending 机制扣住。绝不抛异常，异常时放行（保守方向：
        门控失效退化为无门控，行为等于旧管线）。
        """
        try:
            if not turn_id:
                return GATE_PROCEED
            with self._lock:
                state = self._turn_states.get(str(turn_id))
            if state == TURN_PENDING:
                return GATE_WAIT
            if state == TURN_DISCARDED:
                return GATE_DROP
            return GATE_PROCEED
        except Exception:
            return GATE_PROCEED

    def wait_turn_decided(
        self,
        turn_id: str,
        timeout: float = PENDING_TURN_GATE_TIMEOUT_S,
    ) -> str:
        """阻塞等待 pending 轮决议，返回等待后的门控裁决。

        同步、线程安全；TTS worker 经 asyncio.to_thread 调用，
        不阻塞事件循环。超时仍 pending 时返回 GATE_WAIT，由调用方决策。
        """
        try:
            with self._lock:
                ev = self._turn_events.get(str(turn_id))
            if ev is not None:
                ev.wait(timeout=max(0.0, float(timeout)))
            return self.turn_gate(turn_id)
        except Exception:
            return GATE_PROCEED

    def on_chat_turn_started(self, *, turn_id: str, session_id: str = "",
                             chat_epoch: int | None = None, source: str = "") -> None:
        """观察期遗留接口：事后上报轮次开始。

        server 主路径已改用 open_turn() 申领；本方法保留给尚未迁移的
        上报方（无）与测试。新代码请用 open_turn。
        """
        try:
            with self._lock:
                if (
                    self._active_turn_id
                    and self._active_turn_id != turn_id
                    and self._output_mode in (OUT_LLM_STREAMING, OUT_PLAYING)
                ):
                    # 新轮开始时旧轮仍活跃且未观察到打断/完成——重叠轮次
                    self._violation(
                        "overlapping_turns",
                        "new chat turn started while previous turn still active",
                        prev_turn=self._active_turn_id, new_turn=turn_id,
                    )
                self._active_turn_id = str(turn_id or "")
                self._turn_source = str(source or "")
                if session_id:
                    self._session_id = str(session_id)
                if chat_epoch is not None:
                    self._chat_epoch = int(chat_epoch)
                self._output_mode = OUT_LLM_STREAMING
                self._counters["turns_started"] += 1
                if turn_id:
                    self._open_playback_event_locked(str(turn_id))
                self._record("chat_turn_started", turn_id=turn_id, source=source,
                             chat_epoch=chat_epoch)
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    def on_chat_turn_finished(self, *, turn_id: str, ok: bool = True) -> None:
        try:
            with self._lock:
                stale = bool(self._active_turn_id and turn_id != self._active_turn_id)
                if not stale:
                    self._active_turn_id = ""
                    if self._output_mode in (OUT_LLM_STREAMING, OUT_PLAYING):
                        self._output_mode = OUT_IDLE
                self._counters["turns_completed" if ok else "turns_failed"] += 1
                self._record("chat_turn_finished", turn_id=turn_id, ok=ok, stale=stale)
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    def on_chat_aborted(self, *, turn_id: str = "") -> None:
        try:
            with self._lock:
                self._output_mode = OUT_INTERRUPTED
                if self._interrupt_depth == 0:
                    self._counters["interrupts"] += 1
                self._record("chat_aborted", turn_id=turn_id)
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    # ── epoch 发放（所有权迁移·切片 B）────────────────────────────────────────
    #
    # 账本成为三个 epoch 的发放方；各所有者的计数变量保留为热路径只读缓存
    # （合成/播放循环每 chunk 检查 epoch，绝不能变成跨模块锁读）。
    # 调用方传入 local_next（本地缓存的下一个值）用于双轨连续性校验：
    # - 账本未观察过该 epoch（mirror<0）→ 采纳 local_next 完成初始化对齐；
    # - local_next == mirror+1 → 正常发放；
    # - 分歧 → 记违规并发放 max(两者)。取大是安全方向：更大的 epoch 只会
    #   多失效旧作业，绝不会让已打断的作业复活。
    # 发放方法绝不抛异常；调用方在账本不可用时回退本地自增（旧行为）。

    def _issue_epoch(self, kind: str, mirror: int, local_next: int, source: str) -> int:
        local_next = int(local_next)
        if mirror < 0:
            self._record(f"{kind}_epoch_issued", value=local_next, seeded=True, source=source)
            return local_next
        expected = mirror + 1
        if local_next != expected:
            issued = max(local_next, expected)
            self._violation(
                "epoch_divergence",
                f"{kind} epoch local_next={local_next} ledger_expected={expected}; issued={issued}",
                source=source,
            )
            self._record(f"{kind}_epoch_issued", value=issued, source=source)
            return issued
        self._record(f"{kind}_epoch_issued", value=expected, source=source)
        return expected

    def advance_chat_epoch(self, *, local_next: int, source: str = "") -> int:
        try:
            with self._lock:
                issued = self._issue_epoch("chat", self._chat_epoch, local_next, source)
                self._chat_epoch = issued
                return issued
        except Exception:
            logger.debug("epoch issuance failed; falling back to local", exc_info=True)
            return int(local_next)

    def advance_tts_epoch(self, *, local_next: int, source: str = "") -> int:
        try:
            with self._lock:
                issued = self._issue_epoch("tts", self._tts_epoch, local_next, source)
                self._tts_epoch = issued
                return issued
        except Exception:
            logger.debug("epoch issuance failed; falling back to local", exc_info=True)
            return int(local_next)

    def advance_playback_epoch(self, *, local_next: int, source: str = "") -> int:
        try:
            with self._lock:
                issued = self._issue_epoch("playback", self._playback_epoch, local_next, source)
                self._playback_epoch = issued
                return issued
        except Exception:
            logger.debug("epoch issuance failed; falling back to local", exc_info=True)
            return int(local_next)

    # ── 复合打断括号（interrupt_flow 编排器上报）──────────────────────────────

    def on_interrupt_begin(self, *, source: str = "") -> None:
        """复合打断开始：chat abort + TTS/playback 打断作为一次原子事件记账。"""
        try:
            with self._lock:
                self._interrupt_depth += 1
                self._output_mode = OUT_INTERRUPTED
                self._counters["interrupts"] += 1
                self._discard_all_pending_locked(f"interrupted:{source or 'unknown'}")
                self._set_all_playback_events_locked()
                self._record("interrupt_begin", source=source)
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    def on_interrupt_end(self, *, source: str = "") -> None:
        try:
            with self._lock:
                self._interrupt_depth = max(0, self._interrupt_depth - 1)
                self._record(
                    "interrupt_end",
                    source=source,
                    epochs=f"chat={self._chat_epoch},tts={self._tts_epoch},playback={self._playback_epoch}",
                )
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    # ── TTS / 播放（tts.pipeline / tts.playback 上报）────────────────────────

    def on_tts_interrupted(self, *, tts_epoch: int, source: str = "") -> None:
        try:
            with self._lock:
                self._tts_epoch = int(tts_epoch)
                self._output_mode = OUT_INTERRUPTED
                if self._interrupt_depth == 0:
                    self._counters["interrupts"] += 1
                    # 独立打断（非复合括号）同样要作废全部 pending 轮；
                    # 括号路径已在 on_interrupt_begin 处理
                    self._discard_all_pending_locked(f"tts_interrupted:{source or 'standalone'}")
                self._set_all_playback_events_locked()
                self._record("tts_interrupted", tts_epoch=tts_epoch, source=source)
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    def on_playback_interrupted(self, *, playback_epoch: int) -> None:
        try:
            with self._lock:
                self._playback_epoch = int(playback_epoch)
                self._output_mode = OUT_INTERRUPTED
                self._set_all_playback_events_locked()
                self._record("playback_interrupted", playback_epoch=playback_epoch)
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    def on_sentence_playback_started(self, *, sentence_id: str = "") -> None:
        try:
            with self._lock:
                if self._output_mode == OUT_INTERRUPTED:
                    # 打断后没有新轮开始，却有音频开始播——旧 epoch 泄漏。
                    # 违规的播放不获得模式所有权：保持 interrupted，
                    # 避免陈旧泄漏"复活"已打断的轮次、污染后续判定。
                    self._violation(
                        "playback_after_interrupt",
                        "sentence playback started while output_mode=interrupted",
                        sentence_id=sentence_id,
                    )
                else:
                    self._output_mode = OUT_PLAYING
                self._record("sentence_playback_started", sentence_id=sentence_id)
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    def on_sentence_playback_complete(self, *, sentence_id: str = "") -> None:
        """Release sentence-scoped playback, including Observer narration.

        Chat turns also have a turn-level completion marker.  Work/Observer speech
        does not, so sentence completion is the common lifecycle boundary that
        prevents the coordinator from remaining in ``playing`` indefinitely.
        """
        try:
            with self._lock:
                if self._output_mode == OUT_PLAYING:
                    self._output_mode = (
                        OUT_LLM_STREAMING if self._active_turn_id else OUT_IDLE
                    )
                self._record(
                    "sentence_playback_complete",
                    sentence_id=sentence_id,
                )
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    def on_turn_playback_complete(self, turn_id: str | None = None) -> None:
        try:
            with self._lock:
                reported_turn_id = str(turn_id or self._active_turn_id)
                is_active_turn = reported_turn_id == self._active_turn_id
                self._set_playback_event_locked(reported_turn_id)
                if is_active_turn:
                    if self._output_mode not in (OUT_PLAYING, OUT_LLM_STREAMING):
                        self._violation(
                            "unexpected_playback_complete",
                            f"turn playback complete while output_mode={self._output_mode}",
                        )
                    self._output_mode = OUT_IDLE
                self._record(
                    "turn_playback_complete",
                    turn_id=reported_turn_id,
                    stale=not is_active_turn,
                )
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    def on_stale_dropped(self, *, kind: str) -> None:
        """epoch 机制成功拦截了一个陈旧条目（这是系统在正确工作，非违规）。"""
        try:
            with self._lock:
                self._counters["stale_drops"] += 1
                self._record("stale_dropped", kind=kind)
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    # ── ASR / 唤醒（asr_handler / wake_handler / app 上报）───────────────────

    def on_wake_listening(self, *, running: bool) -> None:
        try:
            with self._lock:
                self._wake_running = bool(running)
                if self._asr_mode in (ASR_SLEEPING, ASR_WAKE_LISTENING):
                    self._asr_mode = ASR_WAKE_LISTENING if running else ASR_SLEEPING
                self._record("wake_listening", running=running)
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    def on_wake_detected(self) -> None:
        try:
            with self._lock:
                self._record("wake_detected")
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    def on_asr_listening(self, *, source: str = "", hot_window: bool = False) -> None:
        try:
            with self._lock:
                self._asr_mode = ASR_QWEN_HOT if (hot_window or source == "wake") else ASR_LISTENING
                self._record("asr_listening", source=source, hot_window=hot_window)
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    def on_asr_stopped(self, *, reason: str = "") -> None:
        try:
            with self._lock:
                self._asr_mode = ASR_WAKE_LISTENING if self._wake_running else ASR_SLEEPING
                self._record("asr_stopped", reason=reason)
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    # ── provider（provider_runtime 上报，可选）────────────────────────────────

    def on_provider_run(self, *, run_id: str, status: str = "") -> None:
        try:
            with self._lock:
                final = str(status or "").lower() in {
                    "completed", "failed", "cancelled", "canceled", "error", "done",
                }
                self._provider_run_id = "" if final else str(run_id or "")
                self._record("provider_run", run_id=run_id, status=status)
        except Exception:
            logger.debug("coordinator notify failed", exc_info=True)

    # ── 快照 ─────────────────────────────────────────────────────────────────

    @property
    def initialized(self) -> bool:
        """是否已观察到任何事件（runtime_status 据此决定用账本还是启发式）。"""
        return self._seen_any_event

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            pending_turns = [
                tid for tid, state in self._turn_states.items() if state == TURN_PENDING
            ]
            return {
                "initialized": self._seen_any_event,
                "session_id": self._session_id,
                "active_turn_id": self._active_turn_id,
                "pending_turns": pending_turns,
                "turn_source": self._turn_source,
                "asr_mode": self._asr_mode,
                "output_mode": self._output_mode,
                "wake_running": self._wake_running,
                "epochs": {
                    "chat": self._chat_epoch,
                    "tts": self._tts_epoch,
                    "playback": self._playback_epoch,
                },
                "provider_run_id": self._provider_run_id,
                "counters": dict(self._counters),
                "recent_transitions": list(self._transitions)[-20:],
                "violations": list(self._violations),
            }


# 进程级单例
coordinator = TurnCoordinator()


def get_turn_coordinator() -> TurnCoordinator:
    return coordinator
