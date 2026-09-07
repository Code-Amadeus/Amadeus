"""Local visual fixture server for the actual compiled Companion component.

No Codex connection, model calls, speech synthesis or persistent task writes.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.codex_desktop_observer import CodexDesktopObserver

CHARACTER = Path(os.environ.get("AMADEUS_PREVIEW_CHARACTER_ROOT", str(ROOT / "assets/spriteforge/runtime/kurisu"))).resolve()
app = FastAPI()
app.mount("/ui", StaticFiles(directory=ROOT / "electron/dist/renderer", html=True))
app.mount("/render", StaticFiles(directory=ROOT / "render/web", html=True))
app.mount("/character", StaticFiles(directory=CHARACTER))
clients = set()
runs = []
observer = None
retention_tasks = []


async def observe_codex(ws):
    previous = None
    while True:
        snapshot = await asyncio.to_thread(observer.poll)
        if snapshot != previous:
            await event(ws, "companion.tasks", snapshot)
            previous = snapshot
        await asyncio.sleep(1)


async def event(ws, method, params):
    await ws.send_json({"type": "evt", "id": "preview", "method": method, "params": params})


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    observation = asyncio.create_task(observe_codex(ws)) if observer else None
    try:
        while True:
            message = await ws.receive_json()
            method = message["method"]
            result = {}
            if method == "render.start":
                result = {"url": f"http://{ws.headers['host']}/render/index.html"}
            elif method == "provider.list":
                result = {"runs": runs}
            elif method == "companion.tasks":
                result = await asyncio.to_thread(observer.poll) if observer else {"tasks": retention_tasks}
            elif method == "render.ready":
                manifest = json.loads((CHARACTER / "runtime_manifest.json").read_text())
                for name in ("idle", "speaking_short"):
                    clip = manifest["clips"][name]
                    urls = [f"http://{ws.headers['host']}/character/{file}" for file in clip["frames"]]
                    await event(ws, "render.sprite_frames", {"emotion": name, "urls": urls})
                    await event(ws, "render.idle_frame_interval", {"emotion": name, "intervalMs": clip["frameIntervalMs"]})
                await event(ws, "render.mode", {"mode": "sprite"})
                await event(ws, "render.idle_animation", {"enabled": True})
                await event(ws, "render.emotion", {"emotion": "idle"})
            elif method == "companion.speak":
                result = {"error": "视觉预览不合成语音"}
            await ws.send_json({"type": "res", "id": message["id"], "method": method, "params": result})
    except WebSocketDisconnect:
        pass
    finally:
        if observation:
            observation.cancel()
        clients.discard(ws)


@app.post("/scene/{count}")
async def scene(count: int):
    global retention_tasks
    if observer:
        return {"error": "真实 Codex 观察模式不能混入示例任务"}
    now = int(time.time() * 1000)
    samples = [
        {"id":"preview-question", "title":"确认导出格式", "phase":"attention", "projectId":"preview-amadeus", "projectName":"Amadeus · 示例",
         "detail":"这份结果需要导出为 CSV，还是保留为 Excel 工作簿？"},
        {"id":"preview-end", "title":"调整人物交互", "phase":"ready", "projectId":"preview-amadeus", "projectName":"Amadeus · 示例",
         "detail":"已调整卡片的展开与收起。点击空白可以回到概览，同项目的任务仍然聚在一起。"},
        {"id":"preview-progress", "title":"核对示例记录", "phase":"running", "projectId":"preview-report", "projectName":"示例报表 · 示例",
         "detail":"正在核对示例报表的记录与验证结果。"},
        {"id":"preview-agent", "title":"检查副屏布局", "phase":"ready", "parentTaskId":"preview-question", "sourceKind":"subagent", "projectId":"preview-amadeus", "projectName":"Amadeus · 示例",
         "detail":"三种卡片状态都在工作区内，文字和角色没有重叠。"},
    ]
    if count == 15:
        samples.extend({"id":f"preview-extra-amadeus-{i}","title":f"同项目任务 {i + 3}","phase":"running",
                        "projectId":"preview-amadeus","projectName":"Amadeus · 示例","detail":"检查层叠展开后，每个任务仍可独立阅读。"}
                       for i in range(3))
        samples.extend({"id":f"preview-project-{i}-task-{j}","title":f"{title} · {j + 1}","phase":"running",
                        "projectId":f"preview-project-{i}","projectName":f"{name} · 示例","detail":detail}
                       for i, name, title, detail in [
                           (0, "演示网站", "检查页面布局", "正在检查导航、按钮与正文的布局。"),
                           (1, "示例笔记", "整理今日练习", "已整理今天的复习内容，下一步核对例句。"),
                           (2, "素材整理", "检查运行状态", "正在检查后台任务的最新状态与记录。")]
                       for j in range(2))
    elif count == 9:
        samples.extend({"id":f"preview-extra-amadeus-{i}","title":f"同项目任务 {i}","phase":"running",
                        "projectId":"preview-amadeus","projectName":"Amadeus · 示例","detail":"检查从项目展开后选中第三、第四个任务。"}
                       for i in range(2))
    elif count > 4:
        samples.extend({"id":f"preview-extra-{i}","title":f"独立任务 {i}","phase":"running",
                        "projectId":f"preview-project-{i}","projectName":f"其他项目 {i} · 示例","detail":"仅用于拥挤状态下的交互检查。"}
                       for i in range(4))
    retention_tasks = []
    for index, sample in enumerate(samples[:max(0,count)]):
        sample.update({"key":sample["id"]+f":{now}","provider":"示例","sourceLabel":"视觉夹具","lastActivityAt":now,
                       "codexThreadId":f"00000000-0000-4000-8000-{index:012d}",
                       "repeatable":sample["phase"] in ("attention","ready"),"announce":False,
                       "activities":[{"id":"p1","kind":"progress","text":"先核对当前任务的目标和已有结果。","at":now},
                                     {"id":"p2","kind":"progress","text":"已完成检查，正在汇总需要你确认的事项。","at":now},
                                     {"id":"t1","kind":"tool","text":"读取项目文件","status":"returned","at":now}]})
        retention_tasks.append(sample)
    if len(retention_tasks) > 1:
        retention_tasks[1]["detail"] = "已调整卡片的展开与收起。**同项目任务**会一起移动，点击空白返回概览。\n\n| 内容 | 显示方式 |\n| --- | --- |\n| 进展更新 | 一条一张小卡片 |\n| 子代理 | 独立的分支任务 |\n\n- [x] 保留项目和任务的对应关系\n- [x] 原文按 Markdown 渲染\n\n```python\nprint('Amadeus')\n```\n\n这是一份视觉夹具，不是真实任务产出。"
    for ws in list(clients):
        await event(ws,"companion.tasks",{"tasks":retention_tasks,"note":"仅用于视觉与交互检查的示例"})
    return {"count":len(retention_tasks)}


@app.post("/organic/{count}")
async def organic_fixture(count: int):
    global retention_tasks
    if observer:
        return {"error": "真实观察模式不能注入示例任务"}
    if count not in range(1, 6):
        return {"error": "项目数量应为 1 到 5"}
    now = int(time.time() * 1000)
    names = ["示例报表", "Amadeus", "素材整理", "示例笔记", "演示网站"]
    counts = [1, 2, 1, 2, 4]
    retention_tasks = [{"id": f"organic-{i}-{j}", "key": f"organic-{i}-{j}", "projectId": f"organic-{i}",
                        "projectName": name + " · 示例", "title": ["核对当前结果", "确认下一步安排"][j % 2],
                        "detail": "当前结果已整理，可以展开阅读完整进展。项目任务聚在一起，位置不会随普通状态更新而改变。",
                        "phase": "attention" if j == 1 else "ready", "provider": "示例", "sourceLabel": "视觉夹具",
                        "repeatable": True, "announce": False, "lastActivityAt": now,
                        "codexThreadId": f"00000000-0000-4000-8000-{i * 10 + j:012d}"}
                       for i, name in enumerate(names[:count]) for j in range(counts[i])]
    for ws in list(clients):
        await event(ws, "companion.tasks", {"tasks": retention_tasks})
    return {"projects": count}


@app.post("/retention/{action}")
async def retention_fixture(action: str):
    global retention_tasks
    if observer:
        return {"error": "真实 Codex 观察模式不能混入示例任务"}
    if action == "start":
        await scene(0)
        fixture_id = int(time.time() * 1000)
        retention_tasks = [{"id": f"retention-{phase}-{fixture_id}", "key": f"retention-{phase}-{fixture_id}:1", "provider": "示例",
                            "sourceLabel": "8 小时边界测试", "title": f"留存测试 {phase}", "detail": "仅用于验证卡片留存，不是真实任务。",
                            "phase": phase, "repeatable": False, "announce": False,
                            "lastActivityAt": int(time.time() * 1000) - 8 * 60 * 60 * 1000 + 3000}
                           for phase in ("running", "attention", "ready")]
    elif action == "activity":
        retention_tasks[0]["lastActivityAt"] = int(time.time() * 1000)
    elif action == "clear":
        retention_tasks = []
    for ws in list(clients):
        await event(ws, "companion.tasks", {"tasks": retention_tasks})
    return {"fixture": True, "action": action}


@app.post("/reading/{kind}")
async def reading_fixture(kind: str):
    if observer:
        return {"error": "真实观察模式不能混入阅读夹具"}
    await scene(4)
    task = retention_tasks[1]
    if kind == "short":
        task["detail"] = "已完成布局检查。五个项目可以同时显示，点击空白逐层返回。"
    elif kind in ("prose", "long-title"):
        task["detail"] = "\n\n".join([
            "已完成副屏布局与阅读区的调整。项目名称和任务标题仍然醒目，正文获得了更多空间，短结果可以直接读完。",
            "当前结果优先展示主要结论。五个项目分布在人物上方和两侧，同项目任务在空间不足时层叠，点击后可以分散展开。",
            "任务卡独立移动和缩放，连线跟随卡片的位置变化。进入阅读时，当前任务不会同时出现在右侧缩略区，避免误解任务数量。",
            "长内容可以继续展开，保留原有段落、列表和格式。查看结果不会向 Codex 提交答案，也不会批准任何操作。",
            "下一步可以在真实任务中检查阅读体验，确认预览提供的信息已经足够判断是否需要查看全文。",
        ])
    if kind == "long-title":
        task["title"] = "检查多项目桌宠提醒中的标题换行、当前结果阅读空间以及任务卡展开后的项目归属与快捷操作"
    task["key"] += ":reading:" + kind
    for ws in list(clients):
        await event(ws, "companion.tasks", {"tasks": retention_tasks, "note": "阅读空间检查 · 示例内容"})
    return {"fixture": True, "kind": kind}


@app.post("/caption")
async def caption_fixture():
    if observer:
        return {"error": "真实观察模式不能注入示例字幕"}
    for ws in list(clients):
        await event(ws, "tts.sentence_start", {"sentence_id": "preview-spoken", "turn_id": "preview",
                    "text": "コーデックスの結果、確認できたわ。元の文章と私の言い方を、ここで見比べられるわね。"})
        await event(ws, "tts.turn_complete", {})
    return {"fixture": True, "audio_played": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--codex-thread", action="append", default=[])
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    args = parser.parse_args()
    if args.codex_thread:
        observer = CodexDesktopObserver(args.codex_home, args.codex_thread)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
