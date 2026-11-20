"""Can the model be told which project we are working on?

Naming a project per instruction does not work: measured 2-4 out of 12, and no
wording moved it (tools/probes/probe_project_declaration.py). The failures were
all references whose target is not in the prompt -- "this project", "our code",
or a filename whose location only the host knows -- so it is missing
information rather than poor judgement, which is why prose could not fix it.

This measures the other shape. Instead of deciding the project on every turn,
the user says once which one they mean, and later instructions inherit it. That
moves every project decision into the one regime the model handles well -- the
user said the name out loud, ~75% -- and asks for it once per topic instead of
once per instruction, where a miss is visible and simply repeated.

    .venv\\Scripts\\python.exe -X utf8 tools/probes/probe_project_switch.py [repeats]

Two projects are registered, one of them a kept draft carrying the title its
task was created with, because that is what a promoted draft's name looks like.
With one candidate the task would be trivial; the question is whether the right
one is chosen.

Two things are measured separately, because they fail for different reasons and
have different fixes:

  * **Identified the project.** The design rests on this. A miss here means the
    approach does not work.
  * **Used the focus intent.** A miss here is wording, not capability.

The controls matter as much as the arms. An ordinary instruction and a one-off
request must *not* switch anything: a switch that fires on "build me a chess
game" would silently redirect every later instruction into a repository.

The switch rule is injected by this probe and does not exist in production, so
this measures a proposal rather than current behaviour.

## What it measured, 2026-08-03 (3 repeats)

**Imperative phrasing is perfect; conversational phrasing does nothing.**

  | utterance                                  | switched |
  |--------------------------------------------|----------|
  | 切换到 amadeus 项目                          | 3/3      |
  | 切到 amadeus，先把 README 改一下              | 3/3      |
  | 今天做 amadeus                              | 1/3      |
  | 咱们来弄 amadeus 吧                          | 0/3      |
  | 回到那个象棋 / 接着弄象棋那个                  | 1/6      |

**Controls: 6 of 6, no false switch.** Neither an ordinary edit nor a one-off
set a project, which is the direction that would silently redirect everything
after it.

**Every id it wrote was correct**: 7 for 7, none wrong and none invented. So
what varies is deciding to emit the tag, not filling it.

Read the prose before the last row. Asked to go back to the kept draft, the
model identified it every single time -- "ああ、那个国际象棋项目是吧" -- and then
emitted `intent="report"` to check its status instead of switching. Resuming by
first reporting is a defensible reading of "back to that chess thing", so 1/6
understates comprehension badly: what it got wrong was the action, not the
referent.

The pattern across both arms is the same. The model switches when the utterance
is an instruction about switching, and treats an opener like "咱们来弄 amadeus 吧"
as conversation -- which is arguably correct, since that is an opener rather
than a command. Listing those phrasings in the rule did not help, consistently
with prose not moving any of this (probe_project_declaration.py).
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

# The contract under test. Not in production: the probe injects it so the
# measurement can happen before anything is built on it.
SWITCH_RULE = (
    "When the user says which project to work on (\"switch to X\", \"let's do X\", "
    "\"back to that X\"), emit [DELEGATE provider=\"codex\" intent=\"focus\" "
    "project_id=\"<exact id>\"] with no task. That sets where later instructions "
    "go and starts no work by itself."
)

# Switching to the main project. Phrasings range from an explicit command to
# how someone actually opens a session.
SWITCH_TO_MAIN = [
    ("explicit", "切换到 amadeus 项目。"),
    ("casual", "咱们来弄 amadeus 吧。"),
    ("today", "今天做 amadeus。"),
    ("switch-then-work", "切到 amadeus，先把 README 改一下。"),
]

# Switching to the kept draft, by how the user would refer to it rather than by
# its recorded title. This is the harder half: the name is a sentence.
SWITCH_TO_DRAFT = [
    ("back-to-that", "回到那个象棋。"),
    ("resume", "接着弄象棋那个项目。"),
]

# Neither of these may switch anything. The second is the dangerous one: if a
# one-off request sets a project, everything after it lands in a repository.
CONTROLS = [
    ("ordinary-work", "把 settings.py 里的超时改成 60 秒。"),
    ("one-off", "做一个五子棋，带简单 AI。"),
]

_DELEGATE_RE = re.compile(r"\[DELEGATE\b[^\]]*\]", re.IGNORECASE)
_PROJECT_RE = re.compile(r'project_id\s*=\s*"([^"]*)"', re.IGNORECASE)
_INTENT_RE = re.compile(r'intent\s*=\s*"([a-z]+)"', re.IGNORECASE)


def _classify(reply: str, wanted_id: str, other_id: str) -> str:
    """What the reply did about choosing a project."""

    tags = _DELEGATE_RE.findall(str(reply or ""))
    if not tags:
        return "no-tag"
    blob = " ".join(tags)
    match = _PROJECT_RE.search(blob)
    named = match.group(1).strip() if match else ""
    intents = {m.group(1).lower() for m in _INTENT_RE.finditer(blob)}
    focus = "focus" in intents
    if not named:
        return "focus-no-project" if focus else "no-project"
    if named == other_id:
        return "wrong-project"
    if named != wanted_id:
        return "invented-id"
    return "switched" if focus else "right-project-other-intent"


def _build_prompt(root: Path) -> tuple[str, str, str]:
    """A prompt with two real candidates: a repository and a kept draft."""

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

        # A draft, then kept -- so its project name is the task's own title,
        # which is what a promoted draft actually looks like on the list.
        draft_request = ProviderRunRequest(
            provider="codex",
            task=DRAFT_TITLE,
            cwd="",
            mode="agent",
            metadata={"source": "probe", "session_id": "probe-session"},
        )
        coordinator.prepare_request(draft_request)
        draft_item = str(draft_request.metadata["work"]["work_item_id"])
        draft_id = str(
            coordinator.promote_work_item_to_project(draft_item)["projectId"]
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


async def _run_arm(
    label: str,
    utterances: list[tuple[str, str]],
    *,
    system: str,
    wanted_id: str,
    other_id: str,
    repeats: int,
    accept: set[str],
    latencies: list[float],
) -> dict[str, int]:
    print(f"-- {label} --")
    counts: dict[str, int] = {}
    for name, utterance in utterances:
        outcomes: list[str] = []
        for _index in range(repeats):
            started = time.monotonic()
            try:
                reply = await asyncio.to_thread(
                    _ask,
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": utterance},
                    ],
                )
            except Exception as exc:
                print(f"  skip {name}: infrastructure: {exc}")
                continue
            latencies.append(time.monotonic() - started)
            outcome = _classify(reply, wanted_id, other_id)
            outcomes.append(outcome)
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome not in accept:
                print(f"     {outcome:26s} {name:18s} {reply.strip()[:88]!r}")
        print(f"  {name:18s} {'/'.join(outcomes)}")
    total = sum(counts.values()) or 1
    good = sum(value for key, value in counts.items() if key in accept)
    print(f"  => {good}/{total}   " + "  ".join(
        f"{key} {value}" for key, value in sorted(counts.items())
    ) + "\n")
    return counts


async def main() -> int:
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    latencies: list[float] = []
    with tempfile.TemporaryDirectory(prefix="probe_switch_") as temp:
        root = Path(temp)
        import config.settings as settings

        settings.WORK_SCRATCH_ROOT = str(root / "scratch")
        system, main_id, draft_id = _build_prompt(root)
        if main_id not in system or draft_id not in system:
            print("both projects must be candidates; the probe would measure nothing")
            return 1
        print(f"main project  : {main_id}")
        print(f"kept draft    : {draft_id} ({DRAFT_TITLE})\n")

        # Identifying the project is what the design rests on; using the focus
        # intent for it is wording, so both count as finding the right one.
        found = {"switched", "right-project-other-intent"}
        to_main = await _run_arm(
            "switch to the repository",
            SWITCH_TO_MAIN,
            system=system,
            wanted_id=main_id,
            other_id=draft_id,
            repeats=repeats,
            accept=found,
            latencies=latencies,
        )
        to_draft = await _run_arm(
            "switch to the kept draft",
            SWITCH_TO_DRAFT,
            system=system,
            wanted_id=draft_id,
            other_id=main_id,
            repeats=repeats,
            accept=found,
            latencies=latencies,
        )
        controls = await _run_arm(
            "controls: these must not switch anything",
            CONTROLS,
            system=system,
            wanted_id="\0none",
            other_id="\0none",
            repeats=repeats,
            accept={"no-project", "no-tag"},
            latencies=latencies,
        )

    print("-- summary --")
    for label, counts, keys in [
        ("switch to repository", to_main, found),
        ("switch to kept draft", to_draft, found),
    ]:
        total = sum(counts.values()) or 1
        right = sum(value for key, value in counts.items() if key in keys)
        print(f"  {label:22s} found the right project {right}/{total}"
              f"   (focus intent {counts.get('switched', 0)},"
              f" wrong project {counts.get('wrong-project', 0)})")
    control_total = sum(controls.values()) or 1
    control_ok = controls.get("no-project", 0) + controls.get("no-tag", 0)
    print(f"  {'controls':22s} left the project alone  {control_ok}/{control_total}")
    print(f"  a one-off or an ordinary edit that switched: "
          f"{control_total - control_ok}   (each would redirect everything after it)")
    if latencies:
        print(f"  latency median={statistics.median(latencies):.2f}s")
    print(
        "\n  Finding the right project is the load-bearing number. Missing the\n"
        "  focus intent while naming the right project is a wording problem."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
