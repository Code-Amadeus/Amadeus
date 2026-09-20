"""Host-deterministic presentation for durable context status facts.

Natural-language operation classification belongs to the model-owned
DELEGATE/ControlDecision path. This module formats already-authorized Ledger
reads only; it does not decide that arbitrary user prose means ``report``.
"""

from __future__ import annotations

from typing import Any


def render_project_status(snapshot: dict[str, Any]) -> tuple[str, str]:
    """Render one Project snapshot without adding facts outside the Ledger."""

    name = str(snapshot.get("projectName") or "当前项目")
    counts = snapshot.get("counts") if isinstance(snapshot.get("counts"), dict) else {}
    current = int(counts.get("current") or 0)
    running = int(counts.get("running") or 0)
    needs = int(counts.get("needsYou") or 0)
    history = int(counts.get("history") or 0)
    recent = snapshot.get("recent") if isinstance(snapshot.get("recent"), list) else []
    recent_bits_zh: list[str] = []
    recent_bits_ja: list[str] = []
    for item in recent[:3]:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "未命名任务").split())[:50]
        zh_state, ja_state = project_item_status_labels(item)
        recent_bits_zh.append(f"{title}（{zh_state}）")
        recent_bits_ja.append(f"{title}（{ja_state}）")
    recent_zh = "、".join(recent_bits_zh) or "还没有任务记录"
    recent_ja = "、".join(recent_bits_ja) or "まだタスク記録はありません"
    if needs:
        next_zh = f"先处理 {needs} 项需要你介入的工作。"
        next_ja = f"まず、あなたの対応が必要な {needs} 件を処理します。"
    elif running:
        next_zh = f"继续跟踪 {running} 项正在执行的工作。"
        next_ja = f"実行中の {running} 件を引き続き追跡します。"
    elif current:
        next_zh = "检查当前工作并决定继续、验收或归档。"
        next_ja = "現在の作業を確認し、継続・受け入れ・アーカイブを決めます。"
    else:
        next_zh = "目前没有待推进的工作。"
        next_ja = "現在、進める必要のある作業はありません。"
    display = (
        f"“{name}”目前有 {current} 项当前工作，其中 {running} 项执行中、"
        f"{needs} 项需要你介入；历史记录 {history} 项。最近：{recent_zh}。下一步：{next_zh}"
    )
    voice_ja = (
        f"「{name}」には現在 {current} 件の作業があり、{running} 件が実行中、"
        f"{needs} 件はあなたの対応待ちです。履歴は {history} 件です。"
        f"直近は、{recent_ja}。次は、{next_ja}"
    )
    return display, voice_ja


def project_item_status_labels(item: dict[str, Any]) -> tuple[str, str]:
    """Return the shared Chinese/Japanese label for a projected WorkItem."""

    execution = str(item.get("execution") or "idle").strip().lower()
    attention = str(item.get("attention") or "none").strip().lower()
    state = str(item.get("state") or "open").strip().lower()
    if execution in {"queued", "running"}:
        return "执行中", "実行中"
    if attention not in {"", "none"}:
        return "需要处理", "対応が必要"
    if state == "accepted":
        return "已验收", "受け入れ済み"
    if state == "archived":
        return "已归档", "アーカイブ済み"
    if execution in {"failed", "orphaned"}:
        return "失败", "失敗"
    return "当前", "進行中"
