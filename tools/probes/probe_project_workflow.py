"""Does switching survive a session with two projects in it?

This is a model-contract probe only: it classifies emitted labels and uses
scripted assistant turns. It is not host or end-to-end evidence. Use
``probe_project_host_matrix.py`` for parser → dispatcher → handler → ledger →
projection verification on the current code.

probe_project_switch.py measured single utterances: an imperative switch lands
6 times out of 6. That is not the same as the workflow holding -- pick a
project, work in it for a while, move to another, work there. The risks only
appear across turns:

  * **Inertia.** Prior turns outweigh the system prompt in this system
    (established 2026-07-31), and by the second switch the history is full of
    work in the first project.
  * **Leakage.** After moving, does anything still point at the project we
    left? That is the failure that would put chess commits in a repository.
  * **Noise.** Do the working turns in between stay quiet, or does the model
    re-announce the project every time?

    .venv\\Scripts\\python.exe -X utf8 tools/probes/probe_project_workflow.py [repeats]

The working turns deliberately accept silence. The host carries the chosen
project, so an instruction in the middle of a session has nothing to declare --
what matters is that it does not point somewhere *else*.

## What it measured, 2026-08-03 (two runs, 3 and 5 rounds)

**The workflow holds: 47 of 48 steps.**

  | step                          | result |
  |-------------------------------|--------|
  | switch in                     | 7/8    |
  | work, work (first project)    | 16/16 quiet, none pointing elsewhere |
  | **switch away**               | **8/8** |
  | work, work (second project)   | 16/16 quiet, **zero leaks back** |

The second switch is the one that mattered and it never missed, even with the
history full of work in the first project -- the condition under which prior
turns have beaten the system prompt before.

Two things worth more than the ratio:

  * **Working turns never repeated the project, 32 times out of 32.** So the
    host must carry the choice; the model states it once and then stops. That
    is the division the design wants, and it is not something the prompt has to
    fight for.
  * **The single miss was a spoken promise.** "了解、amadeus プロジェクトに
    切り替えるわね" with no tag behind it -- the failure shape the persona
    contract already names. It is visible (nothing switches, the next
    instruction goes to scratch) and the user simply says it again.

One thing this does not cover: scripted assistant turns mean the model always
saw a *correct* prior switch. A missed first switch leaves no tag in the
history, and whether that compounds is untested.
"""

from __future__ import annotations

import asyncio
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

MAIN_PROJECT = "amadeus"
DRAFT_TITLE = "做一个国际象棋游戏"

SWITCH_RULE = (
    "When the user says which project to work on (\"switch to X\", \"let's do X\", "
    "\"back to that X\"), emit [DELEGATE provider=\"codex\" intent=\"focus\" "
    "project_id=\"<exact id>\"] with no task. That sets where later instructions "
    "go and starts no work by itself. Later instructions inherit it: do not "
    "repeat the project on every turn."
)

_DELEGATE_RE = re.compile(r"\[DELEGATE\b[^\]]*\]", re.IGNORECASE)
_PROJECT_RE = re.compile(r'project_id\s*=\s*"([^"]*)"', re.IGNORECASE)
_INTENT_RE = re.compile(r'intent\s*=\s*"([a-z]+)"', re.IGNORECASE)


def _observe(reply: str) -> tuple[bool, str]:
    """(did it emit a focus switch, which project id it pointed at)."""

    tags = _DELEGATE_RE.findall(str(reply or ""))
    blob = " ".join(tags)
    match = _PROJECT_RE.search(blob)
    intents = {m.group(1).lower() for m in _INTENT_RE.finditer(blob)}
    return "focus" in intents, (match.group(1).strip() if match else "")


def _build(root: Path) -> tuple[str, str, str]:
    from agent_host.provider_types import ProviderRunRequest
    from agent_host.work_ledger_store import WorkLedgerStore
    from llm.prompts import get_system_prompt
    from server.work_context import augment_system_prompt_with_active_provider_context
    from server.work_ledger_coordinator import WorkLedgerCoordinator

    repo = root / MAIN_PROJECT
    repo.mkdir()
    store = WorkLedgerStore(root / "probe_ledger.sqlite3")
    coordinator = WorkLedgerCoordinator(store)
    with patch(
        "server.work_ledger_coordinator.cwd_in_project_registry", return_value=True
    ):
        main_request = ProviderRunRequest(
            provider="codex",
            task="修好聊天窗口的滚动条，并给 wake_service 补一条启动日志",
            cwd=str(repo),
            mode="plan",
            metadata={"source": "probe", "session_id": "probe-session"},
        )
        coordinator.prepare_request(main_request)
        main_id = str(main_request.metadata["work"]["project_id"])
        draft_request = ProviderRunRequest(
            provider="codex",
            task=DRAFT_TITLE,
            cwd="",
            mode="agent",
            metadata={"source": "probe", "session_id": "probe-session"},
        )
        coordinator.prepare_request(draft_request)
        draft_id = str(
            coordinator.promote_work_item_to_project(
                str(draft_request.metadata["work"]["work_item_id"])
            )["projectId"]
        )
        coordinator.configure()
        try:
            prompt = augment_system_prompt_with_active_provider_context(
                get_system_prompt("with_delegate"),
                session_id="probe-session",
            )
        finally:
            coordinator.close()
    store.close()
    return prompt + "\n" + SWITCH_RULE + "\n", main_id, draft_id


def _ask(messages: list[dict]) -> str:
    import llm.client as client

    if client.llm_client is None:
        client.llm_client = client.init_llm_client()
    response = client.llm_client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=messages,
        temperature=0.7,
        max_tokens=500,
        stream=False,
        timeout=30,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return str(response.choices[0].message.content or "")


async def main() -> int:
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    latencies: list[float] = []
    with tempfile.TemporaryDirectory(prefix="probe_workflow_") as temp:
        root = Path(temp)
        import config.settings as settings

        settings.WORK_SCRATCH_ROOT = str(root / "scratch")
        system, main_id, draft_id = _build(root)
        print(f"repository : {main_id}")
        print(f"kept draft : {draft_id} ({DRAFT_TITLE})\n")

        # The session, in order. Each measured step says what would be correct.
        # Scripted assistant turns keep their raw tags: stripping them is what
        # taught the model not to emit any (2026-07-31).
        script: list[tuple[str, str, str, str]] = [
            ("switch-in", "切换到 amadeus 项目。", "switch:main",
             f'了解。[DELEGATE provider="codex" intent="focus" project_id="{main_id}"]'),
            ("work-1", "把 README 补一段安装说明。", "work:main",
             '了解、READMEにインストール手順を追記するわね。'
             '[DELEGATE provider="codex" intent="execute" task="README にインストール手順を追記する"]'),
            ("work-2", "再给 wake_service 加个启动日志。", "work:main",
             '了解。[DELEGATE provider="codex" intent="execute" task="wake_service に起動ログを追加する"]'),
            ("switch-away", "切换到象棋那个项目。", "switch:draft",
             f'了解。[DELEGATE provider="codex" intent="focus" project_id="{draft_id}"]'),
            ("work-3", "加个悔棋功能。", "work:draft",
             '了解、待った機能ね。'
             '[DELEGATE provider="codex" intent="execute" task="将棋の待った機能を実装する"]'),
            ("work-4", "顺手把提示文案改得友好一点。", "work:draft", ""),
        ]

        tally: dict[str, dict[str, int]] = {}
        for _round in range(repeats):
            messages = [{"role": "system", "content": system}]
            for name, utterance, expectation, scripted in script:
                messages.append({"role": "user", "content": utterance})
                started = time.monotonic()
                try:
                    reply = await asyncio.to_thread(_ask, list(messages))
                except Exception as exc:
                    print(f"  skip {name}: infrastructure: {exc}")
                    break
                latencies.append(time.monotonic() - started)
                focus, pointed = _observe(reply)
                kind, target = expectation.split(":")
                wanted_id = main_id if target == "main" else draft_id
                other_id = draft_id if target == "main" else main_id

                if kind == "switch":
                    outcome = (
                        "switched" if focus and pointed == wanted_id
                        else "switched-wrong" if focus and pointed == other_id
                        else "focus-no-id" if focus
                        else "no-switch"
                    )
                else:
                    outcome = (
                        "leaked-to-other" if pointed == other_id
                        else "re-announced" if focus
                        else "worked-here"
                    )
                tally.setdefault(name, {})
                tally[name][outcome] = tally[name].get(outcome, 0) + 1
                if outcome in {"switched-wrong", "leaked-to-other", "no-switch"}:
                    print(f"     {outcome:16s} {name:12s} {reply.strip()[:88]!r}")
                # Continue from a clean scripted turn so one bad reply does not
                # poison the rest of the session; the last step uses the model's
                # own words, since by then the history is what is being tested.
                messages.append({"role": "assistant", "content": scripted or reply})

    print("-- per step --")
    for name, _u, expectation, _s in script:
        row = tally.get(name, {})
        total = sum(row.values()) or 1
        detail = "  ".join(f"{key} {value}" for key, value in sorted(row.items()))
        print(f"  {name:12s} {expectation:14s} {detail}   (n={total})")

    switch_away = tally.get("switch-away", {})
    leaks = sum(
        tally.get(step, {}).get("leaked-to-other", 0)
        for step in ("work-3", "work-4")
    )
    noise = sum(
        tally.get(step, {}).get("re-announced", 0)
        for step in ("work-1", "work-2", "work-3", "work-4")
    )
    total_switch = sum(switch_away.values()) or 1
    print("\n-- summary --")
    print(f"  moved to the second project : {switch_away.get('switched', 0)}/{total_switch}")
    print(f"  pointed back at the one we left after moving : {leaks}   (must be 0)")
    print(f"  re-announced the project on a working turn   : {noise}")
    if latencies:
        print(f"  latency median={statistics.median(latencies):.2f}s")
    print(
        "\n  The second switch is the load-bearing number: by then the history\n"
        "  is full of work in the first project, and prior turns outweigh the\n"
        "  system prompt in this system."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
