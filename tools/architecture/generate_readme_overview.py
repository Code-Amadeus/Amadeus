"""Render the localized README overview from one shared layout.

This is a presentation of the boundaries documented in ARCHITECTURE.md,
architecture/workspace.dsl, and docs/auip_application_sessions.md, not a
runtime topology model. It also marks the proposed memory/persona extension
as roadmap-only. The detailed generated Mermaid views remain separate.
"""

from __future__ import annotations

import argparse
from html import escape
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BOXES = {
    "host": (470, 175, 650, 809),
    "user": (30, 185, 360, 120),
    "desktop": (30, 350, 360, 180),
    "surface": (30, 570, 360, 158),
    "headless": (30, 798, 360, 132),
    "chat": (500, 220, 590, 148),
    "memory": (1230, 351, 510, 115),
    "work": (500, 406, 590, 210),
    "auip": (500, 658, 590, 146),
    "voice": (500, 824, 590, 148),
    "models": (1230, 185, 510, 140),
    "providers": (1230, 494, 510, 270),
    "apps": (1230, 790, 510, 182),
}

# Compact three-column layout. Ports stay on card borders; labels are placed
# in the routing gutters. Both languages use the same geometry.
EDGES = [
    ("user", "desktop", [(225, 305), (225, 350)], "control", (310, 334), "input"),
    ("desktop", "chat", [(390, 418), (438, 418), (438, 292), (500, 292)], "control", (440, 376), "ws"),
    ("chat", "models", [(1090, 260), (1230, 260)], "control", (1160, 236), "infer"),
    ("chat", "memory", [(1090, 334), (1148, 334), (1148, 412), (1230, 412)], "planned", (1160, 380), "retrieve"),
    ("chat", "work", [(795, 368), (795, 406)], "control", (730, 391), "delegate"),
    ("chat", "voice", [(500, 332), (482, 332), (482, 876), (500, 876)], "control", None, "speech"),
    ("work", "providers", [(1090, 484), (1176, 484), (1176, 548), (1230, 548)], "control", (1160, 463), "run"),
    ("providers", "work", [(1230, 626), (1144, 626), (1144, 578), (1090, 578)], "return", (1160, 653), "results"),
    ("work", "desktop", [(500, 574), (416, 574), (416, 506), (390, 506)], "return", (440, 548), "projection"),
    ("work", "auip", [(795, 616), (795, 658)], "artifact", (921, 645), "artifact"),
    ("headless", "host", [(390, 864), (470, 864)], "control", (430, 835), "api"),
    ("auip", "apps", [(1090, 704), (1160, 704), (1160, 832), (1230, 832)], "artifact", (1160, 690), "action"),
    ("apps", "auip", [(1230, 910), (1196, 910), (1196, 760), (1090, 760)], "return", (1160, 940), "receipts"),
    ("voice", "surface", [(500, 904), (448, 904), (448, 652), (390, 652)], "control", (440, 743), "playback"),
]

COPY = {
    "en": {
        "title": "Amadeus · Public Architecture",
        "description": "An overview of the Host authority boundary, user surfaces, delegated Providers, bounded AUIP applications, and voice presentation. Solid arrows show requests; dashed arrows show returned state or evidence.",
        "subtitle": "One Host owns identity, permissions and durable work; conversation, execution and presentation stay distinct.",
        "columns": ["USER & SURFACES", "HOST · AUTHORITY & DURABLE STATE", "BOUNDED RUNTIMES"],
        "host": "Python · FastAPI · authenticated interfaces · durable stores",
        "legend": ["Requests / control", "State / results", "Artifact / application actions"],
        "user": ["User", "Voice · text · clicks · consent", "Interrupt, redirect or take over"],
        "desktop": ["Electron Desktop", "Chat · Projects · Drafts", "Work · Artifacts · Settings", "Backend status · VN Player", "VN Player remains experimental"],
        "headless": ["Headless / API Clients", "No character / model required", "Same authenticated Host boundary"],
        "surface": ["Character Presentation", "Wallpaper · SpriteForge / PixiJS", "Subtitles · lips · emotion · scene", "Character packs are optional"],
        "chat": ["Main Chat & Turn Control", "Sessions · models · visual context", "Streams · interruption epochs", "Conversation only · no MCP tools"],
        "memory": ["Memory & Persona Runtime", "Retrieval / updates · lifecycle design pending", "PLANNED · interface not finalized"],
        "retrieve": ["Context", "exchange"],
        "planned_legend": "Planned extension",
        "work": ["Work Control Plane", "Projects · Drafts · WorkItems / Attempts", "Provider selection · run / cancel", "Continue / retry · permissions · Artifacts", "Ledger · diffs · completion · recovery", "Host verifies identity and execution facts"],
        "auip": ["AUIP Session Authority", "Attach tickets · session identity · revisions", "Action authorization · declared state · receipts", "No Work / Provider / TTS authority granted"],
        "voice": ["Voice & Embodiment", "Wake / chat ASR · local or remote ASR / TTS", "Playback · mouth / render events", "Presentation stays separate from execution"],
        "models": ["Main Chat Model Endpoints", "Local / remote / hybrid inference", "Remote services are explicitly configured"],
        "apps": ["Attached AUIP Applications", "Verified Artifact entry", "Declared state / events · bounded actions", "Application receipts · visible disconnects", "Separate from Work Providers"],
        "providers": "Work Providers",
        "provider_subtitle": "Separate transports and capabilities",
        "provider_details": [["Browser", "Playwright", "Scoped session"], ["Codex", "App Server", "or Direct"], ["Pi", "Native RPC", "Daily tasks"]],
        "capabilities": "MCP / Skills · compatible Providers only",
        "planned": "Planned: Claude CLI direct Provider",
        "input": ["Input / consent"], "ws": ["Auth.", "WebSocket"],
        "infer": ["Inference"], "delegate": ["Delegate"], "speech": ["Chat speech"],
        "run": ["Run / cancel"], "results": ["Events / results"],
        "projection": ["Work-state", "projection"], "artifact": ["Verified Artifact"],
        "api": ["API access"], "action": ["Attach / action"],
        "receipts": ["State / receipts"], "playback": ["Playback /", "render events"],
        "footer": ["Main Chat ≠ Provider ≠ AUIP app", "MCP / Skills stay Provider-scoped", "Identity · permissions · Artifacts → Host"],
    },
    "zh": {
        "title": "Amadeus · 公开架构总览",
        "description": "展示 Host 权限边界、用户界面、委派执行器、有界 AUIP 应用会话与语音呈现。实线代表请求，虚线代表状态或证据回传。",
        "subtitle": "Host 持有身份、权限与持久工作状态；对话、执行和角色呈现保持清晰分工。",
        "columns": ["用户与交互界面", "HOST · 权限与持久状态", "受限运行组件"],
        "host": "Python · FastAPI · 已认证的接口 · 持久存储",
        "legend": ["请求 / 控制", "状态 / 结果回传", "产物 / 应用动作"],
        "user": ["用户", "语音 · 文字 · 点击 · 授权", "随时打断、改变目标或接管"],
        "desktop": ["Electron 桌面界面", "聊天 · 项目 · 草稿", "工作 · 产物 · 设置", "后端状态 · VN Player", "VN Player 仍为实验性功能"],
        "headless": ["无界面 / API 客户端", "无需角色包或本地模型即可启动", "接入同一已认证的 Host 边界"],
        "surface": ["角色呈现界面", "壁纸 · SpriteForge / PixiJS", "字幕 · 口型 · 表情 · 场景行为", "角色包可选，缺包仍可聊天"],
        "chat": ["主对话与轮次控制", "会话 · 模型配置 · 视觉上下文", "生成流生命周期 · 打断轮次", "只负责对话，不直接调用 MCP 工具"],
        "memory": ["记忆与人格运行时", "检索 / 更新 · 生命周期机制待设计", "规划中 · 交互接口尚未定型"],
        "retrieve": ["上下文交互"],
        "planned_legend": "规划中的扩展模块",
        "work": ["工作控制面", "项目 · 草稿 · 工作项 / 执行尝试", "执行器选择 · 执行与取消", "继续 / 重试 · 权限 · 产物登记", "持久记录 · 差异 · 完成判定 · 恢复", "Host 核验身份和执行事实"],
        "auip": ["AUIP 会话权限", "挂接票据 · 会话身份 · 版本", "动作授权 · 声明状态 · 动作回执", "不授予工作执行或语音权限"],
        "voice": ["语音与角色表现", "唤醒 / 对话识别 · 本地或远程 ASR / TTS", "真实播放 · 嘴型 / 渲染事件", "呈现与执行权限分离"],
        "models": ["主对话模型端点", "本地 / 远程 / 混合模型推理", "远程服务由用户显式配置"],
        "apps": ["已挂接的 AUIP 应用", "已核验的产物入口 · 声明状态 / 事件", "有界动作 · 应用回执", "断连状态可见，不是工作执行器"],
        "providers": "工作执行器",
        "provider_subtitle": "各自独立的传输与能力范围",
        "provider_details": [["Browser", "Playwright", "限定会话"], ["Codex", "App Server", "或 Direct"], ["Pi", "原生 RPC", "日常任务"]],
        "capabilities": "MCP / Skills · 仅授予兼容执行器",
        "planned": "规划中：Claude CLI 直接接入",
        "input": ["输入 / 授权"], "ws": ["已认证的", "WebSocket"],
        "infer": ["模型推理"], "delegate": ["委派任务"], "speech": ["对话语音"],
        "run": ["执行 / 取消"], "results": ["事件 / 结果"],
        "projection": ["工作状态", "投射"], "artifact": ["已核验产物"],
        "api": ["认证 API"], "action": ["挂接 / 动作"],
        "receipts": ["状态 / 回执"], "playback": ["播放 /", "渲染事件"],
        "footer": ["主对话 ≠ 工作执行器 ≠ AUIP 应用", "MCP / Skills 仅在执行器范围内授权", "身份 · 权限 · 产物由 Host 核验"],
    },
}


def render(lang: str) -> str:
    c = COPY[lang]
    out = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="1800" height="1120" viewBox="0 0 1800 1120" role="img" aria-labelledby="title desc" xml:lang="{lang}">
<!-- Generated by tools/architecture/generate_readme_overview.py. -->
<title id="title">{escape(c['title'])}</title><desc id="desc">{escape(c['description'])}</desc>
<defs>
  <linearGradient id="bg" x2="1" y2="1"><stop stop-color="#040e0a"/><stop offset="1" stop-color="#091b13"/></linearGradient>
  <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse"><path d="M32 0H0V32" fill="none" stroke="#8bd8af" opacity=".035"/></pattern>
  <marker id="control" viewBox="0 0 10 10" refX="10" refY="5" markerUnits="userSpaceOnUse" markerWidth="13" markerHeight="13" orient="auto"><path d="M0 0L10 5L0 10Z" fill="#72dfad"/></marker>
  <marker id="return" viewBox="0 0 10 10" refX="10" refY="5" markerUnits="userSpaceOnUse" markerWidth="13" markerHeight="13" orient="auto"><path d="M0 0L10 5L0 10Z" fill="#b4dcc7"/></marker>
  <marker id="artifact" viewBox="0 0 10 10" refX="10" refY="5" markerUnits="userSpaceOnUse" markerWidth="13" markerHeight="13" orient="auto"><path d="M0 0L10 5L0 10Z" fill="#e5bc79"/></marker>
  <marker id="planned" viewBox="0 0 10 10" refX="10" refY="5" markerUnits="userSpaceOnUse" markerWidth="11" markerHeight="11" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="#a4b9a9"/></marker>
  <style>
    text{{font-family:"Segoe UI","Microsoft YaHei",Arial,sans-serif}}
    .title{{font-size:38px;font-weight:700;fill:#edf8f0}}
    .subtitle{{font-size:20px;fill:#a5c5b2}}
    .column{{font-size:17px;letter-spacing:1px;font-weight:700;fill:#89c9a8}}
    .card-title{{font-size:27px;font-weight:650;fill:#effaf3}}
    .body{{font-size:23px;fill:#c0d6c9}}
    .note{{font-size:19px;fill:#a6c5b3}}
    .flow{{font-size:16px;font-weight:600;fill:#cce7d7;paint-order:stroke;stroke:#081b13;stroke-width:6;stroke-linejoin:round}}
    .mini-title{{font-size:23px;font-weight:650;fill:#f5e7ce}}
    .mini{{font-size:20px;fill:#cfccb7}}
    .provider-detail{{font-size:18px;fill:#cfccb7}}
    .footer{{font-size:19px;font-weight:600;fill:#c7e5d3}}
    .planned-title{{font-size:27px;font-weight:650;fill:#d8ddcd}}
    #node-user .body,#node-desktop .body,#node-surface .body,#node-headless .body{{font-size:21px}}
    #node-user .card-title,#node-desktop .card-title,#node-surface .card-title,#node-headless .card-title{{font-size:25px}}
  </style>
</defs>
<rect width="1800" height="1120" rx="24" fill="url(#bg)"/>
<rect width="1800" height="1120" rx="24" fill="url(#grid)"/>''']

    def text(x: float, y: float, value: str, cls: str, anchor: str = "start") -> None:
        out.append(f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{escape(value)}</text>')

    text(48, 66, c["title"], "title")
    text(48, 105, c["subtitle"], "subtitle")
    colors = {"control": "#72dfad", "return": "#b4dcc7", "artifact": "#e5bc79", "planned": "#a4b9a9"}
    for x, kind, label in zip([60, 480, 880], colors, c["legend"]):
        dash = ' stroke-dasharray="7 6"' if kind == "return" else ""
        out.append(f'<path d="M{x} 130H{x + 64}" fill="none" stroke="{colors[kind]}" stroke-width="2.5" marker-end="url(#{kind})"{dash}/>')
        text(x + 80, 136, label, "note")
    out.append('<rect x="1370" y="117" width="24" height="18" rx="3" fill="none" stroke="#a4b9a9" stroke-dasharray="4 3"/>')
    text(1406, 136, c["planned_legend"], "note")
    for x, label in zip([60, 500, 1230], c["columns"]):
        text(x, 160, label, "column")

    x, y, w, h = BOXES["host"]
    out.append(f'<g id="node-host" data-node="host"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="22" fill="#0a2017" stroke="#355f49" stroke-width="1.5"/></g>')
    text(500, 202, c["host"], "note")

    # Wires are below cards and labels; no arrow can obscure card text.
    for source, target, points, kind, _, label in EDGES:
        route = "M" + " L".join(f"{x} {y}" for x, y in points)
        dash = ' stroke-dasharray="8 6"' if kind == "return" else (' stroke-dasharray="2 5"' if kind == "planned" else "")
        start_marker = ' marker-start="url(#planned)"' if kind == "planned" else ""
        # A dark casing distinguishes crossings from junctions without moving ports.
        out.append(f'<g data-edge="{source}-{target}" data-from="{source}" data-to="{target}"><title>{escape(" / ".join(c[label]))}</title>')
        out.append(f'<path d="{route}" fill="none" stroke="#081b13" stroke-width="8" stroke-linejoin="round"/>')
        out.append(f'<path class="connector" d="{route}" fill="none" stroke="{colors[kind]}" stroke-width="2.5" stroke-linejoin="round" marker-end="url(#{kind})"{start_marker}{dash}/></g>')

    for key in ["user", "desktop", "headless", "surface", "chat", "work", "auip", "voice", "models", "apps"]:
        x, y, w, h = BOXES[key]
        warm = key in {"auip", "apps"}
        stroke = "#cfa566" if warm else ("#67c599" if key in {"user", "chat", "work"} else "#456d55")
        fill = "#201e15" if warm else "#0c241a"
        lines = c[key]
        out.append(f'<g id="node-{key}" data-node="{key}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>')
        text(x + 22, y + 37, lines[0], "card-title")
        # Topology is identical across translations; only localized strings vary.
        for i, line in enumerate(lines[1:-1]):
            text(x + 22, y + 71 + i * 32, line, "body")
        text(x + 22, y + h - 16, lines[-1], "note")
        out.append("</g>")

    x, y, w, h = BOXES["memory"]
    out.append(f'<g id="node-memory" data-node="memory" data-status="planned"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="#14221b" stroke="#a4b9a9" stroke-width="1.5" stroke-dasharray="6 5"/>')
    text(x + 22, y + 37, c["memory"][0], "planned-title")
    text(x + 22, y + 69, c["memory"][1], "note")
    text(x + 22, y + 98, c["memory"][2], "mini")
    out.append("</g>")

    x, y, w, h = BOXES["providers"]
    out.append(f'<g id="node-providers" data-node="providers"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="#231f15" stroke="#cfa566" stroke-width="1.6"/>')
    text(x + 22, y + 37, c["providers"], "card-title")
    text(x + 22, y + 68, c["provider_subtitle"], "note")
    for i, words in enumerate(c["provider_details"]):
        px, py = x + 18 + i * 164, y + 86
        out.append(f'<rect x="{px}" y="{py}" width="146" height="98" rx="9" fill="#30281a" stroke="#9e8454"/>')
        text(px + 12, py + 29, words[0], "mini-title")
        text(px + 12, py + 59, words[1], "provider-detail")
        text(px + 12, py + 84, words[2], "provider-detail")
    out.append(f'<rect x="{x + 18}" y="{y + 196}" width="474" height="32" rx="8" fill="#102c1f" stroke="#456d55"/>')
    text(x + w / 2, y + 219, c["capabilities"], "note", "middle")
    text(x + 20, y + 253, c["planned"], "mini")
    out.append("</g>")

    for _, _, _, _, position, label in EDGES:
        if position is None:
            continue
        x, y = position
        lines = c[label]
        out.append(f'<g data-label="{label}">')
        for i, line in enumerate(lines):
            text(x, y + i * 20 - (len(lines) - 1) * 10, line, "flow", "middle")
        out.append("</g>")

    out.append('<rect x="60" y="1010" width="1680" height="64" rx="14" fill="#06140e" stroke="#355f49"/>')
    for x, label in zip([80, 650, 1160], c["footer"]):
        text(x, 1048, label, "footer")
    out.append("</svg>\n")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check generated assets without writing")
    args = parser.parse_args()
    stale = []
    for language, name in [("en", "architecture-overview-crt.svg"), ("zh", "architecture-overview-crt.zh.svg")]:
        path = ROOT / "assets" / name
        svg = render(language)
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != svg:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.write_text(svg, encoding="utf-8", newline="\n")
            print(f"Generated {path.relative_to(ROOT)}")
    if stale:
        print("Stale README overview assets: " + ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
