"""Does this endpoint let the character speak before it delegates?

The inline-tag design exists so the first sentence can start streaming while
the delegation goes out behind it. A native tool call is only a viable
replacement if content still arrives first and just as early. Docs do not
pin that down for a specific model, so measure it.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, r"F:\Computer_Science\Amadeus\amadeus")

from config import settings
from openai import OpenAI

MODEL = "deepseek-v4-flash"
SYSTEM = (
    "あなたは牧瀬紅莉栖。日本語で自然に答える。"
    "ファイルやコードの依頼は必ず実行手段に回すこと。"
    "【重要】実行に回す前に、必ず一言添えてから回すこと（例:「ちょっと待って」）。"
)
USER = "scratch 仓に theme.txt を作って、color=blue と書き込んで。"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "delegate",
            "description": "Send a file/code task to the Codex coding agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "enum": ["codex", "browser", "openclaw"]},
                    "task": {"type": "string", "description": "complete instruction"},
                    "workspace_ref": {
                        "type": "string",
                        "description": "existing task id, omit for new work",
                    },
                },
                "required": ["provider", "task"],
            },
        },
    }
]


def run(label: str, tools) -> None:
    client = OpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=settings.DEEPSEEK_BASE_URL,
    )
    kwargs = dict(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}],
        stream=True,
        max_tokens=300,
    )
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    t0 = time.perf_counter()
    first_content = first_tool = None
    order: list[str] = []
    content = ""
    tool_names: list[str] = []
    try:
        for chunk in client.chat.completions.create(**kwargs):
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                if first_content is None:
                    first_content = time.perf_counter() - t0
                    order.append("content")
                content += piece
            calls = getattr(delta, "tool_calls", None)
            if calls:
                if first_tool is None:
                    first_tool = time.perf_counter() - t0
                    order.append("tool_call")
                for c in calls:
                    name = getattr(getattr(c, "function", None), "name", None)
                    if name:
                        tool_names.append(name)
    except Exception as exc:
        print(f"  {label}: FAILED {type(exc).__name__}: {str(exc)[:160]}")
        return

    total = time.perf_counter() - t0
    print(f"  {label}")
    print(f"    首个 content token : {first_content*1000:.0f}ms" if first_content else "    首个 content token : (无)")
    print(f"    首个 tool_call     : {first_tool*1000:.0f}ms" if first_tool else "    首个 tool_call     : (无)")
    print(f"    到达顺序           : {' → '.join(order) or '(空)'}")
    print(f"    tool 调用          : {tool_names or '(无)'}")
    print(f"    说出来的话         : {content.strip()[:70]!r}")
    print(f"    总耗时             : {total*1000:.0f}ms")


if __name__ == "__main__":
    print("=== 基线：不给 tools（当前的内联标签模式）===")
    run("baseline", None)
    print("\n=== 给 tools：能否先说话再调用 ===")
    for i in range(3):
        run(f"tools run {i+1}", TOOLS)
