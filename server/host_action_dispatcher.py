"""Host-owned dispatch for model control actions.

The speaking model proposes actions; the Host decides whether a complete
DELEGATE proposal reaches the configured control handler.  Character-renderer
modules may consume presentation actions, but they never own Provider work.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import Any

from tools.text_utils import _parse_delegate_attrs

logger = logging.getLogger(__name__)

TASKLESS_CONTROL_INTENTS = frozenset({"focus", "report"})
_UNSET = object()
_delegate_handler: Callable[..., Any] | None = None
_expression_sink: Callable[[Iterable[dict]], Any] | None = None

# asyncio retains tasks weakly. A dispatch batch is intentional background
# work, so the Host keeps it alive until it reaches a terminal state.
dispatch_tasks: set[asyncio.Task] = set()


class HostDispatchUnavailable(RuntimeError):
    """A complete control proposal reached an unassembled Host boundary."""


def configure(
    *,
    delegate_handler: Callable[..., Any],
    expression_sink: Callable[[Iterable[dict]], Any] | None = None,
) -> None:
    if not callable(delegate_handler):
        raise TypeError("delegate_handler must be callable")
    global _delegate_handler, _expression_sink
    _delegate_handler = delegate_handler
    _expression_sink = expression_sink


def _taskless_control_is_dispatchable(attrs: dict) -> bool:
    intent = str(attrs.get("intent") or "").strip().lower()
    if intent == "focus":
        return True
    if intent != "report":
        return False
    subject = (
        str(attrs.get("subject") or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    return subject in {"project", "projects", "work_item", "workitem", "task"}


def _taskless_operation_is_dispatchable(attrs: dict) -> bool:
    intent = str(attrs.get("intent") or "").strip().lower()
    action = str(attrs.get("action") or "").strip()
    return bool(action and intent in {"execute", "amend"})


def _delegate_call(action: dict) -> tuple[str, dict] | None:
    supplied_attrs = action.get("attrs")
    has_structured_attrs = isinstance(supplied_attrs, dict)
    attrs = dict(supplied_attrs) if has_structured_attrs else {}
    raw = str(action.get("raw") or "")
    if not has_structured_attrs and raw:
        try:
            attr_text = (
                raw[len("[DELEGATE") : -1]
                if raw.upper().startswith("[DELEGATE") and raw.endswith("]")
                else ""
            )
            # Raw-only actions remain readable for the bounded legacy bridge.
            # Once typed attrs exist, they are the complete grounded control
            # truth: reparsing an older transport string cannot express fields
            # a later Host stage intentionally removed.
            attrs = _parse_delegate_attrs(attr_text)
        except Exception:
            logger.debug("failed to parse legacy DELEGATE transport", exc_info=True)
    task = str(attrs.get("task") or "").strip()
    intent = str(attrs.get("intent") or "").strip().lower()
    if not (
        task
        or (
            intent in TASKLESS_CONTROL_INTENTS
            and _taskless_control_is_dispatchable(attrs)
        )
        or _taskless_operation_is_dispatchable(attrs)
    ):
        return None
    return task, attrs


async def _run_delegate_batch(handler: Callable[..., Any], calls: list[tuple[str, dict]]) -> None:
    """Run one response's controls in source order and stop on first failure."""

    for task, attrs in calls:
        try:
            try:
                result = handler(task, attrs)
            except TypeError:
                result = handler(task)
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[HostDispatch] delegated action failed: %s / %s",
                str(attrs.get("provider") or "provider"),
                task[:60] or str(attrs.get("intent") or "control"),
            )
            return


def record_actions(
    actions: Iterable[dict] | None,
    *,
    delegate_handler: Callable[..., Any] | None | object = _UNSET,
    expression_sink: Callable[[Iterable[dict]], Any] | None | object = _UNSET,
):
    """Accept one parsed action batch or fail visibly if Host wiring is absent.

    The returned asyncio Task is the acceptance receipt used for ordered focus
    and work batches. Invalid/incomplete DELEGATE proposals are not accepted;
    a complete proposal with no Host handler raises synchronously instead of
    disappearing in a renderer module.
    """

    items = list(actions or ())
    if not items:
        return None
    calls: list[tuple[str, dict]] = []
    expressions: list[dict] = []
    for action in items:
        if str(action.get("type") or "").upper() == "DELEGATE":
            call = _delegate_call(action)
            if call is not None:
                calls.append(call)
        else:
            expressions.append(action)

    sink = _expression_sink if expression_sink is _UNSET else expression_sink
    if expressions and callable(sink):
        sink(expressions)
    if not calls:
        return None

    handler = _delegate_handler if delegate_handler is _UNSET else delegate_handler
    if not callable(handler):
        raise HostDispatchUnavailable(
            "complete DELEGATE proposal reached an unconfigured Host dispatcher"
        )
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        raise HostDispatchUnavailable(
            "complete DELEGATE proposal arrived without a running event loop"
        ) from exc
    task = loop.create_task(_run_delegate_batch(handler, calls))
    dispatch_tasks.add(task)
    task.add_done_callback(dispatch_tasks.discard)
    logger.info("[HostDispatch] accepted controls=%d", len(calls))
    return task
