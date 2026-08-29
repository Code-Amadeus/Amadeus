"""Source-owned character-presentation claims.

The renderer has one body, while Work, VN, AUIP, and the main conversation can
all be alive at the same time.  This module owns only the small piece of state
needed to keep those producers from globally releasing each other's pose.

It deliberately does *not* decide what should be narrated, how often a source
may speak, or which facts are true.  Source-local governors make those
decisions before offering a presentation claim.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from render.spriteforge_intent import spriteforge_intent_payload
from server.event_bus import bus
from server.protocol import Method


PresentationTier = Literal["ambient", "utterance"]
PresentationScenario = Literal["", "computer-use"]
# Normal utterance completion preserves the renderer-owned post-speech exit;
# interruption and unrelated source teardown continue to release immediately.
PresentationHandoff = Literal["immediate", "after_speech"]
PresentationEmitter = Callable[
    [str, dict[str, Any]],
    None | Awaitable[None],
]

_TIER_ORDER: dict[str, int] = {"ambient": 0, "utterance": 1}


@dataclass(frozen=True, slots=True)
class PresentationOwner:
    source_kind: str
    source_id: str
    tier: PresentationTier


@dataclass(frozen=True, slots=True)
class _Claim:
    owner: PresentationOwner
    semantic_label: str
    render_payload: Mapping[str, Any]
    order: int


@dataclass(frozen=True, slots=True)
class PresentationTransition:
    method: Method
    payload: Mapping[str, Any]
    owner: PresentationOwner | None


class PresentationClaimSet:
    """Pure, thread-safe reducer for one embodied character.

    Utterances temporarily sit above ambient activity.  Within one tier, the
    most recently changed claim is visible.  Repeating an identical claim is
    idempotent, so progress heartbeats cannot steal the body from a speaker.
    """

    def __init__(self) -> None:
        self._claims: dict[PresentationOwner, _Claim] = {}
        self._effective: _Claim | None = None
        self._sequence = 0
        self._lock = threading.Lock()

    def claim(
        self,
        *,
        owner: PresentationOwner,
        semantic_label: str,
        render_payload: Mapping[str, Any],
    ) -> PresentationTransition | None:
        label = str(semantic_label or "").strip()
        if not label:
            raise ValueError("semantic_label is required")
        with self._lock:
            previous = self._effective
            existing = self._claims.get(owner)
            if existing is not None and existing.semantic_label == label:
                return None
            self._sequence += 1
            self._claims[owner] = _Claim(
                owner=owner,
                semantic_label=label,
                render_payload=dict(render_payload),
                order=self._sequence,
            )
            self._effective = self._select_effective()
            return self._transition(previous, self._effective)

    def release(
        self,
        *,
        owner: PresentationOwner,
        handoff: PresentationHandoff = "immediate",
    ) -> PresentationTransition | None:
        _validate_handoff(handoff)
        with self._lock:
            previous = self._effective
            if self._claims.pop(owner, None) is None:
                return None
            self._effective = self._select_effective()
            return self._transition(
                previous,
                self._effective,
                released=owner,
                handoff=handoff,
            )

    @property
    def effective_owner(self) -> PresentationOwner | None:
        with self._lock:
            return self._effective.owner if self._effective is not None else None

    def accepts(self, transition: PresentationTransition) -> bool:
        """Return whether a not-yet-emitted transition still describes truth."""

        with self._lock:
            if transition.method == Method.RENDER_SPRITEFORGE_RELEASE:
                return self._effective is None
            if self._effective is None or transition.owner != self._effective.owner:
                return False
            return str(transition.payload.get("label") or "") == str(
                self._effective.render_payload.get("label") or ""
            )

    def _select_effective(self) -> _Claim | None:
        if not self._claims:
            return None
        return max(
            self._claims.values(),
            key=lambda claim: (_TIER_ORDER[claim.owner.tier], claim.order),
        )

    @staticmethod
    def _transition(
        previous: _Claim | None,
        current: _Claim | None,
        *,
        released: PresentationOwner | None = None,
        handoff: PresentationHandoff = "immediate",
    ) -> PresentationTransition | None:
        if previous == current:
            return None
        if current is None:
            payload: dict[str, Any] = {"source": "character_presentation"}
            if released is not None:
                payload.update(_owner_payload(released, prefix="released_"))
            if handoff == "after_speech":
                payload["presentation_handoff"] = handoff
            return PresentationTransition(
                method=Method.RENDER_SPRITEFORGE_RELEASE,
                payload=payload,
                owner=None,
            )
        payload = {
            **dict(current.render_payload),
            "source": "character_presentation",
            **_owner_payload(current.owner),
        }
        if handoff == "after_speech":
            payload["presentation_handoff"] = handoff
        return PresentationTransition(
            method=Method.RENDER_SPRITEFORGE_INTENT,
            payload=payload,
            owner=current.owner,
        )


class _ComputerUseSceneClaims:
    """The one shared scene currently proven by both Work and AUIP."""

    def __init__(self) -> None:
        self._owners: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def claim(self, source_kind: str, source_id: str) -> bool:
        with self._lock:
            was_empty = not self._owners
            self._owners.add((source_kind, source_id))
            return was_empty and bool(self._owners)

    def release(self, source_kind: str, source_id: str) -> bool:
        with self._lock:
            if (source_kind, source_id) not in self._owners:
                return False
            self._owners.discard((source_kind, source_id))
            return not self._owners

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._owners)


class CharacterPresentationCoordinator:
    """Emit only effective claim changes to the existing render contract."""

    def __init__(
        self,
        emitter: PresentationEmitter,
        *,
        emit_now: PresentationEmitter | None = None,
    ) -> None:
        self._claims = PresentationClaimSet()
        self._computer_use = _ComputerUseSceneClaims()
        self._emitter = emitter
        self._emit_now = emit_now

    async def claim(
        self,
        *,
        source_kind: str,
        source_id: str,
        label: str,
        tier: PresentationTier = "ambient",
        scenario: PresentationScenario = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> PresentationTransition | None:
        _validate_scenario(scenario)
        transition = self._apply_claim(
            source_kind=source_kind,
            source_id=source_id,
            label=label,
            tier=tier,
            metadata=metadata,
        )
        scene_event = self._claim_scene(
            source_kind=source_kind,
            source_id=source_id,
            scenario=scenario,
            metadata=metadata,
        )
        await self._emit_event(scene_event)
        await self._emit_transition(transition)
        return transition

    async def release(
        self,
        *,
        source_kind: str,
        source_id: str,
        tier: PresentationTier = "ambient",
        scenario: PresentationScenario = "",
        metadata: Mapping[str, Any] | None = None,
        handoff: PresentationHandoff = "immediate",
    ) -> PresentationTransition | None:
        _validate_scenario(scenario)
        transition = self._claims.release(
            owner=_owner(source_kind=source_kind, source_id=source_id, tier=tier),
            handoff=handoff,
        )
        await self._emit_transition(transition)
        await self._emit_event(
            self._release_scene(
                source_kind=source_kind,
                source_id=source_id,
                scenario=scenario,
                metadata=metadata,
            )
        )
        return transition

    def claim_now(
        self,
        *,
        source_kind: str,
        source_id: str,
        label: str,
        tier: PresentationTier = "utterance",
        metadata: Mapping[str, Any] | None = None,
    ) -> PresentationTransition | None:
        transition = self._apply_claim(
            source_kind=source_kind,
            source_id=source_id,
            label=label,
            tier=tier,
            metadata=metadata,
        )
        self._emit_transition_now(transition)
        return transition

    def release_now(
        self,
        *,
        source_kind: str,
        source_id: str,
        tier: PresentationTier = "utterance",
        handoff: PresentationHandoff = "immediate",
    ) -> PresentationTransition | None:
        transition = self._claims.release(
            owner=_owner(source_kind=source_kind, source_id=source_id, tier=tier),
            handoff=handoff,
        )
        self._emit_transition_now(transition)
        return transition

    @property
    def effective_owner(self) -> PresentationOwner | None:
        return self._claims.effective_owner

    def _apply_claim(
        self,
        *,
        source_kind: str,
        source_id: str,
        label: str,
        tier: PresentationTier,
        metadata: Mapping[str, Any] | None,
    ) -> PresentationTransition | None:
        owner = _owner(source_kind=source_kind, source_id=source_id, tier=tier)
        payload = spriteforge_intent_payload(label, **dict(metadata or {}))
        return self._claims.claim(
            owner=owner,
            semantic_label=label,
            render_payload=payload,
        )

    async def _emit_transition(
        self,
        transition: PresentationTransition | None,
    ) -> None:
        if transition is not None and self._claims.accepts(transition):
            await self._emit_event((transition.method, dict(transition.payload)))

    async def _emit_event(
        self,
        event: tuple[Method, dict[str, Any]] | None,
    ) -> None:
        if event is None:
            return
        method, payload = event
        if method == Method.WALLPAPER_ACTIVITY:
            wants_active = bool(str(payload.get("activity") or "").strip())
            if wants_active != self._computer_use.active:
                return
        result = self._emitter(method, payload)
        if inspect.isawaitable(result):
            await result

    def _emit_transition_now(
        self,
        transition: PresentationTransition | None,
    ) -> None:
        if transition is None or not self._claims.accepts(transition):
            return
        emitter = self._emit_now
        if emitter is None:
            raise RuntimeError("synchronous presentation emitter is not configured")
        emitter(transition.method, dict(transition.payload))

    def _claim_scene(
        self,
        *,
        source_kind: str,
        source_id: str,
        scenario: PresentationScenario,
        metadata: Mapping[str, Any] | None,
    ) -> tuple[Method, dict[str, Any]] | None:
        clean = str(scenario or "").strip().lower()
        if not clean:
            return None
        if clean != "computer-use":
            raise ValueError(f"unsupported presentation scenario: {clean}")
        owner = _owner(source_kind=source_kind, source_id=source_id, tier="ambient")
        if not self._computer_use.claim(owner.source_kind, owner.source_id):
            return None
        return (
            Method.WALLPAPER_ACTIVITY,
            {
                "activity": "work",
                "scenario": "computer-use",
                "source": "character_presentation",
                **_owner_payload(owner),
                **dict(metadata or {}),
            },
        )

    def _release_scene(
        self,
        *,
        source_kind: str,
        source_id: str,
        scenario: PresentationScenario,
        metadata: Mapping[str, Any] | None,
    ) -> tuple[Method, dict[str, Any]] | None:
        clean = str(scenario or "").strip().lower()
        if not clean:
            return None
        if clean != "computer-use":
            raise ValueError(f"unsupported presentation scenario: {clean}")
        owner = _owner(source_kind=source_kind, source_id=source_id, tier="ambient")
        if not self._computer_use.release(owner.source_kind, owner.source_id):
            return None
        return (
            Method.WALLPAPER_ACTIVITY,
            {
                "activity": "",
                "scenario": "computer-use",
                "source": "character_presentation",
                **_owner_payload(owner, prefix="released_"),
                **dict(metadata or {}),
            },
        )


class PlaybackPresentationBridge:
    """Bind verified narration identity to one contiguous playback turn.

    Sentence callbacks still drive subtitles and completion accounting, but a
    request that declares a complete multi-sentence turn is one embodied
    performance. Releasing its claim between adjacent sentences would briefly
    restore the ambient Work/AUIP pose and replay an entry transition for every
    sentence. Sources that deliberately remain sentence-scoped keep the legacy
    release boundary.
    """

    def __init__(self, target: CharacterPresentationCoordinator) -> None:
        self._target = target
        self._owners_by_sentence: dict[str, tuple[str, str, bool]] = {}
        self._claimed_owners: dict[tuple[str, str], None] = {}
        self._lock = threading.Lock()

    def on_sentence_start(
        self,
        sentence_id: str,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        data = metadata if isinstance(metadata, Mapping) else {}
        sentence = str(sentence_id or "").strip()
        source_kind = str(data.get("narration_source_kind") or "").strip().lower()
        source_id = str(data.get("narration_source_id") or "").strip()
        complete_turn = data.get("narration_complete_turn") is True
        label = str(data.get("emotion") or "thinking").strip() or "thinking"
        if not sentence or not source_kind or not source_id:
            return
        owner_id = source_id if complete_turn else f"{source_id}:{sentence}"
        owner = (source_kind, owner_id)
        with self._lock:
            if sentence in self._owners_by_sentence:
                return
            self._owners_by_sentence[sentence] = (*owner, complete_turn)
            should_claim = not complete_turn or owner not in self._claimed_owners
            if complete_turn and should_claim:
                self._claimed_owners[owner] = None
        if should_claim:
            self._target.claim_now(
                source_kind=source_kind,
                source_id=owner_id,
                label=label,
                tier="utterance",
                metadata={"sentence_id": sentence},
            )

    def on_sentence_end(self, sentence_id: str) -> None:
        sentence = str(sentence_id or "").strip()
        with self._lock:
            owner = self._owners_by_sentence.pop(sentence, None)
        if owner is None or owner[2]:
            return
        self._target.release_now(
            source_kind=owner[0],
            source_id=owner[1],
            tier="utterance",
        )

    def release_all(
        self,
        *,
        handoff: PresentationHandoff = "immediate",
    ) -> None:
        _validate_handoff(handoff)
        with self._lock:
            owners = list(self._claimed_owners)
            for source_kind, source_id, _complete_turn in self._owners_by_sentence.values():
                owner = (source_kind, source_id)
                if owner not in self._claimed_owners and owner not in owners:
                    owners.append(owner)
            self._owners_by_sentence.clear()
            self._claimed_owners.clear()
        for source_kind, source_id in owners:
            self._target.release_now(
                source_kind=source_kind,
                source_id=source_id,
                tier="utterance",
                handoff=handoff,
            )


def _owner(
    *,
    source_kind: str,
    source_id: str,
    tier: PresentationTier,
) -> PresentationOwner:
    kind = str(source_kind or "").strip().lower()
    identity = str(source_id or "").strip()
    clean_tier = str(tier or "").strip().lower()
    if not kind:
        raise ValueError("source_kind is required")
    if not identity:
        raise ValueError("source_id is required")
    if clean_tier not in _TIER_ORDER:
        raise ValueError(f"unsupported presentation tier: {clean_tier or '<empty>'}")
    return PresentationOwner(kind, identity, clean_tier)  # type: ignore[arg-type]


def _owner_payload(owner: PresentationOwner, *, prefix: str = "") -> dict[str, str]:
    return {
        f"{prefix}presentation_source_kind": owner.source_kind,
        f"{prefix}presentation_source_id": owner.source_id,
        f"{prefix}presentation_tier": owner.tier,
    }


def _validate_scenario(scenario: PresentationScenario) -> None:
    clean = str(scenario or "").strip().lower()
    if clean not in {"", "computer-use"}:
        raise ValueError(f"unsupported presentation scenario: {clean}")


def _validate_handoff(handoff: PresentationHandoff) -> None:
    clean = str(handoff or "").strip().lower()
    if clean not in {"immediate", "after_speech"}:
        raise ValueError(f"unsupported presentation handoff: {clean or '<empty>'}")


coordinator = CharacterPresentationCoordinator(
    bus.emit,
    emit_now=bus.emit_now,
)
playback_bridge = PlaybackPresentationBridge(coordinator)


async def project_auip_update(
    _method: str,
    payload: Mapping[str, Any],
    *,
    target: CharacterPresentationCoordinator = coordinator,
) -> None:
    """Translate an AppSession lifecycle update into one ambient claim."""

    app_session_id = str(payload.get("app_session_id") or "").strip()
    if not app_session_id:
        return
    if str(payload.get("status") or "").strip().lower() == "active":
        await target.claim(
            source_kind="auip",
            source_id=app_session_id,
            label="work",
            scenario="computer-use",
            metadata={"stance": str(payload.get("stance") or "")},
        )
        return
    await target.release(
        source_kind="auip",
        source_id=app_session_id,
        scenario="computer-use",
        metadata={"status": str(payload.get("status") or "")},
    )
