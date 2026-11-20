"""Where does the turn end relative to the call?

The inline tag is parsed out of a stream we own, so dispatch happens at a
moment we choose and the character can keep talking afterwards. A tool call
is bound to the protocol's turn boundary. That difference is the thing to
measure before trading one for the other.
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, r"F:\Computer_Science\Amadeus\amadeus")

from config import settings
from openai import OpenAI

MODEL = "deepseek-v4-flash"
SYSTEM = (
    "あなたは牧瀬紅莉栖。日本語で自然に答える。"
    "ファイルやコードの依頼は必ず実行手段に回すこと。"
    "実行に回す前に一言添え、回した後も会話を続けて、"
    "何をしているか二、三文で説明すること。"
)
USER = "scratch 仓に theme.txt を作って、color=blue と書き込んで。それと、今何してるか教えて。"

TOOLS = [{
    "type": "function",
    "function": {
        "name": "delegate",
        "description": "Send a file/code task to the Codex coding agent.",
        "parameters": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "enum": ["codex", "browser", "openclaw"]},
                "task": {"type": "string"},
            },
            "required": ["provider", "task"],
        },
    },
}]


def run(i: int) -> None:
    client = OpenAI(api_key=settings.DEEPSEEK_API_KEY, base_url=settings.DEEPSEEK_BASE_URL)
    t0 = time.perf_counter()
    events: list[tuple[str, float]] = []
    before = after = ""
    seen_tool = False
    finish = None
    args = ""
    for chunk in client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}],
        stream=True, tools=TOOLS, tool_choice="auto", max_tokens=400,
    ):
        if not chunk.choices:
            continue
        ch = chunk.choices[0]
        if ch.finish_reason:
            finish = ch.finish_reason
        d = ch.delta
        piece = getattr(d, "content", None)
        if piece:
            events.append(("content", time.perf_counter() - t0))
            if seen_tool:
                after += piece
            else:
                before += piece
        calls = getattr(d, "tool_calls", None)
        if calls:
            if not seen_tool:
                events.append(("tool_call:start", time.perf_counter() - t0))
            seen_tool = True
            for c in calls:
                a = getattr(getattr(c, "function", None), "arguments", None)
                if a:
                    args += a
    total = time.perf_counter() - t0
    kinds = [k for k, _ in events]
    order = []
    for k in kinds:
        if not order or order[-1] != k:
            order.append(k)
    print(f"  run {i}: finish_reason={finish!r}  总耗时={total*1000:.0f}ms")
    print(f"    到达顺序      : {' → '.join(order)}")
    print(f"    调用前说的话  : {before.strip()[:60]!r}")
    print(f"    调用后说的话  : {after.strip()[:60]!r}" + ("  ← 调用后仍在说话" if after.strip() else "  ← 调用后无内容"))
    print(f"    参数完成时刻  : {events[-1][1]*1000:.0f}ms  args={args[:70]!r}")


if __name__ == "__main__":
    print("=== 要求它『调用之后继续说 2-3 句』，看协议允不允许 ===")
    for i in range(3):
        run(i + 1)
