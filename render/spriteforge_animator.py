# -*- coding: utf-8 -*-
"""render/spriteforge_animator.py

SpriteForge graph state machine -> Amadeus render-host adapter.

Usage::

    animator = SpriteForgeAnimator(engine)
    animator.start()
    animator.stop()
    animator.trigger_expression("smile")   # LLM call
    animator.on_speaking(True)             # TTS started
    animator.on_speaking(False)            # TTS ended
"""
from __future__ import annotations

import logging
import random
import threading
import time
from pathlib import Path

from config.asset_paths import (
    SPRITEFORGE_GRAPH_CONFIG,
    SPRITEFORGE_MOUTH_CONFIG,
    SPRITEFORGE_RUNTIME_MANIFEST,
    SPRITEFORGE_RUNTIME_ROOT,
)
from render.character_pack import CharacterPack, CharacterPackError, load_character_pack
from render.spriteforge_intent import (
    RANDOM_TRIGGER_ROUTES as _RANDOM_TRIGGER_ROUTES,
    TRIGGER_ALIASES as _TRIGGER_ALIASES,
    resolve_spriteforge_trigger_label,
)

logger = logging.getLogger(__name__)

GRAPH_CONFIG_PATH = SPRITEFORGE_GRAPH_CONFIG
MOUTH_CONFIG_PATH = SPRITEFORGE_MOUTH_CONFIG
RUNTIME_MANIFEST_PATH = SPRITEFORGE_RUNTIME_MANIFEST

_SPEAKING_TRIGGER_LABEL = "speaking_trans"
_DEFAULT_SPEAKING_TRIGGER_LABEL = "speaking_short"
_CLOSED_EYE_SPEAKING_TRIGGER_LABEL = "closed_eye_trans"
_CLOSED_EYE_SPEAKING_CHANCE = 0.18
_SERIOUS_SPEAKING_LABELS = {"speaking_trans", "speaking_loop1", "speaking_loop2"}
_DEFAULT_SPEAKING_LABELS = {"speaking_short", "speaking_med", "speaking_long"}
_CLOSED_EYE_SPEAKING_LABELS = {
    "closed_eye_trans",
    "speaking_closed_eye_1",
    "speaking_closed_eye_2",
}
_CLOSED_EYE_SPEAKING_ELIGIBLE_LABELS = {
    "idle",
    "idle1",
    "idle2",
    "idle_mic_wind",
    "idle_str_wind",
    "idle_closed_eye",
    *_DEFAULT_SPEAKING_LABELS,
}
_THINKING_SPEAKING_LABELS = {"thinking_speaking1", "thinking_speaking2", "key_point_speaking"}
_THINKING_ENTRY_LABELS = {"thinking_trans", "thinking_to_serious", "thinking_to_key_point"}
_SERIOUS_ENTRY_LABELS = {"speaking_trans", "thinking_to_serious"}
_SERIOUS_EXIT_LABELS = {"serious_to_thinking"}
_EMOTION_SPEAKING_ENTRY_BY_INTENT = {
    "smile": "trans_smile",
    "sad": "sad_trans",
    "shy": "shy_trans",
    "surprise": "surprise_trans",
    "angry": "angry_trans",
}
_EMOTION_SPEAKING_INTENT_BY_LABEL = {
    "smile": "smile",
    "trans_smile": "smile",
    "smile_speaking": "smile",
    "sad": "sad",
    "sad_trans": "sad",
    "sad_speaking": "sad",
    "shy": "shy",
    "shy_trans": "shy",
    "shy_speaking1": "shy",
    "shy_speaking2": "shy",
    "surprised": "surprise",
    "surprise": "surprise",
    "surprise_trans": "surprise",
    "surprise_speaking": "surprise",
    "angry": "angry",
    "angry_trans": "angry",
    "angry_speaking": "angry",
}
_EMOTION_SPEAKING_LABELS = {
    "smile_speaking",
    "sad_speaking",
    "shy_speaking1",
    "shy_speaking2",
    "surprise_speaking",
    "angry_speaking",
}
_POST_SPEECH_EMOTION_LABEL_BY_INTENT = {
    "smile": "smile",
    "sad": "sad",
}
_RUNTIME_POST_EMOTION_LABELS = ("smile", "sad")
_SPEAKING_PERFORMANCE_LABELS = (
    _SERIOUS_SPEAKING_LABELS
    | _DEFAULT_SPEAKING_LABELS
    | _CLOSED_EYE_SPEAKING_LABELS
    | _THINKING_SPEAKING_LABELS
    | _EMOTION_SPEAKING_LABELS
)
# Entry transitions are visual graph hops. They may be part of a speaking route,
# but they are not mouth-sync loops and must not inherit mouth masks.
_SPEAKING_ENTRY_TRANSITION_LABELS = (
    {_SPEAKING_TRIGGER_LABEL, _CLOSED_EYE_SPEAKING_TRIGGER_LABEL}
    | _THINKING_ENTRY_LABELS
    | _SERIOUS_EXIT_LABELS
    | set(_EMOTION_SPEAKING_ENTRY_BY_INTENT.values())
)
# Mouth sync belongs only to new SpriteForge speaking-loop assets. Legacy/static
# post-emotion resources are intentionally kept outside this path.
_MOUTH_SYNC_LABELS = _SPEAKING_PERFORMANCE_LABELS - _SPEAKING_ENTRY_TRANSITION_LABELS
_MOUTH_SILENCE_MASK_OVERRIDES = {
    "key_point_speaking": {
        "overlayAlign": "canvas",
        "preferOwnClosedFrame": True,
        "silenceMaskAmplitude": 0.80,
        "maskWidthMul": 1.00,
        "maskHeightMul": 0.90,
        "maskCyOffset": 0.0,
    },
    "thinking_speaking2": {
        "silenceMaskAmplitude": 1.0,
        "maskWidthMul": 1.04,
        "maskHeightMul": 1.25,
        "maskCyOffset": -4.0,
    },
}
_NON_EMOTION_SPEAKING_LABELS = (
    _SERIOUS_SPEAKING_LABELS
    | _DEFAULT_SPEAKING_LABELS
    | _CLOSED_EYE_SPEAKING_LABELS
    | _THINKING_SPEAKING_LABELS
    | _THINKING_ENTRY_LABELS
    | _SERIOUS_EXIT_LABELS
)
_SPEAKING_RELEASE_LABELS = (
    _SPEAKING_PERFORMANCE_LABELS
    | _THINKING_ENTRY_LABELS
    | _SERIOUS_EXIT_LABELS
    | set(_EMOTION_SPEAKING_ENTRY_BY_INTENT.values())
)
MOUTH_MASKS_ENABLED = True
TRANSITION_HOLD_LABELS = {
    _SPEAKING_TRIGGER_LABEL,
    _CLOSED_EYE_SPEAKING_TRIGGER_LABEL,
    *_THINKING_ENTRY_LABELS,
    *_EMOTION_SPEAKING_ENTRY_BY_INTENT.values(),
}
POST_SPEECH_HOLD_SEC = 1.0


class SpriteForgeAnimator:
    """SpriteForge graph state machine driver for a render host/bridge."""

    # Legacy asset aliases: SpriteForge node label -> legacy emotion keys that
    # should be registered with the same frame set.
    # This keeps set_emotion("happy") and set_emotion("smile") on the same
    # modern SpriteForge frames.
    _LEGACY_ALIASES: dict[str, list[str]] = {
        "smile":      ["happy"],
        "trans_smile": [],
    }

    def __init__(self, engine):
        self.engine = engine
        self._frontend_runtime = callable(getattr(engine, "load_spriteforge_graph", None))
        self._sm_thread: threading.Thread | None = None
        self._running = False

        self._character_pack: CharacterPack | None = None
        self._graph: dict = {"nodes": [], "edges": []}
        self._current_node_id: str | None = None
        self._root_node_id:    str | None = None

        # node_id -> duration_sec (one full loop)
        self._loop_durations: dict[str, float] = {}

        self._pending_expression: str | None = None
        self._forced_node_id: str | None = None
        self._speech_active = False
        self._active_speech_intent: str | None = None
        self._transition_hold_active = False
        self._post_speech_hold_until = 0.0
        self._post_speech_release_node_id: str | None = None
        self._post_speech_timer: threading.Timer | None = None
        self._wake_event = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._character_pack is not None

    def start(self) -> bool:
        try:
            self._character_pack = load_character_pack(SPRITEFORGE_RUNTIME_ROOT)
        except CharacterPackError as exc:
            self._character_pack = None
            logger.info(
                "[SFAnimator] optional character pack unavailable (%s): %s",
                exc.code,
                exc,
            )
            return False
        self._load_graph()
        self._register_all_frames()
        self._load_mouth_configs()

        self.engine.set_mode("sprite")
        self.engine.set_idle_animation(True)

        if self._root_node_id:
            self._current_node_id = self._root_node_id

        if self._frontend_runtime:
            self.engine.load_spriteforge_graph(self._frontend_runtime_payload())
            logger.info("[SFAnimator] frontend runtime armed, root=%s (%s)",
                        self._root_node_id, self._label(self._root_node_id or ""))
            return True

        if self._root_node_id:
            self.engine.set_emotion(self._label(self._root_node_id))

        self._running = True
        self._sm_thread = threading.Thread(
            target=self._run, daemon=True, name="SFAnimator"
        )
        self._sm_thread.start()
        logger.info("[SFAnimator] started, root=%s (%s)",
                    self._root_node_id, self._label(self._root_node_id or ""))
        return True

    def stop(self) -> None:
        self._running = False
        with self._lock:
            self._clear_post_speech_hold_locked()
        self._wake_event.set()

    def trigger_expression(self, expression_label: str) -> None:
        """LLM call: queue an expression trigger for next loop boundary."""
        expression_label = resolve_spriteforge_trigger_label(expression_label)
        if self._frontend_runtime:
            with self._lock:
                self._clear_post_speech_hold_locked()
            trigger = getattr(self.engine, "trigger_spriteforge_intent", None)
            if callable(trigger):
                trigger(expression_label)
            logger.info("[SFAnimator] forwarded expression to frontend: %s", expression_label)
            return
        with self._lock:
            self._clear_post_speech_hold_locked()
            self._transition_hold_active = False
            intent = _EMOTION_SPEAKING_INTENT_BY_LABEL.get(expression_label)
            if intent and self._speech_active:
                self._active_speech_intent = intent
                expression_label = _EMOTION_SPEAKING_ENTRY_BY_INTENT.get(intent, expression_label)
            self._pending_expression = expression_label
        self._clear_sprite_hold()
        self._wake_event.set()
        logger.info("[SFAnimator] queued expression: %s", expression_label)

    def on_speaking(self, speaking: bool) -> None:
        """TTS playback started/ended - overlay mouth animation."""
        if self._frontend_runtime:
            with self._lock:
                self._speech_active = bool(speaking)
                if not speaking:
                    self._active_speech_intent = None
            try:
                self.engine.set_speaking(speaking)
            except Exception as e:
                logger.warning("[SFAnimator] frontend set_speaking(%s) failed: %s", speaking, e)
            return
        emit_speaking = True
        if speaking:
            current_label = self._label(self._current_node_id or "")
            should_wake = False
            already_speaking = False
            with self._lock:
                already_speaking = self._speech_active
                self._speech_active = True
                emit_speaking = not already_speaking
                self._clear_post_speech_hold_locked()
                if self._transition_hold_active and current_label in TRANSITION_HOLD_LABELS:
                    self._forced_node_id = self._next_auto_node(self._current_node_id)
                    self._transition_hold_active = False
                    should_wake = True
                pending = self._pending_expression
                if pending:
                    should_wake = True
                pending_intent = _EMOTION_SPEAKING_INTENT_BY_LABEL.get(pending or "")
                if pending_intent:
                    self._active_speech_intent = pending_intent
                    self._pending_expression = _EMOTION_SPEAKING_ENTRY_BY_INTENT.get(
                        pending_intent, pending
                    )
                    should_wake = True
                    pending = self._pending_expression
                elif pending in _NON_EMOTION_SPEAKING_LABELS:
                    self._active_speech_intent = None
                current_is_speaking_route = current_label in _SPEAKING_RELEASE_LABELS
                if not already_speaking and not pending and not current_is_speaking_route:
                    self._active_speech_intent = None
                    trigger_label = _DEFAULT_SPEAKING_TRIGGER_LABEL
                    if (
                        current_label in _CLOSED_EYE_SPEAKING_ELIGIBLE_LABELS
                        and self._node_by_label(_CLOSED_EYE_SPEAKING_TRIGGER_LABEL)
                        and random.random() < _CLOSED_EYE_SPEAKING_CHANCE
                    ):
                        trigger_label = _CLOSED_EYE_SPEAKING_TRIGGER_LABEL
                    self._pending_expression = trigger_label
                    should_wake = True
                    logger.info(
                        "[SFAnimator] speaking started -> speaking performance: %s (from %s)",
                        trigger_label,
                        current_label,
                    )
            if should_wake:
                self._wake_event.set()
            self._clear_sprite_hold()
        if not speaking:
            current_label = self._label(self._current_node_id or "")
            current_is_performance = current_label in _SPEAKING_RELEASE_LABELS
            with self._lock:
                emit_speaking = self._speech_active
                self._speech_active = False
                pending_label = self._pending_expression
                pending_is_performance = pending_label in _SPEAKING_RELEASE_LABELS
                if pending_is_performance:
                    self._pending_expression = None
                if current_is_performance or pending_is_performance:
                    release_label = current_label if current_is_performance else pending_label
                    release_node = self._post_speech_release_node(release_label or current_label)
                    self._post_speech_release_node_id = release_node or self._root_node_id
                    self._post_speech_hold_until = time.time() + POST_SPEECH_HOLD_SEC
                    self._schedule_post_speech_hold_locked()
            if current_is_performance or pending_is_performance:
                self._wake_event.set()
                logger.info(
                    "[SFAnimator] speaking ended -> hold %.1fs before clearing performance",
                    POST_SPEECH_HOLD_SEC,
                )
        try:
            if emit_speaking:
                self.engine.set_speaking(speaking)
            if not speaking and current_is_performance:
                self._hold_current_sprite_frame()
        except Exception as e:
            logger.warning("[SFAnimator] set_speaking(%s) failed: %s", speaking, e)

    def set_mouth_value(self, value: float) -> None:
        """Mouth amplitude 0.0-1.0 from audio thread."""
        try:
            self.engine.set_mouth_value(value)
        except Exception as e:
            logger.debug("[SFAnimator] set_mouth_value failed: %s", e)

    def reload_graph(self) -> None:
        self._character_pack = load_character_pack(SPRITEFORGE_RUNTIME_ROOT)
        self._load_graph()

    def _clear_post_speech_hold_locked(self) -> None:
        self._post_speech_hold_until = 0.0
        self._post_speech_release_node_id = None
        if self._post_speech_timer is not None:
            self._post_speech_timer.cancel()
            self._post_speech_timer = None

    def _post_speech_release_node(self, current_label: str) -> str | None:
        if current_label in _NON_EMOTION_SPEAKING_LABELS:
            return self._root_node_id
        intent = _EMOTION_SPEAKING_INTENT_BY_LABEL.get(current_label) or self._active_speech_intent
        target_label = _POST_SPEECH_EMOTION_LABEL_BY_INTENT.get(intent or "")
        if target_label:
            target = self._node_by_label(target_label)
            if target:
                return target["id"]
        return self._root_node_id

    def _schedule_post_speech_hold_locked(self) -> None:
        if self._post_speech_timer is not None:
            self._post_speech_timer.cancel()
        delay = max(0.0, self._post_speech_hold_until - time.time())

        def _release():
            with self._lock:
                if time.time() < self._post_speech_hold_until:
                    return
                self._post_speech_hold_until = 0.0
                self._post_speech_timer = None
                self._forced_node_id = self._post_speech_release_node_id or self._root_node_id
                self._post_speech_release_node_id = None
                self._active_speech_intent = None
            self._clear_sprite_hold()
            self._wake_event.set()

        self._post_speech_timer = threading.Timer(delay, _release)
        self._post_speech_timer.daemon = True
        self._post_speech_timer.start()

    def _hold_current_sprite_frame(self) -> None:
        hold = getattr(self.engine, "hold_sprite_frame", None)
        if callable(hold):
            try:
                hold(None)
            except Exception as e:
                logger.debug("[SFAnimator] hold current frame failed: %s", e)

    def _hold_last_sprite_frame(self) -> None:
        hold = getattr(self.engine, "hold_sprite_frame", None)
        if callable(hold):
            try:
                hold(-1)
            except Exception as e:
                logger.debug("[SFAnimator] hold last frame failed: %s", e)

    def _clear_sprite_hold(self) -> None:
        clear = getattr(self.engine, "clear_sprite_hold", None)
        if callable(clear):
            try:
                clear()
            except Exception as e:
                logger.debug("[SFAnimator] clear sprite hold failed: %s", e)

    def _url(self, abs_path: Path) -> str:
        return abs_path.resolve().as_uri()

    # ------------------------------------------------------------------
    # Graph
    # ------------------------------------------------------------------

    def _load_graph(self) -> None:
        pack = self._character_pack
        if pack is None:
            self._graph = {"nodes": [], "edges": []}
            self._root_node_id = None
            return
        raw = pack.graph
        self._graph = raw if "nodes" in raw else raw.get("graph", {"nodes": [], "edges": []})
        self._root_node_id = None
        for n in self._graph["nodes"]:
            if n.get("isRoot"):
                self._root_node_id = n["id"]
                break
        if not self._root_node_id and self._graph["nodes"]:
            self._root_node_id = self._graph["nodes"][0]["id"]
        self._add_runtime_post_emotion_nodes()
        logger.info("[SFAnimator] graph: %d nodes, %d edges, root=%s",
                    len(self._graph["nodes"]), len(self._graph["edges"]),
                    self._label(self._root_node_id or ""))

    def _add_runtime_post_emotion_nodes(self) -> None:
        """Add invisible runtime-only post-speech emotions.

        The SpriteForge reviewer graph intentionally omits ordinary smile/sad
        nodes now. These virtual nodes let speech_end briefly land on the old
        resources and then return to root without adding GUI-only graph edges.
        """
        if not self._root_node_id:
            return
        existing_labels = {n.get("label") for n in self._graph["nodes"]}
        pack = self._character_pack
        if pack is None:
            return
        for label in _RUNTIME_POST_EMOTION_LABELS:
            if label in existing_labels or label not in pack.clip_paths:
                continue
            node_id = f"__runtime_post_{label}"
            self._graph["nodes"].append({
                "id": node_id,
                "label": label,
                "runtime": True,
            })
            self._graph["edges"].append({
                "id": f"__runtime_post_{label}_to_root",
                "from": node_id,
                "to": self._root_node_id,
                "prob": 1,
                "runtime": True,
            })

    # ------------------------------------------------------------------
    # Frame registration
    # ------------------------------------------------------------------

    def _register_all_frames(self) -> None:
        pack = self._character_pack
        if pack is None:
            return
        raw_clips = pack.manifest["clips"]
        for node in self._graph["nodes"]:
            emotion = node["label"]
            clip = raw_clips.get(emotion)
            frames = pack.clip_paths.get(emotion)
            if not isinstance(clip, dict) or not frames:
                logger.warning("[SFAnimator] runtime clip missing from manifest: %s", emotion)
                continue
            interval_ms = int(clip["frameIntervalMs"])
            phase = str(clip.get("phase") or "loop")
            urls = [path.resolve().as_uri() for path in frames]
            self.engine.load_sprite_frames(emotion, urls)
            self.engine.set_idle_frame_interval_ms(emotion, interval_ms)
            if str(clip.get("loopMode") or "loop") == "once_then_hold":
                self.engine.set_sprite_clip_config(
                    emotion,
                    {"loopMode": "once_then_hold", "frameIntervalMs": interval_ms},
                )

            # Register legacy aliases with the same frame set so old emotion
            # keys do not fall back to stale JS-renderer assets. For example,
            # SpriteForge node "smile" also registers frames under "happy".
            for old_name in self._LEGACY_ALIASES.get(emotion, []):
                self.engine.load_sprite_frames(old_name, urls)
                self.engine.set_idle_frame_interval_ms(old_name, interval_ms)
                if str(clip.get("loopMode") or "loop") == "once_then_hold":
                    self.engine.set_sprite_clip_config(
                        old_name,
                        {"loopMode": "once_then_hold", "frameIntervalMs": interval_ms},
                    )
                logger.info("[SFAnimator] aliased legacy: %s -> %s", old_name, emotion)

            duration = len(frames) * interval_ms / 1000.0
            self._loop_durations[node["id"]] = duration
            logger.info(
                "[SFAnimator] registered: %-20s  %s  %d KTX2 frames @ %dms = %.2fs",
                emotion,
                phase,
                len(frames),
                interval_ms,
                duration,
            )

    # ------------------------------------------------------------------
    # Mouth configs
    # ------------------------------------------------------------------

    def _load_mouth_configs(self) -> None:
        if not MOUTH_MASKS_ENABLED:
            logger.info("[SFAnimator] mouth masks disabled globally for graph test")
            return
        pack = self._character_pack
        if pack is None:
            return
        raw = pack.mouth_config
        expressions = raw.get("expressions", {})
        profiles = raw.get("profiles", {})
        if not isinstance(profiles, dict) or not profiles:
            logger.warning("[SFAnimator] mouth config has no v2 speaking profiles")
            return

        def _build(expr_cfg: dict) -> dict | None:
            if not isinstance(expr_cfg, dict):
                return None
            return {
                "cx":     expr_cfg.get("cx", 0.0),
                "cy":     expr_cfg.get("cy", 0.0),
                "width":  expr_cfg.get("width", 40.0),
                "height": expr_cfg.get("height", 20.0),
                "curve":  expr_cfg.get("curve", 0.2),
                "sourceCx": expr_cfg.get("cx", 0.0),
                "sourceCy": expr_cfg.get("cy", 0.0),
                "closedFrameIdx": 0,
            }

        mouth_sets = {name: _build(cfg) for name, cfg in expressions.items()}

        loaded = 0
        for label, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            if label not in _MOUTH_SYNC_LABELS:
                logger.info("[SFAnimator] mouth profile ignored for transition/non-speaking node: %s", label)
                continue
            mouth_set = str(profile.get("mouth_set") or "neutral")
            base_js = mouth_sets.get(mouth_set)
            overlay_paths = pack.mouth_overlay_paths.get(label, ())
            if not base_js or not overlay_paths:
                logger.warning("[SFAnimator] mouth profile skipped (missing set=%s): %s",
                               mouth_set, label)
                continue
            frame_urls = [self._url(path) for path in overlay_paths]
            js_cfg = dict(base_js)
            js_cfg["mode"] = "silence_close"
            js_cfg["silenceThreshold"] = 0.08
            js_cfg.update(_MOUTH_SILENCE_MASK_OVERRIDES.get(label, {}))
            js_cfg["profile"] = label
            js_cfg["cx"] = profile.get("cx", js_cfg["cx"])
            js_cfg["cy"] = profile.get("cy", js_cfg["cy"])
            js_cfg["width"] = profile.get("width", js_cfg["width"])
            js_cfg["height"] = profile.get("height", js_cfg["height"])
            js_cfg["opennessByFrame"] = profile.get("openness", [])
            js_cfg["closedFrameIdx"] = 0
            js_cfg["frameUrls"] = frame_urls
            js_cfg["openness"] = [0.0 for _ in frame_urls]
            raw_overlay_anchor = profile.get("runtime_overlay_anchor")
            overlay_anchor = raw_overlay_anchor if isinstance(raw_overlay_anchor, dict) else {}
            source_anchor = {
                "cx": overlay_anchor.get("cx", profile.get("cx", js_cfg["cx"])),
                "cy": overlay_anchor.get("cy", profile.get("cy", js_cfg["cy"])),
                "width": overlay_anchor.get("width", profile.get("width", js_cfg["width"])),
                "height": overlay_anchor.get("height", profile.get("height", js_cfg["height"])),
            }
            js_cfg["sourceAnchors"] = [source_anchor]
            js_cfg["sourceCx"] = source_anchor["cx"]
            js_cfg["sourceCy"] = source_anchor["cy"]
            anchor_track = []
            for item in profile.get("anchor_track", []) or []:
                if not isinstance(item, dict):
                    continue
                anchor_track.append({
                    "cx": item.get("cx", js_cfg["cx"]),
                    "cy": item.get("cy", js_cfg["cy"]),
                    "width": item.get("width", js_cfg["width"]),
                    "height": item.get("height", js_cfg["height"]),
                    "score": item.get("score", 0.0),
                })
            js_cfg["anchorTrack"] = anchor_track
            self.engine.load_mouth_config(label, js_cfg)
            loaded += 1
            logger.info("[SFAnimator] mouth profile: %s -> %s (%d anchors, %d overlay frames)",
                        label, mouth_set, len(anchor_track), len(frame_urls))

        logger.info("[SFAnimator] mouth profiles loaded: %d", loaded)

    def _frontend_runtime_payload(self) -> dict:
        return {
            "graph": self._graph,
            "rootNodeId": self._root_node_id,
            "durations": self._loop_durations,
            "config": {
                "defaultSpeakingTriggerLabel": _DEFAULT_SPEAKING_TRIGGER_LABEL,
                "closedEyeSpeakingTriggerLabel": _CLOSED_EYE_SPEAKING_TRIGGER_LABEL,
                "closedEyeSpeakingChance": _CLOSED_EYE_SPEAKING_CHANCE,
                "postSpeechHoldSec": POST_SPEECH_HOLD_SEC,
                "seriousSpeakingLabels": sorted(_SERIOUS_SPEAKING_LABELS),
                "defaultSpeakingLabels": sorted(_DEFAULT_SPEAKING_LABELS),
                "closedEyeSpeakingLabels": sorted(_CLOSED_EYE_SPEAKING_LABELS),
                "closedEyeEligibleLabels": sorted(_CLOSED_EYE_SPEAKING_ELIGIBLE_LABELS),
                "thinkingSpeakingLabels": sorted(_THINKING_SPEAKING_LABELS),
                "thinkingEntryLabels": sorted(_THINKING_ENTRY_LABELS),
                "seriousEntryLabels": sorted(_SERIOUS_ENTRY_LABELS),
                "seriousExitLabels": sorted(_SERIOUS_EXIT_LABELS),
                "speakingReleaseLabels": sorted(_SPEAKING_RELEASE_LABELS),
                "nonEmotionSpeakingLabels": sorted(_NON_EMOTION_SPEAKING_LABELS),
                "transitionHoldLabels": sorted(TRANSITION_HOLD_LABELS),
                "emotionEntryByIntent": _EMOTION_SPEAKING_ENTRY_BY_INTENT,
                "emotionIntentByLabel": _EMOTION_SPEAKING_INTENT_BY_LABEL,
                "postSpeechEmotionLabelByIntent": _POST_SPEECH_EMOTION_LABEL_BY_INTENT,
                "triggerAliases": _TRIGGER_ALIASES,
                # Browser-side safety fallback only. Normal random routing is
                # resolved once on the backend so GUI and wallpaper agree.
                "semanticTriggerDefaults": {
                    key: routes[-1] for key, routes in _RANDOM_TRIGGER_ROUTES.items()
                },
            },
        }

    # ------------------------------------------------------------------
    # Graph helpers
    # ------------------------------------------------------------------

    def _node(self, node_id: str) -> dict | None:
        for n in self._graph["nodes"]:
            if n["id"] == node_id:
                return n
        return None

    def _label(self, node_id: str) -> str:
        n = self._node(node_id)
        return n["label"] if n else ""

    def _node_by_label(self, label: str) -> dict | None:
        for n in self._graph["nodes"]:
            if n["label"] == label:
                return n
        return None

    def _duration(self, node_id: str) -> float:
        return self._loop_durations.get(node_id, 2.5)

    def _next_auto_node(self, from_id: str) -> str:
        edges = [e for e in self._graph["edges"]
                 if e["from"] == from_id and e.get("prob", 0) > 0]
        if not edges:
            return from_id
        total = sum(e["prob"] for e in edges)
        r = random.random() * total
        acc = 0.0
        for e in edges:
            acc += e["prob"]
            if r <= acc:
                return e["to"]
        return edges[-1]["to"]

    def _trigger_target_ids(self, target_label: str) -> set[str]:
        labels = {target_label}
        if target_label in (_SERIOUS_SPEAKING_LABELS | _SERIOUS_ENTRY_LABELS):
            labels |= _SERIOUS_SPEAKING_LABELS | _SERIOUS_ENTRY_LABELS
        elif target_label in _DEFAULT_SPEAKING_LABELS:
            labels |= _DEFAULT_SPEAKING_LABELS
        elif target_label in _CLOSED_EYE_SPEAKING_LABELS:
            labels |= _CLOSED_EYE_SPEAKING_LABELS
        elif target_label in (_THINKING_SPEAKING_LABELS | _THINKING_ENTRY_LABELS | _SERIOUS_EXIT_LABELS):
            labels |= _THINKING_SPEAKING_LABELS | _THINKING_ENTRY_LABELS | _SERIOUS_EXIT_LABELS
        else:
            intent = _EMOTION_SPEAKING_INTENT_BY_LABEL.get(target_label)
            if intent:
                labels |= {
                    label
                    for label, label_intent in _EMOTION_SPEAKING_INTENT_BY_LABEL.items()
                    if label_intent == intent
                }

        return {
            node["id"]
            for node in self._graph["nodes"]
            if node.get("label") in labels
        }

    def _first_hop_to_any(self, start_id: str | None, target_ids: set[str]) -> str | None:
        if not start_id or not target_ids:
            return None
        if start_id in target_ids:
            return start_id

        visited = {start_id}
        queue: list[tuple[str, str | None]] = [(start_id, None)]
        while queue:
            node_id, first_hop = queue.pop(0)
            allow_manual = node_id == start_id
            edges = [
                e
                for e in self._graph["edges"]
                if e["from"] == node_id
                and (e.get("prob", 0) > 0 or (allow_manual and e.get("prob", 0) == 0))
            ]
            edges.sort(key=lambda e: (0 if e["to"] in target_ids else 1, 0 if e.get("prob", 0) == 0 else 1))
            for edge in edges:
                next_id = edge["to"]
                hop = first_hop or next_id
                if next_id in target_ids:
                    return hop
                if next_id in visited:
                    continue
                visited.add(next_id)
                queue.append((next_id, hop))
        return None

    def _find_trigger_entry(self, target_label: str) -> str | None:
        """Find the first graph hop for a semantic trigger.

        Prefer the current node so graph-authored transitions such as
        serious_to_thinking / thinking_to_serious are preserved. Fall back to
        root manual trigger edges for normal idle starts.
        """
        target_ids = self._trigger_target_ids(target_label)
        if not target_ids:
            return None
        current_entry = self._first_hop_to_any(self._current_node_id, target_ids)
        if current_entry:
            return current_entry
        return self._first_hop_to_any(self._root_node_id, target_ids)

    def _can_reach_auto(self, from_id: str, target_id: str, visited: set) -> bool:
        if from_id == target_id:
            return True
        if from_id in visited:
            return False
        visited.add(from_id)
        for e in self._graph["edges"]:
            if e["from"] == from_id and e.get("prob", 0) > 0:
                if self._can_reach_auto(e["to"], target_id, visited):
                    return True
        return False

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _play_node(self, node_id: str) -> None:
        label = self._label(node_id)
        if label:
            self.engine.set_emotion(label)
            logger.debug("[SFAnimator] -> %s (%.2fs)", label, self._duration(node_id))

    def _run(self) -> None:
        if not self._graph["nodes"]:
            logger.warning("[SFAnimator] empty graph, exiting")
            return
        while self._running:
            self._play_node(self._current_node_id)
            self._wake_event.wait(self._duration(self._current_node_id))
            self._wake_event.clear()
            if not self._running:
                break

            while self._running:
                with self._lock:
                    forced = self._forced_node_id
                    pending = self._pending_expression
                    hold_remaining = max(0.0, self._post_speech_hold_until - time.time())
                    should_hold_transition = (
                        not forced
                        and not pending
                        and not self._speech_active
                        and self._label(self._current_node_id or "") in TRANSITION_HOLD_LABELS
                    )

                if forced or pending:
                    break

                if hold_remaining > 0:
                    self._wake_event.wait(hold_remaining)
                    self._wake_event.clear()
                    continue

                if should_hold_transition:
                    with self._lock:
                        if not self._transition_hold_active:
                            self._transition_hold_active = True
                            logger.info(
                                "[SFAnimator] transition %s finished before voice -> hold last frame",
                                self._label(self._current_node_id or ""),
                            )
                    self._hold_last_sprite_frame()
                    self._wake_event.wait()
                    self._wake_event.clear()
                    continue

                break

            if not self._running:
                break

            with self._lock:
                forced = self._forced_node_id
                self._forced_node_id = None
            if forced:
                self._current_node_id = forced
                continue

            with self._lock:
                pending = self._pending_expression
            if pending:
                with self._lock:
                    self._pending_expression = None
                entry = self._find_trigger_entry(pending)
                if entry:
                    self._current_node_id = entry
                else:
                    target = self._node_by_label(pending)
                    if target:
                        self._current_node_id = target["id"]
                    else:
                        logger.warning("[SFAnimator] no trigger entry for: %s", pending)
                        self._current_node_id = self._next_auto_node(self._current_node_id)
            else:
                self._current_node_id = self._next_auto_node(self._current_node_id)
