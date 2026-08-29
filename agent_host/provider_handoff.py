"""Human-readable handoff text for persisted Provider conversations.

Provider execution contracts are necessary model context, but a persisted
Codex thread is also a surface a person may open and continue directly. Keep
the user's request readable there and leave Host-only policy in a separate
developer-instruction item.
"""

from __future__ import annotations

from dataclasses import dataclass


_MAX_VISIBLE_SOURCE = 2000
_MAX_VISIBLE_CONTEXT = 1000
_MAX_THREAD_NAME = 96


@dataclass(frozen=True, slots=True)
class ProviderHandoffPresentation:
    """The human-visible message and title for one attached Provider turn."""

    user_message: str
    thread_name: str


CODEX_HANDOFF_CONVERSATION_CONTRACT = """
Codex desktop handoff contract (presentation only; this does not change task authority):
This persisted thread may be opened and continued directly by the user in Codex Desktop.
Keep Host-only policies, staging paths, Attempt ids, protocol boilerplate, and reporting
contracts out of ordinary progress and final prose unless the user must act on one of them.
Describe the requested outcome, material changes, validation, and any remaining blocker in
plain language. Refer to Amadeus by name instead of calling it "the Host". If the user later
continues this thread directly, inspect the current workspace state before making further edits.
On Windows, the temporary `apply_patch.bat` wrapper cannot reliably preserve multiline or
large arguments. For a new or substantially rewritten multiline file, use the JS REPL with
a block-scoped ESM import and write the exact workspace file directly:
```
{
  const fsForWorkspaceEdit = await import('node:fs');
  fsForWorkspaceEdit.writeFileSync(absoluteTargetPath, completeContent, 'utf8');
}
```
Use a fresh block scope for every JS REPL edit so persistent bindings cannot collide. Reserve
patching for small edits to existing files; do not probe the batch wrapper, execute raw
patch/source lines as PowerShell, embed a whole file in `pwsh -Command`, or nest another `pwsh`.
""".strip()


_LABELS = {
    "en-US": {
        "title": "Amadeus handoff",
        "request": "Current user request",
        "context": "Relevant prior context",
        "continue": "Continue in the prepared workspace and inspect existing progress before editing.",
    },
    "zh-CN": {
        "title": "Amadeus 任务交接",
        "request": "用户当前请求",
        "context": "相关前文",
        "continue": "请在已准备的工作区中继续，修改前先检查现有进度。",
    },
    "ja-JP": {
        "title": "Amadeus タスク引き継ぎ",
        "request": "現在のユーザー依頼",
        "context": "関連する直前の文脈",
        "continue": "準備済みのワークスペースで、既存の進捗を確認してから続行してください。",
    },
}

_RECOVERY_MESSAGES = {
    "en-US": (
        "Amadeus execution continuation\n\n"
        "The preceding turn stopped after reporting progress and before any observable "
        "execution. Continue the same already-authorized request from the current workspace "
        "state. Do not broaden its scope or repeat completed work; report a concrete blocker "
        "only if one actually prevents continuation."
    ),
    "zh-CN": (
        "Amadeus 执行续接\n\n"
        "上一轮在汇报进度后、出现任何可观测执行前就停止了。请从当前工作区状态继续同一个"
        "已授权请求，不要扩大范围，也不要重复已经完成的工作；只有确实无法继续时才报告具体阻塞。"
    ),
    "ja-JP": (
        "Amadeus 実行継続\n\n"
        "直前のターンは進捗を報告した後、観測可能な実行を行う前に停止しました。現在の"
        "ワークスペース状態から、承認済みの同じ依頼を続けてください。範囲を広げたり完了済みの"
        "作業を繰り返したりせず、実際に続行不能な場合だけ具体的な阻害要因を報告してください。"
    ),
}


def codex_handoff_presentation(
    task: str,
    *,
    source_user_text: str = "",
    source_user_context: str = "",
    presentation_locale: object = None,
) -> ProviderHandoffPresentation:
    """Build the compact conversation surface a user sees after attaching."""

    locale = _normalize_locale(presentation_locale)
    labels = _LABELS[locale]
    task_text = _compact(task, _MAX_VISIBLE_SOURCE)
    source = _compact(source_user_text, _MAX_VISIBLE_SOURCE) or task_text
    context = _compact(source_user_context, _MAX_VISIBLE_CONTEXT)
    if context == source:
        context = ""

    sections = [
        labels["title"],
        f'{labels["request"]}:\n“{source or task_text or "(empty task)"}”',
    ]
    if context:
        sections.append(f'{labels["context"]}:\n“{context}”')
    sections.append(labels["continue"])

    title_source = source or task_text or labels["title"]
    thread_name = _compact(f"Amadeus · {title_source}", _MAX_THREAD_NAME)
    return ProviderHandoffPresentation(
        user_message="\n\n".join(sections),
        thread_name=thread_name,
    )


def provider_recovery_user_message(*, presentation_locale: object = None) -> str:
    """Describe a Host-owned same-authority continuation without replaying the user."""

    return _RECOVERY_MESSAGES[_normalize_locale(presentation_locale)]


def _normalize_locale(value: object) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    if raw in {"zh", "zh-cn", "chinese", "simplified-chinese"}:
        return "zh-CN"
    if raw in {"ja", "jp", "ja-jp", "japanese"}:
        return "ja-JP"
    return "en-US"


def _compact(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
