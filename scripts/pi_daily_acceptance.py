"""Opt-in live daily-agent acceptance; uses configured models and opens real apps.

Run with --provider pi or openclaw. Artifacts stay under ignored runtime/.
Only the requested Notepad launch is approved by this harness.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_types import ProviderPermissionResponse, ProviderRunRequest


async def main(provider: str, label: str):
    from config import settings
    from agent_host.adapters.pi import PiAdapter
    from agent_host.adapters.openclaw import OpenClawAdapter

    workspace = ROOT / "runtime" / "pi-acceptance" / label / provider
    workspace.mkdir(parents=True, exist_ok=False)
    note = workspace / f"{provider}-{label}-article-summary.txt"
    if provider == "pi":
        adapter = PiAdapter(agent_dir=workspace / "agent", model_provider="deepseek",
            model=settings.DEEPSEEK_MODEL_NAME, timeout=300)
        adapter.require_startup_ready()
    else:
        adapter = OpenClawAdapter()
    task = f"""请实际完成这项日常任务，不要只给操作建议：
搜索并找到 Ashish Vaswani 等作者的 Attention Is All You Need 在 arXiv 的原始页面，
实际读取该页面的摘要（不要求下载论文）。把论文标题、首作者、首次提交日期、三条中文摘要要点、
原文 URL 写入 UTF-8 文件：{note}。
然后用系统默认浏览器实际打开找到的原文网页，再启动 Windows 记事本打开这个摘要文件。
本次已授权上述文件创建及网页和记事本打开操作。文件写入仅限 {workspace}；不要修改其他文件。
完成后用中文简述已做的操作；工具失败时如实说明，不把未完成的步骤说成完成。
"""
    events = []
    run_id = f"acceptance-{provider}-{label}"

    async def emit(event):
        events.append(asdict(event))
        if event.type == "tool.call":
            print(json.dumps({"event": event.type, "tool": event.payload.get("tool")}), flush=True)
        if event.type == "permission.requested":
            request = event.payload["permissionRequest"]
            reason = request.get("reason", "")
            allowed = False
            try:
                title, raw = reason.split("\n", 1)
                args = json.loads(raw)
                executable = str(args.get("executable", "")).replace("\\", "/").rsplit("/", 1)[-1].lower()
                allowed = title == "Launch application" and executable in {"notepad", "notepad.exe"} and args.get("args") == [str(note)]
            except (ValueError, TypeError):
                pass
            await adapter.resolve_permission(run_id, ProviderPermissionResponse(request["request_id"], allowed))
            print(json.dumps({"permission_allowed": allowed, "title": reason.split("\n", 1)[0]}), flush=True)

    start = time.monotonic()
    result = await adapter.run(ProviderRunRequest(provider=provider, task=task, cwd=str(workspace),
        requirements=ProviderRequirements(task_kind="general", workspace_access="write" if provider == "pi" else "none",
            workspace_ownership="caller" if provider == "pi" else "none"),
        metadata={"timeout": 300, "presentation_locale": "zh-CN"}), run_id, emit)
    report = {"provider": provider, "elapsed_seconds": round(time.monotonic()-start, 2),
        "task": task, "result": asdict(result), "events": events,
        "file_exists": note.is_file(), "file_content": note.read_text(encoding="utf-8-sig") if note.is_file() else ""}
    (workspace / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in {"events", "task"}}, ensure_ascii=True), flush=True)
    close = getattr(adapter, "close", None)
    if close:
        await close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("pi", "openclaw"), required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if not args.label.replace("-", "").isalnum():
        parser.error("label must contain only letters, digits and hyphens")
    asyncio.run(main(args.provider, args.label))
