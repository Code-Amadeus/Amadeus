# -*- coding: utf-8 -*-
"""vts/expression_controller.py — 表情生命周期状态机

职责：
  - 维护当前情绪状态 (current_emotion)
  - 以 sentence_id 为 key 存储待播表情事件
  - PlaybackManager 开始播放某句时调用 on_sentence_start → 触发过渡
  - 协调淡出旧表情 + 淡入新表情（支持交叉重叠）
  - 新轮次开始时调用 on_turn_end → 平滑淡出当前表情

依赖注入（configure() 调用后生效）：
  - vts_manager      VTSConnectionManager 实例
  - registry_path    emotion_presets.json 路径（可选，默认 emotion_presets.json）
"""

import json
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------
_controller: Optional["ExpressionController"] = None


def get_controller() -> "ExpressionController":
    global _controller
    if _controller is None:
        _controller = ExpressionController()
    return _controller


# ---------------------------------------------------------------------------
# ExpressionController
# ---------------------------------------------------------------------------

class ExpressionController:
    """表情状态机：负责所有基于 sentence_id 的延迟触发与过渡管理。"""

    # EMO preset 别名 → 规范化名（与 registry key 对应）
    _ALIASES: dict[str, str] = {
        # smile / happy
        "微笑": "smile", "微笑み": "smile", "happy": "smile", "开心": "smile",
        "喜び": "smile", "高兴": "smile",
        # thinking
        "思考": "thinking", "考え": "thinking", "think": "thinking",
        "思考中": "thinking", "考え中": "thinking",
        # angry
        "生气": "angry", "怒り": "angry", "mad": "angry", "furious": "angry",
        # annoyed（独立情绪，不合并到 angry）
        "烦恼": "annoyed", "烦躁": "annoyed", "不耐烦": "annoyed",
        # disappointed / sad
        "失望": "disappointed", "沮丧": "disappointed", "失落": "disappointed",
        "がっかり": "disappointed", "落ち込む": "disappointed",
        "sad": "sad", "悲伤": "sad", "难过": "sad", "sorrow": "sad",
        # surprised
        "惊讶": "surprised", "surprised": "surprised", "びっくり": "surprised",
        # blush
        "害羞": "blush", "脸红": "blush", "はずかしい": "blush",
        "shy": "shy", "羞涩": "shy", "羞怯": "shy",
        # working
        "working": "working", "作業中": "working", "工作中": "working", "处理中": "working",
        # serious speaking performance (SpriteForge graph mode)
        "serious": "serious_speaking",
        "serious_speaking": "serious_speaking",
        "speaking_serious": "serious_speaking",
        "speaking_seriously": "serious_speaking",
        "seriously_speaking": "serious_speaking",
        "严肃": "serious_speaking",
        "严肃讲话": "serious_speaking",
        "认真": "serious_speaking",
        "认真讲话": "serious_speaking",
        # normal / neutral
        "normal": "normal", "平静": "normal", "普通": "normal", "neutral": "normal",
        # pissed
        "pissed": "pissed", "愤怒": "pissed",
        # winking
        "winking": "winking", "眨眼": "winking",
        # side / thinking side
        "side": "side", "侧": "side",
        "sided_thinking": "sided_thinking",
        "sided_worried": "sided_worried", "担心": "sided_worried",
        "sided_blush": "sided_blush",
        "sided_angry": "sided_angry",
        "sided_pleasant": "sided_pleasant",
        "sided_surprised": "sided_surprised",
        "sided_eyes_closed": "sided_eyes_closed",
    }

    def __init__(self) -> None:
        self._vts_manager = None
        self._render_engine = None   # render bridge/host（可选）
        self._sf_animator = None     # SpriteForgeAnimator（graph 模式）
        self._backend: str = "vts"   # "vts" | "pixi" | "both" | "graph"
        self._registry: dict = {}
        self._sentence_map: dict[str, list] = {}   # sentence_id → [actions]
        self._current_emotion: Optional[str] = None
        self._lock = threading.Lock()
        self._idle_timer: Optional[threading.Timer] = None
        self._fade_out_timer: Optional[threading.Timer] = None

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def set_render_engine(self, engine, backend: str = "both") -> None:
        """绑定 PixiJS 渲染引擎。

        Parameters
        ----------
        engine:   render bridge/host
        backend:  "vts"  — 仅走 VTS（原有行为）
                  "pixi" — 仅走 PixiJS 渲染引擎
                  "both" — VTS + PixiJS 同时驱动
        """
        self._render_engine = engine
        self._backend = backend
        logger.info(f"[ExprCtrl] render engine bound, backend={backend!r}")

    def set_animator(self, animator, backend: str = "graph") -> None:
        """绑定 SpriteForgeAnimator，切换到 graph 状态机模式。

        backend="graph": LLM 表情指令路由到 animator.trigger_expression()，
        VTS 完全旁路，render host 由 animator 内部管理。
        """
        self._sf_animator = animator
        self._backend = backend
        logger.info(f"[ExprCtrl] SpriteForgeAnimator bound, backend={backend!r}")

    def configure(self, vts_manager=None, registry_path: str = "emotion_presets.json") -> None:
        if vts_manager is not None:
            self._vts_manager = vts_manager
            # VTS重连后自动恢复表情状态
            vts_manager.on_reconnect_callback = self._on_vts_reconnect
        self.load_registry(registry_path)

    def load_registry(self, path: str) -> None:
        if not os.path.isabs(path):
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(base, path)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._registry = {k: v for k, v in data.items() if not k.startswith("_")}
            logger.info(f"[ExprCtrl] loaded emotion registry: {len(self._registry)} entries ({path})")
        except FileNotFoundError:
            logger.warning(f"[ExprCtrl] registry not found: {path}; using an empty registry")
            self._registry = {}
        except Exception as e:
            logger.error(f"[ExprCtrl] failed to load registry: {e}")
            self._registry = {}

    # ------------------------------------------------------------------
    # 句子表情事件注册（LLM 流式解析时调用）
    # ------------------------------------------------------------------

    def register_sentence_actions(self, sentence_id: str, actions: list) -> None:
        """将解析到的表情 actions 与 sentence_id 绑定，等待播放时触发。"""
        if not actions:
            return
        with self._lock:
            self._sentence_map[sentence_id] = actions
        logger.debug(f"[ExprCtrl] registered {len(actions)} expression event(s) -> {sentence_id}")

    # ------------------------------------------------------------------
    # 播放回调（PlaybackManager 开始播某句时调用）
    # ------------------------------------------------------------------

    def on_sentence_start(self, sentence_id: str) -> None:
        """PlaybackManager calls this when a sentence starts.

        In graph mode, dispatch queued EMO actions before entering speaking so
        serious_speaking can override the default idle-speaking performance.
        """
        with self._lock:
            actions = self._sentence_map.pop(sentence_id, [])
        if actions:
            logger.info(
                f"[ExprCtrl] sentence {sentence_id} start, "
                f"dispatch {len(actions)} expression action(s)"
            )
            for act in actions:
                self._dispatch_action(act)

        if self._backend == "graph" and self._sf_animator is not None:
            self._sf_animator.on_speaking(True)
        elif self._render_engine is not None and self._backend in ("pixi", "both"):
            try:
                self._render_engine.set_speaking(True)
            except Exception as e:
                logger.error(f"[ExprCtrl] render_engine.set_speaking(True) failed: {e}")

    def _dispatch_action(self, act: dict) -> None:
        atype = (act.get("type") or "").upper()
        attrs = act.get("attrs", {})

        if atype == "EMO":
            preset = attrs.get("preset") or ""
            emotion = self._ALIASES.get(preset.lower(), preset.lower()) or preset
            if emotion:
                if self._backend == "graph" and self._sf_animator is not None:
                    if emotion == "disappointed":
                        emotion = "sad"
                    # graph 模式：neutral/normal 静默忽略，其余触发状态机
                    if emotion not in ("neutral", "normal"):
                        self._sf_animator.trigger_expression(emotion)
                else:
                    self.transition_to(emotion)

        elif atype in ("EXPR", "PARAM", "HOTKEY"):
            # 非 EMO 动作：走原有 record_actions 路径（避免重复导入，延迟绑定）
            try:
                from vts.action import record_actions
                record_actions([act])
            except Exception as e:
                logger.error(f"[ExprCtrl] record_actions failed: {e}")

    # ------------------------------------------------------------------
    # 状态机核心：过渡
    # ------------------------------------------------------------------

    def transition_to(self, emotion_name: str) -> None:
        """协调淡出旧表情 + 淡入新表情。"""
        cfg = self._registry.get(emotion_name)
        if cfg is None:
            logger.warning(f"[ExprCtrl] unknown emotion '{emotion_name}'; skipping transition")
            return

        with self._lock:
            prev_emotion = self._current_emotion
            # 取消所有待执行的定时任务
            self._cancel_timers_locked()
            self._current_emotion = emotion_name

        fade_in = cfg.get("fade_in_sec", 0.3)
        overlap = cfg.get("transition_overlap_sec", 0.2)

        if prev_emotion and prev_emotion != emotion_name:
            prev_cfg = self._registry.get(prev_emotion, {})
            fade_out = prev_cfg.get("fade_out_sec", 0.5)
            # 先淡出旧表情
            self._fade_out_emotion(prev_emotion, fade_out)
            # 淡入新表情在 overlap 之后（交叉溶解）
            delay_in = max(0.0, fade_out - overlap)
            t = threading.Timer(delay_in, self._fade_in_emotion, args=(emotion_name, fade_in, cfg))
            with self._lock:
                self._fade_out_timer = t
            t.start()
        else:
            # 无旧表情，直接淡入
            self._fade_in_emotion(emotion_name, fade_in, cfg)

        logger.info(f"[ExprCtrl] transition: {prev_emotion!r} -> {emotion_name!r}")

    def _fade_in_emotion(self, emotion_name: str, fade_in: float, cfg: dict) -> None:
        """执行淡入：激活 expression + 推送 params，并按需调度自动回 idle 计时器。"""
        # PixiJS 渲染引擎路由
        if self._render_engine is not None and self._backend in ("pixi", "both"):
            try:
                self._render_engine.set_emotion(emotion_name)
                clip_cfg = cfg.get("sprite_clip")
                if isinstance(clip_cfg, dict) and hasattr(self._render_engine, "set_sprite_clip_config"):
                    self._render_engine.set_sprite_clip_config(emotion_name, clip_cfg)
            except Exception as e:
                logger.error(f"[ExprCtrl] render_engine.set_emotion failed: {e}")

        if self._vts_manager is None or self._backend == "pixi":
            # 纯 pixi 模式跳过 VTS，但仍需调度 idle 回归
            if cfg.get("auto_return_to_idle", False):
                delay = cfg.get("idle_return_delay_sec", 2.0)
                fade_out = cfg.get("fade_out_sec", 0.5)
                t = threading.Timer(delay, self._auto_return_to_idle, args=(emotion_name, fade_out))
                with self._lock:
                    self._idle_timer = t
                t.start()
            return
        for expr in cfg.get("expressions", []):
            try:
                self._vts_manager.activate_expression(expr, active=True, fade_time=fade_in)
            except Exception as e:
                logger.error(f"[ExprCtrl] activate_expression({expr}) failed: {e}")
        params = cfg.get("params", {})
        if params:
            try:
                self._vts_manager.send_parameters(params)
            except Exception as e:
                logger.error(f"[ExprCtrl] send_parameters failed: {e}")

        # 调度自动回 idle（仅 auto_return_to_idle=true 的情绪）
        if cfg.get("auto_return_to_idle", False):
            delay = cfg.get("idle_return_delay_sec", 2.0)
            fade_out = cfg.get("fade_out_sec", 0.5)
            t = threading.Timer(delay, self._auto_return_to_idle, args=(emotion_name, fade_out))
            with self._lock:
                self._idle_timer = t
            t.start()
            logger.debug(f"[ExprCtrl] scheduled automatic idle return: {emotion_name} after {delay}s")

    def _fade_out_emotion(self, emotion_name: str, fade_out: float) -> None:
        """淡出指定情绪的所有 expressions。"""
        if self._vts_manager is None:
            return
        cfg = self._registry.get(emotion_name, {})
        for expr in cfg.get("expressions", []):
            try:
                self._vts_manager.activate_expression(expr, active=False, fade_time=fade_out)
            except Exception as e:
                logger.error(f"[ExprCtrl] deactivate_expression({expr}) failed: {e}")
        # params 回零
        params = cfg.get("params", {})
        if params:
            zero = {k: 0.0 for k in params}
            try:
                self._vts_manager.send_parameters(zero)
            except Exception:
                pass

    def _auto_return_to_idle(self, emotion_name: str, fade_out: float) -> None:
        """auto_return_to_idle 定时器触发：淡出指定情绪并清空状态。"""
        with self._lock:
            if self._current_emotion != emotion_name:
                return  # 已经被别的过渡取代，不做操作
            self._current_emotion = None
            self._idle_timer = None
        self._fade_out_emotion(emotion_name, fade_out)
        logger.info(f"[ExprCtrl] automatic idle return: {emotion_name}")

    # ------------------------------------------------------------------
    # VTS 重连恢复
    # ------------------------------------------------------------------

    def _on_vts_reconnect(self) -> None:
        """VTS WebSocket 重连成功后调用：将当前情绪重新推送给 VTS。

        VTS 断线后会丢失所有表情激活状态，重连后需要重新 activate。
        若当前处于 idle（无表情），则不做任何操作。
        """
        with self._lock:
            emotion = self._current_emotion
        if not emotion:
            logger.info("[ExprCtrl] VTS reconnected: no active expression to restore")
            return
        cfg = self._registry.get(emotion)
        if cfg is None:
            logger.warning(f"[ExprCtrl] VTS reconnected: emotion {emotion!r} is not in the registry; skipping restore")
            return
        fade_in = cfg.get("fade_in_sec", 0.3)
        logger.info(f"[ExprCtrl] VTS reconnected; restoring expression: {emotion!r} (fade_in={fade_in}s)")
        self._fade_in_emotion(emotion, fade_in, cfg)

    # ------------------------------------------------------------------
    # 轮次生命周期
    # ------------------------------------------------------------------

    def on_turn_end(self, preserve_emotion: bool = False) -> None:
        """新一轮对话开始时调用：默认淡出当前表情；可选择保留当前表情。"""
        # 停止口型动画
        # graph 路径与 on_sentence_start 保持对称：不依赖 _render_engine，
        # 壁纸模式下 set_animator() 不会设置 _render_engine，旧守卫会导致永不停止。
        if self._backend == "graph" and self._sf_animator is not None:
            try:
                self._sf_animator.on_speaking(False)
            except Exception as e:
                logger.error(f"[ExprCtrl] sf_animator.on_speaking(False) failed: {e}")
        elif self._render_engine is not None and self._backend in ("pixi", "both"):
            try:
                self._render_engine.set_speaking(False)
            except Exception as e:
                logger.error(f"[ExprCtrl] render_engine.set_speaking(False) failed: {e}")
        with self._lock:
            emotion = self._current_emotion
            if not preserve_emotion:
                self._current_emotion = None
            self._cancel_timers_locked()
            self._sentence_map.clear()

        if emotion and not preserve_emotion:
            cfg = self._registry.get(emotion, {})
            fade_out = cfg.get("fade_out_sec", 0.4)
            self._fade_out_emotion(emotion, fade_out)
            logger.info(f"[ExprCtrl] turn ended; fading out {emotion!r} (fade={fade_out}s)")
        elif emotion and preserve_emotion:
            logger.info(f"[ExprCtrl] turn ended; keeping expression {emotion!r}")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _cancel_timers_locked(self) -> None:
        """取消所有待执行定时器（调用前必须持有 _lock）。"""
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        if self._fade_out_timer is not None:
            self._fade_out_timer.cancel()
            self._fade_out_timer = None

    @property
    def current_emotion(self) -> Optional[str]:
        return self._current_emotion
