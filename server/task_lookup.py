"""Task lookup: the host resolves which past task the user means, by retrieval.

Order is the design (task_lookup_work_order.md section 3.0). The model speaks
before the host sees any tag, so a model asked to notice that a task is absent
from its list answers about the closest present one instead — measured 3 wrong
in 9, once inventing a task that never existed. The host, however, sees the
user's words BEFORE the model does, so absence never has to be judged: resolve
first, then hand the model a context in which the referenced task is simply
present.

Three rungs, each cheaper than the next:

  1. A unique spoken filename is answered from the artifact/title index —
     deterministic, zero model involvement, unbounded by any recency window.
  2. When that misses or ties, a literal-overlap prefilter (recall-tuned, no
     tokeniser) collects candidates and one side-channel call picks among
     them. Picking from a short list is classification, the shape that has
     worked (probe: 12/12 over 8 deliberately similar candidates, ~1s);
     transcription is the shape that has not (workspace_ref, 0/28).
  3. Still unsure means asking the user, in task titles, never ids.

The side channel never speaks, never enters history, and never touches turn
identity — the same discipline as the resend channel it is modelled on.
Everything here is inert unless ``TASK_LOOKUP_ENABLED`` is on (single switch,
rule R7; it retires when the work order's acceptance holds).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import time
from typing import Any

from server.assistant_language import text_matches_assistant_language

logger = logging.getLogger(__name__)

# The probe chose correctly from eight deliberately similar candidates; more
# rows add prompt weight without adding measured discrimination.
_PREFILTER_LIMIT = 8

# One distinctive ASCII token (async, theme) is enough to suspect a task
# reference; CJK needs two shared bigrams, i.e. roughly one shared three-char
# run. Recall is the tuning goal — the second rung, not the prefilter, decides.
_SCORE_FLOOR = 2

# How many rows the pick may be shown at once. Measured 2026-08-02: when the
# referenced task is *absent* from the list, the model picks the nearest one
# 9 times out of 9 and never declines — the same failure the roster's hot list
# showed (3/9), reappearing inside the side channel because "output UNSURE if
# you cannot tell" is still asking the model to notice an absence. So the fix
# is not a better prompt: the pick is only consulted when the candidate set
# provably contains every task the utterance could mean. Past this budget the
# set cannot be proven complete, and the host asks the user instead.
_PICK_ROW_BUDGET = 60
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.\-]{2,}")
_CJK_RUN_RE = re.compile(r"[^\x00-\x7f]{2,}")

_TURN_RESOLUTION: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "task_lookup_turn_resolution", default=None
)


def lookup_enabled() -> bool:
    """Read at call time so the switch works without a restart."""

    from config import settings as _settings

    return bool(getattr(_settings, "TASK_LOOKUP_ENABLED", False))


# ── per-turn hand-off ────────────────────────────────────────────────────────


def set_turn_resolution(payload: dict[str, Any] | None) -> None:
    _TURN_RESOLUTION.set(payload)


def peek_turn_resolution(session_id: str) -> dict[str, Any] | None:
    """The current turn's pre-resolution, if it belongs to this session.

    Consumers must treat this as advisory: it exists so the roster render and
    the report path do not pay twice for the same utterance, not as a second
    source of truth.
    """

    payload = _TURN_RESOLUTION.get()
    if not isinstance(payload, dict):
        return None
    if str(payload.get("session_id") or "") != str(session_id or "").strip():
        return None
    return payload


# ── rung 1: exact reference against the index ───────────────────────────────


def _file_references(text: str) -> set[str]:
    from core.chat_runtime import _explicit_file_references

    return _explicit_file_references(text)


def _exact_matches_for_reference(session_id: str, reference: str) -> list[dict[str, Any]]:
    """Every conversation row whose produced files or title carry ``reference``.

    Recall comes from the ledger indexes (whole table, not a window); the
    precision filter here is the same predicate amend grounding has always
    used, so an index hit means exactly what a roster hit used to mean.
    """

    from server.work_ledger_coordinator import get_work_ledger_coordinator

    coordinator = get_work_ledger_coordinator()
    if coordinator is None:
        return []
    by_file = getattr(coordinator, "conversation_work_items_by_file", None)
    if not callable(by_file):
        return []
    # A kept project's past is answerable in any later conversation; a draft's
    # is not. This rung is an index query, so widening it costs no model call
    # and no prompt budget, and an exact match cannot be incomplete -- unlike
    # the pick below, which never refuses and so must keep its candidate set
    # inside one conversation.
    rows = by_file(session_id, reference, include_kept_projects=True)
    wanted = {str(reference).lower()}
    exact = [
        row
        for row in rows
        if wanted
        & (
            {str(name).lower() for name in (row.get("files") or [])}
            | _file_references(str(row.get("title") or ""))
        )
    ]
    return _collapse_continuation_lineages(exact)


def _collapse_continuation_lineages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the latest leaf from each explicit WorkItem continuation.

    The same artifact is normally registered again when a follow-up WorkItem
    changes or inspects it. Treating every version as an unrelated candidate
    makes the second follow-up ambiguous forever. ``related_work_item_id`` is a
    host-written ledger edge, so a parent and its one descendant are one
    lineage; independent items with the same filename remain separate leaves
    and therefore remain visibly ambiguous. Corrupt cycles fail closed by
    returning the original rows.
    """

    if len(rows) < 2:
        return rows
    ids = {
        str(row.get("work_item_id") or "")
        for row in rows
        if str(row.get("work_item_id") or "")
    }
    ancestors = {
        related
        for row in rows
        for related in [str(row.get("related_work_item_id") or "")]
        if related in ids
    }
    leaves = [
        row
        for row in rows
        if str(row.get("work_item_id") or "") not in ancestors
    ]
    return leaves or rows


# ── prefilter: literal overlap, recall first ────────────────────────────────


def _overlap_tokens(text: str) -> dict[str, int]:
    """Literal tokens with weights: distinctive ASCII words 2, CJK bigrams 1."""

    normalized = " ".join(str(text or "").lower().split())
    tokens: dict[str, int] = {}
    for token in _ASCII_TOKEN_RE.findall(normalized):
        tokens[token] = 2 if len(token) >= 4 else 1
    for run in _CJK_RUN_RE.findall(normalized):
        for index in range(len(run) - 1):
            tokens.setdefault(run[index : index + 2], 1)
    return tokens


def _prefilter(utterance: str, index_rows: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    spoken = _overlap_tokens(utterance)
    if not spoken:
        return []
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in index_rows:
        candidate_text = " ".join(
            (
                str(row.get("title") or ""),
                " ".join(str(name) for name in (row.get("files") or [])),
            )
        )
        candidate = _overlap_tokens(candidate_text)
        score = sum(weight for token, weight in spoken.items() if token in candidate)
        if score > 0:
            scored.append((score, row))
    # Stable two-pass sort: newest first within a score, highest score first.
    scored.sort(key=lambda pair: str(pair[1].get("updated_at") or ""), reverse=True)
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:_PREFILTER_LIMIT]


# ── rung 2: one side-channel pick ───────────────────────────────────────────


def _status_line(row: dict[str, Any]) -> str:
    from server.work_context import _status_phrase

    return _status_phrase(row)


def _candidate_line(row: dict[str, Any]) -> str:
    title = " ".join(str(row.get("title") or "").split())[:60].strip()
    if not title:
        files = [str(name).strip() for name in (row.get("files") or []) if str(name).strip()]
        title = "/".join(files[:3]) or "(untitled)"
    return f"- {row.get('work_item_id')} | {title} | {_status_line(row)}"


async def _side_channel_pick(utterance: str, candidates: list[dict[str, Any]]) -> str:
    """Ask one question, read one identifier, say nothing.

    Same shape as the resend channel: non-streaming, reply parsed for the id
    alone, nothing enters conversation history. The default system prompt is
    deliberate — the probe validated the pick without persona vocabulary, and
    the delegate contract has no business in a channel that must never act.

    Callers must hand over a set that provably contains the answer. UNSURE is
    honoured when it comes, but it cannot be relied on: with the referent
    missing the model picked the nearest row 9 times out of 9 (2026-08-02).
    """

    from llm.client import remote_llm_query

    rows = "\n".join(_candidate_line(row) for row in candidates)
    prompt = (
        f"[用户刚才说]\n{utterance}\n\n"
        f"[候选任务]\n{rows}\n\n"
        "[SYSTEM] 用户指的是上面哪一个任务？只输出那一行的 work_item_id，"
        "不要输出任何其他文字。如果无法确定，只输出 UNSURE。"
    )
    reply = str(await asyncio.to_thread(remote_llm_query, prompt, None) or "")
    mentioned = [
        str(row.get("work_item_id") or "")
        for row in candidates
        if str(row.get("work_item_id") or "") and str(row.get("work_item_id") or "") in reply
    ]
    return mentioned[0] if len(set(mentioned)) == 1 else ""


# ── the ladder ───────────────────────────────────────────────────────────────


def _result(
    *,
    row: dict[str, Any] | None = None,
    level: int = 0,
    reason: str,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "row": row,
        "level": level,
        "reason": reason,
        "candidates": list(candidates or []),
    }


async def resolve(
    session_id: str,
    utterance: str,
    *,
    consumer: str,
    recency_fallback: bool = False,
) -> dict[str, Any]:
    """Resolve which existing task ``utterance`` refers to, or say why not.

    reason values: ``hit`` (row set), ``empty`` (a named file no task ever
    produced — definitive, not a window miss), ``ambiguous`` (candidates
    survive but no single answer; ask, do not guess), ``no_reference``
    (nothing suggests a past task), ``error`` (infrastructure; fail open).

    ``recency_fallback`` lets a consumer that already knows the turn is about
    a past task ("刚才那个好了吗") skip the prefilter gate, which exists only
    to keep ordinary chat from spending a model call.
    """

    clean_session_id = str(session_id or "").strip()
    clean_utterance = str(utterance or "").strip()
    if not clean_session_id or not clean_utterance:
        return _result(reason="no_reference")
    try:
        references = _file_references(clean_utterance)
        if len(references) == 1:
            reference = next(iter(references))
            exact = await asyncio.to_thread(
                _exact_matches_for_reference, clean_session_id, reference
            )
            if len(exact) == 1:
                logger.info(
                    "[TASK-LOOKUP] level=1 outcome=hit consumer=%s ref=%s work_item=%s",
                    consumer,
                    reference,
                    exact[0].get("work_item_id"),
                )
                return _result(row=exact[0], level=1, reason="hit", candidates=exact)
            if not exact:
                logger.info(
                    "[TASK-LOOKUP] level=1 outcome=empty consumer=%s ref=%s",
                    consumer,
                    reference,
                )
                return _result(level=1, reason="empty")
            logger.info(
                "[TASK-LOOKUP] level=1 outcome=ambiguous consumer=%s ref=%s n=%d",
                consumer,
                reference,
                len(exact),
            )
            candidates = exact[:_PREFILTER_LIMIT]
        else:
            # The prefilter is a gate, not a shortlist. Handing the pick only
            # the rows that scored would put it in front of a set that may not
            # contain the answer, and there it does not decline: with the
            # referent absent it named the nearest row 9 times out of 9
            # (2026-08-02). "把颜色改绿那个任务" shares nothing literal with
            # the task titled "把 theme.txt 的 color 改成 green" — 颜色/color
            # is a translation gap, not a spelling one — so a scoring shortlist
            # is exactly the set most likely to be missing its own answer. The
            # gate decides whether to spend a call; the pick then sees the
            # whole conversation, and absence stops being possible.
            if not recency_fallback and not await asyncio.to_thread(
                _prefilter_gate, clean_session_id, clean_utterance
            ):
                logger.info(
                    "[TASK-LOOKUP] level=1 outcome=none consumer=%s refs=%d",
                    consumer,
                    len(references),
                )
                return _result(reason="no_reference")
            candidates, complete = await asyncio.to_thread(
                _fallback_candidates, clean_session_id
            )
            if not candidates:
                return _result(reason="no_reference")
            if not complete:
                # Too many tasks to prove the answer is on the table. Asking
                # costs one turn; a confident wrong task costs trust.
                logger.info(
                    "[TASK-LOOKUP] level=2 outcome=skipped consumer=%s "
                    "reason=roster_incomplete n=%d",
                    consumer,
                    len(candidates),
                )
                return _result(
                    reason="ambiguous", candidates=candidates[:_PREFILTER_LIMIT]
                )
            logger.info(
                "[TASK-LOOKUP] level=1 outcome=none consumer=%s "
                "candidates=whole_conversation n=%d",
                consumer,
                len(candidates),
            )
        started = time.monotonic()
        picked = ""
        try:
            picked = await _side_channel_pick(clean_utterance, candidates)
        except Exception as exc:
            logger.warning("[TASK-LOOKUP] level=2 outcome=error consumer=%s: %s", consumer, exc)
            return _result(reason="error", candidates=candidates)
        elapsed = time.monotonic() - started
        if picked:
            row = next(
                (
                    candidate
                    for candidate in candidates
                    if str(candidate.get("work_item_id") or "") == picked
                ),
                None,
            )
            logger.info(
                "[TASK-LOOKUP] level=2 outcome=pick consumer=%s work_item=%s elapsed=%.2fs",
                consumer,
                picked,
                elapsed,
            )
            return _result(row=row, level=2, reason="hit", candidates=candidates)
        logger.info(
            "[TASK-LOOKUP] level=2 outcome=unsure consumer=%s n=%d elapsed=%.2fs",
            consumer,
            len(candidates),
            elapsed,
        )
        return _result(reason="ambiguous", candidates=candidates)
    except Exception as exc:
        logger.warning("[TASK-LOOKUP] resolution unavailable (%s): %s", consumer, exc)
        return _result(reason="error")


def _prefilter_gate(session_id: str, utterance: str) -> bool:
    """Is this utterance worth one model call, or is it ordinary conversation?

    Only a gate: what it scores never becomes the candidate set, because a
    set built from scores is the set most likely to be missing its answer.
    Cheap by design — a lean scan with no artifact hydration, measured at
    5.4 ms over a 200-task conversation, and it opened for none of eight
    ordinary utterances.
    """

    from server.work_ledger_coordinator import get_work_ledger_coordinator

    coordinator = get_work_ledger_coordinator()
    if coordinator is None:
        return False
    index = getattr(coordinator, "conversation_work_item_index", None)
    if not callable(index):
        return False
    scored = _prefilter(utterance, index(session_id))
    top_score = scored[0][0] if scored else 0
    if top_score < _SCORE_FLOOR:
        logger.info(
            "[TASK-LOOKUP] gate closed: top=%d n=%d", top_score, len(scored)
        )
        return False
    logger.info("[TASK-LOOKUP] gate open: top=%d n=%d", top_score, len(scored))
    return True


def _fallback_candidates(session_id: str) -> tuple[list[dict[str, Any]], bool]:
    """Every task this conversation owns, and whether that is provably all.

    Recency was the wrong shape here. A question the prefilter cannot reach is
    usually a paraphrase, and a paraphrase says nothing about how recent its
    target is — so a recency window leaves the answer off the table exactly
    when it is needed, and the pick then names the nearest row with full
    confidence (0 of 9 declined, 2026-08-02). Handing over the whole
    conversation removes the absent case instead of asking the model to
    detect it. When the roster cannot prove it is whole, the host asks.
    """

    from server.work_ledger_coordinator import get_work_ledger_coordinator

    coordinator = get_work_ledger_coordinator()
    if coordinator is None:
        return [], False
    resolution = getattr(coordinator, "conversation_work_items_for_resolution", None)
    if not callable(resolution):
        return [], False
    try:
        payload = resolution(session_id, limit=_PICK_ROW_BUDGET)
    except Exception:
        logger.debug("conversation roster unavailable for lookup", exc_info=True)
        return [], False
    if isinstance(payload, dict):
        rows = payload.get("items")
        return (
            list(rows) if isinstance(rows, list) else [],
            bool(payload.get("complete")),
        )
    rows = list(payload) if isinstance(payload, list) else []
    return rows, len(rows) < _PICK_ROW_BUDGET


# ── the pre-turn entry point ─────────────────────────────────────────────────


def _pre_turn_has_a_consumer() -> bool:
    """Only the roster injection needs the answer *before* the model speaks.

    Everything else resolves in its own time: amend grounds itself off the
    index when the tag arrives, and report resolves inside the pause its "let
    me check" already bought. So with candidate rows off — where the roster
    deliberately injects nothing, because it has no row to displace — the
    pre-turn pass has nothing to hand anyone, and the only thing it can still
    do is warm the report path's lookup.

    That warming is not free, and it is charged to the wrong meter. Measured
    on a real machine 2026-08-02: a turn whose gate opened took 1.91s to its
    first sentence against 0.75-1.14s for every turn whose gate stayed shut,
    because the pick runs before the model is even asked to speak. It bought
    about a second off the whole exchange, but it spent it on the one number
    this system has repeatedly gone out of its way to protect, and it spent it
    on every gate-opening turn including the ones that were never questions.
    """

    from config import settings as _settings

    return bool(getattr(_settings, "WORK_ROSTER_CANDIDATES", True))


async def pre_turn_resolve(session_id: str, utterance: str) -> dict[str, Any] | None:
    """Resolve before the model speaks, and leave the result for this turn.

    Runs on every turn while the switch is on, so its own fast path matters:
    an utterance with no file reference and no literal overlap with session
    tasks costs one lean scan and no model call (rule R2 — the second rung is
    the exception path, never the per-turn tax).
    """

    if not lookup_enabled() or not _pre_turn_has_a_consumer():
        set_turn_resolution(None)
        return None
    resolution = await resolve(session_id, utterance, consumer="pre_turn")
    payload = {
        "session_id": str(session_id or "").strip(),
        "utterance": str(utterance or ""),
        **resolution,
    }
    set_turn_resolution(payload)
    return payload


def current_status_facts(
    row: dict[str, Any],
    *,
    display_language: str = "simplified_chinese",
) -> dict[str, str]:
    """Select truthful status facts without owning the character's wording."""

    title = " ".join(str(row.get("title") or "当前任务").split())[:80]
    phase = str(row.get("activity_phase") or "").strip().lower()
    execution = str(row.get("execution") or "idle").strip().lower()
    stage_key = phase or execution
    stages_zh = {
        "queued": "排队等待执行",
        "working": "执行中",
        "running": "执行中",
        "waiting_for_user": "等待你的确认",
        "stalled": "执行停滞，宿主仍在监控",
        "cancelling": "等待取消确认",
        "review": "Provider 已结束，等待结果验收",
        "succeeded": "Provider 已结束，等待结果验收",
        "terminal": "已结束",
        "failed": "执行失败",
        "cancelled": "已取消",
        "idle": "尚未执行",
    }
    stages_ja = {
        "queued": "実行待ち",
        "working": "実行中",
        "running": "実行中",
        "waiting_for_user": "あなたの確認待ち",
        "stalled": "停滞中ですが、ホストは監視を続けています",
        "cancelling": "取消確認待ち",
        "review": "Provider の実行が終わり、結果の確認待ち",
        "succeeded": "Provider の実行が終わり、結果の確認待ち",
        "terminal": "終了",
        "failed": "実行失敗",
        "cancelled": "取消済み",
        "idle": "未実行",
    }
    stage_zh = stages_zh.get(stage_key, stages_zh.get(execution, stage_key or "未知"))
    stage_ja = stages_ja.get(stage_key, stages_ja.get(execution, stage_key or "不明"))

    milestones = row.get("activity_milestones")
    milestones = milestones if isinstance(milestones, dict) else {}
    milestone_kind, milestone_fact = _latest_status_milestone(milestones)
    terminal_fact = _terminal_status_fact(
        row,
        display_language=display_language,
        milestone_fact=milestone_fact,
    )
    recent = terminal_fact or milestone_fact
    direction = " ".join(
        str(row.get("activity_direction_summary") or "").split()
    )[:360]
    direction_only = bool(
        direction
        and not recent
        and execution in {"queued", "running"}
        and stage_key not in {"review", "terminal", "failed", "cancelled"}
    )
    # A reported direction is useful current awareness, but it remains
    # separate from verified milestones and terminal outcome authority.
    recent_kind = (
        "terminal_result"
        if terminal_fact
        else milestone_kind
        if milestone_fact
        else "direction"
        if direction_only
        else ""
    )
    recent_source = recent or (direction if direction_only else "")
    recent_zh = _localized_status_fact(
        recent_source,
        kind=recent_kind,
        language="simplified_chinese",
    )
    recent_ja = _localized_status_fact(
        recent_source,
        kind=recent_kind,
        language="japanese",
    )

    attention = str(row.get("attention") or "none").strip().lower()
    uncertainty = str(row.get("activity_uncertainty") or "").strip().lower()
    completion_rationale = " ".join(
        str(row.get("completion_rationale") or "").split()
    )[:400]
    blocker_zh = {
        "permission": "正在等待权限或用户确认",
        "error": "账本记录了需要处理的错误",
        "conflict": "结果存在需要处理的冲突",
    }.get(attention, "")
    blocker_ja = {
        "permission": "権限またはユーザー確認を待っています",
        "error": "台帳に対処が必要なエラーがあります",
        "conflict": "結果に対処が必要な競合があります",
    }.get(attention, "")
    if completion_rationale and attention in {"error", "conflict"}:
        if attention == "conflict":
            blocker_zh = f"结果存在冲突，具体阻碍是：{completion_rationale}"
            blocker_ja = f"結果に競合があり、具体的な理由は「{completion_rationale}」です"
        else:
            blocker_zh = f"发生了错误，具体阻碍是：{completion_rationale}"
            blocker_ja = f"エラーがあり、具体的な理由は「{completion_rationale}」です"
    uncertainty_text = {
        "provider_silent": ("Provider 长时间没有新事件", "Provider から長時間新しいイベントがありません"),
        "waiting_for_user": ("正在等待你的输入", "あなたの入力を待っています"),
        "cancellation_not_yet_confirmed": ("取消尚未得到确认", "取消はまだ確認されていません"),
        "steer_not_applied": ("新的修改指令尚未应用", "新しい変更指示はまだ適用されていません"),
        "provider_action_denied": ("有操作被 Provider 策略拒绝", "Provider ポリシーに拒否された操作があります"),
    }.get(uncertainty)
    if not blocker_zh and uncertainty_text:
        blocker_zh, blocker_ja = uncertainty_text
    if not blocker_zh:
        blocker_zh, blocker_ja = "没有已知阻碍", "既知の障害はありません"

    completion = str(row.get("completion") or "unknown").strip().lower()
    if attention == "permission" or phase == "waiting_for_user":
        next_zh, next_ja = "处理当前确认后继续", "現在の確認を処理してから続行します"
    elif execution in {"failed", "cancelled", "orphaned"}:
        next_zh, next_ja = "处理失败原因后再决定是否重试", "失敗原因を確認してから再試行を判断します"
    elif execution in {"queued", "running"}:
        if "validation" in milestones:
            next_zh, next_ja = "继续收口剩余工作并形成终态报告", "残作業をまとめ、最終報告を作ります"
        elif "capability" in milestones:
            next_zh, next_ja = "验证已经实现的能力并报告结果", "実装済みの機能を検証し、結果を報告します"
        elif "design" in milestones:
            next_zh, next_ja = "按已确认的方案继续实现", "確認済みの方針に沿って実装を続けます"
        else:
            next_zh, next_ja = "形成可确认的实现方案并继续实现", "確認できる実装方針を固めて作業を続けます"
    elif completion == "complete":
        next_zh, next_ja = "任务已完成，没有待执行步骤", "タスクは完了しており、実行待ちの手順はありません"
    else:
        next_zh, next_ja = "检查结果并决定接受、修改或重试", "結果を確認し、受入れ・修正・再試行を決めます"

    fact_kind, _milestone_summary = milestone_kind, milestone_fact
    if terminal_fact:
        fact_kind = "terminal_result"
    elif direction_only:
        fact_kind = "direction"
    return {
        "title": title,
        "stage_key": stage_key,
        "stage_zh": stage_zh,
        "stage_ja": stage_ja,
        "recent_zh": recent_zh,
        "recent_ja": recent_ja,
        "blocker_zh": blocker_zh,
        "blocker_ja": blocker_ja,
        "next_zh": next_zh,
        "next_ja": next_ja,
        "fact_kind": fact_kind,
        "fact_source": (
            "terminal_outcome"
            if fact_kind == "terminal_result"
            else "provider_direction"
            if fact_kind == "direction"
            else "activity_milestone"
            if fact_kind
            else "none"
        ),
        "direction_only": "true" if direction_only else "false",
    }


def render_current_status_facts(
    facts: dict[str, str],
    *,
    display_language: str = "simplified_chinese",
) -> tuple[str, str]:
    """Render selected facts as a short conversational character update.

    The host remains authoritative about phase, result, blocker and next step;
    this function only decides cadence.  In particular it avoids reading the
    complete task instruction or ledger field labels aloud.
    """

    stage_key = str(facts.get("stage_key") or "idle")
    recent_zh = _embedded_sentence(facts.get("recent_zh") or "尚无新的可确认语义成果")
    recent_ja = _embedded_sentence(facts.get("recent_ja") or "確認できる新しい成果はまだない")
    blocker_zh = str(facts.get("blocker_zh") or "没有已知阻碍")
    blocker_ja = str(facts.get("blocker_ja") or "既知の障害はない")
    next_zh = str(facts.get("next_zh") or "继续检查进展")
    next_ja = str(facts.get("next_ja") or "このまま進捗を確認する")
    has_result = str(facts.get("fact_kind") or "") != ""
    direction_only = str(facts.get("direction_only") or "").lower() == "true"
    has_blocker = blocker_zh != "没有已知阻碍"

    if stage_key in {"queued"}:
        display = f"还在等待开始，目前没有发现阻碍；下一步会{next_zh}。"
        voice_ja = f"まだ実行待ちよ。今のところ問題はないから、次は{next_ja}。"
    elif stage_key in {"working", "running"}:
        if direction_only and has_blocker:
            display = f"还在处理中，当前执行方向是：{recent_zh}；不过{blocker_zh}，接下来会{next_zh}。"
            voice_ja = f"まだ作業中よ。進め方は更新されているけれど、{blocker_ja}。次は{next_ja}。"
        elif direction_only:
            display = f"还在处理中，当前执行方向是：{recent_zh}；这还不是完成结果，目前没有发现阻碍。"
            voice_ja = "まだ作業中よ。進め方は更新されていて、内容は画面に出してある。まだ完了報告ではないけど、今のところ問題はないわ。"
        elif has_result and has_blocker:
            display = f"还在处理中，最近确认的是：{recent_zh}；不过{blocker_zh}，接下来会{next_zh}。"
            voice_ja = f"まだ作業中よ。{recent_ja}。ただ、{blocker_ja}。次は{next_ja}。"
        elif has_result:
            display = f"还在处理中，最近确认的是：{recent_zh}；目前没有发现阻碍，接下来会{next_zh}。"
            voice_ja = f"まだ作業中よ。{recent_ja}。今のところ問題はなくて、次は{next_ja}。"
        else:
            title = _embedded_sentence(facts.get("title") or "当前任务")
            display = f"还在处理“{title}”。暂时没有新的可确认成果，也没有发现阻碍；我会{next_zh}，有验证结果就告诉你。"
            voice_ja = f"「{title}」を進めているところよ。確認できる新しい成果も問題も、今のところ出ていない。{next_ja}から、検証結果が出たら知らせるわ。"
    elif stage_key in {"waiting_for_user", "stalled", "cancelling"} or has_blocker:
        recent_clause_zh = f"目前确认到的是：{recent_zh}；" if has_result else ""
        recent_clause_ja = f"ここまでに{recent_ja}は確認できているけれど、" if has_result else ""
        display = f"这件事现在停在需要处理的环节。{recent_clause_zh}{blocker_zh}；接下来会{next_zh}。"
        voice_ja = f"今はここで止まっているわ。{recent_clause_ja}{blocker_ja}。次は{next_ja}。"
    elif stage_key in {"review", "succeeded", "terminal", "failed", "cancelled"}:
        display = f"这轮工作已经结束。结果是：{recent_zh}；接下来会{next_zh}。"
        voice_ja = f"この作業は終わっているわ。確認できた結果は、{recent_ja}。次は{next_ja}。"
    else:
        display = f"现在是{facts.get('stage_zh') or '待确认状态'}。{recent_zh}；接下来会{next_zh}。"
        voice_ja = f"今は{facts.get('stage_ja') or '確認待ち'}よ。{recent_ja}。次は{next_ja}。"

    if str(display_language or "").strip().lower() == "japanese":
        display = voice_ja
    return display, voice_ja


def _embedded_sentence(value: object) -> str:
    return str(value or "").strip().rstrip("。．.!！?？")


def _localized_status_fact(
    value: object,
    *,
    kind: str,
    language: str,
) -> str:
    """Keep a Provider fact only when it already matches the output language.

    Provider progress is evidence, not ready-to-speak prose.  Embedding its raw
    English summary inside a Japanese or Chinese frame creates the exact mixed-
    language status line this boundary is meant to prevent.  When no localized
    wording exists, retain the semantic milestone class rather than attempting
    a keyword translation or replaying the foreign sentence.
    """

    clean = _embedded_sentence(value)
    if clean and text_matches_assistant_language(clean, language):
        return clean
    normalized = str(language or "").strip().lower().replace("-", "_")
    semantic_kind = str(kind or "").strip().lower()
    if normalized in {
        "zh",
        "zh_cn",
        "chinese",
        "simplified_chinese",
        "中文",
        "简体中文",
    }:
        return {
            "design": "实现方案已经确定",
            "diagnostic": "已经定位到需要处理的具体原因",
            "capability": "主要能力已经实现",
            "validation": "已经取得新的验证结果",
            "direction": "执行方向已经更新，正在按该方向推进",
            "terminal_result": "终态结果已经记入账本，详细内容已显示在任务卡片中",
        }.get(semantic_kind, "尚无新的可确认语义成果")
    if normalized in {"en", "en_us", "english", "英文"}:
        return clean or {
            "design": "The implementation direction is established",
            "diagnostic": "A concrete cause has been identified",
            "capability": "The main capability is implemented",
            "validation": "New validation evidence is available",
            "direction": "The execution direction has been updated",
            "terminal_result": "The final result is recorded in the ledger",
        }.get(semantic_kind, "There is no new verified semantic result yet")
    return {
        "design": "実装方針は固まっているわ",
        "diagnostic": "対処すべき具体的な原因まで絞り込めているわ",
        "capability": "主要な機能の実装まで進んでいるわ",
        "validation": "新しい検証結果まで確認できているわ",
        "direction": "現在の進め方は更新されていて、その方針で進めているわ",
        "terminal_result": "最終結果は台帳に記録済みで、詳細は画面に表示しているわ",
    }.get(semantic_kind, "確認できる新しい意味的成果はまだないわ")


def render_current_status_answer(
    row: dict[str, Any],
    *,
    display_language: str = "simplified_chinese",
) -> tuple[str, str]:
    """Emergency wording when the configured Narrator cannot answer.

    Normal report turns pass :func:`status_query_narration_note` to the Work
    Narrator.  Keeping this renderer deterministic preserves a truthful text
    fallback during model/configuration outages without making it the ordinary
    character-expression path.
    """

    facts = current_status_facts(row, display_language=display_language)
    return render_current_status_facts(facts, display_language=display_language)


def status_query_narration_note(
    row: dict[str, Any],
    *,
    session_id: str = "",
) -> dict[str, Any]:
    """Project one resolved WorkItem into verified Narrator evidence.

    Lookup has already selected the WorkItem before this function runs.  The
    resulting note deliberately retains Provider prose as evidence: it is the
    existing Narrator's job to express that content in the character language,
    while structured Host fields keep lifecycle and authority out of prose.
    """

    title = " ".join(str(row.get("title") or "current task").split())[:160]
    execution = str(row.get("execution") or "idle").strip().lower()
    activity_phase = str(row.get("activity_phase") or execution).strip().lower()
    completion = str(row.get("completion") or "unknown").strip().lower()
    attention = str(row.get("attention") or "none").strip().lower()
    uncertainty = str(row.get("activity_uncertainty") or "").strip().lower()
    rationale = " ".join(
        str(row.get("completion_rationale") or "").split()
    )[:400]

    milestones = row.get("activity_milestones")
    milestones = milestones if isinstance(milestones, dict) else {}
    milestone_kind, milestone_summary = _latest_status_milestone(milestones)
    direction = " ".join(
        str(row.get("activity_direction_summary") or "").split()
    )[:420]

    from agent_host.provider_progress import split_progress_milestones

    visible_terminal, _embedded = split_progress_milestones(
        str(row.get("terminal_summary") or "")
    )
    visible_terminal = " ".join(visible_terminal.split())[:600]
    is_terminal = execution in {"succeeded", "failed", "cancelled", "orphaned"}
    if is_terminal:
        fact_kind = "terminal_result"
        fact_summary = visible_terminal or milestone_summary
    elif milestone_summary:
        fact_kind = milestone_kind
        fact_summary = milestone_summary
    elif direction:
        fact_kind = "direction"
        fact_summary = direction
    else:
        fact_kind = ""
        fact_summary = ""

    signals: list[dict[str, str]] = [
        {
            "label": "status",
            "text": (
                f"execution={execution}; activity_phase={activity_phase}; "
                f"completion={completion}; attention={attention}"
            ),
            "detail": "Host-owned WorkItem lifecycle facts",
            "kind": "status",
        }
    ]
    if fact_summary:
        signals.append(
            {
                "label": "direction" if fact_kind == "direction" else "report",
                "text": fact_summary[:420],
                "detail": (
                    "unverified reported execution direction"
                    if fact_kind == "direction"
                    else f"verified {fact_kind} evidence"
                ),
                "kind": "status",
            }
        )
    if rationale or uncertainty:
        signals.append(
            {
                "label": "attention",
                "text": rationale or uncertainty,
                "detail": attention,
                "kind": "status",
            }
        )

    outcome_verdict = row.get("outcome_verdict")
    outcome_verdict = outcome_verdict if isinstance(outcome_verdict, dict) else {}
    work_item_id = str(row.get("work_item_id") or "").strip()
    attempt_id = str(
        row.get("attempt_id") or row.get("current_attempt_id") or ""
    ).strip()
    run_id = str(
        row.get("activity_run_id") or row.get("run_id") or f"status-{work_item_id}"
    ).strip()
    return {
        "source": "task_lookup",
        "provider": str(row.get("provider") or "provider"),
        "run_id": run_id,
        "session_id": str(session_id or "").strip(),
        # This is a requested presentation checkpoint, never a new lifecycle
        # Result. Terminal truth remains in outcome_verdict/status_facts.
        "phase": "Checkpoint",
        "importance": "important",
        "title": title,
        "summary": fact_summary or title,
        "signals": signals,
        "metadata": {
            "status_query": True,
            "narration_keypoint": "status_query",
            "work_item_id": work_item_id,
            "attempt_id": attempt_id,
            "semantic_milestone": milestone_kind,
            "directional_summary": direction,
            "outcome_verdict": outcome_verdict,
            "execution_status": execution,
            "status_facts": {
                "execution": execution,
                "activity_phase": activity_phase,
                "completion": completion,
                "attention": attention,
                "uncertainty": uncertainty,
                "completion_rationale": rationale,
                "fact_kind": fact_kind,
            },
        },
    }


def _terminal_status_fact(
    row: dict[str, Any],
    *,
    display_language: str,
    milestone_fact: str = "",
) -> str:
    execution = str(row.get("execution") or "").strip().lower()
    if execution not in {"succeeded", "failed", "cancelled"}:
        return ""
    verdict = row.get("outcome_verdict")
    if isinstance(verdict, dict) and verdict:
        provider_report_allowed = verdict.get("provider_report_allowed") is True
        verified = verdict.get("verified") is True
        if not provider_report_allowed or not verified:
            from server.outcome_verification import localize_outcome_verdict

            bounded = localize_outcome_verdict(
                verdict,
                execution_status=execution,
                display_language=display_language,
            )
            if bounded:
                return " ".join(str(bounded).split())[:600]
    from agent_host.provider_progress import split_progress_milestones

    visible, embedded_milestones = split_progress_milestones(
        str(row.get("terminal_summary") or "")
    )
    compact_terminal = " ".join(visible.split())
    # A concise terminal result can carry names or exact values that the
    # validation milestone intentionally summarized away.  Keep it.  Long
    # reports and outputs containing the internal progress contract project
    # through the bounded semantic milestone instead of being replayed.
    if compact_terminal and len(compact_terminal) <= 600 and not embedded_milestones:
        return compact_terminal
    milestone = " ".join(str(milestone_fact or "").split())
    if milestone:
        return milestone[:600]
    return compact_terminal[:600]


def _latest_status_milestone(
    milestones: dict[str, Any],
) -> tuple[str, str]:
    """Return the latest ledger milestone, never an unconfirmed candidate."""

    rows: list[tuple[float, str, str]] = []
    for key, value in milestones.items():
        kind = str(key or "").strip().lower()
        if kind not in {"design", "diagnostic", "capability", "validation"} or not isinstance(value, dict):
            continue
        summary = " ".join(str(value.get("summary") or "").split())
        if summary:
            rows.append((float(value.get("observedAt") or 0.0), kind, summary))
    rows.sort(reverse=True)
    if not rows:
        return "", ""
    _observed_at, kind, summary = rows[0]
    return kind, summary


# ── facts for the answering pass ─────────────────────────────────────────────


def render_task_facts(row: dict[str, Any]) -> str:
    """One line of ledger truth, in the shape the answering probe validated.

    Written in the character's own output language on purpose. These facts and
    the follow-up around them are the freshest, most on-topic text in the
    answering turn, so whatever language they are in pulls the reply after it:
    an earlier Chinese version of this line produced Chinese replies despite
    the persona prompt saying, in as many words, to always answer in Japanese.
    Host-authored text that the model reads as content has to speak the
    character's language, or it competes with her.
    """

    title = " ".join(str(row.get("title") or "").split())[:80].strip()
    files = [str(name).strip() for name in (row.get("files") or []) if str(name).strip()]
    if not title:
        title = "/".join(files[:3]) or "(untitled)"
    attention = str(row.get("attention") or "none")
    parts = [
        f"タスク：{title}",
        f"実行状態：{row.get('execution') or 'idle'}",
        f"完了度：{row.get('completion') or 'unknown'}",
        f"要確認：{'なし' if attention in ('', 'none') else attention}",
    ]
    phase = " ".join(str(row.get("activity_phase") or "").split())
    if phase:
        parts.insert(2, f"現在段階：{phase}")
    elapsed = _fact_seconds(row.get("activity_elapsed_seconds"))
    if elapsed > 0:
        parts.append(f"実行時間：約{_japanese_duration(elapsed)}")
    silent = _fact_seconds(row.get("activity_silent_seconds"))
    if silent >= 5:
        parts.append(f"最後の意味のある進展から：{_japanese_duration(silent)}")
    semantic = " ".join(str(row.get("activity_semantic_summary") or "").split())
    if semantic:
        parts.append(
            "最新のProvider報告（原文。返答では必ず日本語に直す）："
            f"{semantic[:240]}"
        )
    direction = " ".join(str(row.get("activity_direction_summary") or "").split())
    if direction:
        parts.append(
            "現在の実行方針（Provider報告。完了事実ではない。返答では必ず日本語に直す）："
            f"{direction[:240]}"
        )
    last_tool = " ".join(str(row.get("activity_last_tool") or "").split())
    tool_count = int(row.get("activity_tool_count") or 0)
    if last_tool:
        parts.append(f"直近のツール：{last_tool}（観測{tool_count}回）")
    observation = (
        row.get("workspace_observation")
        if isinstance(row.get("workspace_observation"), dict)
        else {}
    )
    changed_files = [
        str(name).strip()
        for name in (observation.get("changed_files") or [])
        if str(name).strip()
    ]
    staged_files = [
        str(name).strip()
        for name in (observation.get("staged_files") or [])
        if str(name).strip()
    ]
    ambiguous_files = [
        str(name).strip()
        for name in (observation.get("ambiguous_paths") or [])
        if str(name).strip()
    ]
    if changed_files:
        parts.append(f"作業中の変更：{', '.join(changed_files[:8])}")
    elif observation.get("available") is True and not ambiguous_files:
        parts.append("作業中の変更：Git基準からの変更はまだ観測されていない")
    if ambiguous_files:
        parts.append(
            "変更の帰属未確認（開始前からdirty）："
            f"{', '.join(ambiguous_files[:8])}"
        )
    if staged_files:
        parts.append(f"外部出力の準備中：{', '.join(staged_files[:8])}")
    liveness = (
        row.get("activity_liveness")
        if isinstance(row.get("activity_liveness"), dict)
        else {}
    )
    liveness_state = str(liveness.get("state") or "").strip()
    if liveness_state:
        parts.append(f"Provider稼働：{liveness_state}")
    steering = (
        row.get("activity_steering")
        if isinstance(row.get("activity_steering"), dict)
        else {}
    )
    if steering.get("state"):
        revision = int(steering.get("revision") or 0)
        suffix = f"（revision {revision}）" if revision > 0 else ""
        parts.append(f"変更指示：{steering.get('state')}{suffix}")
    uncertainty = str(row.get("activity_uncertainty") or "").strip()
    uncertainty_text = {
        "provider_has_not_reported_semantic_progress": "Providerから意味のある途中報告がまだない",
        "provider_silent": "Providerから新しいイベントがなく、進行位置は未確認",
        "waiting_for_user": "ユーザーの入力または許可を待っている",
        "cancellation_not_yet_confirmed": "取消しの確認を待っている",
        "steer_not_applied": "変更指示は適用されず、元の実行が続いている",
    }.get(uncertainty, "")
    if uncertainty_text:
        parts.append(f"不確実性：{uncertainty_text}")
    if files:
        parts.insert(1, f"生成ファイル：{', '.join(files[:5])}")
    rationale = " ".join(str(row.get("completion_rationale") or "").split())
    if rationale:
        parts.append(f"根拠：{rationale[:200]}")
    return "；".join(parts)


def _fact_seconds(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _japanese_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}時間")
    if minutes:
        parts.append(f"{minutes}分")
    if not parts or (hours == 0 and remaining_seconds):
        parts.append(f"{remaining_seconds}秒")
    return "".join(parts)
