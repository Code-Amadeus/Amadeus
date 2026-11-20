"""Locale projection for host-authored Slice process copy.

Canvas payloads stay canonical and preserve provider/artifact text verbatim.
Only fields explicitly marked with presentation message descriptors, plus
canonical phase and signal-label tokens, are localized here.  Both the legacy
wallpaper surface and Electron Slice therefore consume the same projection.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from server.presentation_runtime import normalize_presentation_locale


_COPY: dict[str, dict[str, str]] = {
    "en-US": {
        "phase.ready": "Ready",
        "phase.intake": "Intake",
        "phase.contract": "Contract",
        "phase.work": "Work",
        "phase.checkpoint": "Checkpoint",
        "phase.preview": "Preview",
        "phase.review": "Review",
        "phase.result": "Result",
        "phase.archive": "Archive",
        "phase.check": "Check",
        "phase.blocked": "Blocked",
        "phase.recovery": "Recovery",
        "provider.work_signal": "{provider} work signal",
        "provider.action_blocked": "{provider} action blocked",
        "provider.diff_preview": "{provider} diff preview",
        "provider.result_report": "{provider} result report",
        "provider.result_markdown": "### {provider} result\nProcess: `{status}`\nTask: {task}\nTools: {tools}\n\n{result}",
        "provider.executing_selected_task": "Working on the selected task.",
        "provider.phase": "{provider} is in the {phase} phase.",
        "provider.ready_for_review": "{provider} run is ready for review.",
        "provider.returned": "{provider} returned {status}.",
        "tool.latest": "Latest tool event: {tool}",
        "tool.event_count": "{count} tool event(s) observed.",
        "event.count": "{count} event(s)",
        "progress.provider_update": "Provider progress update",
        "progress.not_terminal": "Provider update; not terminal",
        "progress.semantic": "Semantic milestone",
        "progress.streaming": "Live provider output",
        "task.selected": "Selected task",
        "task.same_run": "Same provider run",
        "permission.provider_blocked": "The provider action was blocked by policy.",
        "permission.denied_operation": "{operation} was denied; this run cannot approve it in place.",
        "permission.narrower_alternative": "The run may continue with a narrower alternative.",
        "permission.stopped_run": "The stopped run cannot accept an in-place policy grant.",
        "permission.denied_title": "Permission denied",
        "permission.resolved_title": "Permission resolved",
        "permission.denied_lead": "The provider cannot continue with this operation.",
        "permission.resolved_lead": "The checkpoint was resolved; retry remains a separate run.",
        "permission.approval_required": "Approval required",
        "permission.approval_lead": "This operation requires explicit approval before it can continue.",
        "permission.action_blocked": "Provider action blocked",
        "permission.scoped_operation": "Scoped operation",
        "permission.recovery_required": "Export recovery required",
        "permission.recovery_lead": "The authorized export was interrupted. Retry only the previously approved targets.",
        "permission.recovery_signal": "Authorized Desktop export interrupted",
        "permission.no_additional": "No additional permission is requested",
        "heartbeat.elapsed": "{duration} elapsed",
        "heartbeat.quiet": "Provider quiet for {duration}",
        "heartbeat.monitoring": "Amadeus is still monitoring",
        "heartbeat.stopping": "Stopping…",
        "heartbeat.waiting_cancel": "Waiting for the provider to confirm cancellation",
        "heartbeat.no_events": "No provider events yet",
        "browser.instruction_updated": "Browser instruction updated",
        "browser.plan_stopped": "The remaining browser plan stopped; the browser session is still available.",
        "browser.stop_waiting": "Stop requested; waiting for the current browser action to reach a safe boundary.",
        "browser.replanned": "The new instruction is active; Browser replanned from the current page.",
        "browser.instruction_waiting": "New instruction received; waiting for the current browser action to reach a safe boundary.",
        "browser.revision": "Revision {revision}: {detail}",
        "diff.clean": "Working tree is clean.",
        "diff.no_new_changes": "No new provider changes detected.",
        "diff.file_count": "{count} file(s)",
        "diff.preview": "Diff preview",
        "diff.baseline_filtered": "Baseline filtered",
        "diff.local_git": "Local Git",
        "attempt.diff": "Attempt {number} diff",
        "diff.historical_unavailable": "Historical diff unavailable",
        "diff.no_persistent_baseline": "No persistent attempt baseline",
        "diff.historical_attribution_unavailable": "Historical attribution unavailable",
        "diff.ambiguous": "Diff unavailable because baseline ownership is ambiguous",
        "diff.proposed_desktop_files": "{count} proposed Desktop file(s)",
        "diff.attributed_files": "{count} attributed file(s)",
        "diff.no_attributed_changes": "No attributed Git changes",
        "diff.baseline_range": "Baseline {start} to {end}",
        "diff.ambiguous_origin": "Ambiguous origin",
        "diff.attempt_baseline": "Attempt baseline",
        "links.navigable": "{count} navigable source link(s)",
        "links.actions": "Link actions",
        "result.no_text": "No result text was returned.",
        "status.none": "None",
        "status.running": "Running",
        "status.done": "Done",
        "status.failed": "Failed",
        "status.error": "Error",
        "status.cancelled": "Cancelled",
    },
    "zh-CN": {
        "phase.ready": "就绪", "phase.intake": "接收", "phase.contract": "确认约束",
        "phase.work": "执行", "phase.checkpoint": "检查点", "phase.preview": "预览",
        "phase.review": "审阅", "phase.result": "结果", "phase.archive": "归档",
        "phase.check": "确认", "phase.blocked": "受阻", "phase.recovery": "恢复",
        "provider.work_signal": "{provider} 工作进展", "provider.action_blocked": "{provider} 操作受阻",
        "provider.diff_preview": "{provider} 差异预览", "provider.result_report": "{provider} 结果报告",
        "provider.result_markdown": "### {provider} 结果\n进程状态：`{status}`\n任务：{task}\n工具：{tools}\n\n{result}",
        "provider.executing_selected_task": "正在处理当前选中的任务。",
        "provider.phase": "{provider} 正处于{phase}阶段。", "provider.ready_for_review": "{provider} 执行已可供审阅。",
        "provider.returned": "{provider} 已返回，状态为{status}。", "tool.latest": "最近的工具事件：{tool}",
        "tool.event_count": "已观察到 {count} 个工具事件。", "event.count": "{count} 个事件",
        "progress.provider_update": "Provider 进度更新", "progress.not_terminal": "Provider 更新；尚未结束",
        "progress.semantic": "语义里程碑", "progress.streaming": "Provider 实时输出",
        "task.selected": "当前任务", "task.same_run": "同一次 Provider 执行",
        "permission.provider_blocked": "Provider 操作被策略阻止。",
        "permission.denied_operation": "{operation} 已被拒绝；本次执行无法在原地批准它。",
        "permission.narrower_alternative": "本次执行可以改用权限更窄的方案继续。",
        "permission.stopped_run": "已停止的执行无法再接受原地授权。",
        "permission.denied_title": "权限已拒绝", "permission.resolved_title": "权限已处理",
        "permission.denied_lead": "Provider 无法继续此操作。",
        "permission.resolved_lead": "检查点已处理；重试仍会创建一次新的执行。",
        "permission.approval_required": "需要批准", "permission.action_blocked": "Provider 操作受阻",
        "permission.approval_lead": "此操作需要明确批准后才能继续。",
        "permission.scoped_operation": "限定范围的操作", "permission.recovery_required": "需要恢复导出",
        "permission.recovery_lead": "已授权的导出被中断。只能重试此前批准的目标。",
        "permission.recovery_signal": "已授权的桌面导出被中断", "permission.no_additional": "不需要额外权限",
        "heartbeat.elapsed": "已运行 {duration}", "heartbeat.quiet": "Provider 已静默 {duration}",
        "heartbeat.monitoring": "Amadeus 仍在监控", "heartbeat.stopping": "正在停止…",
        "heartbeat.waiting_cancel": "正在等待 Provider 确认取消", "heartbeat.no_events": "尚未收到 Provider 事件",
        "browser.instruction_updated": "浏览器指令已更新",
        "browser.plan_stopped": "剩余的浏览器计划已停止；浏览器会话仍可使用。",
        "browser.stop_waiting": "已请求停止；正在等待当前浏览器操作到达安全边界。",
        "browser.replanned": "新指令已生效；浏览器已从当前页面重新规划。",
        "browser.instruction_waiting": "已收到新指令；正在等待当前浏览器操作到达安全边界。",
        "browser.revision": "修订 {revision}：{detail}",
        "diff.clean": "工作区没有变更。", "diff.no_new_changes": "未检测到新的 Provider 变更。",
        "diff.file_count": "{count} 个文件", "diff.preview": "差异预览",
        "diff.baseline_filtered": "已过滤基线", "diff.local_git": "本地 Git",
        "attempt.diff": "第 {number} 次执行的差异", "diff.historical_unavailable": "历史差异不可用",
        "diff.no_persistent_baseline": "没有持久化的执行基线", "diff.historical_attribution_unavailable": "无法进行历史归属",
        "diff.ambiguous": "基线归属不明确，无法显示可信差异",
        "diff.proposed_desktop_files": "{count} 个待导出的桌面文件", "diff.attributed_files": "{count} 个可归属文件",
        "diff.no_attributed_changes": "没有可归属的 Git 变更", "diff.baseline_range": "基线 {start} 至 {end}",
        "diff.ambiguous_origin": "来源不明确", "diff.attempt_baseline": "本次执行基线",
        "links.navigable": "{count} 个可访问的来源链接", "links.actions": "链接操作",
        "result.no_text": "Provider 未返回结果文本。", "status.none": "无", "status.running": "运行中",
        "status.done": "完成", "status.error": "错误", "status.cancelled": "已取消",
        "status.failed": "失败",
    },
    "ja-JP": {
        "phase.ready": "待機", "phase.intake": "受付", "phase.contract": "制約確認",
        "phase.work": "実行", "phase.checkpoint": "確認点", "phase.preview": "プレビュー",
        "phase.review": "レビュー", "phase.result": "結果", "phase.archive": "アーカイブ",
        "phase.check": "確認", "phase.blocked": "ブロック", "phase.recovery": "復旧",
        "provider.work_signal": "{provider} 作業進捗", "provider.action_blocked": "{provider} 操作がブロックされました",
        "provider.diff_preview": "{provider} 差分プレビュー", "provider.result_report": "{provider} 結果レポート",
        "provider.result_markdown": "### {provider} の結果\nプロセス状態：`{status}`\nタスク：{task}\nツール：{tools}\n\n{result}",
        "provider.executing_selected_task": "選択中のタスクを処理しています。",
        "provider.phase": "{provider} は{phase}フェーズです。", "provider.ready_for_review": "{provider} の実行はレビュー可能です。",
        "provider.returned": "{provider} が返りました。状態：{status}。", "tool.latest": "最新のツールイベント：{tool}",
        "tool.event_count": "{count} 件のツールイベントを確認しました。", "event.count": "{count} 件のイベント",
        "progress.provider_update": "Provider 進捗更新", "progress.not_terminal": "Provider 更新・未完了",
        "progress.semantic": "意味的マイルストーン", "progress.streaming": "Provider ライブ出力",
        "task.selected": "選択中のタスク", "task.same_run": "同じ Provider 実行",
        "permission.provider_blocked": "Provider 操作はポリシーによりブロックされました。",
        "permission.denied_operation": "{operation} は拒否されました。この実行中には承認できません。",
        "permission.narrower_alternative": "より限定的な代替手段で続行できます。",
        "permission.stopped_run": "停止済みの実行には、その場で権限を付与できません。",
        "permission.denied_title": "権限が拒否されました", "permission.resolved_title": "権限を処理しました",
        "permission.denied_lead": "Provider はこの操作を続行できません。",
        "permission.resolved_lead": "確認点は処理されました。再試行は別の実行になります。",
        "permission.approval_required": "承認が必要です", "permission.action_blocked": "Provider 操作がブロックされました",
        "permission.approval_lead": "この操作を続行するには明示的な承認が必要です。",
        "permission.scoped_operation": "範囲限定の操作", "permission.recovery_required": "エクスポートの復旧が必要です",
        "permission.recovery_lead": "承認済みのエクスポートが中断されました。以前に承認された対象だけを再試行してください。",
        "permission.recovery_signal": "承認済みデスクトップエクスポートが中断しました", "permission.no_additional": "追加の権限は要求されません",
        "heartbeat.elapsed": "経過時間 {duration}", "heartbeat.quiet": "Provider の更新なし：{duration}",
        "heartbeat.monitoring": "Amadeus が引き続き監視しています", "heartbeat.stopping": "停止中…",
        "heartbeat.waiting_cancel": "Provider のキャンセル確認を待っています", "heartbeat.no_events": "Provider イベントはまだありません",
        "browser.instruction_updated": "ブラウザ指示を更新しました",
        "browser.plan_stopped": "残りのブラウザ計画を停止しました。ブラウザセッションは引き続き利用できます。",
        "browser.stop_waiting": "停止を要求しました。現在のブラウザ操作が安全な境界に達するのを待っています。",
        "browser.replanned": "新しい指示が有効になり、現在のページから再計画しました。",
        "browser.instruction_waiting": "新しい指示を受信しました。現在のブラウザ操作が安全な境界に達するのを待っています。",
        "browser.revision": "改訂 {revision}：{detail}",
        "diff.clean": "作業ツリーはクリーンです。", "diff.no_new_changes": "新しい Provider 変更はありません。",
        "diff.file_count": "{count} ファイル", "diff.preview": "差分プレビュー",
        "diff.baseline_filtered": "ベースラインを除外", "diff.local_git": "ローカル Git",
        "attempt.diff": "実行 {number} の差分", "diff.historical_unavailable": "履歴差分は利用できません",
        "diff.no_persistent_baseline": "永続化された実行ベースラインがありません",
        "diff.historical_attribution_unavailable": "履歴の帰属を確認できません",
        "diff.ambiguous": "ベースラインの所有が曖昧なため、信頼できる差分を表示できません",
        "diff.proposed_desktop_files": "デスクトップ出力予定 {count} ファイル", "diff.attributed_files": "帰属済み {count} ファイル",
        "diff.no_attributed_changes": "帰属可能な Git 変更はありません", "diff.baseline_range": "ベースライン {start} から {end}",
        "diff.ambiguous_origin": "出所が曖昧", "diff.attempt_baseline": "実行ベースライン",
        "links.navigable": "移動可能なソースリンク {count} 件", "links.actions": "リンク操作",
        "result.no_text": "結果テキストは返されませんでした。", "status.none": "なし", "status.running": "実行中",
        "status.done": "完了", "status.error": "エラー", "status.cancelled": "キャンセル済み",
        "status.failed": "失敗",
    },
}


_SIGNAL_LABELS = {
    "provider": {"en-US": "Provider", "zh-CN": "Provider", "ja-JP": "Provider"},
    "tool": {"en-US": "Tool", "zh-CN": "工具", "ja-JP": "ツール"},
    "tools": {"en-US": "Tools", "zh-CN": "工具", "ja-JP": "ツール"},
    "report": {"en-US": "Report", "zh-CN": "报告", "ja-JP": "レポート"},
    "stream": {"en-US": "Stream", "zh-CN": "实时输出", "ja-JP": "ストリーム"},
    "task": {"en-US": "Task", "zh-CN": "任务", "ja-JP": "タスク"},
    "elapsed": {"en-US": "Elapsed", "zh-CN": "用时", "ja-JP": "経過"},
    "heartbeat": {"en-US": "Heartbeat", "zh-CN": "运行状态", "ja-JP": "稼働状態"},
    "instruction": {"en-US": "Instruction", "zh-CN": "指令", "ja-JP": "指示"},
    "permission": {"en-US": "Permission", "zh-CN": "权限", "ja-JP": "権限"},
    "checkpoint": {"en-US": "Checkpoint", "zh-CN": "检查点", "ja-JP": "確認点"},
    "diff": {"en-US": "Diff", "zh-CN": "差异", "ja-JP": "差分"},
    "status": {"en-US": "Status", "zh-CN": "状态", "ja-JP": "状態"},
    "source": {"en-US": "Source", "zh-CN": "来源", "ja-JP": "ソース"},
    "links": {"en-US": "Links", "zh-CN": "链接", "ja-JP": "リンク"},
    "engine": {"en-US": "Engine", "zh-CN": "引擎", "ja-JP": "エンジン"},
    "recovery": {"en-US": "Recovery", "zh-CN": "恢复", "ja-JP": "復旧"},
}

_PROVIDER_DISPLAY_ALIASES = {
    "codex": "Codex",
    "codex app server": "Codex",
    "codex-app-server": "Codex",
    "direct codex": "Codex",
}


def project_canvas_presentation(
    payload: Mapping[str, Any] | None,
    *,
    locale: object = None,
) -> dict[str, Any]:
    """Resolve presentation metadata without modifying canonical content."""

    selected_locale = normalize_presentation_locale(locale)
    output = dict(payload or {})
    phase = str(output.get("phase") or "").strip()
    if phase:
        output["phase"] = _copy(selected_locale, f"phase.{phase.lower()}", phase)
    _project_fields(output, selected_locale)

    signals = output.get("signals")
    if isinstance(signals, list):
        projected_signals: list[Any] = []
        for value in signals:
            if not isinstance(value, Mapping):
                projected_signals.append(value)
                continue
            signal = dict(value)
            raw_label = str(signal.get("label") or "").strip()
            if raw_label:
                label_copy = _SIGNAL_LABELS.get(raw_label.lower())
                if label_copy:
                    signal["label"] = label_copy.get(selected_locale) or label_copy["en-US"]
            _project_fields(signal, selected_locale)
            projected_signals.append(signal)
        output["signals"] = projected_signals

    report_view = output.get("reportView")
    if isinstance(report_view, Mapping):
        projected_report = dict(report_view)
        _project_fields(projected_report, selected_locale)
        report_phase = str(projected_report.get("phase") or "").strip()
        if report_phase:
            projected_report["phase"] = _copy(
                selected_locale,
                f"phase.{report_phase.lower()}",
                report_phase,
            )
        output["reportView"] = projected_report
    return output


def _project_fields(value: dict[str, Any], locale: str) -> None:
    metadata = value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {}
    presentation = metadata.get("presentation") if isinstance(metadata.get("presentation"), Mapping) else {}
    for field, descriptor in presentation.items():
        if not isinstance(descriptor, Mapping):
            continue
        key = str(descriptor.get("key") or "").strip()
        if not key:
            continue
        field_path = str(field)
        fallback = str(_get_path(value, field_path) or "")
        params = descriptor.get("params") if isinstance(descriptor.get("params"), Mapping) else {}
        _set_path(value, field_path, _format(locale, key, params, fallback))


def _get_path(value: Mapping[str, Any], field_path: str) -> Any:
    current: Any = value
    for part in field_path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _set_path(value: dict[str, Any], field_path: str, projected: str) -> None:
    parts = [part for part in field_path.split(".") if part]
    if not parts:
        return
    current = value
    for part in parts[:-1]:
        nested = current.get(part)
        if not isinstance(nested, Mapping):
            return
        copied = dict(nested)
        current[part] = copied
        current = copied
    current[parts[-1]] = projected


def _format(locale: str, key: str, params: Mapping[str, Any], fallback: str) -> str:
    template = _copy(locale, key, fallback)
    normalized: dict[str, str] = {}
    for name, value in params.items():
        text = str(value)
        if str(name) in {"phase", "status"}:
            text = _copy(locale, f"{name}.{text.strip().lower()}", text)
        elif str(name) == "provider":
            text = _PROVIDER_DISPLAY_ALIASES.get(text.strip().lower(), text)
        elif str(name) == "tools" and text.strip().lower() == "none":
            text = _copy(locale, "status.none", text)
        normalized[str(name)] = text
    try:
        return template.format_map(_SafeParams(normalized))
    except (ValueError, KeyError):
        return fallback or template


def _copy(locale: str, key: str, fallback: str) -> str:
    return _COPY.get(locale, {}).get(key) or _COPY["en-US"].get(key) or fallback


class _SafeParams(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
