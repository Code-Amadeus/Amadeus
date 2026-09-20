"""Codex-root presentation contract derived from the shipping Amadeus role prompt.

The experiment in :mod:`tools.eval_codex_role_contract` treats Codex as the
conversation root and native subagents as an execution lane.  This module keeps
that experiment honest in two ways:

* the visible-role instructions embed the current ``llm.prompts`` base prompt
  instead of maintaining a second Kurisu persona; and
* every visible message is scored by the same streaming tag parser used by the
  shipping chat path before it can count as consumable Amadeus speech.

The scorer is intentionally stricter than the runtime.  Runtime parsing drops
unknown or incomplete tags so the user does not hear protocol debris.  An eval
must record those model errors rather than credit the recovery boundary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re
from typing import Iterable, Mapping, Sequence

from llm.prompts import get_system_prompt
from llm.stream_parser import StreamTagParser
from tools.text_utils import _parse_seconds


_PROTOCOL_TAG_RE = re.compile(
    r"\[(PARAM|EXPR|HOTKEY|EMO|ANIM|DELEGATE|CONTROL|AUIP)([^\]]*)\]",
    flags=re.IGNORECASE,
)
_ANY_BRACKET_TAG_RE = re.compile(r"\[([A-Za-z_][A-Za-z0-9_-]*)([^\]]*)\]")
_UNTERMINATED_PROTOCOL_RE = re.compile(
    r"\[(?:PARAM|EXPR|HOTKEY|EMO|ANIM|DELEGATE|CONTROL|AUIP)\b[^\]]*$",
    flags=re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"[。！？!?](?:[\"'」』）)]*)")
_VISIBLE_INTERNAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("spawn_agent", re.compile(r"\bspawn_agent\b", re.IGNORECASE)),
    ("subagent", re.compile(r"\bsub-?agent\b", re.IGNORECASE)),
    ("agent_thread", re.compile(r"\bagent thread\b", re.IGNORECASE)),
    ("tool_call", re.compile(r"\btool call\b", re.IGNORECASE)),
    ("provider_result", re.compile(r"\bprovider\.result\b", re.IGNORECASE)),
    ("progress_marker", re.compile(r"\[PROGRESS\s*:", re.IGNORECASE)),
)


# These are copied from the ranges declared in the current _JA_BASE/_EN_BASE
# prompt.  The actual persona and wording are not copied: build_role_contract()
# embeds get_system_prompt("base") and records its fingerprint.
EMOTION_DURATION_RANGES: Mapping[str, tuple[float, float] | None] = {
    "normal": (2.0, 6.0),
    "thinking": (10.0, 15.0),
    "smile": (1.0, 2.0),
    "happy": (1.0, 2.0),
    "shy": (2.0, 4.0),
    "blush": (2.0, 4.0),
    "angry": (3.0, 5.0),
    "sad": (3.0, 5.0),
    "disappointed": (3.0, 5.0),
    "surprised": (1.0, 2.0),
    # The shipping prompt defines this preset by semantic span instead of a
    # numeric duration.  Require a positive, bounded duration for consumption.
    "serious_speaking": None,
}


@dataclass(frozen=True)
class ContractViolation:
    code: str
    detail: str
    hard: bool = True


@dataclass(frozen=True)
class RoleOutputEvaluation:
    raw_text: str
    clean_text: str
    actions: tuple[dict, ...]
    emotion_presets: tuple[str, ...]
    violations: tuple[ContractViolation, ...]
    source_prompt_sha256: str

    @property
    def conformant(self) -> bool:
        return not any(item.hard for item in self.violations)

    def to_dict(self) -> dict:
        value = asdict(self)
        value["conformant"] = self.conformant
        return value


def current_role_prompt() -> str:
    """Return the shipping visible-role prompt for the configured TTS language."""

    return get_system_prompt("base")


def prompt_fingerprint(prompt: str) -> str:
    return hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()


def build_codex_root_role_contract(*, source_prompt: str | None = None) -> str:
    """Build the developer contract for a persistent Codex Kurisu root.

    The embedded prompt remains the persona source of truth.  The additional
    rules define channel ownership and measurable delegation behavior only.
    """

    role_prompt = str(source_prompt if source_prompt is not None else current_role_prompt())
    fingerprint = prompt_fingerprint(role_prompt)
    return f"""[AMADEUS CODEX ROOT ROLE CONTRACT v1]

You are the persistent conversation root for one Amadeus Chat Session. The
embedded shipping Amadeus role prompt below is the sole source of truth for
Kurisu's identity, language, tone, and EMO performance. Follow it for every
root commentary message and every root final answer throughout the session.

<shipping_amadeus_role_prompt sha256=\"{fingerprint}\">
{role_prompt}
</shipping_amadeus_role_prompt>

[Channel ownership]
- The root agent is the only character-presentation authority.
- Subagents are role-free execution workers. Their prose, plans, logs, tool
  output, and errors are evidence for the root, never Kurisu speech.
- Native collaboration and tool events stay on the execution channel. Never
  narrate system terms such as spawn_agent, subagent, agent thread, tool call,
  provider.result, routing, or PROGRESS markers in visible character speech.
- Do not emit DELEGATE, CONTROL, AUIP, PARAM, EXPR, HOTKEY, or ANIM tags in
  visible Codex messages. Native Codex collaboration owns execution. The only
  allowed bracket protocol in visible speech is the shipping [EMO ...] tag.

[Visible speech]
- Root commentary that is sent before or during work is real user-facing
  Kurisu speech and must obey the same language and EMO rules as the final.
- Before delegated work, prefer one short in-character acknowledgement. Do not
  recite the plan, agent topology, commands, filenames, or mechanical progress.
- After work, synthesize only verified results returned by workers or tools.
  Never claim completion from a plan, spawn, or silence.
- Keep control markup out of the spoken text. Amadeus strips valid EMO tags and
  schedules the resulting actions with the corresponding spoken sentence.

[Delegation policy for this experiment]
- Do not spawn a subagent for ordinary conversation, persona banter, a short
  explanation answerable from the supplied conversation, or a request to
  recall a result already established in this thread.
- Spawn one focused read-only subagent when the user asks to inspect one
  workspace artifact and report evidence.
- When the user asks to compare two independent workspace artifacts, spawn two
  focused read-only subagents concurrently, one per artifact, then reconcile
  their evidence.
- Wait for all requested workers before the final answer. Do not let a worker
  write character dialogue. Do not spawn replacement workers merely to restate
  an already available result.

[Stopping rule]
Return a concise in-character answer once the current user request is answered
and all required evidence is available. Do not add an unsolicited question.
"""


def _stream_parse(chunks: Iterable[str]) -> tuple[str, tuple[dict, ...]]:
    parser = StreamTagParser()
    cleaned: list[str] = []
    actions: list[dict] = []
    for chunk in chunks:
        visible, parsed = parser.process_chunk(str(chunk or ""))
        cleaned.append(visible)
        actions.extend(parsed)
    return "".join(cleaned).strip(), tuple(actions)


def _sentence_fragments(text: str) -> list[str]:
    fragments: list[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        fragments.append(text[start : match.end()])
        start = match.end()
    tail = text[start:]
    if tail.strip():
        fragments.append(tail)
    return fragments


def evaluate_role_output(
    text_or_chunks: str | Sequence[str],
    *,
    source_prompt: str | None = None,
    required_presets: Sequence[str] = (),
    required_presets_hard: bool = False,
    allow_internal_terms: bool = False,
    allowed_action_types: Sequence[str] = ("EMO",),
) -> RoleOutputEvaluation:
    """Score one root-visible Codex message against the Amadeus contract."""

    chunks = (
        (str(text_or_chunks),)
        if isinstance(text_or_chunks, str)
        else tuple(str(value or "") for value in text_or_chunks)
    )
    raw_text = "".join(chunks)
    role_prompt = str(source_prompt if source_prompt is not None else current_role_prompt())
    clean_text, actions = _stream_parse(chunks)
    violations: list[ContractViolation] = []
    allowed_actions = {
        str(value or "").strip().upper()
        for value in allowed_action_types
        if str(value or "").strip()
    }

    if not clean_text:
        violations.append(ContractViolation("missing_visible_speech", "no consumable speech remained"))
    if raw_text.lstrip().upper().startswith("[EMO"):
        violations.append(
            ContractViolation("emo_at_response_start", "the shipping prompt forbids EMO at response start")
        )
    if _UNTERMINATED_PROTOCOL_RE.search(raw_text):
        violations.append(ContractViolation("unterminated_protocol_tag", "a protocol tag was not closed"))

    protocol_matches = list(_PROTOCOL_TAG_RE.finditer(raw_text))
    parsed_protocol_count = len(actions)
    if protocol_matches and parsed_protocol_count < len(protocol_matches):
        violations.append(
            ContractViolation(
                "parser_truncated_protocol",
                "the streaming parser stopped before consuming every visible protocol tag",
            )
        )

    unknown_bracket_tags = [
        match.group(1)
        for match in _ANY_BRACKET_TAG_RE.finditer(raw_text)
        if match.group(1).upper()
        not in {"PARAM", "EXPR", "HOTKEY", "EMO", "ANIM", "DELEGATE", "CONTROL", "AUIP"}
    ]
    if unknown_bracket_tags:
        violations.append(
            ContractViolation(
                "unknown_visible_bracket_tag",
                "unknown visible bracket tag(s): " + ", ".join(unknown_bracket_tags),
            )
        )

    emotions: list[str] = []
    for action in actions:
        action_type = str(action.get("type") or "").upper()
        attrs = dict(action.get("attrs") or {})
        if action_type not in allowed_actions:
            violations.append(
                ContractViolation(
                    "forbidden_visible_control",
                    f"visible {action_type or 'unknown'} action belongs to the execution channel",
                )
            )
            continue
        if action_type != "EMO":
            continue
        preset = str(attrs.get("preset") or "").strip().lower()
        duration_text = str(attrs.get("dur") or "").strip().lower()
        emotions.append(preset)
        if preset not in EMOTION_DURATION_RANGES:
            violations.append(ContractViolation("unknown_emo_preset", f"unsupported preset: {preset or '<missing>'}"))
            continue
        if not duration_text:
            violations.append(ContractViolation("missing_emo_duration", f"{preset} has no dur attribute"))
            continue
        duration_s = _parse_seconds(duration_text, -1.0)
        if duration_s <= 0:
            violations.append(ContractViolation("invalid_emo_duration", f"{preset} duration is {duration_text!r}"))
            continue
        bounds = EMOTION_DURATION_RANGES[preset]
        if bounds is None:
            if duration_s > 30.0:
                violations.append(
                    ContractViolation("emo_duration_out_of_range", f"{preset} duration {duration_s:g}s exceeds 30s")
                )
        elif not bounds[0] <= duration_s <= bounds[1]:
            violations.append(
                ContractViolation(
                    "emo_duration_out_of_range",
                    f"{preset} duration {duration_s:g}s is outside {bounds[0]:g}-{bounds[1]:g}s",
                )
            )
        extras = sorted(set(attrs) - {"preset", "dur"})
        if extras:
            violations.append(
                ContractViolation("unexpected_emo_attributes", f"{preset} has unsupported attrs: {', '.join(extras)}")
            )

    sentences = _sentence_fragments(raw_text)
    previous_preset = ""
    for index, sentence in enumerate(sentences):
        matches = list(_PROTOCOL_TAG_RE.finditer(sentence))
        emo_matches = [m for m in matches if m.group(1).upper() == "EMO"]
        if len(emo_matches) > 1:
            violations.append(
                ContractViolation(
                    "multiple_emo_in_sentence",
                    f"sentence {index + 1} contains {len(emo_matches)} EMO tags",
                )
            )
        if index == 0 and not emo_matches:
            violations.append(
                ContractViolation("first_sentence_missing_emo", "the first sentence contains no EMO performance beat")
            )
        if index > 0 and not emo_matches and previous_preset != "normal":
            violations.append(
                ContractViolation(
                    "later_sentence_missing_emo",
                    f"sentence {index + 1} has no EMO tag and does not follow normal",
                )
            )
        if emo_matches:
            preset_match = re.search(r"\bpreset\s*=\s*([^\s\]]+)", emo_matches[-1].group(2), re.IGNORECASE)
            previous_preset = (preset_match.group(1).strip("\"'").lower() if preset_match else "")

    if not allow_internal_terms:
        for code, pattern in _VISIBLE_INTERNAL_PATTERNS:
            if pattern.search(raw_text):
                violations.append(ContractViolation("visible_internal_term", f"visible speech leaked {code}"))

    required = {str(value or "").strip().lower() for value in required_presets if str(value or "").strip()}
    if required and not required.intersection(emotions):
        violations.append(
            ContractViolation(
                "required_emotion_missing",
                "expected one of " + ", ".join(sorted(required)) + f"; observed {emotions or ['none']}",
                hard=bool(required_presets_hard),
            )
        )

    return RoleOutputEvaluation(
        raw_text=raw_text,
        clean_text=clean_text,
        actions=actions,
        emotion_presets=tuple(emotions),
        violations=tuple(violations),
        source_prompt_sha256=prompt_fingerprint(role_prompt),
    )


__all__ = [
    "ContractViolation",
    "EMOTION_DURATION_RANGES",
    "RoleOutputEvaluation",
    "build_codex_root_role_contract",
    "current_role_prompt",
    "evaluate_role_output",
    "prompt_fingerprint",
]
