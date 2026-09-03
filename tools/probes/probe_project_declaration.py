"""Does the model name a project when it means one?

Work that names no project now goes to its own scratch workspace instead of
falling through to the server's launch directory, which was the user's own
repository. That default is only safe in one direction. Getting it wrong the
safe way sends a project task to scratch, and the user redoes it; getting it
wrong the other way writes a one-off into a repository they care about.

But it moves a load the host used to carry onto the model: naming a project is
now the *only* thing that keeps work out of scratch. Before this, a single
registered project was auto-resolved, so the model had no reason to fill
`project_id` at all -- which means existing logs predict nothing about how it
behaves under the new prompt. Hence a probe rather than a query.

    .venv\\Scripts\\python.exe -X utf8 tools/probes/probe_project_declaration.py [repeats]

Two rates, and they are not symmetric:

  * **Named when it meant a project.** The one that can degrade the product. A
    low rate is a wording problem in the routing block, not a design problem.
  * **Silent when it meant new work.** Near-free, because silence is the
    default -- this arm exists to catch the opposite failure, a model that
    names the project for everything and puts chess games in the repository.

The system prompt is assembled the way production assembles it, workspace
routing block included. Measuring the bare contract would repeat the mistake
recorded on 2026-08-02: a declaration rate of 144/144 on a bare `with_delegate`
prompt did not survive contact with the roster and routing blocks the model
actually sees.

## What it measured, 2026-08-03

**Project work: 2-4 out of 12, across five runs. Nothing moved it.**

  | intervention                              | amadeus | kanban |
  |-------------------------------------------|---------|--------|
  | baseline                                   | 4, 3    | 4      |
  | spelling the discriminator out at length   | 3 (+2 one-offs aimed at the repo) | -- |
  | a line of "what this project recently did" | 3       | 2      |

**New work: 10-12 out of 12 in every run.** Silence is a strong attractor and
one-offs stay out of the repository without being told anything.

The shape is sharper than the ratio. It names the project when the user says
its name and essentially never otherwise: "change the timeout in settings.py to
60" was silent 6 times out of 6 across every run and both names. So this is not
the transcription weakness that sank workspace_ref -- it invents no ids and
copies the forty-character one correctly whenever it decides at all. What it
cannot do is decide that an oblique reference means the one project listed.

Two hypotheses tested and rejected, so nobody retries them:

  * **Naming collision.** The repository is called amadeus and so is the
    character, so a candidate reading `name: amadeus` might be read as the
    assistant's own system. Controlled with a neutral name: no difference.
  * **Nothing connects the utterance to the project.** The candidate row
    carries only an id and a name, so "settings.py" has nothing to attach to.
    Adding a line of recent work did not help. (A first attempt at this was
    invalid: the seeded task was titled "seed the project", so the hint said
    nothing. The seed task is realistic now -- keep it that way.)

**One limit that matters.** This is a cold single turn. Production also injects
the conversation's own roster, so it measures the first thing said in a
session -- real, but not the common case, and the warm case is untested here.
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

# The repository the user actually works in is called amadeus, which is also
# the name of the character speaking. A candidate row reading `name: amadeus`
# may therefore be read as the assistant's own system rather than as somewhere
# to send code, so the same utterances run against a neutral name too. Without
# the control, a naming collision would be indistinguishable from an inability
# to route.
PROJECT_NAMES = ["amadeus", "kanban"]

# Work on the registered project. These must carry project_id: without it the
# instruction lands in a scratch workspace and the repository never sees it.
# Deliberately mixed -- named outright, referred to obliquely, and by a file
# that only exists in that repository.
PROJECT_WORK = [
    ("named", "帮我在 {name} 项目里加一个表情包功能。"),
    ("named-file", "{name} 里的 chat_handler.py 加个日志。"),
    ("oblique-this-project", "给这个项目的 README 补一段安装说明。"),
    ("oblique-our-code", "我们代码里那个唤醒词的阈值调高一点。"),
    ("bare-file", "把 settings.py 里的超时改成 60 秒。"),
    ("continuation-flavoured", "接着改我们项目的语音那块，加个静音检测。"),
]

# New work that belongs to no registered project. Naming one here would write
# a one-off into the user's repository, which is the failure the scratch
# default exists to remove.
NEW_WORK = [
    ("classic-oneoff", "做一个国际象棋游戏，能两个人对下就行。"),
    ("script", "写个小脚本，把一个文件夹里的图片批量压缩。"),
    ("toy", "帮我做个五子棋，带简单 AI。"),
    ("demo", "写一个演示用的网页，展示一下 CSS 动画。"),
    ("unrelated-tool", "做个命令行工具，把 markdown 转成纯文本。"),
    ("experiment", "试着写个爬虫，抓一下天气预报。"),
]

_DELEGATE_RE = re.compile(r"\[DELEGATE\b[^\]]*\]", re.IGNORECASE)
_PROJECT_RE = re.compile(r'project_id\s*=\s*"([^"]*)"', re.IGNORECASE)
_CWD_RE = re.compile(r'cwd\s*=\s*"([^"]*)"', re.IGNORECASE)


def _classify(reply: str, project_id: str) -> str:
    """named / other-project / cwd / silent / no-delegate.

    ``silent`` is the interesting one: a well-formed delegate that names no
    destination. That is correct for new work and a miss for project work, so
    the same outcome is a pass in one arm and a failure in the other.
    """

    tags = _DELEGATE_RE.findall(str(reply or ""))
    if not tags:
        return "no-delegate"
    blob = " ".join(tags)
    named = _PROJECT_RE.search(blob)
    if named and named.group(1).strip():
        return "named" if named.group(1).strip() == project_id else "other-project"
    cwd = _CWD_RE.search(blob)
    if cwd and cwd.group(1).strip():
        return "cwd"
    return "silent"


def _build_system_prompt(repo: Path) -> tuple[str, str]:
    """The prompt production would build, with one project registered.

    Returns the assembled prompt and the project id it advertises, so the
    classifier can tell "named the right one" from "invented an id".
    """

    from agent_host.provider_types import ProviderRunRequest
    from agent_host.work_ledger_store import WorkLedgerStore
    from llm.prompts import get_system_prompt
    from server.work_context import augment_system_prompt_with_active_provider_context
    from server.work_ledger_coordinator import WorkLedgerCoordinator

    store = WorkLedgerStore(repo.parent / "probe_ledger.sqlite3")
    coordinator = WorkLedgerCoordinator(store)
    with patch(
        "server.work_ledger_coordinator.cwd_in_project_registry", return_value=True
    ):
        request = ProviderRunRequest(
            provider="codex",
            # A plausible piece of past work, not "seed the probe". The routing
            # block may show what a project recently did, and a meaningless
            # title would test the wrong thing while looking like it worked.
            task="修好聊天窗口的滚动条，并给 wake_service 补一条启动日志",
            cwd=str(repo),
            mode="plan",
            metadata={"source": "probe", "session_id": "probe-session"},
        )
        coordinator.prepare_request(request)
        project_id = str(request.metadata["work"]["project_id"])
        coordinator.configure()
        try:
            prompt = augment_system_prompt_with_active_provider_context(
                get_system_prompt("with_delegate"),
                session_id="probe-session",
            )
        finally:
            coordinator.close()
    store.close()
    return prompt, project_id


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
    project_id: str,
    project_name: str,
    repeats: int,
    wanted: str,
    latencies: list[float],
) -> dict[str, int]:
    print(f"-- {label} (wanted: {wanted}) --")
    counts: dict[str, int] = {}
    for name, template in utterances:
        utterance = template.format(name=project_name)
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
                # A dead endpoint is not evidence about the model. The 08-02
                # session mistook one read timeout for a routing failure and
                # wrote it into a document before catching it.
                print(f"  skip {name}: infrastructure: {exc}")
                continue
            latencies.append(time.monotonic() - started)
            outcome = _classify(reply, project_id)
            outcomes.append(outcome)
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome != wanted:
                print(f"     {outcome:14s} {name:22s} {reply.strip()[:96]!r}")
        print(f"  {name:22s} {'/'.join(outcomes)}")
    total = sum(counts.values()) or 1
    print(f"  => {wanted} {counts.get(wanted, 0)}/{total}   " + "  ".join(
        f"{key} {value}" for key, value in sorted(counts.items()) if key != wanted
    ) + "\n")
    return counts


async def main() -> int:
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    latencies: list[float] = []
    results: dict[str, tuple[dict[str, int], dict[str, int]]] = {}

    for project_name in PROJECT_NAMES:
        with tempfile.TemporaryDirectory(prefix="probe_project_decl_") as temp:
            repo = Path(temp) / project_name
            repo.mkdir()
            import config.settings as settings

            settings.WORK_SCRATCH_ROOT = str(Path(temp) / "scratch")
            system, project_id = _build_system_prompt(repo)

            if "Known project candidates" not in system:
                print("the workspace routing block is absent; nothing to measure")
                return 1
            if project_id not in system:
                print("the seeded project is not a candidate; nothing to measure")
                return 1
            print(f"\n===== project named {project_name!r} ({project_id}) =====\n")

            project_counts = await _run_arm(
                "work on the registered project",
                PROJECT_WORK,
                system=system,
                project_id=project_id,
                project_name=project_name,
                repeats=repeats,
                wanted="named",
                latencies=latencies,
            )
            new_counts = await _run_arm(
                "new work that belongs to no project",
                NEW_WORK,
                system=system,
                project_id=project_id,
                project_name=project_name,
                repeats=repeats,
                wanted="silent",
                latencies=latencies,
            )
            results[project_name] = (project_counts, new_counts)

    print("-- summary --")
    for project_name, (project_counts, new_counts) in results.items():
        project_total = sum(project_counts.values()) or 1
        new_total = sum(new_counts.values()) or 1
        named = project_counts.get("named", 0)
        # A cwd is not a miss: an explicit directory routes just as well, and
        # the contract offers it as the alternative to a project id.
        project_ok = named + project_counts.get("cwd", 0)
        new_ok = new_counts.get("silent", 0) + new_counts.get("no-delegate", 0)
        print(f"  [{project_name}]")
        print(f"    project work reached the project : {project_ok}/{project_total}"
              f"   (named {named}, cwd {project_counts.get('cwd', 0)})")
        print(f"    new work stayed out of it        : {new_ok}/{new_total}")
        print(f"    new work aimed at the project    : {new_counts.get('named', 0)}"
              "   (each would write a one-off into the repository)")
        print(f"    invented a project id            : "
              f"{project_counts.get('other-project', 0) + new_counts.get('other-project', 0)}")
    if latencies:
        print(f"  latency median={statistics.median(latencies):.2f}s")
    print(
        "\n  Read the prose above before the ratios. A gap between the two\n"
        "  names is a naming collision, not an inability to route; a low rate\n"
        "  in both is neither, and is not fixed by more prose (2026-08-03)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
