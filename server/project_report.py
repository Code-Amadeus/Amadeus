"""Provider-neutral Project report queries over the durable Work Ledger.

The conversational model classifies the referent; this module does not repeat
that NLP with language-specific regular expressions.  Once ``subject=project``
is declared, every fact below comes from Project Registry / Work Ledger state
and no execution Provider is involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from server.context_status import project_item_status_labels, render_project_status


ReportSubject = Literal["work_item", "project"]


@dataclass(frozen=True)
class ProjectReportAnswer:
    display_text: str
    voice_text_ja: str
    status: Literal["answered", "empty", "not_found", "unavailable"]
    project_id: str = ""


def normalize_report_subject(value: Any) -> ReportSubject | None:
    """Normalize the small report vocabulary; missing preserves legacy calls."""

    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return "work_item"
    aliases: dict[str, ReportSubject] = {
        "work_item": "work_item",
        "workitem": "work_item",
        "task": "work_item",
        "project": "project",
        "projects": "project",
    }
    return aliases.get(raw)


def answer_project_report(
    coordinator: Any,
    *,
    project_id: str = "",
    display_language: str = "simplified_chinese",
    limit: int = 5,
) -> ProjectReportAnswer:
    """Answer one Project status or list recent routeable Projects."""

    if coordinator is None:
        return _localized_answer(
            "项目账本目前不可读取；我没有据此猜测，也没有启动任何工作。",
            "現在プロジェクト台帳を読み取れません。推測も作業開始もしていません。",
            status="unavailable",
            display_language=display_language,
        )

    clean_project_id = str(project_id or "").strip()
    if clean_project_id:
        snapshot = coordinator.project_status_snapshot(clean_project_id)
        if snapshot is None:
            return _localized_answer(
                "工作账本中找不到这个项目；我没有猜测其他项目，也没有启动任何工作。",
                "作業台帳にそのプロジェクトはありません。別のプロジェクトを推測せず、作業も開始していません。",
                status="not_found",
                display_language=display_language,
                project_id=clean_project_id,
            )
        display, voice_ja = render_project_status(snapshot)
        if _is_japanese(display_language):
            display = voice_ja
        return ProjectReportAnswer(
            display_text=display,
            voice_text_ja=voice_ja,
            status="answered",
            project_id=str(snapshot.get("projectId") or clean_project_id),
        )

    catalog = coordinator.project_catalog(limit=max(1, min(int(limit), 20)))
    snapshots: list[dict[str, Any]] = []
    for project in catalog:
        if not isinstance(project, dict):
            continue
        snapshot = coordinator.project_status_snapshot(str(project.get("projectId") or ""))
        if isinstance(snapshot, dict):
            snapshots.append(snapshot)
    if not snapshots:
        return _localized_answer(
            "项目账本中还没有可继续的本地项目。",
            "プロジェクト台帳には、まだ再開できるローカルプロジェクトがありません。",
            status="empty",
            display_language=display_language,
        )

    zh_rows: list[str] = []
    ja_rows: list[str] = []
    for index, snapshot in enumerate(snapshots, start=1):
        name = _trim(snapshot.get("projectName"), fallback="未命名项目", limit=52)
        counts = snapshot.get("counts") if isinstance(snapshot.get("counts"), dict) else {}
        current = int(counts.get("current") or 0)
        running = int(counts.get("running") or 0)
        needs = int(counts.get("needsYou") or 0)
        history = int(counts.get("history") or 0)
        recent = snapshot.get("recent") if isinstance(snapshot.get("recent"), list) else []
        latest = next((item for item in recent if isinstance(item, dict)), None)
        latest_zh = "还没有任务记录"
        latest_ja = "まだタスク記録なし"
        if latest is not None:
            title = _trim(latest.get("title"), fallback="未命名任务", limit=42)
            state_zh, state_ja = project_item_status_labels(latest)
            latest_zh = f"最近是 {title}（{state_zh}）"
            latest_ja = f"直近は {title}（{state_ja}）"
        zh_rows.append(
            f"{index}. {name}：当前 {current} 项，执行中 {running} 项，"
            f"需要你介入 {needs} 项，历史 {history} 项；{latest_zh}"
        )
        ja_rows.append(
            f"{index}. {name}：現在 {current} 件、実行中 {running} 件、"
            f"対応待ち {needs} 件、履歴 {history} 件。{latest_ja}"
        )
    display_zh = f"最近可继续的本地项目有 {len(zh_rows)} 个：" + "；".join(zh_rows) + "。"
    voice_ja = f"最近再開できるローカルプロジェクトは {len(ja_rows)} 件です。" + "。".join(ja_rows) + "。"
    return ProjectReportAnswer(
        display_text=voice_ja if _is_japanese(display_language) else display_zh,
        voice_text_ja=voice_ja,
        status="answered",
    )


def _localized_answer(
    display_zh: str,
    voice_ja: str,
    *,
    status: Literal["answered", "empty", "not_found", "unavailable"],
    display_language: str,
    project_id: str = "",
) -> ProjectReportAnswer:
    return ProjectReportAnswer(
        display_text=voice_ja if _is_japanese(display_language) else display_zh,
        voice_text_ja=voice_ja,
        status=status,
        project_id=project_id,
    )


def _trim(value: Any, *, fallback: str, limit: int) -> str:
    text = " ".join(str(value or fallback).split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _is_japanese(display_language: str) -> bool:
    return str(display_language or "").strip().lower() == "japanese"
