"""Launch the shipping Electron product and drive one evidence-rich live Journey.

Unlike the isolated host and AUIP fixture Journeys, this driver starts Electron
itself. Electron starts the normal Python backend, the renderer sends normal
``chat.send`` requests, Codex performs real Provider work, Host permission and
Ledger state remain active, and Electron opens any resulting AUIP surface.

The script owns only an isolated runtime profile under ``runtime/``. It never
points a Provider at the source checkout or the user's real Desktop. Structural
assertions are automatic; role quality, audible delivery and visual polish are
retained as a review packet for a human or coding agent to judge afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import ntpath
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
ELECTRON_ROOT = ROOT / "electron"
RUNTIME = ROOT / "runtime"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.e2e_real_work_conversation import EventRecord, WsProbe, _safe_excerpt
from tools.live_runtime_acceptance import (
    EVIDENCE_METHODS,
    _event_run_id,
    _event_type,
    _is_provider_result_for_run,
    _is_terminal_observer_decision_for_run,
    _run_created_events,
    _wait_output_idle,
    is_bounded_progress_recovery_chain,
    progress_recovery_successor,
)
from tools.semantic_journey_evidence import build_evidence, code_identity
from tools.sync_auip_manifest import sync_manifest
from agent_host.work_ledger_store import WorkLedgerStore
from agent_host.work_ledger_types import CompletionDecision
from agent_host.provider_authoring import materialize_auip_runtime_assets
from config import settings
from server.auip_contract import parse_manifest
from server.auip_bundle_validation import validate_staged_auip_web_bundle


BACKEND_PORT = 17777
SCHEMA = "amadeus.live-product-journey.v1"
SUCCESS_STATUSES = {"done", "succeeded", "completed"}
JOURNEY_LAYERS = {"full", "adaptation", "interaction"}
ENGAGEMENT_MODES = {"observe", "collaborate", "delegate"}
WORK_STATUS_ANSWER_SOURCES = frozenset(
    {
        "work_status_narrator",
        "work_ledger_status",
        "work_ledger_status_fallback",
    }
)
SCENARIOS = {
    "lights": {
        "create": (
            "请在桌面创建一个可以直接打开的 HTML 小游戏：三乘三点灯，"
            "点格子能切换亮灭，再放一个重置按钮就够了。"
        ),
        "step": "你先自己操作一步看看。",
        "expected_situation_kind": "grid/v1",
    },
    "signal-routing": {
        "create": (
            "请在桌面创建一个可以直接打开的 HTML 信号路由小游戏。"
            "界面有 A、B、C 三个信号源和红、绿、蓝三个目标通道；玩家选择一个信号源，"
            "再选择一个当前空闲的目标通道来建立连接，每个目标最多接一个信号，"
            "目标是把三个信号全部连接到不同通道。再放一个重置按钮清空全部连接。"
        ),
        "step": "请从当前合法的连接选项里选择一个，并实际执行一步。",
        "expected_situation_kind": "choice/v1",
        "query": "刚才真的操作了吗？现在A、B、C分别接到哪个通道？",
        "query_oracle": {
            "state_paths": [
                "connections.A",
                "connections.B",
                "connections.C",
            ],
        },
    },
    "reactor": {
        "create": (
            "请在桌面创建一个可以直接打开的 HTML 反应堆温度控制小游戏。"
            "界面要显示当前核心温度、变化趋势和安全区间 45–55°C；初始温度为 85°C 且正在上升。"
            "提供升温、降温和稳定三个控制按钮，每次操作都要更新数值与趋势；目标是让温度进入安全区间并稳定下来。"
            "再提供一个重置按钮恢复初始状态。"
        ),
        "step": "请根据当前温度、安全区间和趋势选择一个当前合法的调节动作，并实际执行一步。",
        "expected_situation_kind": "scalars/v1",
        "ambient_state_advances": True,
        # The app may continue heating while launch speech is delivered, then
        # requires eight stable ticks after returning from ~95 C. This is an
        # application response horizon, not Host action latency.
        "controller_effect_timeout": 40.0,
        "scalar_oracle": {
            "metric_ids": ["temperature", "core_temp", "heat"],
            "controller_policy_direction": "toward_safe",
            "action_directions": {
                "heat": "increase",
                "cool": "decrease",
                "stabilize": "toward_safe",
                "set_cooling": "toward_safe",
            },
            "forbid_safe_interval_overshoot": True,
        },
    },
    "reactive-defense": {
        "create": (
            "请在桌面创建一个可以直接打开的 HTML 区域防御响应模拟器。"
            "界面有三个区域；玩家只设置最低严重度1–5和仅告警、派无人机、隔离区域三种持续策略，"
            "威胁随机出现后由应用本地立即响应，不要逐次询问主对话。"
            "另放一个‘生成严重度5威胁’按钮（data-testid=\"trigger-threat\"）、"
            "响应次数（data-testid=\"response-count\"）和重置按钮。策略可以立即停止，应用也要能单独使用。"
        ),
        "step": (
            "请把B区的持续响应策略设置为：严重度达到4时隔离该区域，并实际启用这个策略。"
        ),
        "expected_situation_kind": "controller/v1",
        "ambient_state_advances": True,
        "controller_oracle": {
            "trigger_test_id": "trigger-threat",
            "response_count_test_id": "response-count",
            "expected_policy_outcomes": [
                {
                    "policy": {
                        "zone": "zone-b",
                        "strategy": "quarantine",
                        "min": 4,
                    },
                    "instruction_relation": "follows",
                },
                {
                    # A valid one-shot app may expose the requested threshold
                    # and strategy globally rather than inventing a per-zone
                    # control dimension. Kurisu must explain that supported
                    # alternative instead of pretending B-only control exists.
                    "policy": {
                        "minimumSeverity": 4,
                        "strategy": "isolate",
                    },
                    "instruction_relation": "safe_alternative",
                },
            ],
            # The reference button deliberately chooses one of three zones
            # through Math.random. 0.5 deterministically selects zone-b so the
            # oracle tests the accepted B policy rather than probability.
            "trigger_random_value": 0.5,
        },
    },
    "bullet-hell-danger": {
        "create": (
            "请在桌面创建一个可以直接打开的 HTML 弹幕威胁实验。"
            "应用显示敌人、弹幕和生命状态；角色只设置持续战术，"
            "闪避和射击由应用本地控制器即时执行。"
        ),
        "step": "这波太密了，你能先保命吗",
        "expected_situation_kind": "controller/v1",
        "ambient_state_advances": True,
        "pre_step_setup": {
            "field_expectations": {
                "enemyPressure": "many",
                "projectilePressure": "dense",
                "healthCondition": "critical",
            },
        },
        "controller_oracle": {
            "trigger_test_id": "advance-tick",
            "response_count_test_id": "dodge-count",
            "expected_policy": {"mode": "evade"},
            "visual_command_test_id": "last-command",
            "visual_frame_test_id": "controller-frame-count",
            "motion_test_id": "player",
            "expected_visual_command": "dodge",
            "expect_narration": True,
            "narration_event_type": "battle.controller_milestone",
            "narration_payload_keys": ["mode", "command", "outcome"],
        },
        "query": "刚才有实际闪避吗？现在弹幕压力怎样？",
        "query_oracle": {"field_ids": ["projectilePressure"]},
    },
    "bullet-hell-calm": {
        "create": (
            "请在桌面创建一个可以直接打开的 HTML 弹幕威胁实验。"
            "应用显示敌人、弹幕和生命状态；角色只设置持续战术，"
            "闪避和射击由应用本地控制器即时执行。"
        ),
        "step": "现在空隙挺大，赶紧清场吧。",
        "expected_situation_kind": "controller/v1",
        "ambient_state_advances": True,
        "pre_step_setup": {
            "click_test_id": "calm-wave",
            "field_expectations": {
                "enemyPressure": "few",
                "projectilePressure": "light",
                "healthCondition": "stable",
            },
        },
        "controller_oracle": {
            "trigger_test_id": "advance-tick",
            "response_count_test_id": "shot-count",
            "expected_policy_options": [
                {"mode": "balance"},
                {"mode": "attack"},
            ],
            "visual_command_test_id": "last-command",
            "visual_frame_test_id": "controller-frame-count",
            "expected_visual_command": "fire",
            "expect_narration": True,
            "narration_event_type": "battle.controller_milestone",
            "narration_payload_keys": ["mode", "command", "outcome"],
        },
        "query": "刚才有实际开火吗？现在敌人压力怎样？",
        "query_oracle": {"field_ids": ["enemyPressure"]},
    },
    "bullet-hell-follow": {
        "create": (
            "请在桌面创建一个可以直接打开的 HTML 弹幕威胁实验。"
            "应用内 AI 连续操控，主对话只设置跟随、保命、攻击或额外目标优先级。"
        ),
        "step": "离得太远了，你能先跟上我吗",
        "outside_surface_proposal": "你能飞到屏幕上面吗",
        "expected_situation_kind": "controller/v1",
        "ambient_state_advances": True,
        "pre_step_setup": {
            "click_test_id": "follow-scene",
            "field_expectations": {
                "rewardOpportunity": "few",
                "healthCondition": "stable",
            },
        },
        "controller_oracle": {
            "trigger_test_id": "advance-tick",
            "response_count_test_id": "follow-count",
            "expected_policy": {"mode": "follow"},
            "visual_command_test_id": "last-command",
            "visual_frame_test_id": "controller-frame-count",
            "expected_visual_command": "follow",
            # The foreground B2 line already owns the immediate follow
            # commitment. A later Controller milestone may speak only when it
            # adds a distinct consequence; exact restatement is correctly
            # silent and must not make this mechanical oracle fail.
            "expect_narration": False,
            "narration_event_type": "battle.controller_milestone",
            "narration_payload_keys": ["mode", "command", "outcome"],
        },
        "query": "刚才真的跟上来了吗，现在还安全吗？",
        "query_oracle": {"field_ids": ["healthCondition"]},
    },
    "bullet-hell-rewards": {
        "create": (
            "请在桌面创建一个可以直接打开的 HTML 弹幕威胁实验。"
            "应用内 AI 连续操控，主对话只设置跟随、保命、攻击或额外目标优先级。"
        ),
        "step": "奖励挺多的，你能顺手拿一下吗",
        "expected_situation_kind": "controller/v1",
        "ambient_state_advances": True,
        "pre_step_setup": {
            "click_test_id": "reward-scene",
            "field_expectations": {
                "rewardOpportunity": "many",
                "healthCondition": "stable",
            },
        },
        "controller_oracle": {
            "trigger_test_id": "advance-tick",
            "response_count_test_id": "reward-count",
            "expected_policy_options": [
                {"mode": "rewards"},
                {"mode": "balance"},
            ],
            "visual_command_test_id": "last-command",
            "visual_frame_test_id": "controller-frame-count",
            "expected_visual_command_options": ["collect", "follow", "fire"],
            "expect_narration": True,
            "narration_event_type": "battle.controller_milestone",
            "narration_payload_keys": ["mode", "command", "outcome"],
        },
        "query": "刚才真的拿到了吗？现在奖励机会还多吗？",
        "query_oracle": {"field_ids": ["rewardOpportunity"]},
    },
    "eternal-loop": {
        "create": (
            "请在桌面创建一个自包含的 HTML 肉鸽射击游戏 ETERNAL LOOP。"
            "玩家在连续战斗中移动、瞄准、射击、躲避敌人并拾取同步结晶，"
            "循环结束后保留记忆强化。接入共同操作后，角色通过高层策略持续操控"
            "原游戏的移动、避敌、射击和拾取，不另写一套平行计数模拟。"
        ),
        "step": "别停，保命清怪，顺手捡奖励。",
        "steps": [
            "别停，保命清怪，顺手捡奖励。",
            "继续跑，改成猛攻，优先清怪。",
        ],
        "expected_situation_kind": "controller/v1",
        "controller_policy": True,
        "controller_effect_required": True,
        "controller_takeover_required": True,
        "controller_soak_oracle": {
            "phase_path": "phase",
            "active_phase": "running",
            "successful_terminal_phases": ["upgrade"],
            "progress_metric_id": "time",
            "progress_direction": "decrease",
            "health_metric_id": "hp",
            "health_floor": 0,
            "min_controller_effects": 2,
        },
        "adaptation_requirement": (
            "这次持续响应必须覆盖原游戏里的移动、避敌、瞄准射击和拾取，"
            "让角色能在无人按键时依照高层策略自主生存；"
            "不要把移动留给玩家，也不要用平行计数模拟代替原游戏物理；"
            "共享状态要保留原应用可见的运行与暂停事实，便于用户理解局面并核验控制窗口。"
        ),
        "ambient_state_advances": True,
        "pre_step_setup": {
            "local_sequence": [
                {
                    "click_selector": "#startBtn",
                    "situation_kind": "choice/v1",
                    "capture_situation_as": "running-controls",
                },
                {
                    "press_key": "p",
                    "situation_kind": "choice/v1",
                    "situation_changed_from": "running-controls",
                },
                {
                    "press_key": "p",
                    "situation_kind": "choice/v1",
                    "situation_matches": "running-controls",
                },
            ],
        },
        "query": "刚才接管了吗？局面怎样？",
        "query_oracle": {
            "metric_ids": ["hp", "time"],
            "terminal_state_path_any": ["phase"],
            "terminal_state_values": ["gameover", "upgrade"],
            "terminal_metric_ids": [],
            "terminal_state_field_ids": ["loop"],
        },
    },
    "launch-sequence": {
        "create": (
            "请在桌面创建一个可以直接打开的 HTML 火箭发射顺序小游戏。"
            "界面显示四个固定阶段：接通电源、校准导航、燃料加压、点火发射；"
            "玩家必须严格按这个顺序完成，只有当前下一阶段的按钮可用，已完成和尚未轮到的按钮都不可重复或跳过。"
            "用清晰的进度时间线显示已完成、当前下一步和待执行阶段，并提供重置按钮恢复到第一阶段。"
        ),
        "step": "请根据当前顺序选择唯一合法的下一阶段，并实际执行一步。",
        "expected_situation_kind": "sequence/v1",
    },
    "gomoku": {
        "create": (
            "请在桌面创建一个可以直接打开的 15×15 HTML 五子棋。"
            "玩家执黑先手，CPU 执白；轮到一方时另一方不能落子，横、竖或斜线先连成五子者获胜，"
            "并提供重置按钮。"
        ),
        "step": "你能下一手吗",
        "expected_situation_kind": "grid/v1",
        "query_oracle": {
            "state_field_ids": ["turn"],
            "terminal_state_field_ids": ["winner", "lifecycle"],
        },
    },
}


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _http_json(
    url: str,
    *,
    method: str = "GET",
    timeout: float = 5.0,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = Request(
        url,
        method=method,
        headers={"User-Agent": "amadeus-live-product", **(headers or {})},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class WindowsLaunchIdentity:
    account: str
    sid: str
    registered_profile: str | None
    inherited_profile: str


def _windows_launch_identity() -> WindowsLaunchIdentity | None:
    if os.name != "nt":
        return None

    completed = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    rows = list(csv.reader(completed.stdout.splitlines()))
    if completed.returncode or len(rows) != 1 or len(rows[0]) < 2:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise RuntimeError(f"could not resolve the Windows launch token: {detail}")

    account, sid = (str(value).strip() for value in rows[0][:2])
    registered_profile: str | None = None
    try:
        import winreg

        key_path = (
            "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList\\"
            + sid
        )
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            value, _value_type = winreg.QueryValueEx(key, "ProfileImagePath")
            registered_profile = os.path.expandvars(str(value).strip()) or None
    except FileNotFoundError:
        pass
    except OSError as error:
        raise RuntimeError(
            f"could not resolve the registered profile for Windows token {account} "
            f"({sid}): {error}"
        ) from error

    return WindowsLaunchIdentity(
        account=account,
        sid=sid,
        registered_profile=registered_profile,
        inherited_profile=str(os.environ.get("USERPROFILE") or "").strip(),
    )


def _require_windows_electron_profile(
    identity: WindowsLaunchIdentity | None,
) -> None:
    if identity is None:
        return

    registered = str(identity.registered_profile or "").strip()
    inherited = str(identity.inherited_profile or "").strip()
    paths_match = bool(registered and inherited) and (
        ntpath.normcase(ntpath.normpath(registered))
        == ntpath.normcase(ntpath.normpath(inherited))
    )
    if paths_match:
        return

    registered_label = registered or "<not registered>"
    inherited_label = inherited or "<not inherited>"
    raise RuntimeError(
        "refuse to launch hardware-accelerated Electron from a Windows token "
        "without its matching user profile: "
        f"token={identity.account} sid={identity.sid} "
        f"registered_profile={registered_label} "
        f"USERPROFILE={inherited_label}. "
        "Run this live GUI Journey as the interactive Windows user (for Codex, "
        "approve full-permission execution). Disabling the GPU is not a valid "
        "substitute."
    )


def _work_projection(response: dict[str, Any]) -> dict[str, Any]:
    value = response.get("work")
    if isinstance(value, dict):
        return value
    value = response.get("projection")
    return value if isinstance(value, dict) else {}


def _provider_status(event: EventRecord) -> str:
    payload = event.params.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    return str(
        event.params.get("status")
        or payload.get("status")
        or ""
    ).strip().lower()


def _provider_error(event: EventRecord) -> str:
    payload = event.params.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    return str(
        event.params.get("error")
        or payload.get("error")
        or ""
    ).strip()


def _work_item_is_settled(item: dict[str, Any]) -> bool:
    execution = str(item.get("execution") or "").strip().lower()
    completion = str(item.get("completion") or "").strip().lower()
    attention = str(item.get("attention") or "").strip().lower()
    state = str(item.get("state") or "").strip().lower()
    pending = int(item.get("pendingPermissionCount") or 0)
    if pending or attention == "permission":
        return False
    if execution == "succeeded":
        # An explicitly/policy accepted item is a stronger Host-owned terminal
        # fact than the last completion assessment.  Approved ephemeral exports
        # intentionally retain their earlier partial assessment as audit
        # evidence even after the permission commits and the item is accepted.
        if completion == "complete" or state == "accepted":
            return True
        return (
            str(item.get("liveness") or "").strip().lower() == "terminal"
            and completion in {"partial", "incomplete"}
        )
    if execution in {"failed", "cancelled"}:
        return completion in {"complete", "partial", "incomplete"}
    return False


def _contains_situation_kind(value: Any, expected_kind: str) -> bool:
    return _find_situation(value, expected_kind) is not None


def _find_situation(value: Any, expected_kind: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if str(value.get("kind") or "") == str(expected_kind or ""):
            return value
        for item in value.values():
            found = _find_situation(item, expected_kind)
            if found is not None:
                return found
        return None
    if isinstance(value, list):
        for item in value:
            found = _find_situation(item, expected_kind)
            if found is not None:
                return found
    return None


def _receipt_bound_state(
    update: dict[str, Any],
    later_session: dict[str, Any],
) -> Any:
    """Prefer the action receipt's exact revision over later ambient state."""

    state = update.get("state")
    if isinstance(state, dict):
        return state
    return later_session.get("state")


def _first_accepted_situation(
    events: list[EventRecord],
    *,
    after: int,
    app_session_id: str,
    expected_kind: str,
    fallback_state: Any,
) -> dict[str, Any] | None:
    """Bind launch truth to the first accepted projection, not a later timer tick."""

    for event in events[max(0, int(after)) :]:
        if (
            event.method != "auip.updated"
            or str(event.params.get("app_session_id") or "") != app_session_id
            or int(event.params.get("revision") or 0) <= 0
        ):
            continue
        situation = _find_situation(event.params.get("state"), expected_kind)
        if situation is not None:
            return situation
    return _find_situation(fallback_state, expected_kind)


def _event_work_item_id(event: EventRecord) -> str:
    payload = event.params.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    metadata = event.params.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    payload_metadata = payload.get("metadata")
    payload_metadata = payload_metadata if isinstance(payload_metadata, dict) else {}
    for owner in (event.params, payload, metadata, payload_metadata):
        work = owner.get("work")
        if isinstance(work, dict):
            value = str(work.get("work_item_id") or work.get("workItemId") or "").strip()
            if value:
                return value
        value = str(owner.get("work_item_id") or owner.get("workItemId") or "").strip()
        if value:
            return value
    return ""


def _is_work_status_answer(event: EventRecord) -> bool:
    return bool(
        event.method == "chat.observer_decision"
        and str(event.params.get("source") or "") in WORK_STATUS_ANSWER_SOURCES
    )


def _inside(path: str | Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return any(resolved == root or root in resolved.parents for root in roots)


def _wire_refreshed_auip_runtime_assets(
    entry_path: Path,
    runtime_assets: dict[str, dict[str, str]],
) -> tuple[str, ...]:
    """Retarget known legacy SDK script refs inside an isolated test copy."""

    stable_by_filename = {
        Path(relative_name).name: relative_name.replace("\\", "/")
        for relative_name in runtime_assets
    }
    source = entry_path.read_text(encoding="utf-8")
    replaced: list[str] = []
    pattern = re.compile(
        r"(?P<prefix><script\b[^>]*?\bsrc\s*=\s*[\"'])(?P<src>[^\"']+)(?P<suffix>[\"'][^>]*>)",
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        raw_src = str(match.group("src") or "")
        clean_src = raw_src.split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
        stable = stable_by_filename.get(clean_src.rsplit("/", 1)[-1])
        if not stable:
            return match.group(0)
        target = "./" + stable
        if raw_src == target:
            return match.group(0)
        replaced.append(f"{raw_src}->{target}")
        return f"{match.group('prefix')}{target}{match.group('suffix')}"

    updated = pattern.sub(replace, source)
    if updated != source:
        entry_path.write_text(updated, encoding="utf-8")
    return tuple(replaced)


def _seed_verified_app(
    *,
    run_root: Path,
    session_id: str,
    scenario_name: str,
    scenario: dict[str, Any],
    journey_layer: str,
    source: str | Path,
    controller_lease_ms: int = 0,
    refresh_host_runtime_assets: bool = False,
) -> dict[str, Any]:
    """Materialize a fresh WorkItem at the requested product-test boundary.

    ``adaptation`` copies only one verified standalone HTML application. The
    Provider must still author every AUIP artifact. ``interaction`` copies a
    complete verified bundle, but the shipping Host still launches it into a
    fresh AppSession. The seed is test setup, not evidence for an earlier
    layer, and is therefore recorded explicitly in the report.
    """

    layer = str(journey_layer or "").strip().lower()
    if layer not in {"adaptation", "interaction"}:
        raise ValueError(f"cannot seed journey layer: {journey_layer!r}")
    clean_session = str(session_id or "").strip()
    if not clean_session:
        raise ValueError("the live runtime did not expose a current Session")
    source_path = Path(source).expanduser().resolve()
    controller_policy = False
    if layer == "adaptation":
        if not source_path.is_file() or source_path.suffix.lower() not in {
            ".html",
            ".htm",
        }:
            raise ValueError("adaptation seed must be one standalone HTML file")
    elif not source_path.is_dir():
        raise ValueError("interaction seed must be one complete AUIP bundle directory")

    state = run_root / "state"
    workspace = state / "scratch" / f"seeded-{scenario_name}-{uuid.uuid4().hex[:8]}"
    workspace.mkdir(parents=True, exist_ok=False)
    if layer == "adaptation":
        shutil.copy2(source_path, workspace / source_path.name)
    else:
        shutil.copytree(
            source_path,
            workspace,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".git", ".amadeus", "__pycache__"),
        )
        manifest_path = workspace / "auip.manifest.json"
        if not manifest_path.is_file():
            raise ValueError(
                "interaction seed must contain auip.manifest.json at its root"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        controller_policy = isinstance(manifest.get("controller"), dict)
        if controller_lease_ms:
            controller = manifest.get("controller")
            if not isinstance(controller, dict):
                raise ValueError(
                    "controller lease override requires a Controller manifest"
                )
            controller["leaseDurationMs"] = int(controller_lease_ms)
            parse_manifest(manifest)
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            html_entries = sorted(
                path
                for path in workspace.iterdir()
                if path.is_file() and path.suffix.casefold() in {".html", ".htm"}
            )
            preferred = [
                path for path in html_entries if path.name.casefold() == "index.html"
            ]
            entries = preferred if len(preferred) == 1 else html_entries
            if len(entries) != 1:
                raise ValueError(
                    "controller lease override requires one unambiguous HTML entry"
                )
            sync_manifest(manifest_path, entries[0])
        declared = {
            str(value)
            for value in manifest.get("situationKinds") or []
            if str(value)
        }
        expected_kind = str(scenario.get("expected_situation_kind") or "")
        if expected_kind not in declared:
            raise ValueError(
                "interaction seed does not declare the scenario situation kind: "
                f"{expected_kind}"
            )
        runtime_assets: dict[str, dict[str, str]] = {}
        if refresh_host_runtime_assets:
            runtime_assets = materialize_auip_runtime_assets(workspace)
            html_entries = sorted(
                path
                for path in workspace.iterdir()
                if path.is_file() and path.suffix.casefold() in {".html", ".htm"}
            )
            preferred = [
                path for path in html_entries if path.name.casefold() == "index.html"
            ]
            entries = preferred if len(preferred) == 1 else html_entries
            if len(entries) != 1:
                raise ValueError(
                    "runtime refresh requires one unambiguous HTML entry"
                )
            _wire_refreshed_auip_runtime_assets(entries[0], runtime_assets)
            validate_staged_auip_web_bundle(
                workspace,
                materialized_files=tuple(sorted(runtime_assets)),
            )
    if layer != "interaction":
        runtime_assets = {}

    _initialize_seed_repository(workspace)

    ledger_path = state / "work_ledger.sqlite3"
    with WorkLedgerStore(ledger_path) as store:
        project = store.create_or_get_project(workspace)
        work = store.create_work_item(
            project.project_id,
            title=f"Seeded {scenario_name} application",
            goal=str(scenario.get("create") or ""),
            workspace_mode="local",
            workspace_path=workspace,
            metadata={
                "source": "live_product_journey_seed",
                "journey_layer": layer,
                "seed_source": str(source_path),
                "source_user_text": str(scenario.get("create") or ""),
            },
        )
        attempt = store.create_attempt(
            work.work_item_id,
            provider="fixture",
            task=f"Seed verified {scenario_name} application at {layer} boundary",
            metadata={
                "session_id": clean_session,
                "source": "live_product_journey_seed",
                "journey_layer": layer,
                "source_user_text": str(scenario.get("create") or ""),
                **(
                    {
                        "auip_bundle_root": str(workspace),
                        "auip_host_validates_bundle": True,
                        "auip_host_materialized_files": sorted(runtime_assets),
                        "auip_host_materialized_assets": {
                            filename: {
                                "sha256": identity["sha256"],
                                "size_bytes": Path(identity["path"]).stat().st_size,
                            }
                            for filename, identity in runtime_assets.items()
                        },
                    }
                    if runtime_assets
                    else {}
                ),
            },
        )
        files = sorted(
            path
            for path in workspace.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(workspace).parts
        )
        if not files:
            raise ValueError("seed source contained no files")
        for path in files:
            payload = path.read_bytes()
            store.register_artifact(
                work.work_item_id,
                attempt_id=attempt.attempt_id,
                kind="business.file",
                title=path.name,
                path=path,
                status="registered",
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                metadata={"seeded": True, "journey_layer": layer},
            )
        store.update_attempt(attempt.attempt_id, execution_status="succeeded")
        store.record_completion(
            work.work_item_id,
            CompletionDecision(
                execution_status="succeeded",
                completeness="complete",
                attention="none",
                work_item_state="review_ready",
                rationale=(
                    "Verified application seeded at the declared live Journey boundary."
                ),
                terminal=True,
            ),
            attempt_id=attempt.attempt_id,
            source="host",
            evidence={
                "journey_layer": layer,
                "seed_source": str(source_path),
                "file_count": len(files),
            },
        )
        store.set_session_active_work_item(
            clean_session,
            work.work_item_id,
            metadata={"source": "live_product_journey_seed"},
        )
        return {
            "journey_layer": layer,
            "source": str(source_path),
            "workspace": str(workspace),
            "work_item_id": work.work_item_id,
            "attempt_id": attempt.attempt_id,
            "controller_policy": controller_policy,
            "files": [str(path.relative_to(workspace)).replace("\\", "/") for path in files],
        }


def _initialize_seed_repository(workspace: Path) -> None:
    """Give the seeded WorkItem a clean writer/diff boundary of its own."""

    git = shutil.which("git.exe") or shutil.which("git")
    if not git:
        raise FileNotFoundError("git is required to isolate a seeded live Journey")
    commands = (
        [git, "init", "--quiet"],
        [git, "add", "--all"],
        [
            git,
            "-c",
            "user.name=Amadeus Journey",
            "-c",
            "user.email=journey@localhost",
            "commit",
            "--quiet",
            "-m",
            "Seed verified application boundary",
        ],
    )
    for command in commands:
        result = subprocess.run(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                "failed to initialize seeded WorkItem repository: "
                + (result.stderr.strip() or result.stdout.strip())
            )


def _compact_event(event: EventRecord, *, source_index: int) -> dict[str, Any]:
    return {
        "source_index": int(source_index),
        "elapsed_s": round(float(event.elapsed_s), 3),
        "method": event.method,
        "params": _safe_excerpt(event.params, 1600),
    }


@dataclass
class TurnEvidence:
    label: str
    text: str
    event_start: int
    started_elapsed_s: float = 0.0
    event_end: int = 0
    turn_id: str = ""
    reply: str = ""
    screenshot: str = ""
    run_ids: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "text": self.text,
            "turn_id": self.turn_id,
            "reply": self.reply,
            "screenshot": self.screenshot,
            "run_ids": list(self.run_ids),
            "checks": dict(self.checks),
            "notes": list(self.notes),
            "timings": dict(self.timings),
            # Indices refer to the unfiltered WebSocket stream. Every compact
            # report event carries the matching source_index, so an offline
            # reviewer cannot accidentally slice the filtered list with raw
            # stream offsets.
            "source_event_range": [self.event_start, self.event_end],
        }


class ElectronProduct:
    def __init__(
        self,
        *,
        run_root: Path,
        debug_port: int,
        no_tts: bool,
        identity: dict[str, Any],
    ) -> None:
        self.run_root = run_root
        self.debug_port = int(debug_port)
        self.no_tts = bool(no_tts)
        self.identity = dict(identity)
        self.process: subprocess.Popen | None = None
        self.log_handle = None
        self.playwright = None
        self.browser = None
        self.page = None
        self.app_console_errors: list[str] = []
        self.app_page_errors: list[str] = []
        self._instrumented_app_pages: set[int] = set()
        self.log_path = run_root / "electron-and-backend.log"
        # Pin one ephemeral credential pair for this isolated product run so
        # Electron, its child backend, and the test probe authenticate as the
        # same desktop instance. These values stay in process memory and are
        # never written to the report or command line.
        self.backend_token = uuid.uuid4().hex
        self.backend_instance_nonce = uuid.uuid4().hex

    @property
    def backend_websocket_protocols(self) -> tuple[str, str]:
        return (
            "amadeus.local.v1",
            f"amadeus.auth.{self.backend_token}",
        )

    def _instrument_app_page(self, page: Any) -> None:
        marker = id(page)
        if marker in self._instrumented_app_pages:
            return
        self._instrumented_app_pages.add(marker)
        page.on(
            "console",
            lambda message: self.app_console_errors.append(str(message.text))
            if str(message.type) == "error"
            else None,
        )
        page.on("pageerror", lambda error: self.app_page_errors.append(str(error)))

    def app_diagnostics(self) -> dict[str, list[str]]:
        return {
            "console_errors": list(self.app_console_errors),
            "page_errors": list(self.app_page_errors),
        }

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        for name in (
            "AMADEUS_PYTHON",
            "AMADUES_PYTHON",
        ):
            env.pop(name, None)
        for name in tuple(env):
            if name.startswith(("LOCUS_", "AMADEUS_LOCUS_")):
                env.pop(name, None)
        state = self.run_root / "state"
        env.update(
            {
                "NODE_ENV": "production",
                "AMADEUS_SESSION_DIR": str(state / "sessions"),
                "AMADEUS_WORK_LEDGER_PATH": str(state / "work_ledger.sqlite3"),
                "AMADEUS_PROVIDER_ACTIVITY_PATH": str(
                    state / "provider_activity.jsonl"
                ),
                "AMADEUS_SERVER_LOG_PATH": str(state / "server.log"),
                "AMADEUS_DESKTOP_PATH": str(state / "desktop"),
                "AMADEUS_ELECTRON_USER_DATA_DIR": str(state / "electron-user-data"),
                "AMADEUS_ELECTRON_CACHE_DIR": str(state / "electron-cache"),
                "WORK_SCRATCH_ROOT": str(state / "scratch"),
                "WORK_WORKTREE_ROOT": str(state / "worktrees"),
                "WORK_PROJECT_ALLOWLIST": str(state / "scratch"),
                "WAKE_ENABLED": "0",
                "VTS_ENABLED": "0",
                "AEC_REALTIME_ENABLED": "0",
                "AMADEUS_PRE_TRANSLATION_ENABLED": "0",
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "AMADEUS_CODE_SHA": str(self.identity.get("commit_sha") or ""),
                "AMADEUS_WORKSPACE_DIRTY": (
                    "1" if self.identity.get("workspace_dirty") else "0"
                ),
                "AMADEUS_WORKSPACE_FINGERPRINT": str(
                    self.identity.get("workspace_fingerprint") or ""
                ),
                "AMADEUS_BACKEND_AUTH_MODE": "required",
                "AMADEUS_BACKEND_TOKEN": self.backend_token,
                "AMADEUS_BACKEND_INSTANCE_NONCE": self.backend_instance_nonce,
            }
        )
        if self.no_tts:
            env["AMADEUS_E2E_NO_TTS"] = "1"
        else:
            env.pop("AMADEUS_E2E_NO_TTS", None)
        return env

    async def start(self, *, startup_timeout: float) -> None:
        _require_windows_electron_profile(_windows_launch_identity())
        if _port_is_open(BACKEND_PORT):
            raise RuntimeError(
                f"port {BACKEND_PORT} is already in use; refuse to replace a live runtime"
            )
        if _port_is_open(self.debug_port):
            raise RuntimeError(f"debug port {self.debug_port} is already in use")
        state = self.run_root / "state"
        for name in ("sessions", "desktop", "scratch", "worktrees"):
            (state / name).mkdir(parents=True, exist_ok=True)
        electron = (
            ELECTRON_ROOT / "node_modules" / "electron" / "dist" / "electron.exe"
            if os.name == "nt"
            else ELECTRON_ROOT / "node_modules" / ".bin" / "electron"
        )
        if not electron.is_file():
            raise FileNotFoundError(f"Electron executable is missing: {electron}")
        self.log_handle = self.log_path.open("w", encoding="utf-8", newline="\n")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self.process = subprocess.Popen(
            [
                str(electron),
                ".",
                f"--remote-debugging-port={self.debug_port}",
            ],
            cwd=str(ELECTRON_ROOT),
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        deadline = time.monotonic() + max(1.0, startup_timeout)
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"Electron exited during startup with {self.process.returncode}"
                )
            try:
                health = await asyncio.to_thread(
                    _http_json,
                    f"http://127.0.0.1:{BACKEND_PORT}/health",
                    timeout=3.0,
                )
                if health.get("status") == "ok" and _port_is_open(self.debug_port):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.4)
        else:
            raise TimeoutError("Electron product did not become ready")

        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.debug_port}"
        )
        for context in self.browser.contexts:
            context.on("page", self._instrument_app_page)
        page_deadline = time.monotonic() + 30.0
        while time.monotonic() < page_deadline:
            pages = [
                page
                for context in self.browser.contexts
                for page in context.pages
                if "electron/dist/renderer/index.html" in page.url.replace("\\", "/")
            ]
            if pages:
                self.page = pages[0]
                break
            await asyncio.sleep(0.2)
        if self.page is None:
            raise RuntimeError("Electron main renderer page was not exposed over CDP")
        await self.page.locator(
            'textarea[placeholder*="Type a message"]'
        ).wait_for(state="visible", timeout=30_000)

    async def select_chat_provider(self, provider: str) -> None:
        """Select the same visible provider control a person would use."""

        value = str(provider or "").strip()
        if not value or self.page is None:
            return
        selected = await self.page.locator("select").evaluate_all(
            """(elements, value) => {
              const target = elements.find(element =>
                Array.from(element.options || []).some(option => option.value === value)
              )
              if (!target) return false
              target.value = value
              target.dispatchEvent(new Event('change', { bubbles: true }))
              return true
            }""",
            value,
        )
        if selected is not True:
            raise RuntimeError(f"chat provider is not selectable in the product UI: {value}")
        await self.page.wait_for_timeout(250)

    async def screenshot(self, name: str) -> Path:
        if self.page is None:
            raise RuntimeError("Electron renderer is unavailable")
        target = self.run_root / "screenshots" / f"{name}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(target), full_page=True)
        return target

    async def app_pages(self) -> list[Any]:
        if self.browser is None:
            return []
        return [
            page
            for context in self.browser.contexts
            for page in context.pages
            if page is not self.page
        ]

    async def stop(self) -> None:
        try:
            await asyncio.to_thread(
                _http_json,
                f"http://127.0.0.1:{BACKEND_PORT}/shutdown",
                method="POST",
                timeout=3.0,
                headers={"X-Amadeus-Token": self.backend_token},
            )
        except Exception:
            pass
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        if self.process is not None and self.process.poll() is None:
            try:
                await asyncio.to_thread(self.process.wait, 5.0)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    await asyncio.to_thread(self.process.wait, 5.0)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    await asyncio.to_thread(self.process.wait, 5.0)
        if self.log_handle is not None:
            self.log_handle.close()


async def _wait_provider_ready(
    probe: WsProbe,
    *,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = await probe.request("runtime.status", {}, timeout=20.0)
        if _runtime_ready_for_live_journey(latest):
            return latest
        await asyncio.sleep(0.4)
    raise TimeoutError(
        "Codex Provider and current Session did not become ready in the live product"
    )


def _runtime_ready_for_live_journey(status: Mapping[str, Any]) -> bool:
    provider = status.get("provider")
    availability = (
        provider.get("availability") if isinstance(provider, Mapping) else []
    )
    provider_ready = any(
        isinstance(item, Mapping)
        and item.get("provider_id") == "codex"
        and item.get("ready") is True
        and item.get("registered") is True
        for item in (availability or [])
    )
    session = status.get("session")
    session_id = str(
        session.get("current_session_id")
        if isinstance(session, Mapping)
        else ""
    ).strip()
    return bool(provider_ready and session_id)


def _controller_effect_timeout(
    *,
    args: argparse.Namespace,
    scenario: Mapping[str, Any],
) -> float:
    explicit = float(getattr(args, "controller_effect_timeout", 0.0) or 0.0)
    scenario_horizon = float(scenario.get("controller_effect_timeout") or 20.0)
    requested = explicit if explicit > 0 else scenario_horizon
    return max(1.0, min(float(args.auip_timeout), requested))


async def _finalize_product_run(
    product: ElectronProduct,
    report: dict[str, Any],
    report_path: Path,
) -> None:
    """Persist evidence and stop the exact product tree even on cancellation."""

    try:
        if product.page is not None:
            report["paths"].setdefault(
                "failure_screenshot",
                str(await product.screenshot("failure-or-cleanup")),
            )
    except Exception:
        pass
    finally:
        await product.stop()
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def _send_ui_turn(
    product: ElectronProduct,
    probe: WsProbe,
    *,
    label: str,
    text: str,
    chat_timeout: float,
) -> TurnEvidence:
    if product.page is None:
        raise RuntimeError("Electron renderer is unavailable")
    turn = TurnEvidence(
        label=label,
        text=text,
        event_start=len(probe.state.events),
        started_elapsed_s=time.monotonic() - probe.state.started_at,
    )
    field = product.page.locator('textarea[placeholder*="Type a message"]')
    await field.fill(text)
    await field.press("Enter")
    complete = await probe.wait_event(
        lambda event: event.method == "chat.complete",
        after=turn.event_start,
        timeout=chat_timeout,
        description=f"{label} chat.complete",
    )
    turn.turn_id = str(complete.params.get("turn_id") or "")
    turn.reply = str(complete.params.get("full_text") or "")
    await asyncio.sleep(0.25)
    turn.event_end = len(probe.state.events)
    turn.run_ids = [
        _event_run_id(event)
        for event in _run_created_events(
            probe.state.events[turn.event_start : turn.event_end]
        )
        if _event_run_id(event)
    ]
    turn.screenshot = str(await product.screenshot(f"turn-{label}"))
    return turn


def _populate_turn_timings(
    turns: list[TurnEvidence],
    events: list[EventRecord],
) -> None:
    """Attach comparable UI-to-visible/action timing without timing oracles.

    A direct B2 branch may emit ``chat.complete`` before its TTS line starts,
    while ordinary Main Chat completes after playback. Use the next turn's
    source boundary, rather than the early per-turn event_end, so both shapes
    retain their first visible/audio evidence without making incidental event
    order a pass condition.
    """

    for index, turn in enumerate(turns):
        start = float(turn.started_elapsed_s or 0.0)
        if start <= 0.0:
            continue
        next_start = (
            int(turns[index + 1].event_start)
            if index + 1 < len(turns)
            else len(events)
        )
        window_end = max(int(turn.event_end), next_start)
        segment = events[int(turn.event_start) : min(window_end, len(events))]

        def first_elapsed(method: str, *, accepted_receipt: bool = False) -> float:
            for event in segment:
                if event.method != method:
                    continue
                if accepted_receipt:
                    receipt = (
                        event.params.get("receipt")
                        if isinstance(event.params.get("receipt"), dict)
                        else None
                    )
                    if receipt is None or receipt.get("accepted") is not True:
                        continue
                return round(max(0.0, float(event.elapsed_s) - start), 3)
            return -1.0

        timings = {
            "first_chat_token_s": first_elapsed("chat.token"),
            "application_action_requested_s": first_elapsed(
                "auip.action.requested"
            ),
            "accepted_receipt_s": first_elapsed(
                "auip.updated",
                accepted_receipt=True,
            ),
            "first_tts_sentence_start_s": first_elapsed("tts.sentence_start"),
            "chat_complete_s": first_elapsed("chat.complete"),
        }
        turn.timings.update(
            {name: value for name, value in timings.items() if value >= 0.0}
        )


async def _selected_work(probe: WsProbe) -> tuple[dict[str, Any], dict[str, Any]]:
    response = await probe.request("work.list", {}, timeout=20.0)
    projection = _work_projection(response)
    selected = (
        projection.get("selected")
        if isinstance(projection.get("selected"), dict)
        else {}
    )
    return projection, selected


async def _resolve_safe_permission(
    *,
    product: ElectronProduct,
    probe: WsProbe,
    run_root: Path,
    seen: set[str],
    evidence: list[dict[str, Any]],
) -> bool:
    projection, selected = await _selected_work(probe)
    request_id = str(selected.get("pendingPermissionRequestId") or "").strip()
    if not request_id or request_id in seen:
        return False
    work_item_id = str(projection.get("selectedWorkItemId") or "").strip()
    attempt_id = str(selected.get("attemptId") or "").strip()
    revision = str(projection.get("revision") or "").strip()
    detail = await probe.request(
        "work.get",
        {"work_item_id": work_item_id},
        timeout=20.0,
    )
    item = detail.get("item") if isinstance(detail.get("item"), dict) else {}
    request = next(
        (
            row
            for row in item.get("permissions") or []
            if isinstance(row, dict) and row.get("request_id") == request_id
        ),
        {},
    )
    workspace_path = str(item.get("workspacePath") or item.get("workspace_path") or "")
    allowed_roots = (
        run_root.resolve(),
        (ROOT / "skills").resolve(),
    )
    scopes = [str(value) for value in request.get("scope_paths") or []]
    workspace_safe = bool(workspace_path and _inside(workspace_path, allowed_roots))
    scopes_safe = all(_inside(path, allowed_roots) for path in scopes)
    allowed = bool(
        workspace_safe
        and scopes_safe
        and "allow_once" in (request.get("options") or [])
    )
    screenshot = ""
    screenshot_error = ""
    try:
        screenshot = str(
            await product.screenshot(f"permission-{len(evidence) + 1}")
        )
    except Exception as exc:
        # Visual evidence is useful, but a renderer capture timeout must not
        # prevent the Host from resolving an otherwise bounded permission.
        screenshot_error = f"{type(exc).__name__}: {exc}"
    row = {
        "request": _safe_excerpt(request, 2400),
        "workspace_path": workspace_path,
        "workspace_safe": workspace_safe,
        "scopes_safe": scopes_safe,
        "decision": "allow_once" if allowed else "withheld",
        "screenshot": screenshot,
        **({"screenshot_error": screenshot_error} if screenshot_error else {}),
    }
    seen.add(request_id)
    if not allowed:
        evidence.append(row)
        raise RuntimeError(
            f"permission {request_id} exceeded the isolated live Journey scope"
        )
    resolution = await probe.request(
        "work.permission.resolve",
        {
            "permission_request_id": request_id,
            "work_item_id": work_item_id,
            "attempt_id": attempt_id,
            "revision": revision,
            "decision": "allow_once",
        },
        timeout=30.0,
    )
    row["resolution"] = _safe_excerpt(resolution, 1600)
    evidence.append(row)
    if resolution.get("ok") is not True:
        raise RuntimeError(f"permission {request_id} was not accepted by Host")
    return True


async def _wait_run_terminal(
    *,
    product: ElectronProduct,
    probe: WsProbe,
    run_id: str,
    after: int,
    run_root: Path,
    timeout: float,
    seen_permissions: set[str],
    permission_evidence: list[dict[str, Any]],
) -> tuple[EventRecord, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    terminal: EventRecord | None = None
    while time.monotonic() < deadline:
        if terminal is None:
            terminal = next(
                (
                    event
                    for event in probe.state.events[after:]
                    if _is_provider_result_for_run(event, run_id)
                ),
                None,
            )
        await _resolve_safe_permission(
            product=product,
            probe=probe,
            run_root=run_root,
            seen=seen_permissions,
            evidence=permission_evidence,
        )
        if terminal is not None:
            created_for_run = next(
                (
                    event
                    for event in _run_created_events(probe.state.events[after:])
                    if _event_run_id(event) == run_id
                ),
                None,
            )
            recovery_successor = (
                progress_recovery_successor(
                    probe.state.events[after:],
                    created_for_run,
                )
                if created_for_run is not None
                else None
            )
            if recovery_successor is not None:
                return terminal, {
                    "execution": "recovering",
                    "attention": "recovery",
                    "providerRecoveryRunId": _event_run_id(recovery_successor),
                }
            projection, _selected = await _selected_work(probe)
            item = next(
                (
                    row
                    for row in projection.get("items") or []
                    if isinstance(row, dict)
                    and str(row.get("runId") or row.get("currentRunId") or "")
                    == run_id
                ),
                None,
            )
            if isinstance(item, dict):
                if _work_item_is_settled(item):
                    return terminal, dict(item)
        await asyncio.sleep(0.5)
    raise TimeoutError(f"timed out waiting for Provider result {run_id}")


async def _wait_created_run(
    probe: WsProbe,
    *,
    after: int,
    timeout: float,
) -> EventRecord:
    return await probe.wait_event(
        lambda event: event.method == "provider.event"
        and _event_type(event) == "run.created",
        after=after,
        timeout=timeout,
        description="Provider run.created",
    )


async def _wait_provider_chain_terminal(
    *,
    product: ElectronProduct,
    probe: WsProbe,
    initial_created: EventRecord,
    after: int,
    run_root: Path,
    timeout: float,
    seen_permissions: set[str],
    permission_evidence: list[dict[str, Any]],
) -> tuple[EventRecord, dict[str, Any], list[str]]:
    """Follow at most one Host-signed progress-only successor Attempt."""

    current = initial_created
    run_ids = [_event_run_id(current)]
    deadline = time.monotonic() + max(1.0, float(timeout))
    for ordinal in range(2):
        remaining = max(1.0, deadline - time.monotonic())
        terminal, item = await _wait_run_terminal(
            product=product,
            probe=probe,
            run_id=_event_run_id(current),
            after=after,
            run_root=run_root,
            timeout=remaining,
            seen_permissions=seen_permissions,
            permission_evidence=permission_evidence,
        )
        terminal_metadata = (
            terminal.params.get("metadata")
            if isinstance(terminal.params.get("metadata"), dict)
            else {}
        )
        completion = (
            terminal_metadata.get("provider_completion")
            if isinstance(terminal_metadata.get("provider_completion"), dict)
            else {}
        )
        if (
            _provider_status(terminal) not in SUCCESS_STATUSES
            and ordinal == 0
            and completion.get("classification") == "progress_only_completion"
        ):
            successor = progress_recovery_successor(
                probe.state.events[after:],
                current,
            )
            if successor is None:
                try:
                    successor = await probe.wait_event(
                        lambda event: event.method == "provider.event"
                        and _event_type(event) == "run.created"
                        and progress_recovery_successor([event], current) is event,
                        after=after,
                        timeout=min(5.0, remaining),
                        description="bounded progress-only Provider recovery",
                    )
                except TimeoutError:
                    successor = None
            if successor is not None and is_bounded_progress_recovery_chain(
                [current, successor]
            ):
                current = successor
                run_ids.append(_event_run_id(successor))
                continue
        return terminal, item, run_ids
    raise RuntimeError("Provider progress-only recovery exceeded one successor Attempt")


def _new_runs(events: list[EventRecord], *, after: int) -> list[EventRecord]:
    return _run_created_events(events[after:])


async def _wait_auip_active(
    probe: WsProbe,
    *,
    after: int,
    timeout: float,
) -> EventRecord:
    return await probe.wait_event(
        lambda event: event.method == "auip.updated"
        and str(event.params.get("status") or "").strip().lower() == "active",
        after=after,
        timeout=timeout,
        description="active AUIP AppSession",
    )


async def _wait_situation_kind(
    probe: WsProbe,
    *,
    app_session_id: str,
    expected_kind: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = await probe.request(
            "auip.session.get",
            {"app_session_id": app_session_id},
            timeout=20.0,
        )
        if _contains_situation_kind(latest.get("state"), expected_kind):
            return latest
        status = str(latest.get("status") or "").strip().lower()
        if status in {"closed", "disconnected"}:
            raise RuntimeError(
                "AUIP AppSession ended before publishing its declared situation: "
                f"status={status} expected={expected_kind}"
            )
        await asyncio.sleep(0.2)
    raise TimeoutError(
        f"active AUIP AppSession did not publish {expected_kind}: "
        f"{_safe_excerpt(latest.get('state'), 600)!r}"
    )


async def _wait_controller_status(
    probe: WsProbe,
    *,
    app_session_id: str,
    expected_status: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    clean_expected = str(expected_status or "").strip().lower()
    while time.monotonic() < deadline:
        latest = await probe.request(
            "auip.session.get",
            {"app_session_id": app_session_id},
            timeout=20.0,
        )
        host_controller = latest.get("controller")
        host_controller = host_controller if isinstance(host_controller, dict) else {}
        situation = _find_situation(latest.get("state"), "controller/v1") or {}
        if (
            str(host_controller.get("status") or "idle").strip().lower()
            == clean_expected
            and str(situation.get("status") or "idle").strip().lower()
            == clean_expected
        ):
            return latest
        await asyncio.sleep(0.1)
    raise TimeoutError(
        f"Controller did not reach {clean_expected}: "
        f"{_safe_excerpt(latest, 1000)!r}"
    )


def _snapshot_has_active_controller_lease(
    snapshot: dict[str, Any],
    lease: dict[str, Any],
) -> bool:
    """Verify a captured snapshot activated this exact Host lease."""

    controller = snapshot.get("controller")
    controller = controller if isinstance(controller, dict) else {}
    active_lease = controller.get("lease")
    active_lease = active_lease if isinstance(active_lease, dict) else {}
    situation = _find_situation(snapshot.get("state"), "controller/v1") or {}
    return bool(
        str(controller.get("status") or "").strip().lower() == "active"
        and str(situation.get("status") or "").strip().lower() == "active"
        and str(lease.get("lease_id") or "")
        and all(
            active_lease.get(key) == lease.get(key)
            for key in ("lease_id", "generation", "policy_revision")
        )
    )


async def _wait_controller_expiry(
    *,
    probe: WsProbe,
    app_session_id: str,
    state_expectations: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    """Wait for both Host expiry truth and app actuator neutralization."""

    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = await probe.request(
            "auip.session.get",
            {"app_session_id": app_session_id},
            timeout=20.0,
        )
        controller = latest.get("controller")
        controller = controller if isinstance(controller, dict) else {}
        situation = _find_situation(latest.get("state"), "controller/v1") or {}
        intent_cleared = all(
            _nested_state_fact_matches(latest.get("state"), key, value)
            for key, value in state_expectations.items()
        )
        if (
            str(controller.get("status") or "").strip().lower() == "idle"
            and str(controller.get("reason") or "").strip().lower() == "expired"
            and str(situation.get("status") or "").strip().lower() == "idle"
            and intent_cleared
        ):
            return latest
        await asyncio.sleep(0.1)
    raise TimeoutError(
        "Controller lease did not expire and clear app intent: "
        f"{_safe_excerpt(latest, 1200)!r}"
    )


def _controller_soak_checks(
    *,
    samples: list[dict[str, Any]],
    events: list[EventRecord],
    lease_id: str,
    oracle: dict[str, Any],
) -> tuple[dict[str, bool], dict[str, Any]]:
    """Evaluate a no-chat Controller soak from Host facts and checkpoints."""

    clean_lease = str(lease_id or "").strip()
    effect_events = [
        event
        for event in events
        if event.method == "auip.updated"
        and isinstance(event.params.get("event"), dict)
        and event.params["event"].get("controller_effect") is True
        and isinstance(event.params["event"].get("controller_lease"), dict)
        and str(event.params["event"]["controller_lease"].get("lease_id") or "")
        == clean_lease
    ]
    action_requests = [
        event for event in events if event.method == "auip.action.requested"
    ]
    local_user_events = [
        event
        for event in events
        if event.method == "auip.updated"
        and isinstance(event.params.get("event"), dict)
        and str(event.params["event"].get("actor") or "").strip().lower()
        == "user"
    ]
    controller_statuses = [
        str((sample.get("controller") or {}).get("status") or "idle")
        .strip()
        .lower()
        for sample in samples
    ]
    phase_path = str(oracle.get("phase_path") or "").strip()
    active_phase = str(oracle.get("active_phase") or "").strip().lower()
    successful_terminal_phases = {
        str(value or "").strip().lower()
        for value in oracle.get("successful_terminal_phases") or []
        if str(value or "").strip()
    }
    phases = (
        [
            str(_nested_state_value(sample.get("state"), phase_path) or "")
            .strip()
            .lower()
            for sample in samples
        ]
        if phase_path
        else []
    )

    def metric_values(metric_id: str) -> list[float]:
        values: list[float] = []
        for sample in samples:
            metric = _scalar_metric(sample.get("state"), metric_id)
            if not metric or "value" not in metric:
                continue
            try:
                values.append(float(metric["value"]))
            except (TypeError, ValueError):
                continue
        return values

    progress_id = str(oracle.get("progress_metric_id") or "").strip()
    progress_values = metric_values(progress_id) if progress_id else []
    progress_direction = str(oracle.get("progress_direction") or "").strip().lower()
    progress_observed = bool(progress_values)
    if len(progress_values) >= 2:
        if progress_direction == "decrease":
            progress_observed = progress_values[-1] < progress_values[0]
        elif progress_direction == "increase":
            progress_observed = progress_values[-1] > progress_values[0]
        else:
            progress_observed = progress_values[-1] != progress_values[0]

    health_id = str(oracle.get("health_metric_id") or "").strip()
    health_values = metric_values(health_id) if health_id else []
    health_floor = float(oracle.get("health_floor") or 0)
    min_effects = max(0, int(oracle.get("min_controller_effects") or 0))
    allowed_phases = {active_phase, *successful_terminal_phases}
    phase_sequence_valid = bool(phases) and all(
        phase in allowed_phases for phase in phases
    )
    terminal_seen = False
    for phase in phases:
        if phase in successful_terminal_phases:
            terminal_seen = True
        elif terminal_seen and phase == active_phase:
            phase_sequence_valid = False
            break
    checks = {
        "soak_sent_no_application_actions": not action_requests,
        "passive_soak_received_no_local_user_events": not local_user_events,
        "controller_lease_stayed_active": bool(controller_statuses)
        and all(status == "active" for status in controller_statuses),
        # A declared success phase is completion, not a failure to sustain the
        # application. Undeclared terminals (for example gameover) still fail,
        # and a transition back to active after success is rejected.
        "application_remained_in_active_phase": phase_sequence_valid,
        "controller_effects_continued": len(effect_events) >= min_effects,
        "projected_progress_advanced": progress_observed,
        "health_remained_above_floor": bool(health_values)
        and min(health_values) > health_floor,
    }
    effect_payloads = [
        dict(event.params["event"].get("payload") or {})
        for event in effect_events
    ]
    effect_payload_samples = (
        effect_payloads
        if len(effect_payloads) <= 10
        else effect_payloads[:5] + effect_payloads[-5:]
    )
    summary = {
        "sample_count": len(samples),
        "controller_statuses": controller_statuses,
        "phases": phases,
        "successful_terminal_phases": sorted(successful_terminal_phases),
        "controller_effect_count": len(effect_events),
        "controller_effect_payload_samples": effect_payload_samples,
        "unexpected_action_request_count": len(action_requests),
        "local_user_event_count": len(local_user_events),
        "local_user_event_types": [
            str(event.params["event"].get("type") or "")
            for event in local_user_events
        ],
        "progress_metric_id": progress_id,
        "progress_values": progress_values,
        "health_metric_id": health_id,
        "health_values": health_values,
    }
    return checks, summary


async def _exercise_controller_soak(
    *,
    probe: WsProbe,
    app_page: Any,
    app_session_id: str,
    duration_s: float,
    interval_s: float,
    oracle: dict[str, Any],
    screenshot_root: Path,
) -> tuple[TurnEvidence, dict[str, Any], dict[str, Any]]:
    """Observe sustained app-local control without issuing another chat turn."""

    evidence = TurnEvidence(
        label="controller_soak",
        text=f"Host passive soak: {duration_s:.1f}s without a chat instruction.",
        event_start=len(probe.state.events),
    )
    initial = await probe.request(
        "auip.session.get",
        {"app_session_id": app_session_id},
        timeout=20.0,
    )
    controller = initial.get("controller")
    controller = controller if isinstance(controller, dict) else {}
    lease = controller.get("lease")
    lease = lease if isinstance(lease, dict) else {}
    lease_id = str(lease.get("lease_id") or "")
    samples: list[dict[str, Any]] = []
    started = time.monotonic()
    clean_duration = max(0.1, float(duration_s))
    deadline = started + clean_duration
    next_sample = started
    latest = initial
    midpoint_captured = False
    while True:
        now = time.monotonic()
        if now >= next_sample or not samples:
            latest = await probe.request(
                "auip.session.get",
                {"app_session_id": app_session_id},
                timeout=20.0,
            )
            samples.append(
                {
                    "elapsed_s": round(now - started, 3),
                    "revision": int(latest.get("revision") or 0),
                    "controller": dict(latest.get("controller") or {}),
                    "state": dict(latest.get("state") or {}),
                }
            )
            next_sample = now + max(0.2, float(interval_s))
        if not midpoint_captured and now - started >= clean_duration / 2:
            midpoint = screenshot_root / "app-controller-soak-midpoint.png"
            await app_page.screenshot(path=str(midpoint), full_page=True)
            evidence.screenshot = str(midpoint)
            midpoint_captured = True
        if now >= deadline:
            break
        await asyncio.sleep(min(0.2, max(0.01, deadline - now)))
    ended = time.monotonic()
    if not samples or float(samples[-1].get("elapsed_s") or 0.0) < clean_duration - 0.1:
        latest = await probe.request(
            "auip.session.get",
            {"app_session_id": app_session_id},
            timeout=20.0,
        )
        samples.append(
            {
                "elapsed_s": round(ended - started, 3),
                "revision": int(latest.get("revision") or 0),
                "controller": dict(latest.get("controller") or {}),
                "state": dict(latest.get("state") or {}),
            }
        )
    checks, summary = _controller_soak_checks(
        samples=samples,
        events=list(probe.state.events[evidence.event_start :]),
        lease_id=lease_id,
        oracle=oracle,
    )
    checks["requested_soak_duration_elapsed"] = (
        ended - started >= clean_duration * 0.98
    )
    evidence.checks.update(checks)
    summary.update(
        {
            "requested_duration_s": clean_duration,
            "observed_duration_s": round(ended - started, 3),
            "lease_id": lease_id,
        }
    )
    evidence.notes.append(json.dumps(summary, ensure_ascii=False))
    evidence.event_end = len(probe.state.events)
    final_shot = screenshot_root / "app-controller-soak-final.png"
    await app_page.screenshot(path=str(final_shot), full_page=True)
    evidence.screenshot = str(final_shot)
    return evidence, latest, summary


def _matching_controller_effect(
    events: list[EventRecord],
    *,
    after: int,
    lease_id: str,
) -> dict[str, Any] | None:
    clean_lease = str(lease_id or "").strip()
    return next(
        (
            dict(event.params["event"])
            for event in events[max(0, int(after)):]
            if event.method == "auip.updated"
            and isinstance(event.params.get("event"), dict)
            and event.params["event"].get("controller_effect") is True
            and (
                not clean_lease
                or str(
                    (event.params["event"].get("controller_lease") or {}).get(
                        "lease_id"
                    )
                    or ""
                )
                == clean_lease
            )
        ),
        None,
    )


def _operator_failure_from_update(event: EventRecord) -> dict[str, str] | None:
    """Return one terminal AUIP operator failure carried by a projection update."""

    if event.method != "auip.updated":
        return None
    params = event.params if isinstance(event.params, dict) else {}
    if str(params.get("operator_status") or "").strip().lower() != "error":
        return None
    outcome = params.get("operator_outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    return {
        "code": str(params.get("operator_error") or "operator_failed").strip(),
        "detail": str(params.get("operator_error_detail") or "").strip(),
        "reason": str(outcome.get("reason") or "").strip(),
    }


def _controller_frame_evidence(
    before_text: str,
    after_text: str,
    *,
    effect_already_observed: bool,
) -> bool:
    before_match = re.search(r"\d+", str(before_text or ""))
    after_match = re.search(r"\d+", str(after_text or ""))
    if not before_match or not after_match:
        return False
    before_frame = int(before_match.group())
    after_frame = int(after_match.group())
    if after_frame > before_frame:
        return True
    return bool(
        effect_already_observed
        and after_frame >= before_frame
        and after_frame > 0
    )


async def _exercise_controller_urgent_response(
    *,
    probe: WsProbe,
    app_page: Any,
    app_session_id: str,
    scenario: dict[str, Any],
    timeout: float,
    controller_event_after: int | None = None,
) -> tuple[TurnEvidence, dict[str, Any]]:
    oracle = scenario.get("controller_oracle")
    if not isinstance(oracle, dict):
        raise ValueError("Controller oracle is not declared")
    evidence = TurnEvidence(
        label="controller_urgent_event",
        text="Host test trigger: one urgent application event under the active Controller lease.",
        event_start=len(probe.state.events),
    )
    before = await probe.request(
        "auip.session.get",
        {"app_session_id": app_session_id},
        timeout=20.0,
    )
    before_revision = int(before.get("revision") or 0)
    before_lease = ((before.get("controller") or {}).get("lease") or {})
    trigger = app_page.locator(
        f'[data-testid="{str(oracle["trigger_test_id"])}"]'
    )
    counter = app_page.locator(
        f'[data-testid="{str(oracle["response_count_test_id"])}"]'
    )
    visual_command = None
    visual_frame = None
    motion_target = None
    if oracle.get("visual_command_test_id"):
        visual_command = app_page.locator(
            f'[data-testid="{str(oracle["visual_command_test_id"])}"]'
        )
    if oracle.get("visual_frame_test_id"):
        visual_frame = app_page.locator(
            f'[data-testid="{str(oracle["visual_frame_test_id"])}"]'
        )
    if oracle.get("motion_test_id"):
        motion_target = app_page.locator(
            f'[data-testid="{str(oracle["motion_test_id"])}"]'
        )
    evidence.checks["trigger_visible"] = await trigger.count() == 1
    evidence.checks["response_count_visible"] = await counter.count() == 1
    if visual_command is not None:
        evidence.checks["visual_command_visible"] = (
            await visual_command.count() == 1
        )
    if visual_frame is not None:
        evidence.checks["visual_frame_visible"] = await visual_frame.count() == 1
    if motion_target is not None:
        evidence.checks["motion_target_visible"] = await motion_target.count() == 1
    if not all(evidence.checks.values()):
        raise RuntimeError("Controller reference app omitted its declared demo controls")
    before_text = " ".join((await counter.inner_text()).split())
    baseline = int(oracle.get("response_count_baseline") or 0)
    before_match = re.search(r"\d+", before_text)
    before_count = int(before_match.group()) if before_match else baseline
    before_visual_frame = (
        " ".join((await visual_frame.inner_text()).split())
        if visual_frame is not None and await visual_frame.count() == 1
        else ""
    )
    random_value = oracle.get("trigger_random_value")
    if isinstance(random_value, (int, float)):
        await app_page.evaluate(
            """value => {
              window.__amadeusJourneyOriginalRandom = Math.random;
              Math.random = () => value;
            }""",
            float(random_value),
        )
    try:
        await trigger.click()
    finally:
        if isinstance(random_value, (int, float)):
            await app_page.evaluate(
                """() => {
                  if (window.__amadeusJourneyOriginalRandom) {
                    Math.random = window.__amadeusJourneyOriginalRandom;
                    delete window.__amadeusJourneyOriginalRandom;
                  }
                }"""
            )

    motion_positions: list[float] = []
    if motion_target is not None:
        for _index in range(6):
            position = await motion_target.evaluate(
                "element => element.getBoundingClientRect().left"
            )
            motion_positions.append(float(position))
            await asyncio.sleep(0.07)
        evidence.checks["smooth_controller_motion"] = bool(
            len({round(value, 2) for value in motion_positions}) >= 4
            and max(motion_positions) - min(motion_positions) >= 2.0
        )
        evidence.notes.append(
            "visible motion samples: "
            + ",".join(f"{value:.2f}" for value in motion_positions)
        )

    latest = before
    after_text = before_text
    controller_event_start = (
        max(0, int(controller_event_after))
        if controller_event_after is not None
        else evidence.event_start
    )

    effect_already_observed = bool(
        before_count > baseline
        and _matching_controller_effect(
            probe.state.events,
            after=controller_event_start,
            lease_id=str(before_lease.get("lease_id") or ""),
        )
        is not None
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if effect_already_observed:
            break
        after_text = " ".join((await counter.inner_text()).split())
        latest = await probe.request(
            "auip.session.get",
            {"app_session_id": app_session_id},
            timeout=20.0,
        )
        if (
            int(latest.get("revision") or 0) > before_revision
            and (after_text != before_text or before_count > baseline)
        ):
            break
        await asyncio.sleep(0.1)
    after_match = re.search(r"\d+", after_text)
    after_count = int(after_match.group()) if after_match else baseline
    controller_reacted = max(before_count, after_count) > baseline
    evidence.checks["local_controller_reacted"] = controller_reacted
    evidence.checks["semantic_checkpoint_advanced"] = bool(
        int(latest.get("revision") or 0) > before_revision
        or (
            controller_reacted
            and _matching_controller_effect(
                probe.state.events,
                after=controller_event_start,
                lease_id=str(before_lease.get("lease_id") or ""),
            )
            is not None
        )
    )
    after_lease = ((latest.get("controller") or {}).get("lease") or {})
    evidence.checks["same_host_lease_remained_active"] = bool(
        before_lease
        and after_lease
        and before_lease.get("lease_id") == after_lease.get("lease_id")
        and str((latest.get("controller") or {}).get("status") or "") == "active"
    )
    evidence.checks["response_count_increased"] = controller_reacted
    if visual_command is not None and visual_frame is not None:
        command_text = " ".join((await visual_command.inner_text()).split())
        frame_text = " ".join((await visual_frame.inner_text()).split())
        expected_commands = {
            str(value or "").strip()
            for value in (
                oracle.get("expected_visual_command_options")
                if isinstance(oracle.get("expected_visual_command_options"), list)
                else [oracle.get("expected_visual_command")]
            )
            if str(value or "").strip()
        }
        evidence.checks["visible_controller_command_matches"] = bool(
            expected_commands and command_text in expected_commands
        )
        evidence.checks["visible_controller_frame_advanced"] = (
            _controller_frame_evidence(
                before_visual_frame,
                frame_text,
                effect_already_observed=effect_already_observed,
            )
        )
        evidence.notes.append(
            "visible Controller activity: "
            f"command={command_text!r}, frame={before_visual_frame!r}->{frame_text!r}"
        )
    evidence.notes.append(f"response count: {before_text!r} -> {after_text!r}")
    if isinstance(random_value, (int, float)):
        evidence.notes.append(
            f"deterministic trigger random value: {float(random_value)}"
        )
    evidence.notes.append(
        "state after urgent event: "
        + json.dumps(_safe_excerpt(latest.get("state"), 1200), ensure_ascii=False)
    )
    if oracle.get("expect_narration") is True:
        expected_event_type = str(oracle.get("narration_event_type") or "").strip()
        if not expected_event_type:
            raise ValueError("Controller narration oracle omitted its event type")
        source_event: dict[str, Any] | None = None
        narration_deadline = time.monotonic() + timeout
        while time.monotonic() < narration_deadline:
            source_event = next(
                (
                    dict(event.params["event"])
                    for event in probe.state.events[controller_event_start:]
                    if event.method == "auip.updated"
                    and str(event.params.get("app_session_id") or "")
                    == app_session_id
                    and isinstance(event.params.get("event"), dict)
                    and str(event.params["event"].get("type") or "")
                    == expected_event_type
                ),
                None,
            )
            if source_event is not None:
                break
            await asyncio.sleep(0.1)
        source_event_id = str((source_event or {}).get("event_id") or "").strip()
        evidence.checks["meaningful_controller_event_published"] = bool(
            source_event_id
        )
        expected_payload_keys = {
            str(value or "").strip()
            for value in oracle.get("narration_payload_keys") or []
            if str(value or "").strip()
        }
        if expected_payload_keys:
            source_payload = (
                source_event.get("payload")
                if isinstance((source_event or {}).get("payload"), dict)
                else {}
            )
            evidence.checks["controller_narration_payload_is_semantic"] = (
                set(source_payload) == expected_payload_keys
            )
        delivered: dict[str, Any] = {}
        while source_event_id and time.monotonic() < narration_deadline:
            latest = await probe.request(
                "auip.session.get",
                {"app_session_id": app_session_id},
                timeout=20.0,
            )
            candidate = latest.get("latest_delivered_narration")
            candidate = candidate if isinstance(candidate, dict) else {}
            if (
                str(candidate.get("event_id") or "").strip() == source_event_id
                and str(candidate.get("text") or "").strip()
            ):
                delivered = dict(candidate)
                break
            await asyncio.sleep(0.1)
        evidence.checks["controller_effect_narrated"] = bool(delivered)
        if delivered:
            evidence.reply = str(delivered.get("text") or "").strip()
            evidence.notes.append(
                "narration for controller event "
                f"{source_event_id!r}: {evidence.reply!r}"
            )
    evidence.event_end = len(probe.state.events)
    if not all(evidence.checks.values()):
        failed = sorted(name for name, passed in evidence.checks.items() if not passed)
        raise RuntimeError(
            "Controller urgent-response oracle failed: " + ", ".join(failed)
        )
    return evidence, latest


def _scalar_metric(state: Any, metric_id: str) -> dict[str, Any] | None:
    situation = _find_situation(state, "scalars/v1") or {}
    return next(
        (
            item
            for item in situation.get("metrics") or []
            if isinstance(item, dict)
            and str(item.get("id") or "") == str(metric_id or "")
        ),
        None,
    )


def _state_fact_matches(state: dict[str, Any], key: str, expected: Any) -> bool:
    """Match one setup fact without imposing an application field spelling.

    Real-time apps commonly publish run/pause as one closed phase rather than
    duplicating a boolean. Both shapes preserve the same visible fact; the
    Journey should test that semantic boundary, not reward one adapter schema.
    Other facts remain exact.
    """

    if key in state:
        return state.get(key) == expected
    if key == "paused" and isinstance(expected, bool):
        candidates: list[Any] = [
            state.get("phase"),
            state.get("status"),
            state.get("runStatus"),
        ]
        for value in state.values():
            if not isinstance(value, dict) or value.get("kind"):
                continue
            candidates.extend((value.get("phase"), value.get("status")))
        phase_meanings = {
            "paused": True,
            "pause": True,
            "running": False,
            "run": False,
            "playing": False,
            "active": False,
        }
        meanings: set[bool] = set()
        for value in candidates:
            normalized = str(value or "").strip().lower()
            if normalized in phase_meanings:
                meanings.add(phase_meanings[normalized])
        if len(meanings) == 1:
            return expected in meanings
    return False


def _nested_state_fact_matches(state: Any, path: str, expected: Any) -> bool:
    current = state
    for segment in str(path or "").split("."):
        if not segment or not isinstance(current, dict) or segment not in current:
            return False
        current = current[segment]
    return current == expected


async def _exercise_pre_step_setup(
    *,
    probe: WsProbe,
    app_page: Any,
    app_session_id: str,
    setup: dict[str, Any],
    timeout: float,
    label: str = "scene_setup",
) -> tuple[TurnEvidence, dict[str, Any]]:
    evidence = TurnEvidence(
        label=label,
        text="Host test setup: move the application to the declared comparison scene.",
        event_start=len(probe.state.events),
    )
    before = await probe.request(
        "auip.session.get",
        {"app_session_id": app_session_id},
        timeout=20.0,
    )
    before_revision = int(before.get("revision") or 0)
    test_id = str(setup.get("click_test_id") or "").strip()
    selector = str(setup.get("click_selector") or "").strip()
    press_key = str(
        setup.get("key_after_click") or setup.get("press_key") or ""
    ).strip()
    setup_actions = setup.get("actions")
    setup_actions = setup_actions if isinstance(setup_actions, list) else []
    choice_sequence = setup.get("choice_sequence")
    choice_sequence = choice_sequence if isinstance(choice_sequence, list) else []
    local_sequence = setup.get("local_sequence")
    local_sequence = local_sequence if isinstance(local_sequence, list) else []
    if sum(bool(value) for value in (setup_actions, choice_sequence, local_sequence)) > 1:
        raise RuntimeError(
            "comparison scene setup cannot mix exact actions, choice sequence, "
            "and local sequence"
        )
    if local_sequence and (test_id or selector or press_key):
        raise RuntimeError(
            "comparison scene setup cannot mix local sequence and one-shot UI input"
        )
    setup_quiet_ms = int(setup.get("settle_quiet_ms") or 250)
    field_expectations = setup.get("field_expectations")
    field_expectations = (
        field_expectations if isinstance(field_expectations, dict) else {}
    )
    metric_expectations = setup.get("metric_expectations")
    metric_expectations = (
        metric_expectations if isinstance(metric_expectations, dict) else {}
    )
    state_expectations = setup.get("state_expectations")
    state_expectations = (
        state_expectations if isinstance(state_expectations, dict) else {}
    )
    has_transition = bool(
        setup_actions
        or choice_sequence
        or local_sequence
        or test_id
        or selector
        or press_key
    )
    has_expectations = bool(
        field_expectations or metric_expectations or state_expectations
    )

    async def wait_for_setup_publications() -> dict[str, Any]:
        quiet_seconds = max(0.001, setup_quiet_ms / 1000)
        deadline = time.monotonic() + timeout
        last_revision: int | None = None
        stable_since = time.monotonic()
        latest_session: dict[str, Any] = {}
        while time.monotonic() < deadline:
            latest_session = await probe.request(
                "auip.session.get",
                {"app_session_id": app_session_id},
                timeout=20.0,
            )
            revision = int(latest_session.get("revision") or 0)
            pending = latest_session.get("pending_action")
            if pending is not None or revision != last_revision:
                last_revision = revision
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= quiet_seconds:
                return latest_session
            await asyncio.sleep(min(0.05, quiet_seconds))
        raise RuntimeError("comparison scene setup publications did not settle")

    action_sequence = setup_actions or choice_sequence
    resolve_from_choice = bool(choice_sequence)
    for index, action_spec in enumerate(action_sequence, start=1):
        if not isinstance(action_spec, dict):
            raise RuntimeError("comparison scene setup action must be an object")
        current = await probe.request(
            "auip.session.get",
            {"app_session_id": app_session_id},
            timeout=20.0,
        )
        if resolve_from_choice:
            candidates = _available_choice_actions(current.get("state"))
            if len(candidates) != 1:
                raise RuntimeError(
                    "comparison scene setup expected one available choice action, "
                    f"observed {len(candidates)}"
                )
            action_type = str(candidates[0]["type"])
            action_payload = dict(candidates[0]["payload"])
            evidence.notes.append(
                "setup choice resolved: "
                + json.dumps(candidates[0], ensure_ascii=False)
            )
        else:
            action_type = str(action_spec.get("type") or "").strip()
            if not action_type:
                raise RuntimeError("comparison scene setup action type is required")
            action_payload = action_spec.get("payload")
            action_payload = (
                action_payload if isinstance(action_payload, dict) else {}
            )
        action_revision = int(current.get("revision") or 0)
        receipt_after = len(probe.state.events)
        invoked = await probe.request(
            "auip.action.invoke",
            {
                "app_session_id": app_session_id,
                "actor": "user",
                "action_type": action_type,
                "payload": action_payload,
                "expected_revision": action_revision,
            },
            timeout=20.0,
        )
        if invoked.get("ok") is not True:
            raise RuntimeError(
                "comparison scene setup action could not be invoked: "
                f"{action_type}: {invoked.get('error') or invoked.get('detail') or 'unknown'}"
            )
        action = invoked.get("action")
        action = action if isinstance(action, dict) else {}
        action_id = str(action.get("action_id") or "").strip()
        receipt_event = await probe.wait_event(
            lambda event: event.method == "auip.updated"
            and isinstance(event.params.get("receipt"), dict)
            and (
                not action_id
                or str(event.params["receipt"].get("action_id") or "")
                == action_id
            ),
            after=receipt_after,
            timeout=timeout,
            description=f"{label} action {action_type} receipt",
        )
        receipt = dict(receipt_event.params.get("receipt") or {})
        accepted = receipt.get("accepted") is True
        evidence.checks[f"setup_action_{index}_accepted"] = accepted
        evidence.notes.append(
            "setup action receipt: "
            + json.dumps(_safe_excerpt(receipt, 800), ensure_ascii=False)
        )
        if not accepted:
            raise RuntimeError(
                "comparison scene setup action was rejected: "
                f"{action_type}: {receipt.get('reason') or 'unknown'}"
            )
        action_state = await probe.request(
            "auip.session.get",
            {"app_session_id": app_session_id},
            timeout=20.0,
        )
        action_expectations = action_spec.get("state_expectations")
        action_expectations = (
            action_expectations if isinstance(action_expectations, dict) else {}
        )
        if action_expectations:
            current_state = action_state.get("state")
            current_state = current_state if isinstance(current_state, dict) else {}
            action_state_matches = all(
                _state_fact_matches(current_state, key, value)
                for key, value in action_expectations.items()
            )
            evidence.checks[f"setup_action_{index}_state_matches"] = (
                action_state_matches
            )
            if not action_state_matches:
                raise RuntimeError(
                    "comparison scene setup action state did not match: "
                    f"{action_type}"
                )
        settled_state = await wait_for_setup_publications()
        evidence.checks[f"setup_action_{index}_settled"] = True
        evidence.notes.append(
            f"setup action settled at revision {int(settled_state.get('revision') or 0)}"
        )
    captured_situations: dict[str, Any] = {}
    for index, local_spec in enumerate(local_sequence, start=1):
        if not isinstance(local_spec, dict):
            raise RuntimeError("comparison scene local step must be an object")
        local_selector = str(local_spec.get("click_selector") or "").strip()
        local_key = str(local_spec.get("press_key") or "").strip()
        if bool(local_selector) == bool(local_key):
            raise RuntimeError(
                "comparison scene local step needs exactly one click_selector or press_key"
            )
        current = await probe.request(
            "auip.session.get",
            {"app_session_id": app_session_id},
            timeout=20.0,
        )
        local_before_revision = int(current.get("revision") or 0)
        if local_selector:
            control = app_page.locator(local_selector)
            visible = await control.count() == 1
            evidence.checks[f"setup_local_{index}_control_visible"] = visible
            if not visible:
                raise RuntimeError(
                    f"comparison scene local control is unavailable: {local_selector}"
                )
            await control.click()
        else:
            await app_page.keyboard.press(local_key)
            evidence.checks[f"setup_local_{index}_key_sent"] = True

        local_expectations = local_spec.get("state_expectations")
        local_expectations = (
            local_expectations if isinstance(local_expectations, dict) else {}
        )
        situation_kind = str(local_spec.get("situation_kind") or "").strip()
        capture_situation_as = str(
            local_spec.get("capture_situation_as") or ""
        ).strip()
        situation_changed_from = str(
            local_spec.get("situation_changed_from") or ""
        ).strip()
        situation_matches = str(
            local_spec.get("situation_matches") or ""
        ).strip()
        if any(
            (capture_situation_as, situation_changed_from, situation_matches)
        ) and not situation_kind:
            raise RuntimeError(
                "comparison scene situation relation requires situation_kind"
            )
        if situation_changed_from and situation_matches:
            raise RuntimeError(
                "comparison scene situation cannot both change from and match a capture"
            )
        for capture_name in (situation_changed_from, situation_matches):
            if capture_name and capture_name not in captured_situations:
                raise RuntimeError(
                    f"comparison scene situation capture is unavailable: {capture_name}"
                )
        deadline = time.monotonic() + timeout
        latest_local: dict[str, Any] = current
        latest_situation: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            latest_local = await probe.request(
                "auip.session.get",
                {"app_session_id": app_session_id},
                timeout=20.0,
            )
            latest_state = latest_local.get("state")
            latest_state = latest_state if isinstance(latest_state, dict) else {}
            latest_situation = (
                _find_situation(latest_state, situation_kind)
                if situation_kind
                else None
            )
            situation_relation_matches = bool(
                not situation_kind
                or latest_situation is not None
                and (
                    not situation_changed_from
                    or latest_situation
                    != captured_situations[situation_changed_from]
                )
                and (
                    not situation_matches
                    or latest_situation == captured_situations[situation_matches]
                )
            )
            if (
                int(latest_local.get("revision") or 0) > local_before_revision
                and all(
                    _state_fact_matches(latest_state, key, value)
                    for key, value in local_expectations.items()
                )
                and situation_relation_matches
            ):
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError(
                f"comparison scene local step did not publish expected state: {index}"
            )
        evidence.checks[f"setup_local_{index}_revision_advanced"] = True
        if local_expectations:
            evidence.checks[f"setup_local_{index}_state_matches"] = True
        if situation_kind and latest_situation is None:
            raise RuntimeError(
                f"comparison scene local step did not publish {situation_kind}: {index}"
            )
        if capture_situation_as:
            captured_situations[capture_situation_as] = json.loads(
                json.dumps(latest_situation, ensure_ascii=False)
            )
            evidence.checks[f"setup_local_{index}_situation_captured"] = True
        if situation_changed_from:
            evidence.checks[f"setup_local_{index}_situation_changed"] = True
        if situation_matches:
            evidence.checks[f"setup_local_{index}_situation_restored"] = True
        settled_state = await wait_for_setup_publications()
        evidence.checks[f"setup_local_{index}_settled"] = True
        evidence.notes.append(
            "setup local step settled: "
            + json.dumps(
                {
                    "index": index,
                    "selector": local_selector,
                    "key": local_key,
                    "revision": int(settled_state.get("revision") or 0),
                },
                ensure_ascii=False,
            )
        )
    if test_id or selector:
        control = app_page.locator(
            selector if selector else f'[data-testid="{test_id}"]'
        )
        evidence.checks["setup_control_visible"] = await control.count() == 1
        if evidence.checks["setup_control_visible"] is not True:
            raise RuntimeError("comparison scene setup control is unavailable")
        await control.click()
        wait_after_click_ms = int(setup.get("wait_after_click_ms") or 0)
        if not 0 <= wait_after_click_ms <= 5000:
            raise RuntimeError("comparison scene wait_after_click_ms is out of range")
        if wait_after_click_ms:
            await asyncio.sleep(wait_after_click_ms / 1000)
    if press_key:
        await app_page.keyboard.press(press_key)
        evidence.checks["setup_key_sent"] = True
    if not has_transition and not has_expectations:
        raise RuntimeError("comparison scene setup declares no UI transition")

    def _declared_setup_state_matches(state: Any) -> bool:
        current_state = state if isinstance(state, dict) else {}
        field = current_state.get("field")
        field = field if isinstance(field, dict) else {}
        return bool(
            all(field.get(key) == value for key, value in field_expectations.items())
            and all(
                (_scalar_metric(current_state, key) or {}).get("value") == value
                for key, value in metric_expectations.items()
            )
            and all(
                _state_fact_matches(current_state, key, value)
                for key, value in state_expectations.items()
            )
        )

    latest = before
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        revision = int(latest.get("revision") or 0)
        revision_ready = revision > before_revision if has_transition else revision >= before_revision
        if revision_ready and _declared_setup_state_matches(latest.get("state")):
            break
        latest = await probe.request(
            "auip.session.get",
            {"app_session_id": app_session_id},
            timeout=20.0,
        )
        await asyncio.sleep(0.1)
    if has_transition:
        evidence.checks["setup_revision_advanced"] = (
            int(latest.get("revision") or 0) > before_revision
        )
    else:
        evidence.checks["setup_initial_state_observed"] = (
            int(latest.get("revision") or 0) >= before_revision
        )
    state = latest.get("state")
    field = state.get("field") if isinstance(state, dict) else None
    field = field if isinstance(field, dict) else {}
    if field_expectations:
        evidence.checks["setup_field_matches"] = all(
            field.get(key) == value for key, value in field_expectations.items()
        )
    if metric_expectations:
        evidence.checks["setup_metrics_match"] = all(
            (_scalar_metric(state, key) or {}).get("value") == value
            for key, value in metric_expectations.items()
        )
    if state_expectations:
        current_state = state if isinstance(state, dict) else {}
        evidence.checks["setup_state_matches"] = all(
            _state_fact_matches(current_state, key, value)
            for key, value in state_expectations.items()
        )
    evidence.notes.append(
        "comparison state: "
        + json.dumps(_safe_excerpt(state, 1000), ensure_ascii=False)
    )
    evidence.event_end = len(probe.state.events)
    if not all(evidence.checks.values()):
        failed = sorted(name for name, passed in evidence.checks.items() if not passed)
        raise RuntimeError("comparison scene setup failed: " + ", ".join(failed))
    return evidence, latest


def _query_metrics_grounded(
    *,
    scenario: dict[str, Any],
    state: Any,
    reply: str,
) -> bool | None:
    oracle = scenario.get("query_oracle")
    if not isinstance(oracle, dict):
        # Structural transport evidence cannot prove free-text semantic
        # grounding without a scenario-owned oracle. Leave the result for the
        # report's explicit human/AI review instead of manufacturing a pass.
        return None
    current_state = state if isinstance(state, dict) else {}
    metric_ids = [
        str(value or "").strip()
        for value in oracle.get("metric_ids") or []
        if str(value or "").strip()
    ]
    field_ids = [
        str(value or "").strip()
        for value in oracle.get("field_ids") or []
        if str(value or "").strip()
    ]
    state_field_ids = [
        str(value or "").strip()
        for value in oracle.get("state_field_ids") or []
        if str(value or "").strip()
    ]
    terminal_state_field_ids = [
        str(value or "").strip()
        for value in oracle.get("terminal_state_field_ids") or []
        if str(value or "").strip()
    ]
    state_path_any = [
        str(value or "").strip()
        for value in oracle.get("state_path_any") or []
        if str(value or "").strip()
    ]
    state_paths = [
        str(value or "").strip()
        for value in oracle.get("state_paths") or []
        if str(value or "").strip()
    ]
    terminal_state_path_any = [
        str(value or "").strip()
        for value in oracle.get("terminal_state_path_any") or []
        if str(value or "").strip()
    ]
    terminal_state_values = {
        str(value or "").strip().lower()
        for value in oracle.get("terminal_state_values") or []
        if str(value or "").strip()
    }
    terminal_selected = next(
        (
            _nested_state_value(current_state, path)
            for path in terminal_state_path_any
            if _nested_state_value(current_state, path) is not None
        ),
        None,
    )
    legacy_lifecycle = str(current_state.get("lifecycle") or "").strip().lower()
    terminal_mode = bool(
        (
            terminal_selected is not None
            and str(terminal_selected).strip().lower() in terminal_state_values
        )
        or legacy_lifecycle
        in {"round_finished", "finished", "completed", "concluded"}
    )
    if terminal_mode:
        metric_ids = [
            str(value or "").strip()
            for value in oracle.get("terminal_metric_ids") or []
            if str(value or "").strip()
        ]
        field_ids = [
            str(value or "").strip()
            for value in oracle.get("terminal_field_ids") or []
            if str(value or "").strip()
        ]
        state_field_ids = terminal_state_field_ids
        if terminal_state_path_any:
            state_path_any = terminal_state_path_any
    if (
        not metric_ids
        and not field_ids
        and not state_field_ids
        and not terminal_state_field_ids
        and not state_path_any
        and not state_paths
    ):
        return False
    values: list[str] = []
    for metric_id in metric_ids:
        metric = _scalar_metric(state, metric_id)
        if not metric or "value" not in metric:
            return False
        values.append(str(metric["value"]))
    field = state.get("field") if isinstance(state, dict) else None
    field = field if isinstance(field, dict) else {}
    for field_id in field_ids:
        if field_id not in field:
            return False
        values.append(str(field[field_id]))
    for field_id in state_field_ids:
        if field_id not in current_state:
            return False
        values.append(str(current_state[field_id]))
    if state_path_any:
        selected = None
        for path in state_path_any:
            selected = _nested_state_value(current_state, path)
            if selected is not None:
                break
        if selected is None:
            return False
        values.append(str(selected))
    for path in state_paths:
        selected = _nested_state_value(current_state, path)
        if selected is None:
            return False
        values.append(str(selected))
    return all(_query_value_visible(value, reply) for value in values)


def _query_grounded_across_states(
    *,
    scenario: dict[str, Any],
    states: list[Any],
    reply: str,
) -> bool | None:
    """Judge an ambient reply against facts authoritative during its turn."""

    results = [
        _query_metrics_grounded(
            scenario=scenario,
            state=state,
            reply=reply,
        )
        for state in states
        if isinstance(state, dict)
    ]
    if any(result is True for result in results):
        return True
    if any(result is False for result in results):
        return False
    return None


def _sequence_query_grounded(state: Any, reply: str) -> bool:
    """Require the next step, or a truthful completed-sequence readback."""

    sequence = _find_situation(state, "sequence/v1")
    if not isinstance(sequence, dict):
        return False
    steps = [item for item in sequence.get("steps") or [] if isinstance(item, dict)]
    next_step_id = str(sequence.get("nextStepId") or "").strip()
    if next_step_id:
        next_label = next(
            (
                str(item.get("label") or "").strip()
                for item in steps
                if str(item.get("id") or "").strip() == next_step_id
            ),
            "",
        )
        return bool(next_label and next_label in str(reply or ""))
    completed_count = int(sequence.get("completedCount") or 0)
    if not steps or completed_count < len(steps):
        return False
    rendered = str(reply or "")
    return bool(
        str(completed_count) in rendered
        and _query_value_visible("completed", rendered)
    )


def _nested_state_value(state: Any, path: str) -> Any:
    current = state
    for segment in str(path or "").split("."):
        if not segment or not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def _query_value_visible(value: str, reply: str) -> bool:
    """Accept a natural qualitative paraphrase, not only an internal enum."""

    expected = str(value or "").strip().casefold()
    if expected == "concluded":
        expected = "round_finished"
    rendered = str(reply or "").casefold()
    if expected and expected in rendered:
        return True
    aliases = {
        "none": (
            "no bullets",
            "no projectiles",
            "nothing incoming",
            "zero",
            "没有子弹",
            "没有弹幕",
            "零",
            "ゼロ",
            "弾は飛んでいない",
            "弾は飛んでない",
            "弾幕はない",
            "何も飛んでいない",
            "空っぽ",
        ),
        "few": ("a few", "low", "少量", "很少", "少ない", "わずか"),
        "many": ("many", "a lot", "high", "很多", "大量", "多い"),
        "dense": ("dense", "heavy", "密集", "密度", "激しい", "濃い"),
        "light": ("light", "sparse", "稀疏", "薄い", "まばら"),
        "moderate": ("moderate", "medium", "中等", "中程度"),
        "critical": ("critical", "danger", "危险", "危険", "危機"),
        "stable": ("stable", "safe", "稳定", "安定", "大丈夫", "問題ない"),
        "black": ("black", "黑", "黒"),
        "white": ("white", "白"),
        "red": ("red", "红", "赤"),
        "green": ("green", "绿", "緑"),
        "blue": ("blue", "蓝", "青"),
        "upgrade": ("upgrade", "强化", "強化", "アップグレード"),
        "running": ("running", "in progress", "运行", "進行", "実行中"),
        "paused": ("paused", "暂停", "一時停止"),
        "start": ("start", "title", "开始", "标题", "開始", "タイトル"),
        "gameover": ("game over", "结束", "死亡", "ゲームオーバー"),
        "round_finished": (
            "round finished",
            "round ended",
            "回合结束",
            "对局结束",
            "対局終了",
            "ラウンドはもう終わ",
            "ラウンド終了",
            "対局は終わ",
            "対局が終わ",
            "終了して",
            "勝負はついた",
            "決着",
            "勝ちが確定",
        ),
        "completed": (
            "complete",
            "completed",
            "all done",
            "finished",
            "完成",
            "完了",
            "全部完成",
            "全部完成了",
            "全部完了",
            "すべて完了",
            "全て完了",
        ),
    }
    return any(alias in rendered for alias in aliases.get(expected, ()))


async def _wait_app_page(product: ElectronProduct, *, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pages = await product.app_pages()
        for page in reversed(pages):
            try:
                page_url = str(page.url or "")
                # AUIP launch policy admits one verified local file entry.
                # Work Preview shells use Amadeus's renderer URL and ordinary
                # live previews use loopback HTTP; neither is the application
                # authority surface that should receive journey input.
                if not page_url.lower().startswith("file:"):
                    continue
                if "electron/dist/renderer/index.html" in page_url.replace("\\", "/"):
                    continue
                await page.wait_for_load_state("domcontentloaded", timeout=2_000)
                viewport = await page.evaluate(
                    """() => ({
                      width: Math.max(window.innerWidth || 0, document.documentElement?.clientWidth || 0),
                      height: Math.max(window.innerHeight || 0, document.documentElement?.clientHeight || 0),
                      hasBody: Boolean(document.body),
                    })"""
                )
            except Exception:
                continue
            if (
                isinstance(viewport, dict)
                and viewport.get("hasBody") is True
                and int(viewport.get("width") or 0) > 0
                and int(viewport.get("height") or 0) > 0
            ):
                return page
        await asyncio.sleep(0.25)
    raise TimeoutError(
        "Electron did not expose a loaded AUIP application surface with non-zero bounds"
    )


def _entry_text(
    *,
    journey_layer: str,
    engagement_mode: str,
    controller_policy: bool = False,
    adaptation_requirement: str = "",
    natural_adaptation_request: bool = False,
) -> str:
    if natural_adaptation_request:
        if journey_layer != "adaptation":
            raise ValueError(
                "natural adaptation request is only valid for adaptation Journeys"
            )
        return "请你接入它。"
    if journey_layer == "interaction":
        if engagement_mode == "observe":
            return "打开这个已经接好的小游戏，我自己玩，你只观察就好。"
        if engagement_mode == "delegate":
            return "打开这个已经接好的小游戏，接下来交给你玩。"
        return "打开这个已经接好的小游戏，我们一起玩。"
    prefix = (
        "把这个小游戏接好以后直接打开"
    )
    controller = (
        "打开后先保持共同操作；我会再请你设置一项持续响应策略，"
        "切到只观察或离开时必须停止由你授权的持续响应。"
        if controller_policy
        else ""
    )
    requirement = (
        str(adaptation_requirement or "").strip()
        if journey_layer == "adaptation"
        else ""
    )
    if requirement:
        requirement = requirement.rstrip("。") + "。"
    if engagement_mode == "observe":
        return prefix + "。" + requirement + "这次只观察我操作，不要替我行动。"
    if engagement_mode == "delegate":
        return (
            prefix
            + "。"
            + controller
            + requirement
            + "打开后交给你自主操作，我也会在中途参与一步。"
        )
    return prefix + "。" + controller + requirement + "我们一起按这个应用自己的规则试一下。"


def _choice_options(state: Any) -> list[dict[str, Any]]:
    situation = _find_situation(state, "choice/v1") or {}
    return [
        dict(option)
        for option in situation.get("options") or []
        if isinstance(option, dict)
    ]


def _available_choice_actions(state: Any) -> list[dict[str, Any]]:
    """Resolve legal choice actions without depending on app action names."""

    situation = _find_situation(state, "choice/v1") or {}
    shared_action = str(situation.get("action") or "").strip()
    actions: list[dict[str, Any]] = []
    for option in situation.get("options") or []:
        if not isinstance(option, dict) or option.get("available") is False:
            continue
        action_type = str(option.get("action") or shared_action).strip()
        if not action_type:
            continue
        payload = option.get("payload")
        actions.append(
            {
                "type": action_type,
                "payload": dict(payload) if isinstance(payload, dict) else {},
                "label": str(option.get("label") or option.get("id") or "").strip(),
            }
        )
    return actions


def _scalar_transition_checks(
    *,
    scenario: dict[str, Any],
    before_state: Any,
    after_state: Any,
    action_type: str,
    direction_override: str = "",
) -> dict[str, bool]:
    """Evaluate app-specific scalar expectations only in the live test driver."""

    oracle = scenario.get("scalar_oracle")
    if not isinstance(oracle, dict):
        return {}
    before = _find_situation(before_state, "scalars/v1") or {}
    after = _find_situation(after_state, "scalars/v1") or {}
    metric_ids = {
        str(value or "").strip()
        for value in oracle.get("metric_ids") or []
        if str(value or "").strip()
    }

    def metric(situation: dict[str, Any]) -> dict[str, Any] | None:
        metrics = [
            item
            for item in situation.get("metrics") or []
            if isinstance(item, dict)
        ]
        matched = next(
            (
                item
                for item in metrics
                if str(item.get("id") or "").strip() in metric_ids
            ),
            None,
        )
        # A scalars/v1 projection with one metric is unambiguous regardless of
        # the adapter's local identifier. Multiple metrics still require the
        # scenario oracle to name the intended semantic target.
        return matched or (metrics[0] if len(metrics) == 1 else None)

    before_metric = metric(before)
    after_metric = metric(after)
    observed = bool(before_metric and after_metric)
    checks = {"scalar_oracle_observed_metric": observed}
    if not observed:
        return checks
    try:
        before_value = float(before_metric["value"])
        after_value = float(after_metric["value"])
        safe = before_metric.get("safe")
        safe_low = float(safe[0])
        safe_high = float(safe[1])
    except (KeyError, TypeError, ValueError, IndexError):
        checks["scalar_oracle_values_are_comparable"] = False
        return checks
    checks["scalar_oracle_values_are_comparable"] = safe_low <= safe_high
    action_name = str(action_type or "").strip().lower().rsplit(".", 1)[-1]
    directions = oracle.get("action_directions")
    directions = directions if isinstance(directions, dict) else {}
    direction = str(
        direction_override or directions.get(action_name) or ""
    ).strip().lower()
    checks["scalar_oracle_knows_action_direction"] = direction in {
        "increase",
        "decrease",
        "toward_safe",
    }
    if not checks["scalar_oracle_knows_action_direction"]:
        return checks

    def distance_to_safe(value: float) -> float:
        if value < safe_low:
            return safe_low - value
        if value > safe_high:
            return value - safe_high
        return 0.0

    if direction == "increase":
        moved_as_declared = after_value > before_value
    elif direction == "decrease":
        moved_as_declared = after_value < before_value
    else:
        moved_as_declared = distance_to_safe(after_value) <= distance_to_safe(
            before_value
        )
    checks["scalar_action_moved_as_declared"] = moved_as_declared
    checks["scalar_action_matches_current_zone"] = not (
        (before_value < safe_low and direction == "decrease")
        or (before_value > safe_high and direction == "increase")
    )
    if oracle.get("forbid_safe_interval_overshoot") is True:
        checks["scalar_action_did_not_cross_the_entire_safe_interval"] = not (
            (before_value > safe_high and after_value < safe_low)
            or (before_value < safe_low and after_value > safe_high)
        )
    return checks


def _signal_routing_control_selectors(source: str, channel: str) -> tuple[str, str]:
    """Resolve the concrete fixture's equivalent source/channel controls.

    The Journey owns browser automation for this scenario, but Provider-authored
    adapters must not rename the original DOM merely to satisfy the instrument.
    Accept the two established standalone control shapes and reject untrusted
    state values before interpolating them into CSS.
    """

    clean_source = str(source or "").strip()
    clean_channel = str(channel or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", clean_source) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,40}", clean_channel
    ):
        raise RuntimeError("signal-routing state exposed an invalid control id")
    return (
        (
            f'.source[data-id="{clean_source}"], '
            f'[data-source="{clean_source}"], #src-{clean_source}'
        ),
        (
            f'.target[data-id="{clean_channel}"], '
            f'[data-channel="{clean_channel}"], #ch-{clean_channel}'
        ),
    )


async def _perform_scenario_local_action(
    *,
    product: ElectronProduct,
    probe: WsProbe,
    app_page: Any,
    app_session_id: str,
    scenario_name: str,
    label: str,
    timeout: float,
    gomoku_coordinate_order: list[tuple[int, int]] | None = None,
) -> TurnEvidence:
    """Perform one real local UI action at the test-only scenario boundary.

    AUIP and the Host remain application-neutral.  A browser Journey necessarily
    knows how a human operates its concrete fixture, just as a future native-app
    Journey would own a different adapter outside the runtime contract.
    """

    if scenario_name not in {"signal-routing", "gomoku"}:
        raise ValueError(
            f"local player automation is not defined for scenario {scenario_name!r}"
        )
    before = await probe.request(
        "auip.session.get",
        {"app_session_id": app_session_id},
        timeout=20.0,
    )
    revision = int(before.get("revision") or 0)
    if scenario_name == "signal-routing":
        connections = before.get("state", {}).get("connections", {})
        connections = connections if isinstance(connections, dict) else {}
        option = next(
            (
                item
                for item in _choice_options(before.get("state"))
                if isinstance(item.get("payload"), dict)
                and not connections.get(
                    str(
                        item["payload"].get("source")
                        or item["payload"].get("s")
                        or ""
                    )
                )
                and (item["payload"].get("source") or item["payload"].get("s"))
                and (item["payload"].get("channel") or item["payload"].get("c"))
            ),
            None,
        )
        if option is None:
            raise RuntimeError("no locally playable signal-routing choice remained")
        payload = dict(option["payload"])
        source = str(payload.get("source") or payload.get("s"))
        channel = str(payload.get("channel") or payload.get("c"))
        action_text = f"local player selected {source}->{channel}"
    else:
        grid = _find_situation(before.get("state"), "grid/v1") or {}
        width = int(grid.get("width") or 0)
        height = int(grid.get("height") or 0)
        empty = str(grid.get("empty") or ".")
        rows = [str(row) for row in grid.get("rows") or []]
        preferred = (height // 2, width // 2)
        preferred_order = list(gomoku_coordinate_order or [preferred])
        candidates = preferred_order + [
            (row, col)
            for row in range(height)
            for col in range(width)
            if (row, col) not in set(preferred_order)
        ]
        coordinate = next(
            (
                (row, col)
                for row, col in candidates
                if 0 <= row < len(rows)
                and 0 <= col < len(rows[row])
                and rows[row][col] == empty
            ),
            None,
        )
        if coordinate is None:
            raise RuntimeError("no locally playable Gomoku cell remained")
        row, col = coordinate
        action_text = f"local player selected Gomoku row={row} col={col}"
    turn = TurnEvidence(
        label=label,
        text=action_text,
        event_start=len(probe.state.events),
    )
    if scenario_name == "signal-routing":
        source_selector, channel_selector = _signal_routing_control_selectors(
            source,
            channel,
        )
        source_control = app_page.locator(source_selector)
        channel_control = app_page.locator(channel_selector)
        if await source_control.count() < 1 or await channel_control.count() < 1:
            raise RuntimeError(
                "signal-routing fixture omitted its visible source/channel controls"
            )
        await source_control.first.click()
        await channel_control.first.click()
    else:
        await app_page.locator("#board .cell").nth(row * width + col).click()
    update = await probe.wait_event(
        lambda event: event.method == "auip.updated"
        and str(event.params.get("app_session_id") or "") == app_session_id
        and isinstance(event.params.get("event"), dict)
        and str(event.params["event"].get("actor") or "") == "user",
        after=turn.event_start,
        timeout=timeout,
        description=f"{label} accepted local user event",
    )
    current = await probe.request(
        "auip.session.get",
        {"app_session_id": app_session_id},
        timeout=20.0,
    )
    turn.checks["local_revision_advanced"] = int(current.get("revision") or 0) > revision
    turn.notes.append(json.dumps(_safe_excerpt(update.params, 1200), ensure_ascii=False))
    turn.event_end = len(probe.state.events)
    target = product.run_root / "screenshots" / f"{label}.png"
    await app_page.screenshot(path=str(target), full_page=True)
    turn.screenshot = str(target)
    return turn


async def _prepare_gomoku_player_interleave(
    *,
    product: ElectronProduct,
    probe: WsProbe,
    app_page: Any,
    app_session_id: str,
    timeout: float,
    allow_delegate_opening: bool = False,
) -> TurnEvidence:
    """Ensure the concrete Journey fixture leaves a real turn to the player.

    This is scenario-owned browser instrumentation, not an AUIP Host rule.
    Older generated fixtures expose a visible role selector; the maintained
    sample already publishes the desired role binding in accepted state. A
    delegate may also have taken the atomic opening move before this setup.
    """

    turn = TurnEvidence(
        label="player_setup",
        text="local player has a legal Gomoku turn",
        event_start=len(probe.state.events),
    )
    current = await probe.request(
        "auip.session.get",
        {"app_session_id": app_session_id},
        timeout=20.0,
    )
    state = current.get("state") if isinstance(current.get("state"), dict) else {}
    binding_status = _gomoku_interleave_binding_status(
        state,
        allow_delegate_opening=allow_delegate_opening,
    )
    if binding_status:
        turn.checks["player_interleave_ready"] = True
        turn.notes.append(
            "accepted state already binds Participant to white"
            if binding_status == "participant_white"
            else (
                "delegate already took the verified opening move; accepted "
                "state now leaves the local player's side to move"
            )
        )
    else:
        role_select = app_page.locator("#role-select")
        if await role_select.count() != 1:
            raise RuntimeError(
                "Gomoku fixture neither binds Participant to white nor exposes "
                "one #role-select control"
            )
        await role_select.select_option("white")
        update = await probe.wait_event(
            lambda event: event.method == "auip.updated"
            and str(event.params.get("app_session_id") or "") == app_session_id
            and isinstance(event.params.get("event"), dict)
            and str(event.params["event"].get("actor") or "") == "user"
            and str(event.params["event"].get("type") or "")
            == "game.participant_role",
            after=turn.event_start,
            timeout=timeout,
            description="Gomoku local participant role binding",
        )
        current = await probe.request(
            "auip.session.get",
            {"app_session_id": app_session_id},
            timeout=20.0,
        )
        current_state = (
            current.get("state")
            if isinstance(current.get("state"), dict)
            else {}
        )
        roles = (
            current_state.get("roles")
            if isinstance(current_state.get("roles"), dict)
            else {}
        )
        turn.checks["player_interleave_ready"] = roles.get("kurisu") == "white"
        turn.notes.append(
            json.dumps(_safe_excerpt(update.params, 1200), ensure_ascii=False)
        )
    turn.event_end = len(probe.state.events)
    target = product.run_root / "screenshots" / "player-setup.png"
    await app_page.screenshot(path=str(target), full_page=True)
    turn.screenshot = str(target)
    return turn


def _gomoku_interleave_binding_status(
    state: Any,
    *,
    allow_delegate_opening: bool,
) -> str:
    """Classify only the two player-interleave states the Journey supports."""

    current = state if isinstance(state, dict) else {}
    bindings = (
        current.get("roleBindings")
        if isinstance(current.get("roleBindings"), dict)
        else {}
    )
    legacy_roles = (
        current.get("roles") if isinstance(current.get("roles"), dict) else {}
    )
    participant_side = str(
        bindings.get("participant") or legacy_roles.get("kurisu") or ""
    ).strip().lower()
    if participant_side == "white":
        return "participant_white"
    user_side = str(bindings.get("user") or "").strip().lower()
    turn = str(current.get("turn") or "").strip().lower()
    move_count = int(current.get("moveCount") or 0)
    if (
        allow_delegate_opening
        and participant_side == "black"
        and user_side == "white"
        and turn == user_side
        and move_count >= 1
    ):
        return "delegate_opening"
    return ""


async def _wait_automatic_participant_action(
    *,
    product: ElectronProduct,
    probe: WsProbe,
    app_session_id: str,
    label: str,
    after: int,
    timeout: float,
    require_b2: bool = False,
    expected_action_type: str = "",
) -> TurnEvidence:
    turn = TurnEvidence(
        label=label,
        text="host-scheduled Participant response",
        event_start=after,
    )
    def _matches_automatic_request(event: EventRecord) -> bool:
        if (
            event.method != "auip.action.requested"
            or str(event.params.get("app_session_id") or "") != app_session_id
        ):
            return False
        if not require_b2:
            return True
        action = (
            event.params.get("action")
            if isinstance(event.params.get("action"), dict)
            else {}
        )
        return bool(
            event.params.get("decision_path") == "b2"
            and str(action.get("proposal_id") or "").startswith("b2a:")
        )

    requested = await probe.wait_event(
        _matches_automatic_request,
        after=after,
        timeout=timeout,
        description=f"{label} automatic action request",
    )
    action_id = str(
        requested.params.get("action_id")
        or (
            (requested.params.get("action") or {}).get("action_id")
            if isinstance(requested.params.get("action"), dict)
            else ""
        )
    )
    receipt_event = await probe.wait_event(
        lambda event: event.method == "auip.updated"
        and isinstance(event.params.get("receipt"), dict)
        and (
            not action_id
            or str(event.params["receipt"].get("action_id") or "") == action_id
        ),
        after=after,
        timeout=timeout,
        description=f"{label} automatic accepted receipt",
    )
    receipt = dict(receipt_event.params.get("receipt") or {})
    turn.checks["accepted_receipt"] = receipt.get("accepted") is True
    if expected_action_type:
        turn.checks["expected_action_type"] = (
            str(receipt.get("type") or "") == expected_action_type
        )
    if require_b2:
        candidate_id = str(requested.params.get("candidate_id") or "")
        action = (
            requested.params.get("action")
            if isinstance(requested.params.get("action"), dict)
            else {}
        )
        proposal_id = str(action.get("proposal_id") or "")
        turn.checks["b2_action_request_path"] = bool(
            requested.params.get("decision_path") == "b2" and candidate_id
        )
        turn.checks["b2_candidate_receipt_linked"] = bool(
            proposal_id.startswith("b2a:")
            and candidate_id in proposal_id
            and str(receipt.get("proposal_id") or "") == proposal_id
        )
        request_index = next(
            (
                index
                for index, event in enumerate(probe.state.events)
                if event is requested
            ),
            -1,
        )
        receipt_index = next(
            (
                index
                for index, event in enumerate(probe.state.events)
                if event is receipt_event
            ),
            -1,
        )
        turn.checks["b2_receipt_order"] = bool(
            request_index >= 0 and receipt_index > request_index
        )
    turn.notes.append(
        json.dumps(_safe_excerpt(receipt, 1200), ensure_ascii=False)
    )
    if turn.checks["accepted_receipt"] is not True:
        raise RuntimeError(
            f"{label} application rejected B2 action: "
            f"{str(receipt.get('reason') or 'unknown reason')}"
        )
    turn.event_end = len(probe.state.events)
    turn.screenshot = str(await product.screenshot(label))
    return turn


async def _exercise_foreground_b2_action(
    *,
    product: ElectronProduct,
    probe: WsProbe,
    app_session_id: str,
    label: str,
    text: str,
    expected_action_type: str,
    timeout: float,
    chat_timeout: float,
    settle_timeout: float,
    no_tts: bool,
) -> tuple[TurnEvidence, dict[str, Any], dict[str, Any]]:
    """Send one short proposal and verify the complete B2 foreground commit."""

    turn = await _send_ui_turn(
        product,
        probe,
        label=label,
        text=text,
        chat_timeout=chat_timeout,
    )
    requested = await probe.wait_event(
        lambda event: event.method == "auip.action.requested"
        and str(event.params.get("app_session_id") or "") == app_session_id,
        after=turn.event_start,
        timeout=timeout,
        description=f"{label} B2 action request",
    )
    action = (
        requested.params.get("action")
        if isinstance(requested.params.get("action"), dict)
        else {}
    )
    action_id = str(action.get("action_id") or "")
    receipt_event = await probe.wait_event(
        lambda event: event.method == "auip.updated"
        and isinstance(event.params.get("receipt"), dict)
        and (
            not action_id
            or str(event.params["receipt"].get("action_id") or "")
            == action_id
        ),
        after=turn.event_start,
        timeout=timeout,
        description=f"{label} accepted receipt",
    )
    receipt = dict(receipt_event.params.get("receipt") or {})
    candidate_id = str(requested.params.get("candidate_id") or "")
    proposal_id = str(action.get("proposal_id") or "")
    complete_index = next(
        (
            index
            for index, event in enumerate(probe.state.events)
            if event.method == "chat.complete"
            and str(event.params.get("turn_id") or "") == turn.turn_id
        ),
        -1,
    )
    request_index = next(
        (
            index
            for index, event in enumerate(probe.state.events)
            if event is requested
        ),
        -1,
    )
    receipt_index = next(
        (
            index
            for index, event in enumerate(probe.state.events)
            if event is receipt_event
        ),
        -1,
    )
    complete_params = (
        probe.state.events[complete_index].params
        if complete_index >= 0
        else {}
    )
    turn.checks.update(
        {
            "accepted_receipt": receipt.get("accepted") is True,
            "expected_action_type": (
                str(receipt.get("type") or "") == expected_action_type
            ),
            "b2_action_request_path": bool(
                requested.params.get("decision_path") == "b2" and candidate_id
            ),
            "b2_candidate_receipt_linked": bool(
                proposal_id.startswith("b2f:")
                and candidate_id in proposal_id
                and str(receipt.get("proposal_id") or "") == proposal_id
                and str(complete_params.get("candidate_id") or "") == candidate_id
                and str(complete_params.get("proposal_id") or "") == proposal_id
                and str(complete_params.get("action_id") or "") == action_id
            ),
            "b2_receipt_precedes_visible_chat": bool(
                request_index >= 0
                and receipt_index > request_index
                and complete_index > receipt_index
                and not any(
                    event.method == "chat.token"
                    and str(event.params.get("turn_id") or "") == turn.turn_id
                    for event in probe.state.events[
                        turn.event_start:receipt_index
                    ]
                )
            ),
            "instruction_relation_follows": (
                str(requested.params.get("instruction_relation") or "")
                == "follows"
            ),
        }
    )
    failed = sorted(name for name, passed in turn.checks.items() if not passed)
    if failed:
        raise RuntimeError(
            f"{label} B2 foreground contract failed: {', '.join(failed)}"
        )
    turn.notes.append(
        json.dumps(_safe_excerpt(receipt, 1200), ensure_ascii=False)
    )
    if not no_tts:
        await _wait_output_idle(probe, timeout=settle_timeout)
    session = await probe.request(
        "auip.session.get",
        {"app_session_id": app_session_id},
        timeout=20.0,
    )
    turn.event_end = len(probe.state.events)
    turn.screenshot = str(await product.screenshot(label))
    return turn, session, receipt


async def _exercise_gomoku_post_round_lifecycle(
    *,
    product: ElectronProduct,
    probe: WsProbe,
    app_session_id: str,
    timeout: float,
    chat_timeout: float,
    settle_timeout: float,
    no_tts: bool,
) -> tuple[list[TurnEvidence], dict[str, Any], dict[str, Any]]:
    """Exercise restart, an explicit resignation, and series completion."""

    turns: list[TurnEvidence] = []
    restart, _session, restart_receipt = await _exercise_foreground_b2_action(
        product=product,
        probe=probe,
        app_session_id=app_session_id,
        label="gomoku_restart_round",
        text="再来一盘。",
        expected_action_type="game.restart_round",
        timeout=timeout,
        chat_timeout=chat_timeout,
        settle_timeout=settle_timeout,
        no_tts=no_tts,
    )
    reset_event = await probe.wait_event(
        lambda event: event.method == "auip.updated"
        and isinstance(event.params.get("event"), dict)
        and str(event.params["event"].get("type") or "") == "game.reset",
        after=restart.event_start,
        timeout=timeout,
        description="Gomoku restart event",
    )
    restart.checks["round_reset_event"] = bool(reset_event)
    turns.append(restart)

    automatic = await _wait_automatic_participant_action(
        product=product,
        probe=probe,
        app_session_id=app_session_id,
        label="gomoku_restart_first_move",
        after=restart.event_start,
        timeout=timeout,
        require_b2=True,
        expected_action_type="game.place_stone",
    )
    turns.append(automatic)
    active = await probe.request(
        "auip.session.get",
        {"app_session_id": app_session_id},
        timeout=20.0,
    )
    active_state = (
        active.get("state") if isinstance(active.get("state"), dict) else {}
    )
    automatic.checks["new_round_active_after_automatic_move"] = bool(
        str(active_state.get("lifecycle") or "") == "playing"
        and str(active_state.get("winner") or "none") == "none"
        and int(active_state.get("moveCount") or 0) == 1
    )

    resign, resigned_session, resign_receipt = (
        await _exercise_foreground_b2_action(
            product=product,
            probe=probe,
            app_session_id=app_session_id,
            label="gomoku_resign_round",
            text="这盘你认输吧。",
            expected_action_type="game.resign",
            timeout=timeout,
            chat_timeout=chat_timeout,
            settle_timeout=settle_timeout,
            no_tts=no_tts,
        )
    )
    resigned_state = (
        resigned_session.get("state")
        if isinstance(resigned_session.get("state"), dict)
        else {}
    )
    role_bindings = (
        resigned_state.get("roleBindings")
        if isinstance(resigned_state.get("roleBindings"), dict)
        else {}
    )
    resign.checks["round_finished_by_participant_resignation"] = bool(
        str(resigned_state.get("lifecycle") or "") == "round_finished"
        and str(resigned_state.get("finishReason") or "")
        == "participant_resigned"
        and str(resigned_state.get("winner") or "none")
        == str(role_bindings.get("user") or "")
    )
    turns.append(resign)

    finish, completed, finish_receipt = await _exercise_foreground_b2_action(
        product=product,
        probe=probe,
        app_session_id=app_session_id,
        label="gomoku_finish_experience",
        text="结束系列。",
        expected_action_type="game.finish_experience",
        timeout=timeout,
        chat_timeout=chat_timeout,
        settle_timeout=settle_timeout,
        no_tts=no_tts,
    )
    completed_state = (
        completed.get("state")
        if isinstance(completed.get("state"), dict)
        else {}
    )
    finish.checks["experience_completed_and_branch_collapsed"] = bool(
        str(completed.get("status") or "") == "completed"
        and str(completed_state.get("lifecycle") or "")
        == "concluded"
        and isinstance(completed.get("experience_capsule"), dict)
    )
    turns.append(finish)
    return (
        turns,
        completed,
        {
            "restart_accepted": restart_receipt.get("accepted") is True,
            "automatic_first_move_accepted": (
                automatic.checks.get("accepted_receipt") is True
            ),
            "resignation_accepted": resign_receipt.get("accepted") is True,
            "finish_experience_accepted": finish_receipt.get("accepted") is True,
            "final_status": str(completed.get("status") or ""),
            "final_lifecycle": str(completed_state.get("lifecycle") or ""),
        },
    )


def _gomoku_passive_player_order(width: int, height: int) -> list[tuple[int, int]]:
    """Spread fixture-player stones so the test exercises, not solves, Gomoku."""

    raw = [
        (0, 0),
        (height - 1, width - 1),
        (0, width - 1),
        (height - 1, 0),
        (0, width // 2),
        (height - 1, width // 2),
        (height // 2, 0),
        (height // 2, width - 1),
    ]
    raw.extend(
        (row, col)
        for row in range(height)
        for col in range(width)
        if (row + col) % 2 == 0
    )
    raw.extend(
        (row, col)
        for row in range(height)
        for col in range(width)
        if (row + col) % 2 == 1
    )
    return list(dict.fromkeys(raw))


async def _play_complete_gomoku_round(
    *,
    product: ElectronProduct,
    probe: WsProbe,
    app_page: Any,
    app_session_id: str,
    initial_session: dict[str, Any],
    timeout: float,
    require_b2: bool,
) -> tuple[list[TurnEvidence], dict[str, Any], dict[str, Any]]:
    """Alternate real local clicks and automatic B2 turns until round terminal."""

    turns: list[TurnEvidence] = []
    current = dict(initial_session)
    for pair_index in range(1, 46):
        state = current.get("state") if isinstance(current.get("state"), dict) else {}
        lifecycle = str(state.get("lifecycle") or "")
        winner = str(state.get("winner") or "none")
        if lifecycle == "round_finished" or winner != "none":
            break
        board = _find_situation(state, "grid/v1") or {}
        width = int(board.get("width") or 0)
        height = int(board.get("height") or 0)
        bindings = (
            state.get("roleBindings")
            if isinstance(state.get("roleBindings"), dict)
            else {}
        )
        user_side = str(bindings.get("user") or "")
        if not width or not height or str(state.get("turn") or "") != user_side:
            raise RuntimeError(
                "complete Gomoku round lost the expected local-player turn: "
                f"turn={state.get('turn')!r} user={user_side!r}"
            )
        human = await _perform_scenario_local_action(
            product=product,
            probe=probe,
            app_page=app_page,
            app_session_id=app_session_id,
            scenario_name="gomoku",
            label=f"round_human_{pair_index}",
            timeout=timeout,
            gomoku_coordinate_order=_gomoku_passive_player_order(width, height),
        )
        turns.append(human)
        current = await probe.request(
            "auip.session.get",
            {"app_session_id": app_session_id},
            timeout=20.0,
        )
        post_human = (
            current.get("state")
            if isinstance(current.get("state"), dict)
            else {}
        )
        if (
            str(post_human.get("lifecycle") or "") == "round_finished"
            or str(post_human.get("winner") or "none") != "none"
        ):
            break

        automatic = await _wait_automatic_participant_action(
            product=product,
            probe=probe,
            app_session_id=app_session_id,
            label=f"round_b2_{pair_index}",
            after=human.event_start,
            timeout=timeout,
            require_b2=require_b2,
        )
        turns.append(automatic)
        current = await probe.request(
            "auip.session.get",
            {"app_session_id": app_session_id},
            timeout=20.0,
        )
    state = current.get("state") if isinstance(current.get("state"), dict) else {}
    lifecycle = str(state.get("lifecycle") or "")
    winner = str(state.get("winner") or "none")
    if lifecycle != "round_finished" or winner == "none":
        raise RuntimeError(
            "complete Gomoku acceptance did not reach a round result within 45 pairs"
        )
    return (
        turns,
        current,
        {
            "completed": True,
            "lifecycle": lifecycle,
            "winner": winner,
            "move_count": int(state.get("moveCount") or 0),
            "human_turns": sum(item.label.startswith("round_human_") for item in turns),
            "b2_turns": sum(item.label.startswith("round_b2_") for item in turns),
        },
    )


async def _wait_auip_narration_for_events(
    probe: WsProbe,
    *,
    app_session_id: str,
    event_ids: set[str],
    timeout: float,
) -> dict[str, Any]:
    """Wait for the verified-event lane to deliver one expected outcome fact."""

    expected = {
        str(value or "").strip()
        for value in event_ids
        if str(value or "").strip()
    }
    if not expected:
        raise RuntimeError("no important round event was published")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = await probe.request(
            "auip.session.get",
            {"app_session_id": app_session_id},
            timeout=20.0,
        )
        latest = session.get("latest_delivered_narration")
        latest = latest if isinstance(latest, dict) else {}
        if (
            str(latest.get("event_id") or "") in expected
            and str(latest.get("text") or "").strip()
        ):
            return dict(latest)
        await asyncio.sleep(0.1)
    raise TimeoutError("verified round-outcome AUIP narration was not delivered")


def _b2_automatic_presentation_summary(
    events: list[EventRecord],
) -> dict[str, Any]:
    """Correlate automatic receipts, source events, and delivered narration."""

    automatic_revisions = {
        int(receipt.get("resulting_revision") or 0)
        for event in events
        if event.method == "auip.updated"
        and isinstance(event.params.get("receipt"), dict)
        for receipt in [event.params["receipt"]]
        if str(receipt.get("proposal_id") or "").startswith("b2a:")
        and receipt.get("accepted") is True
    }
    routine_event_ids = {
        str(source.get("event_id") or "")
        for event in events
        if event.method == "auip.updated"
        and isinstance(event.params.get("event"), dict)
        for source in [event.params["event"]]
        if str(source.get("actor") or "").strip().lower() == "kurisu"
        and int(source.get("revision") or 0) in automatic_revisions
        and source.get("terminal") is not True
        and str(source.get("importance") or "normal").strip().lower()
        not in {"important", "blocking"}
        and str(source.get("event_id") or "").strip()
    }
    delivered = {
        str(narration.get("event_id") or ""): dict(narration)
        for event in events
        if event.method == "auip.updated"
        and isinstance(event.params.get("narration"), dict)
        for narration in [event.params["narration"]]
        if str(narration.get("event_id") or "").strip()
    }
    narrated_routine_ids = sorted(routine_event_ids.intersection(delivered))
    outcome_event_ids = {
        str(source.get("event_id") or "")
        for event in events
        if event.method == "auip.updated"
        and isinstance(event.params.get("event"), dict)
        for source in [event.params["event"]]
        if int(source.get("revision") or 0) in automatic_revisions
        and (
            source.get("terminal") is True
            or str(source.get("importance") or "").strip().lower()
            in {"important", "blocking"}
        )
        and str(source.get("event_id") or "").strip()
    }
    outcome_ids = sorted(outcome_event_ids.intersection(delivered))
    return {
        "automatic_action_count": len(automatic_revisions),
        "routine_source_event_count": len(routine_event_ids),
        "narrated_routine_action_count": len(narrated_routine_ids),
        "narrated_routine_event_ids": narrated_routine_ids,
        "outcome_narration_count": len(outcome_ids),
        "outcome_narration_event_ids": outcome_ids,
    }


async def _wait_auip_operator_idle(
    probe: WsProbe,
    *,
    app_session_id: str,
    timeout: float,
) -> dict[str, Any]:
    # Let the bounded deferred-beat poll observe Chat becoming idle before an
    # already-idle snapshot is accepted as the settled result.
    await asyncio.sleep(0.25)
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = await probe.request(
            "auip.session.get",
            {"app_session_id": app_session_id},
            timeout=20.0,
        )
        if (
            str(latest.get("operator_status") or "idle") == "idle"
            and latest.get("pending_action") is None
        ):
            return latest
        await asyncio.sleep(0.1)
    raise TimeoutError(
        "AUIP delegate did not settle its optional initial decision: "
        f"{_safe_excerpt(latest, 800)!r}"
    )


def _semantic_review(
    *,
    turns: list[TurnEvidence],
    events: list[EventRecord],
    permissions: list[dict[str, Any]],
    expected_identity: dict[str, Any],
    runtime_identity: dict[str, Any],
    tts_required: bool,
    journey_layer: str = "full",
    engagement_mode: str = "collaborate",
    expected_explicit_steps: int | None = None,
    expected_human_steps: int = 0,
    expect_delegate_reactions: bool = False,
) -> dict[str, Any]:
    by_label = {turn.label: turn for turn in turns}
    failures: list[str] = []
    checks: dict[str, bool] = {}

    def record(name: str, value: bool, detail: str) -> None:
        checks[name] = bool(value)
        if not value:
            failures.append(detail)

    record(
        "runtime_code_identity_matches_launcher",
        runtime_identity.get("workspace_fingerprint")
        == expected_identity.get("workspace_fingerprint"),
        "runtime loaded a different checkout/worktree than the Journey launcher",
    )
    creation = by_label.get("create")
    status = by_label.get("status")
    prepare = by_label.get("prepare")
    launch = by_label.get("launch")
    entry = prepare or launch
    steps = [
        turn
        for turn in turns
        if turn.label == "step" or turn.label.startswith("step_")
    ]
    required_explicit_steps = (
        len(steps)
        if expected_explicit_steps is None
        else max(0, int(expected_explicit_steps))
    )
    human_steps = [
        turn for turn in turns if turn.label.startswith("human_action")
    ]
    automatic_steps = [
        turn
        for turn in turns
        if turn.label.startswith("delegate_action")
        or turn.label.startswith("participant_action")
    ]
    reset = by_label.get("reset")
    outside_surface = by_label.get("outside_surface_proposal")
    query = by_label.get("query")
    leave = by_label.get("leave")
    post_leave_chat = by_label.get("post_leave_chat")
    layer = str(journey_layer or "full").strip().lower()
    if layer == "full":
        record(
            "create_turn_started_one_provider_run",
            bool(creation and len(creation.run_ids) == 1),
            "the creation turn did not start exactly one Provider run",
        )
        record(
            "creation_business_outcome_verified",
            bool(creation and creation.checks.get("business_outcome_verified") is True),
            "the creation Provider process ended without a complete Host-verified business outcome",
        )
        record(
            "status_turn_started_no_provider_run",
            bool(status and not status.run_ids),
            "the read-only status question started another Provider run",
        )
        record(
            "status_answer_entered_the_visible_main_chat",
            bool(status and status.checks.get("canonical_status_answer_visible")),
            "the Work Ledger answered but its canonical status did not enter main Chat",
        )
    if layer in {"full", "adaptation"}:
        record(
            "prepare_turn_preserved_one_work_continuation",
            bool(
                prepare
                and (
                    len(prepare.run_ids) <= 1
                    or is_bounded_progress_recovery_chain(
                        [
                            event
                            for event in _run_created_events(
                                events[prepare.event_start :]
                            )
                            if _event_run_id(event) in set(prepare.run_ids)
                        ]
                    )
                )
                and prepare.checks.get("same_work_item") is True
            ),
            "AUIP preparation did not continue the application's WorkItem",
        )
        record(
            "prepare_business_outcome_verified",
            bool(prepare and prepare.checks.get("business_outcome_verified") is True),
            "the AUIP preparation process ended without a complete Host-verified application outcome",
        )
    elif layer == "interaction":
        record(
            "launch_turn_started_no_provider_run",
            bool(launch and not launch.run_ids),
            "the interaction-only launch unexpectedly started Provider authoring",
        )
    record(
        "prepare_exposed_expected_situation_kind",
        bool(
            entry
            and entry.checks.get("expected_situation_kind_visible") is True
        ),
        "the active AUIP projection did not expose the scenario's standard situation kind",
    )
    record(
        "requested_engagement_mode_became_active",
        bool(entry and entry.checks.get("engagement_mode_active") is True),
        "the AppSession did not enter the requested engagement mode",
    )
    expected_progress_runs = {
        str(run_id)
        for turn in (creation, prepare)
        if turn is not None
        for run_id in turn.run_ids
        if str(run_id)
    }
    substantive_progress_runs = {
        str(event.params.get("run_id") or "")
        for event in events
        if event.method == "chat.observer_decision"
        and event.params.get("terminal") is not True
        and event.params.get("speak") is True
        and {
            str(value)
            for value in event.params.get("narration_keypoints") or []
        }.intersection({"directional_progress", "semantic_progress"})
    }
    if expected_progress_runs:
        record(
            "each_provider_attempt_spoke_substantive_progress",
            expected_progress_runs.issubset(substantive_progress_runs),
            "a Provider attempt completed without one spoken design/direction update",
        )
    if required_explicit_steps:
        record(
            "step_produced_an_application_action_request",
            len(steps) == required_explicit_steps
            and all(
                any(
                    event.method == "auip.action.requested"
                    for event in events[item.event_start : item.event_end]
                )
                for item in steps
            ),
            "an explicit live AUIP step produced no application action request",
        )
        step_counts = [
            (
                sum(
                    event.method == "auip.action.requested"
                    for event in events[item.event_start : item.event_end]
                ),
                sum(
                    event.method == "auip.updated"
                    and isinstance(event.params.get("receipt"), dict)
                    and event.params["receipt"].get("accepted") is True
                    for event in events[item.event_start : item.event_end]
                ),
            )
            for item in steps
        ]
        record(
            "explicit_step_produced_exactly_one_action_and_receipt",
            len(step_counts) == required_explicit_steps
            and all(counts == (1, 1) for counts in step_counts),
            "an explicit AUIP step produced more or fewer than one action/accepted receipt",
        )
    if expected_human_steps:
        record(
            "local_player_actions_advanced_application_state",
            len(human_steps) == expected_human_steps
            and all(
                item.checks.get("local_revision_advanced") is True
                for item in human_steps
            ),
            "a local player action did not advance accepted application state",
        )
        if (
            engagement_mode in {"collaborate", "delegate"}
            and expect_delegate_reactions
        ):
            record(
                "participant_reacted_once_to_each_declared_opportunity",
                len(automatic_steps) == expected_human_steps
                and all(
                    item.checks.get("accepted_receipt") is True
                    for item in automatic_steps
                ),
                "the active Participant did not produce one accepted reaction per declared opportunity",
            )
        elif engagement_mode == "observe":
            record(
                "observe_local_actions_did_not_gain_automatic_authority",
                all(
                    not any(
                        event.method == "auip.action.requested"
                        for event in events[item.event_start : item.event_end]
                    )
                    for item in human_steps
                ),
                "observe mode unexpectedly granted automatic Participant authority",
            )
    if reset is not None:
        reset_events = events[reset.event_start : reset.event_end]
        record(
            "reset_produced_exactly_one_action_and_receipt",
            sum(event.method == "auip.action.requested" for event in reset_events) == 1
            and sum(
                event.method == "auip.updated"
                and isinstance(event.params.get("receipt"), dict)
                and event.params["receipt"].get("accepted") is True
                for event in reset_events
            )
            == 1,
            "the reset turn did not produce exactly one action and accepted receipt",
        )
        record(
            "reset_restored_the_initial_situation",
            reset.checks.get("initial_situation_restored") is True,
            "the reset receipt did not restore the standard situation projection",
        )
    if outside_surface is not None:
        outside_events = events[
            outside_surface.event_start : outside_surface.event_end
        ]
        request_count = sum(
            event.method == "auip.action.requested" for event in outside_events
        )
        accepted_count = sum(
            event.method == "auip.updated"
            and isinstance(event.params.get("receipt"), dict)
            and event.params["receipt"].get("accepted") is True
            for event in outside_events
        )
        record(
            "outside_surface_proposal_stayed_within_one_declared_role_choice",
            (request_count, accepted_count) in {(0, 0), (1, 1)},
            "an outside-surface proposal produced an unbounded or rejected application action instead of no action or one settled declared alternative",
        )
    record(
        "query_produced_no_application_action_request",
        bool(
            query
            and not any(
                event.method == "auip.action.requested"
                for event in events[query.event_start : query.event_end]
            )
        ),
        "the read-only AUIP status question produced another application action",
    )
    record(
        "query_produced_no_work_ledger_answer",
        bool(
            query
            and not any(
                _is_work_status_answer(event)
                for event in events[query.event_start : query.event_end]
            )
        ),
        "the AUIP state question also produced an unrelated Work Ledger report",
    )
    if query and "state_answer_grounded" in query.checks:
        record(
            "query_answer_was_grounded_in_current_state",
            query.checks.get("state_answer_grounded") is True,
            "the AUIP state question did not report the current standard situation fact",
        )
    record(
        "leave_closed_the_application_session",
        bool(leave and leave.checks.get("app_session_closed")),
        "the explicit leave turn did not close its AppSession",
    )
    if post_leave_chat is not None:
        post_leave_events = events[
            post_leave_chat.event_start : post_leave_chat.event_end
        ]
        record(
            "post_leave_chat_returned_a_visible_role_reply",
            bool(post_leave_chat.reply.strip()),
            "the first ordinary Chat turn after leave returned no visible reply",
        )
        record(
            "post_leave_chat_started_no_provider_work",
            not post_leave_chat.run_ids,
            "the first ordinary Chat turn after leave unexpectedly started Work",
        )
        record(
            "post_leave_chat_did_not_reopen_or_operate_the_app",
            not any(
                event.method
                in {"auip.launch.requested", "auip.action.requested"}
                for event in post_leave_events
            ),
            "the first ordinary Chat turn after leave reopened or operated the app",
        )
        if tts_required:
            record(
                "post_leave_chat_reached_first_tts",
                "first_tts_sentence_start_s" in post_leave_chat.timings,
                "the first ordinary Chat turn after leave produced no TTS start",
            )
    record(
        "all_automatic_permissions_were_bounded_and_accepted",
        all(
            row.get("workspace_safe")
            and row.get("scopes_safe")
            and (row.get("resolution") or {}).get("ok") is True
            for row in permissions
        ),
        "an automatic permission decision escaped the isolated Journey scope",
    )
    if tts_required:
        record(
            "tts_started_and_completed_visible_speech",
            any(event.method == "tts.sentence_start" for event in events)
            and any(event.method == "tts.sentence_end" for event in events),
            "the live product emitted no complete TTS sentence lifecycle",
        )
    return {
        "status": "passed" if not failures else "failed",
        "checks": checks,
        "failures": failures,
        "ai_review_required": [
            "Judge whether each Japanese role reply matches the user's natural Chinese wording.",
            "Judge whether progress and terminal narration are useful, timely and in character.",
            "Inspect screenshots for visible Chat, permission and AUIP window state.",
            "Check the correlated event/log packet for claims made before Provider or app receipts.",
            "Check that state answers do not recommend actions outside the current choice/v1 actionTypes and available options.",
        ],
    }


async def _run_full_creation_stage(
    *,
    product: ElectronProduct,
    probe: WsProbe,
    scenario: dict[str, Any],
    args: argparse.Namespace,
    run_root: Path,
    seen_permissions: set[str],
    permission_evidence: list[dict[str, Any]],
    turns: list[TurnEvidence],
) -> tuple[EventRecord, TurnEvidence]:
    create = await _send_ui_turn(
        product,
        probe,
        label="create",
        text=scenario["create"],
        chat_timeout=args.chat_timeout,
    )
    created = await _wait_created_run(
        probe,
        after=create.event_start,
        timeout=args.dispatch_timeout,
    )
    create.run_ids = [_event_run_id(created)]
    turns.append(create)

    # Ask while the same Provider run is still active. This is the ordinary
    # long-conversation interruption that isolated one-turn probes cannot
    # reproduce.
    status = await _send_ui_turn(
        product,
        probe,
        label="status",
        text="现在做到哪一步了？",
        chat_timeout=args.chat_timeout,
    )
    status_answer = await probe.wait_event(
        _is_work_status_answer,
        after=status.event_start,
        timeout=args.settle_timeout,
        description="deterministic Work Ledger status answer",
    )
    status_text = str(
        status_answer.params.get("main_chat_entry")
        or status_answer.params.get("display_text")
        or ""
    ).strip()
    dom_visible = False
    if status_text and product.page is not None:
        try:
            await product.page.get_by_text(status_text, exact=True).wait_for(
                state="visible",
                timeout=max(1000, int(args.settle_timeout * 1000)),
            )
            dom_visible = True
        except Exception as exc:
            status.notes.append(
                "ledger answer DOM visibility: " f"{type(exc).__name__}: {exc}"
            )
    status.checks["canonical_status_answer_visible"] = bool(
        status_text
        and status_answer.params.get("append_to_main_chat") is True
        and dom_visible
    )
    if status_text:
        status.notes.append(f"ledger answer: {status_text}")
    await asyncio.sleep(0.25)
    status.event_end = len(probe.state.events)
    status.run_ids = [
        _event_run_id(row)
        for row in _new_runs(probe.state.events, after=status.event_start)
        if _event_run_id(row)
    ]
    status.screenshot = str(await product.screenshot("turn-status-with-ledger"))
    turns.append(status)

    creation_terminal, creation_item, creation_run_ids = await _wait_provider_chain_terminal(
        product=product,
        probe=probe,
        initial_created=created,
        after=create.event_start,
        run_root=run_root,
        timeout=args.provider_timeout,
        seen_permissions=seen_permissions,
        permission_evidence=permission_evidence,
    )
    create.run_ids = creation_run_ids
    create.checks["provider_succeeded"] = (
        _provider_status(creation_terminal) in SUCCESS_STATUSES
    )
    create.checks["business_outcome_verified"] = bool(
        str(creation_item.get("state") or "").strip().lower() == "accepted"
        or str(creation_item.get("completion") or "").strip().lower() == "complete"
    )
    if not create.checks["business_outcome_verified"]:
        failure = (
            _provider_error(creation_terminal)
            or str(creation_item.get("completionRationale") or "")
            or "unknown"
        )
        raise RuntimeError(
            "creation ended without a complete business outcome: " + failure
        )
    try:
        await probe.wait_event(
            lambda event: _is_terminal_observer_decision_for_run(
                event,
                create.run_ids[-1],
            ),
            after=create.event_start,
            timeout=args.settle_timeout,
            description="creation terminal narration",
        )
        await _wait_output_idle(probe, timeout=args.settle_timeout)
        create.checks["terminal_narration_delivered"] = True
    except Exception as exc:
        create.checks["terminal_narration_delivered"] = False
        create.notes.append(f"terminal presentation: {type(exc).__name__}: {exc}")
    create.screenshot = str(await product.screenshot("after-create-terminal"))
    return created, create


async def _run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    scenario = SCENARIOS[args.scenario]
    journey_layer = str(args.journey_layer or "full").strip().lower()
    engagement_mode = str(args.engagement_mode or "collaborate").strip().lower()
    if journey_layer not in JOURNEY_LAYERS:
        raise ValueError(f"unsupported Journey layer: {journey_layer}")
    if engagement_mode not in ENGAGEMENT_MODES:
        raise ValueError(f"unsupported engagement mode: {engagement_mode}")
    explicit_steps = max(0, int(args.interaction_steps))
    if args.require_b2 and str(
        settings.AUIP_APPSESSION_ROLE_BRANCH_MODE or ""
    ).strip().lower() != "b2":
        raise ValueError(
            "--require-b2 requires AUIP_APPSESSION_ROLE_BRANCH_MODE=b2"
        )
    if args.complete_gomoku_round and (
        args.scenario != "gomoku"
        or journey_layer != "interaction"
        or not args.require_b2
        or explicit_steps != 1
        or engagement_mode != "collaborate"
    ):
        raise ValueError(
            "--complete-gomoku-round requires the interaction Gomoku Journey, "
            "--require-b2, collaborate mode, and exactly one explicit first step"
        )
    if args.exercise_gomoku_post_round and not args.complete_gomoku_round:
        raise ValueError(
            "--exercise-gomoku-post-round requires --complete-gomoku-round"
        )
    human_steps = max(0, int(args.human_steps))
    controller_oracle = scenario.get("controller_oracle")
    controller_policy_expected = bool(scenario.get("controller_policy")) or isinstance(
        controller_oracle,
        dict,
    )
    controller_lease_ms = int(args.controller_lease_ms or 0)
    controller_soak_seconds = max(0.0, float(args.controller_soak_seconds or 0.0))
    if controller_lease_ms and journey_layer != "interaction":
        raise ValueError("--controller-lease-ms is only valid for interaction Journeys")
    if args.refresh_host_runtime_assets and journey_layer != "interaction":
        raise ValueError(
            "--refresh-host-runtime-assets is only valid for interaction Journeys"
        )
    if args.natural_adaptation_request and journey_layer != "adaptation":
        raise ValueError(
            "--natural-adaptation-request is only valid for adaptation Journeys"
        )
    if args.exercise_active_amendment and journey_layer != "interaction":
        raise ValueError(
            "--exercise-active-amendment is only valid for interaction Journeys"
        )
    if controller_lease_ms and not 250 <= controller_lease_ms <= 300_000:
        raise ValueError("--controller-lease-ms must be between 250 and 300000")
    if controller_soak_seconds and (
        journey_layer != "interaction"
        or not controller_policy_expected
        or explicit_steps != 1
        or not isinstance(scenario.get("controller_soak_oracle"), dict)
    ):
        raise ValueError(
            "Controller soak requires one explicit step in an interaction "
            "Controller scenario with a controller_soak_oracle"
        )
    if (
        controller_soak_seconds
        and controller_lease_ms
        and controller_lease_ms <= int((controller_soak_seconds + 2.0) * 1000)
    ):
        raise ValueError(
            "--controller-lease-ms must outlive --controller-soak-seconds by 2s"
        )
    if args.exercise_controller_expiry and (
        journey_layer != "interaction"
        or not controller_policy_expected
        or not controller_lease_ms
        or explicit_steps != 1
    ):
        raise ValueError(
            "Controller expiry requires one explicit step in an interaction "
            "Controller scenario and --controller-lease-ms"
        )
    if args.renew_controller_after_expiry and not args.exercise_controller_expiry:
        raise ValueError(
            "--renew-controller-after-expiry requires --exercise-controller-expiry"
        )
    if (
        isinstance(controller_oracle, dict)
        and controller_oracle.get("expect_narration") is True
        and args.no_tts
    ):
        raise ValueError(
            "this scenario requires the real narration delivery lane; "
            "omit --no-tts"
        )
    if engagement_mode == "observe" and explicit_steps:
        raise ValueError("observe Journeys cannot request Participant action steps")
    if engagement_mode == "observe" and args.exercise_reset:
        raise ValueError("observe Journeys cannot request a Participant reset")
    if engagement_mode == "observe" and args.expect_delegate_reactions:
        raise ValueError(
            "observe Journeys cannot expect automatic Participant reactions"
        )
    if human_steps and args.scenario not in {"signal-routing", "gomoku"}:
        raise ValueError(
            "this live Journey currently has local-player UI instrumentation only for signal-routing"
        )
    if journey_layer == "full" and str(args.seed or "").strip():
        raise ValueError("--seed is only valid for adaptation or interaction Journeys")
    if journey_layer != "full" and not str(args.seed or "").strip():
        raise ValueError(f"{journey_layer} Journey requires --seed")
    run_id = (
        f"live_product_{args.scenario}_{journey_layer}_{_utc_stamp()}_"
        f"{uuid.uuid4().hex[:6]}"
    )
    report_dir = Path(args.report_dir).resolve()
    run_root = report_dir / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    identity = code_identity(ROOT)
    debug_port = int(args.debug_port or _free_port())
    product = ElectronProduct(
        run_root=run_root,
        debug_port=debug_port,
        no_tts=bool(args.no_tts),
        identity=identity,
    )
    report_path = run_root / "report.json"
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": run_id,
        "scenario": args.scenario,
        "journey_layer": journey_layer,
        "engagement_mode": engagement_mode,
        "adaptation_request_profile": (
            "natural" if args.natural_adaptation_request else "diagnostic"
        ),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "failed",
        "paths": {
            "run_root": str(run_root),
            "report": str(report_path),
            "process_log": str(product.log_path),
        },
        "launcher_code_identity": identity,
        "model_profile": {
            "chat_provider": str(args.chat_provider),
            "execution_model": str(args.model),
            "auip_role_branch_mode": str(settings.AUIP_APPSESSION_ROLE_BRANCH_MODE),
            "auip_b2_open_payload_mode": str(settings.AUIP_B2_OPEN_PAYLOAD_MODE),
            "auip_action_provider": str(settings.AUIP_ACTION_PROVIDER),
            "auip_action_model": str(settings.AUIP_ACTION_MODEL),
            "auip_action_reasoning_effort": str(
                settings.AUIP_ACTION_REASONING_EFFORT
            ),
            "auip_action_service_tier": str(settings.AUIP_ACTION_SERVICE_TIER),
        },
        "turns": [],
        "permissions": [],
        "events": [],
    }
    turns: list[TurnEvidence] = []
    permission_evidence: list[dict[str, Any]] = []
    seen_permissions: set[str] = set()
    probe: WsProbe | None = None
    exit_code = 1
    runtime_identity: dict[str, Any] = {}
    try:
        if not args.no_build:
            npm = shutil.which("npm.cmd") or shutil.which("npm")
            if not npm:
                raise FileNotFoundError("npm is required to build the Electron renderer")
            build_log = run_root / "electron-build.log"
            with build_log.open("w", encoding="utf-8", newline="\n") as handle:
                built = subprocess.run(
                    [npm, "run", "build"],
                    cwd=str(ELECTRON_ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            report["paths"]["build_log"] = str(build_log)
            if built.returncode:
                raise RuntimeError(f"Electron build failed with {built.returncode}")

        await product.start(startup_timeout=args.startup_timeout)
        report["paths"]["startup_screenshot"] = str(
            await product.screenshot("startup")
        )
        async with WsProbe(
            f"ws://127.0.0.1:{BACKEND_PORT}/ws",
            subprotocols=product.backend_websocket_protocols,
        ) as probe:
            runtime = await _wait_provider_ready(probe, timeout=args.startup_timeout)
            runtime_identity = dict(
                ((runtime.get("server") or {}).get("code_identity") or {})
                if isinstance(runtime.get("server"), dict)
                else {}
            )
            report["runtime_status"] = _safe_excerpt(runtime, 5000)
            report["runtime_code_identity"] = runtime_identity
            await product.select_chat_provider(str(args.chat_provider))
            created: EventRecord | None = None
            create: TurnEvidence | None = None
            seeded_work_item_id = ""
            if journey_layer == "full":
                created, create = await _run_full_creation_stage(
                    product=product,
                    probe=probe,
                    scenario=scenario,
                    args=args,
                    run_root=run_root,
                    seen_permissions=seen_permissions,
                    permission_evidence=permission_evidence,
                    turns=turns,
                )
            else:
                session = runtime.get("session")
                session = session if isinstance(session, dict) else {}
                seed = _seed_verified_app(
                    run_root=run_root,
                    session_id=str(session.get("current_session_id") or ""),
                    scenario_name=str(args.scenario),
                    scenario=scenario,
                    journey_layer=journey_layer,
                    source=str(args.seed),
                    controller_lease_ms=int(args.controller_lease_ms or 0),
                    # A live interaction seed stands in for a Host-finalized
                    # artifact. Always materialize and verify the current
                    # Host-owned runtime in its isolated copy; otherwise a
                    # legacy ../../sdk reference can only fail after handoff.
                    refresh_host_runtime_assets=journey_layer == "interaction",
                )
                report["seed"] = seed
                controller_policy_expected = bool(
                    controller_policy_expected or seed.get("controller_policy")
                )
                seeded_work_item_id = str(seed["work_item_id"])
                await probe.request(
                    "work.focus",
                    {"work_item_id": seeded_work_item_id},
                    timeout=20.0,
                )

            before_prepare = len(probe.state.events)
            prepare = await _send_ui_turn(
                product,
                probe,
                label="launch" if journey_layer == "interaction" else "prepare",
                text=_entry_text(
                    journey_layer=journey_layer,
                    engagement_mode=engagement_mode,
                    controller_policy=controller_policy_expected,
                    adaptation_requirement=str(
                        scenario.get("adaptation_requirement") or ""
                    ),
                    natural_adaptation_request=bool(
                        args.natural_adaptation_request
                    ),
                ),
                chat_timeout=args.chat_timeout,
            )
            prepare.checks["same_work_item"] = True
            turns.append(prepare)
            if journey_layer == "interaction":
                prepare.checks["business_outcome_verified"] = True
            else:
                try:
                    prepared_run = await _wait_created_run(
                        probe,
                        after=before_prepare,
                        timeout=args.dispatch_timeout,
                    )
                    prepare.run_ids = [_event_run_id(prepared_run)]
                    expected_work_item_id = (
                        _event_work_item_id(created)
                        if created is not None
                        else seeded_work_item_id
                    )
                    prepare.checks["same_work_item"] = bool(
                        expected_work_item_id
                        and _event_work_item_id(prepared_run) == expected_work_item_id
                    )
                    if args.natural_adaptation_request:
                        created_payload = prepared_run.params.get("payload")
                        created_payload = (
                            created_payload
                            if isinstance(created_payload, dict)
                            else {}
                        )
                        created_metadata = prepared_run.params.get("metadata")
                        created_metadata = (
                            created_metadata
                            if isinstance(created_metadata, dict)
                            else {}
                        )
                        expected_request = " ".join(str(prepare.text or "").split())
                        provider_task = " ".join(
                            str(created_payload.get("task") or "").split()
                        )
                        source_request = " ".join(
                            str(created_metadata.get("source_user_text") or "").split()
                        )
                        prepare.checks["natural_request_remained_provider_task"] = (
                            bool(expected_request)
                            and provider_task == expected_request
                            and source_request == expected_request
                        )
                        prepare.checks["natural_request_kept_prepare_authority"] = (
                            str(created_metadata.get("source") or "")
                            == "auip_prepare"
                            and _event_work_item_id(prepared_run)
                            == expected_work_item_id
                        )
                        if not all(
                            prepare.checks[name]
                            for name in (
                                "natural_request_remained_provider_task",
                                "natural_request_kept_prepare_authority",
                            )
                        ):
                            raise RuntimeError(
                                "natural AUIP adaptation changed the user's request "
                                "or lost its Host-grounded WorkItem"
                            )
                    prepared_terminal, prepared_item, prepare_run_ids = (
                        await _wait_provider_chain_terminal(
                        product=product,
                        probe=probe,
                        initial_created=prepared_run,
                        after=before_prepare,
                        run_root=run_root,
                        timeout=args.provider_timeout,
                        seen_permissions=seen_permissions,
                        permission_evidence=permission_evidence,
                        )
                    )
                    prepare.run_ids = prepare_run_ids
                    prepare.checks["provider_succeeded"] = (
                        _provider_status(prepared_terminal) in SUCCESS_STATUSES
                    )
                    preparation_blocked = (
                        not prepare.checks["provider_succeeded"]
                        or str(prepared_item.get("attention") or "").strip().lower()
                        == "error"
                        or str(prepared_item.get("execution") or "").strip().lower()
                        in {"failed", "cancelled"}
                    )
                    if preparation_blocked:
                        failure = (
                            _provider_error(prepared_terminal)
                            or str(prepared_item.get("completionRationale") or "")
                            or "unknown"
                        )
                        raise RuntimeError(
                            "AUIP preparation ended without a complete business outcome: "
                            + failure
                        )
                    # A verified AUIP authoring outcome remains review_ready /
                    # partial until the deferred Host launch crosses into a
                    # real AppSession.  Do not mistake that expected boundary
                    # for incomplete authoring; the active receipt below is
                    # the stronger business-outcome proof.
                    prepare.checks["business_outcome_verified"] = False
                except TimeoutError as exc:
                    if journey_layer == "adaptation":
                        if prepare.run_ids:
                            raise RuntimeError(
                                "adaptation Provider run did not finish within "
                                f"{float(args.provider_timeout):.0f}s: {prepare.run_ids[0]}"
                            ) from exc
                        raise RuntimeError(
                            "adaptation Journey did not start fresh AUIP authoring"
                        ) from exc
                    prepare.notes.append("no prerequisite Provider run was required")
                    prepare.checks["business_outcome_verified"] = True

            active_event = await _wait_auip_active(
                probe,
                after=before_prepare,
                timeout=args.auip_timeout,
            )
            app_session_id = str(active_event.params.get("app_session_id") or "")
            initial_app_session_id = app_session_id
            prepare.checks["auip_active"] = bool(app_session_id)
            prepare.checks["business_outcome_verified"] = bool(app_session_id)
            active_session = await _wait_situation_kind(
                probe,
                app_session_id=app_session_id,
                expected_kind=str(scenario["expected_situation_kind"]),
                timeout=args.auip_timeout,
            )
            prepare.checks["expected_situation_kind_visible"] = (
                _contains_situation_kind(
                    active_session.get("state"),
                    str(scenario["expected_situation_kind"]),
                )
            )
            prepare.checks["engagement_mode_active"] = (
                str(active_session.get("engagement_mode") or "").strip().lower()
                == engagement_mode
            )
            if journey_layer == "interaction":
                prepare.run_ids = [
                    _event_run_id(row)
                    for row in _new_runs(
                        probe.state.events,
                        after=before_prepare,
                    )
                    if _event_run_id(row)
                ]
            app_page = await _wait_app_page(product, timeout=args.auip_timeout)
            product._instrument_app_page(app_page)
            app_shot = run_root / "screenshots" / "auip-active.png"
            await app_page.screenshot(path=str(app_shot), full_page=True)
            prepare.notes.append(f"AUIP screenshot: {app_shot}")
            prepare.event_end = len(probe.state.events)
            if prepare.checks["engagement_mode_active"] is not True:
                raise RuntimeError(
                    "live AUIP entry resolved the wrong engagement mode: "
                    f"expected={engagement_mode} "
                    f"observed={active_session.get('engagement_mode')!r}"
                )

            pre_step_setup = scenario.get("pre_step_setup")
            if isinstance(pre_step_setup, dict):
                setup_turn, active_session = await _exercise_pre_step_setup(
                    probe=probe,
                    app_page=app_page,
                    app_session_id=app_session_id,
                    setup=pre_step_setup,
                    timeout=args.auip_timeout,
                )
                setup_shot = run_root / "screenshots" / "app-scene-setup.png"
                await app_page.screenshot(path=str(setup_shot), full_page=True)
                setup_turn.screenshot = str(setup_shot)
                turns.append(setup_turn)

            outside_surface_text = str(
                scenario.get("outside_surface_proposal") or ""
            ).strip()
            if outside_surface_text:
                outside_surface = await _send_ui_turn(
                    product,
                    probe,
                    label="outside_surface_proposal",
                    text=outside_surface_text,
                    chat_timeout=args.chat_timeout,
                )
                # The visible role turn completes before a silent Participant
                # review necessarily settles. Let that review reach idle so
                # the evidence range catches any late application request.
                await asyncio.sleep(min(0.75, float(args.dispatch_timeout)))
                await _wait_auip_operator_idle(
                    probe,
                    app_session_id=app_session_id,
                    timeout=args.auip_timeout,
                )
                outside_surface.event_end = len(probe.state.events)
                outside_events = probe.state.events[
                    outside_surface.event_start : outside_surface.event_end
                ]
                outside_surface.checks["application_action_count"] = sum(
                    event.method == "auip.action.requested"
                    for event in outside_events
                )
                outside_surface.checks["accepted_receipt_count"] = sum(
                    event.method == "auip.updated"
                    and isinstance(event.params.get("receipt"), dict)
                    and event.params["receipt"].get("accepted") is True
                    for event in outside_events
                )
                turns.append(outside_surface)
                active_session = await probe.request(
                    "auip.session.get",
                    {"app_session_id": app_session_id},
                    timeout=20.0,
                )

            initial_situation = _first_accepted_situation(
                probe.state.events,
                after=before_prepare,
                app_session_id=app_session_id,
                expected_kind=str(scenario["expected_situation_kind"]),
                fallback_state=active_session.get("state"),
            )
            session_after_step = active_session
            previous_revision = int(active_session.get("revision") or 0)

            if engagement_mode == "delegate":
                # Delegate may reasonably wait on a generic app.ready beat.
                # Settle that optional decision before introducing the player
                # beat whose remaining legal action makes the test meaningful.
                session_after_step = await _wait_auip_operator_idle(
                    probe,
                    app_session_id=app_session_id,
                    timeout=args.auip_timeout,
                )
                previous_revision = int(session_after_step.get("revision") or 0)

            if (
                args.scenario == "gomoku"
                and human_steps
                and engagement_mode != "observe"
            ):
                setup = await _prepare_gomoku_player_interleave(
                    product=product,
                    probe=probe,
                    app_page=app_page,
                    app_session_id=app_session_id,
                    timeout=args.auip_timeout,
                    allow_delegate_opening=engagement_mode == "delegate",
                )
                if setup.checks.get("player_interleave_ready") is not True:
                    raise RuntimeError("Gomoku local role binding did not commit")
                turns.append(setup)
                session_after_step = await probe.request(
                    "auip.session.get",
                    {"app_session_id": app_session_id},
                    timeout=20.0,
                )
                previous_revision = int(session_after_step.get("revision") or 0)

            for human_index in range(1, human_steps + 1):
                human = await _perform_scenario_local_action(
                    product=product,
                    probe=probe,
                    app_page=app_page,
                    app_session_id=app_session_id,
                    scenario_name=str(args.scenario),
                    label=(
                        "human_action"
                        if human_index == 1
                        else f"human_action_{human_index}"
                    ),
                    timeout=args.auip_timeout,
                )
                turns.append(human)
                session_after_step = await probe.request(
                    "auip.session.get",
                    {"app_session_id": app_session_id},
                    timeout=20.0,
                )
                previous_revision = int(session_after_step.get("revision") or 0)
                if engagement_mode in {"collaborate", "delegate"}:
                    if args.expect_delegate_reactions:
                        automatic = await _wait_automatic_participant_action(
                            product=product,
                            probe=probe,
                            app_session_id=app_session_id,
                            label=f"participant_action_after_human_{human_index}",
                            after=human.event_start,
                            timeout=args.auip_timeout,
                        )
                        turns.append(automatic)
                        session_after_step = await probe.request(
                            "auip.session.get",
                            {"app_session_id": app_session_id},
                            timeout=20.0,
                        )
                    else:
                        session_after_step = await _wait_auip_operator_idle(
                            probe,
                            app_session_id=app_session_id,
                            timeout=args.auip_timeout,
                        )
                        human.event_end = len(probe.state.events)
                    previous_revision = int(session_after_step.get("revision") or 0)
                else:
                    # Give an unauthorized automatic proposal enough time to
                    # become observable before closing the local-action range.
                    await asyncio.sleep(min(2.0, args.dispatch_timeout))
                    human.event_end = len(probe.state.events)

            controller_generations: list[int] = []
            controller_lease_ids: list[str] = []
            controller_payloads: list[dict[str, Any]] = []
            scenario_steps = [
                str(value)
                for value in scenario.get("steps") or []
                if str(value).strip()
            ]
            default_step_text = str(args.step_text or "").strip() or str(
                scenario["step"]
            )
            for step_index in range(1, explicit_steps + 1):
                state_before_step = session_after_step.get("state")
                step = await _send_ui_turn(
                    product,
                    probe,
                    label="step" if step_index == 1 else f"step_{step_index}",
                    text=(
                        "我刚才已经操作了一步，现在轮到你。"
                        + default_step_text
                        if human_steps and step_index == 1
                        else (
                            scenario_steps[step_index - 1]
                            if step_index <= len(scenario_steps)
                            else default_step_text
                        )
                    ),
                    chat_timeout=args.chat_timeout,
                )
                action_requested = await probe.wait_event(
                    lambda event: event.method == "auip.action.requested"
                    or _operator_failure_from_update(event) is not None,
                    after=step.event_start,
                    timeout=args.auip_timeout,
                    description=(
                        f"live AUIP action request or terminal operator outcome "
                        f"{step_index}"
                    ),
                )
                operator_failure = _operator_failure_from_update(action_requested)
                if operator_failure is not None:
                    step.checks["application_action_requested"] = False
                    step.checks["operator_failed_closed"] = True
                    step.notes.append(
                        "terminal AUIP operator outcome: "
                        + json.dumps(operator_failure, ensure_ascii=False)
                    )
                    step.event_end = len(probe.state.events)
                    turns.append(step)
                    reason = (
                        operator_failure["reason"]
                        or operator_failure["detail"]
                        or "no application action was requested"
                    )
                    raise RuntimeError(
                        "AUIP operator blocked before application action request: "
                        f"{operator_failure['code']}: {reason}"
                    )
                action_id = str(
                    action_requested.params.get("action_id")
                    or (
                        (action_requested.params.get("action") or {}).get("action_id")
                        if isinstance(action_requested.params.get("action"), dict)
                        else ""
                    )
                )
                receipt_event = await probe.wait_event(
                    lambda event: event.method == "auip.updated"
                    and isinstance(event.params.get("receipt"), dict)
                    and (
                        not action_id
                        or str(event.params["receipt"].get("action_id") or "")
                        == action_id
                    ),
                    after=step.event_start,
                    timeout=args.auip_timeout,
                    description=f"live AUIP accepted receipt {step_index}",
                )
                receipt = dict(receipt_event.params.get("receipt") or {})
                step.checks["accepted_receipt"] = receipt.get("accepted") is True
                step.notes.append(
                    json.dumps(_safe_excerpt(receipt, 1200), ensure_ascii=False)
                )
                step.event_end = len(probe.state.events)
                turns.append(step)
                session_after_step = await probe.request(
                    "auip.session.get",
                    {"app_session_id": app_session_id},
                    timeout=20.0,
                )
                controller_activation_snapshot: dict[str, Any] | None = None
                receipt_lease_probe = receipt.get("controller_lease")
                receipt_lease_probe = (
                    receipt_lease_probe
                    if isinstance(receipt_lease_probe, dict)
                    else {}
                )
                if str(receipt_lease_probe.get("lease_id") or ""):
                    # The app may publish controller/v1=active one checkpoint
                    # after the receipt. Observe that transition now: waiting
                    # for visible chat and physical TTS first can consume an
                    # intentionally short lease and erase proof it was active.
                    controller_activation_snapshot = await _wait_controller_status(
                        probe,
                        app_session_id=app_session_id,
                        expected_status="active",
                        timeout=args.auip_timeout,
                    )
                if args.require_b2:
                    action_payload = (
                        action_requested.params.get("action")
                        if isinstance(action_requested.params.get("action"), dict)
                        else {}
                    )
                    candidate_id = str(
                        action_requested.params.get("candidate_id") or ""
                    )
                    proposal_id = str(action_payload.get("proposal_id") or "")
                    complete_index = next(
                        (
                            index
                            for index, event in enumerate(probe.state.events)
                            if event.method == "chat.complete"
                            and str(event.params.get("turn_id") or "") == step.turn_id
                        ),
                        -1,
                    )
                    request_index = next(
                        (
                            index
                            for index, event in enumerate(probe.state.events)
                            if event is action_requested
                        ),
                        -1,
                    )
                    receipt_index = next(
                        (
                            index
                            for index, event in enumerate(probe.state.events)
                            if event is receipt_event
                        ),
                        -1,
                    )
                    complete_event = (
                        probe.state.events[complete_index]
                        if complete_index >= 0
                        else None
                    )
                    complete_params = (
                        complete_event.params
                        if complete_event is not None
                        else {}
                    )
                    step.checks["b2_action_request_path"] = bool(
                        action_requested.params.get("decision_path") == "b2"
                        and candidate_id
                    )
                    step.checks["b2_candidate_receipt_linked"] = bool(
                        proposal_id
                        and proposal_id.startswith("b2f:")
                        and candidate_id in proposal_id
                        and str(receipt.get("proposal_id") or "") == proposal_id
                        and str(complete_params.get("candidate_id") or "")
                        == candidate_id
                        and str(complete_params.get("proposal_id") or "")
                        == proposal_id
                        and str(complete_params.get("action_id") or "") == action_id
                    )
                    step.checks["b2_receipt_precedes_visible_chat"] = bool(
                        request_index >= 0
                        and receipt_index > request_index
                        and complete_index > receipt_index
                        and not any(
                            event.method == "chat.token"
                            and str(event.params.get("turn_id") or "") == step.turn_id
                            for event in probe.state.events[
                                step.event_start:receipt_index
                            ]
                        )
                    )
                    delivery_deadline = time.monotonic() + min(
                        15.0,
                        float(args.auip_timeout),
                    )
                    latest_narration: dict[str, Any] = {}
                    while time.monotonic() < delivery_deadline:
                        session_after_step = await probe.request(
                            "auip.session.get",
                            {"app_session_id": app_session_id},
                            timeout=20.0,
                        )
                        candidate_narration = session_after_step.get(
                            "latest_delivered_narration"
                        )
                        latest_narration = (
                            candidate_narration
                            if isinstance(candidate_narration, dict)
                            else {}
                        )
                        if str(latest_narration.get("event_id") or "") == action_id:
                            break
                        await asyncio.sleep(0.05)
                    step.checks["b2_visible_delivery_recorded"] = bool(
                        str(latest_narration.get("event_id") or "") == action_id
                        and " ".join(
                            str(latest_narration.get("text") or "").split()
                        )
                        == " ".join(str(step.reply or "").split())
                        and float(latest_narration.get("delivered_at") or 0)
                        >= float(receipt.get("resolved_at") or 0)
                    )
                    if not args.no_tts:
                        b2_tts_start = await probe.wait_event(
                            lambda event: event.method == "tts.sentence_start",
                            after=receipt_index,
                            timeout=args.settle_timeout,
                            description="B2 receipt-following TTS sentence start",
                        )
                        b2_sentence_id = str(
                            b2_tts_start.params.get("sentence_id") or ""
                        )
                        await probe.wait_event(
                            lambda event: event.method == "tts.sentence_end"
                            and (
                                not b2_sentence_id
                                or str(event.params.get("sentence_id") or "")
                                == b2_sentence_id
                            ),
                            after=receipt_index,
                            timeout=args.settle_timeout,
                            description="B2 TTS sentence completion",
                        )
                        await _wait_output_idle(
                            probe,
                            timeout=args.settle_timeout,
                        )
                        b2_tts_start_index = next(
                            (
                                index
                                for index, event in enumerate(probe.state.events)
                                if event is b2_tts_start
                            ),
                            -1,
                        )
                        step.checks["b2_receipt_precedes_tts"] = bool(
                            b2_tts_start_index > receipt_index
                        )
                    step.event_end = len(probe.state.events)
                    failed_b2 = sorted(
                        name
                        for name, passed in step.checks.items()
                        if name.startswith("b2_") and passed is not True
                    )
                    if failed_b2:
                        raise RuntimeError(
                            "B2 foreground acceptance failed: "
                            + ", ".join(failed_b2)
                        )
                controller_oracle = scenario.get("controller_oracle")
                lease = receipt.get("controller_lease")
                lease = lease if isinstance(lease, dict) else {}
                controller_step = bool(lease.get("lease_id"))
                if controller_policy_expected or controller_step:
                    step.checks["controller_policy_action"] = bool(
                        str(receipt.get("type") or "").strip()
                        and lease.get("lease_id")
                    )
                    step.checks["host_issued_controller_lease"] = bool(
                        lease.get("lease_id")
                        and lease.get("principal") == "kurisu"
                        and lease.get("executor") == "app_controller"
                    )
                    generation = int(lease.get("generation") or 0)
                    step.checks["controller_generation_advanced"] = bool(
                        generation > 0
                        and (
                            not controller_generations
                            or generation > controller_generations[-1]
                        )
                    )
                    controller_generations.append(generation)
                    lease_id = str(lease.get("lease_id") or "")
                    if controller_lease_ids:
                        step.checks["controller_lease_replaced"] = bool(
                            lease_id and lease_id != controller_lease_ids[-1]
                        )
                    controller_lease_ids.append(lease_id)
                    expected_policy = (
                        controller_oracle.get("expected_policy")
                        if isinstance(controller_oracle, dict)
                        else None
                    )
                    expected_policy_options = (
                        controller_oracle.get("expected_policy_options")
                        if isinstance(controller_oracle, dict)
                        else None
                    )
                    raw_policy_outcomes = (
                        controller_oracle.get("expected_policy_outcomes")
                        if isinstance(controller_oracle, dict)
                        else None
                    )
                    policy_outcomes = [
                        {
                            "policy": dict(value.get("policy") or {}),
                            "instruction_relation": str(
                                value.get("instruction_relation") or ""
                            ).strip().lower(),
                        }
                        for value in (
                            raw_policy_outcomes
                            if isinstance(raw_policy_outcomes, list)
                            else []
                        )
                        if isinstance(value, dict)
                        and isinstance(value.get("policy"), dict)
                        and value.get("policy")
                    ]
                    expected_policies = (
                        [dict(value["policy"]) for value in policy_outcomes]
                        if policy_outcomes
                        else [
                            dict(value)
                            for value in (
                                expected_policy_options
                                if isinstance(expected_policy_options, list)
                                else [expected_policy]
                            )
                            if isinstance(value, dict) and value
                        ]
                    )
                    payload = receipt.get("payload")
                    effects = receipt.get("effects")
                    payload = payload if isinstance(payload, dict) else {}
                    effects = effects if isinstance(effects, dict) else {}
                    if controller_payloads:
                        step.checks["controller_policy_changed"] = bool(
                            payload != controller_payloads[-1]
                        )
                    controller_payloads.append(dict(payload))
                    if expected_policies:
                        matching_policy = next(
                            (
                                policy
                                for policy in expected_policies
                                if all(
                                    payload.get(key) == value
                                    and effects.get(key) == value
                                    for key, value in policy.items()
                                )
                            ),
                            None,
                        )
                        step.checks[
                            "controller_policy_matches_requested_semantics"
                        ] = matching_policy is not None
                        if not step.checks[
                            "controller_policy_matches_requested_semantics"
                        ]:
                            raise RuntimeError(
                                "accepted Controller policy did not carry the exact "
                                "requested application semantics"
                            )
                        if policy_outcomes:
                            matching_outcome = next(
                                (
                                    value
                                    for value in policy_outcomes
                                    if value["policy"] == matching_policy
                                ),
                                None,
                            )
                            expected_relation = str(
                                (matching_outcome or {}).get(
                                    "instruction_relation"
                                )
                                or ""
                            )
                            actual_relation = str(
                                action_requested.params.get(
                                    "instruction_relation"
                                )
                                or ""
                            ).strip().lower()
                            step.checks[
                                "controller_instruction_relation_matches_policy"
                            ] = bool(
                                expected_relation
                                and actual_relation == expected_relation
                            )
                            if not step.checks[
                                "controller_instruction_relation_matches_policy"
                            ]:
                                raise RuntimeError(
                                    "Controller policy alternative was not paired "
                                    "with the expected instruction relation"
                                )
                    if _snapshot_has_active_controller_lease(
                        controller_activation_snapshot or {},
                        lease,
                    ):
                        # Voice delivery is deliberately after the receipt and
                        # may outlive a short test lease. Preserve the pre-TTS
                        # activation fact instead of asking a later expired
                        # snapshot whether activation ever happened.
                        session_after_step = controller_activation_snapshot or {}
                    else:
                        session_after_step = await _wait_controller_status(
                            probe,
                            app_session_id=app_session_id,
                            expected_status="active",
                            timeout=args.auip_timeout,
                        )
                    controller_situation = _find_situation(
                        session_after_step.get("state"),
                        "controller/v1",
                    ) or {}
                    step.checks["controller_policy_visible"] = bool(
                        controller_situation.get("policyAction")
                        == receipt.get("type")
                        and controller_situation.get("policySummary")
                    )
                    post_policy_setup = scenario.get("post_policy_setup")
                    if step_index == 1 and isinstance(post_policy_setup, dict):
                        resumed, session_after_step = await _exercise_pre_step_setup(
                            probe=probe,
                            app_page=app_page,
                            app_session_id=app_session_id,
                            setup=post_policy_setup,
                            timeout=args.auip_timeout,
                            label="controller_scene_resume",
                        )
                        resumed_shot = (
                            run_root / "screenshots" / "app-controller-resumed.png"
                        )
                        await app_page.screenshot(
                            path=str(resumed_shot),
                            full_page=True,
                        )
                        resumed.screenshot = str(resumed_shot)
                        turns.append(resumed)
                needs_controller_effect = bool(
                    scenario.get("controller_effect_required") is True
                    or (
                        controller_step
                        and isinstance(scenario.get("scalar_oracle"), dict)
                    )
                )
                if needs_controller_effect:
                    expected_effect_lease_id = str(lease.get("lease_id") or "")
                    effect_timeout = _controller_effect_timeout(
                        args=args,
                        scenario=scenario,
                    )
                    verified_effect = await probe.wait_event(
                        lambda event: event.method == "auip.updated"
                        and isinstance(event.params.get("event"), dict)
                        and event.params["event"].get("controller_effect") is True
                        and str(event.params["event"].get("actor") or "") == "app"
                        and isinstance(
                            event.params["event"].get("controller_lease"),
                            dict,
                        )
                        and (
                            not expected_effect_lease_id
                            or str(
                                event.params["event"]["controller_lease"].get(
                                    "lease_id"
                                )
                                or ""
                            )
                            == expected_effect_lease_id
                        ),
                        after=step.event_start,
                        timeout=effect_timeout,
                        description="lease-correlated application Controller effect",
                    )
                    step.checks[
                        "lease_correlated_controller_effect_reached_host"
                    ] = bool(verified_effect)
                    session_after_step = await probe.request(
                        "auip.session.get",
                        {"app_session_id": app_session_id},
                        timeout=20.0,
                    )

                current_revision = int(session_after_step.get("revision") or 0)
                step.checks["revision_advanced"] = current_revision > previous_revision
                scalar_oracle = scenario.get("scalar_oracle")
                controller_direction = (
                    str(scalar_oracle.get("controller_policy_direction") or "")
                    if controller_step and isinstance(scalar_oracle, dict)
                    else ""
                )
                scalar_checks = _scalar_transition_checks(
                    scenario=scenario,
                    before_state=state_before_step,
                    after_state=session_after_step.get("state"),
                    action_type=str(receipt.get("type") or ""),
                    direction_override=controller_direction,
                )
                step.checks.update(scalar_checks)
                if scalar_checks and not all(scalar_checks.values()):
                    failed = sorted(
                        name for name, passed in scalar_checks.items() if not passed
                    )
                    step.notes.append("scalar oracle failed: " + ", ".join(failed))
                    raise RuntimeError(
                        "accepted scalar action violated the live semantic oracle: "
                        + ", ".join(failed)
                    )
                previous_revision = current_revision
                step.event_end = len(probe.state.events)

            if controller_soak_seconds:
                soak, session_after_step, soak_summary = (
                    await _exercise_controller_soak(
                        probe=probe,
                        app_page=app_page,
                        app_session_id=app_session_id,
                        duration_s=controller_soak_seconds,
                        interval_s=float(args.controller_soak_interval),
                        oracle=dict(scenario["controller_soak_oracle"]),
                        screenshot_root=run_root / "screenshots",
                    )
                )
                turns.append(soak)
                report["controller_soak"] = soak_summary

            if args.complete_gomoku_round:
                round_turns, session_after_step, round_summary = (
                    await _play_complete_gomoku_round(
                        product=product,
                        probe=probe,
                        app_page=app_page,
                        app_session_id=app_session_id,
                        initial_session=session_after_step,
                        timeout=args.auip_timeout,
                        require_b2=True,
                    )
                )
                turns.extend(round_turns)
                report["gomoku_round"] = round_summary
                if not args.no_tts:
                    important_round_event_ids = {
                        str(event.params["event"].get("event_id") or "")
                        for event in probe.state.events
                        if event.method == "auip.updated"
                        and isinstance(event.params.get("event"), dict)
                        and int(event.params["event"].get("revision") or 0)
                        == int(session_after_step.get("revision") or 0)
                        and (
                            event.params["event"].get("terminal") is True
                            or str(
                                event.params["event"].get("importance") or ""
                            ).strip().lower()
                            in {"important", "blocking"}
                        )
                    }
                    report["gomoku_round"]["outcome_narration"] = (
                        await _wait_auip_narration_for_events(
                            probe,
                            app_session_id=app_session_id,
                            event_ids=important_round_event_ids,
                            timeout=args.settle_timeout,
                        )
                    )
                    await _wait_output_idle(
                        probe,
                        timeout=args.settle_timeout,
                    )
                if args.exercise_gomoku_post_round:
                    lifecycle_turns, session_after_step, lifecycle_summary = (
                        await _exercise_gomoku_post_round_lifecycle(
                            product=product,
                            probe=probe,
                            app_session_id=app_session_id,
                            timeout=args.auip_timeout,
                            chat_timeout=args.chat_timeout,
                            settle_timeout=args.settle_timeout,
                            no_tts=args.no_tts,
                        )
                    )
                    turns.extend(lifecycle_turns)
                    report["gomoku_post_round"] = lifecycle_summary

            controller_oracle = scenario.get("controller_oracle")
            if isinstance(controller_oracle, dict):
                urgent, session_after_step = await _exercise_controller_urgent_response(
                    probe=probe,
                    app_page=app_page,
                    app_session_id=app_session_id,
                    scenario=scenario,
                    timeout=args.auip_timeout,
                    controller_event_after=step.event_start,
                )
                turns.append(urgent)
            if args.exercise_controller_expiry:
                expiry = TurnEvidence(
                    label="controller_expiry",
                    text="Host test wait: let the accepted Controller lease expire.",
                    event_start=len(probe.state.events),
                )
                session_after_step = await _wait_controller_expiry(
                    probe=probe,
                    app_session_id=app_session_id,
                    state_expectations=(
                        scenario.get("takeover_state_expectations")
                        if isinstance(
                            scenario.get("takeover_state_expectations"), dict
                        )
                        else {}
                    ),
                    timeout=min(float(args.auip_timeout), 30.0),
                )
                expiry.checks["controller_expired"] = (
                    str((session_after_step.get("controller") or {}).get("status") or "")
                    == "idle"
                    and str((session_after_step.get("controller") or {}).get("reason") or "")
                    == "expired"
                )
                expiry.checks["collaboration_remained_active"] = (
                    str(session_after_step.get("engagement_mode") or "").lower()
                    == "collaborate"
                )
                expiry.checks["sustained_intent_cleared"] = all(
                    _nested_state_fact_matches(
                        session_after_step.get("state"), key, value
                    )
                    for key, value in (
                        scenario.get("takeover_state_expectations") or {}
                    ).items()
                )
                expiry.event_end = len(probe.state.events)
                turns.append(expiry)
                if args.renew_controller_after_expiry:
                    previous_lease_id = (
                        controller_lease_ids[-1] if controller_lease_ids else ""
                    )
                    previous_generation = (
                        controller_generations[-1] if controller_generations else 0
                    )
                    previous_payload = (
                        controller_payloads[-1] if controller_payloads else {}
                    )
                    renewal = await _send_ui_turn(
                        product,
                        probe,
                        label="controller_renewal_after_expiry",
                        text=(
                            scenario_steps[1]
                            if len(scenario_steps) > 1
                            else "继续接管，换一个适合当前局面的策略。"
                        ),
                        chat_timeout=args.chat_timeout,
                    )
                    renewal_request = await probe.wait_event(
                        lambda event: event.method == "auip.action.requested"
                        or _operator_failure_from_update(event) is not None,
                        after=renewal.event_start,
                        timeout=args.auip_timeout,
                        description=(
                            "Controller policy request or terminal operator outcome "
                            "after natural expiry"
                        ),
                    )
                    renewal_failure = _operator_failure_from_update(renewal_request)
                    if renewal_failure is not None:
                        renewal.checks["application_action_requested"] = False
                        renewal.checks["operator_failed_closed"] = True
                        renewal.notes.append(
                            "terminal AUIP operator outcome: "
                            + json.dumps(renewal_failure, ensure_ascii=False)
                        )
                        renewal.event_end = len(probe.state.events)
                        turns.append(renewal)
                        reason = (
                            renewal_failure["reason"]
                            or renewal_failure["detail"]
                            or "no application action was requested"
                        )
                        raise RuntimeError(
                            "AUIP operator blocked before Controller renewal request: "
                            f"{renewal_failure['code']}: {reason}"
                        )
                    renewal_action_id = str(
                        renewal_request.params.get("action_id")
                        or (
                            (renewal_request.params.get("action") or {}).get(
                                "action_id"
                            )
                            if isinstance(
                                renewal_request.params.get("action"), dict
                            )
                            else ""
                        )
                    )
                    renewal_receipt_event = await probe.wait_event(
                        lambda event: event.method == "auip.updated"
                        and isinstance(event.params.get("receipt"), dict)
                        and (
                            not renewal_action_id
                            or str(
                                event.params["receipt"].get("action_id") or ""
                            )
                            == renewal_action_id
                        ),
                        after=renewal.event_start,
                        timeout=args.auip_timeout,
                        description="accepted Controller policy after natural expiry",
                    )
                    renewal_receipt = dict(
                        renewal_receipt_event.params.get("receipt") or {}
                    )
                    renewal.checks["accepted_receipt"] = (
                        renewal_receipt.get("accepted") is True
                    )
                    renewal_lease = renewal_receipt.get("controller_lease")
                    renewal_lease = (
                        renewal_lease if isinstance(renewal_lease, dict) else {}
                    )
                    renewal_lease_id = str(
                        renewal_lease.get("lease_id") or ""
                    )
                    renewal_generation = int(
                        renewal_lease.get("generation") or 0
                    )
                    renewal_payload = renewal_receipt.get("payload")
                    renewal_payload = (
                        renewal_payload
                        if isinstance(renewal_payload, dict)
                        else {}
                    )
                    renewal.checks["new_host_lease_issued"] = bool(
                        renewal_lease_id
                        and renewal_lease_id != previous_lease_id
                        and renewal_lease.get("principal") == "kurisu"
                        and renewal_lease.get("executor") == "app_controller"
                    )
                    renewal.checks["controller_generation_advanced"] = (
                        renewal_generation > previous_generation
                    )
                    # Expiry ends authority, not the usefulness of a strategy.
                    # Re-authorizing the same exact policy is valid; only a
                    # scenario that explicitly requires a semantic switch may
                    # treat payload equality as a failure.
                    if scenario.get("renewal_policy_change_required") is True:
                        renewal.checks["policy_changed_after_expiry"] = bool(
                            renewal_payload != previous_payload
                        )
                    if not renewal.checks["accepted_receipt"]:
                        raise RuntimeError(
                            "Controller policy was rejected after natural expiry"
                        )
                    session_after_step = await _wait_controller_status(
                        probe,
                        app_session_id=app_session_id,
                        expected_status="active",
                        timeout=args.auip_timeout,
                    )
                    renewal_situation = _find_situation(
                        session_after_step.get("state"), "controller/v1"
                    ) or {}
                    renewal.checks["new_policy_visible_and_active"] = bool(
                        renewal_situation.get("status") == "active"
                        and renewal_situation.get("policyAction")
                        == renewal_receipt.get("type")
                        and renewal_situation.get("policySummary")
                    )
                    renewal.checks["collaboration_remained_active"] = (
                        str(
                            session_after_step.get("engagement_mode") or ""
                        ).lower()
                        == "collaborate"
                    )
                    if scenario.get("controller_effect_required") is True:
                        renewal_activity_expectations = (
                            scenario.get("takeover_state_expectations")
                            if isinstance(
                                scenario.get("takeover_state_expectations"),
                                dict,
                            )
                            else {}
                        )

                        def _is_renewed_controller_activity(
                            event: EventRecord,
                        ) -> bool:
                            if event.method != "auip.updated":
                                return False
                            event_fact = event.params.get("event")
                            if isinstance(event_fact, dict):
                                event_lease = event_fact.get("controller_lease")
                                if (
                                    event_fact.get("controller_effect") is True
                                    and isinstance(event_lease, dict)
                                    and str(event_lease.get("lease_id") or "")
                                    == renewal_lease_id
                                ):
                                    return True
                            host_controller = event.params.get("controller")
                            host_controller = (
                                host_controller
                                if isinstance(host_controller, dict)
                                else {}
                            )
                            host_lease = host_controller.get("lease")
                            host_lease = (
                                host_lease if isinstance(host_lease, dict) else {}
                            )
                            if (
                                str(host_controller.get("status") or "").lower()
                                != "active"
                                or str(host_lease.get("lease_id") or "")
                                != renewal_lease_id
                                or not renewal_activity_expectations
                            ):
                                return False
                            state = event.params.get("state")
                            return any(
                                not _nested_state_fact_matches(state, key, value)
                                for key, value in renewal_activity_expectations.items()
                            )

                        renewed_activity = await probe.wait_event(
                            _is_renewed_controller_activity,
                            after=renewal.event_start,
                            timeout=min(float(args.auip_timeout), 20.0),
                            description=(
                                "lease-correlated Controller activity after expiry"
                            ),
                        )
                        renewal.checks[
                            "new_lease_reached_application_mechanics"
                        ] = bool(renewed_activity)
                        activity_fact = renewed_activity.params.get("event")
                        renewal.notes.append(
                            "renewal mechanical evidence="
                            + (
                                "controller_effect"
                                if isinstance(activity_fact, dict)
                                and activity_fact.get("controller_effect") is True
                                else "sustained_intent"
                            )
                        )
                    if not args.no_tts:
                        renewal_tts_start = await probe.wait_event(
                            lambda event: event.method == "tts.sentence_start",
                            after=renewal.event_start,
                            timeout=args.settle_timeout,
                            description="renewed Controller B2 TTS sentence start",
                        )
                        renewal_sentence_id = str(
                            renewal_tts_start.params.get("sentence_id") or ""
                        )
                        await probe.wait_event(
                            lambda event: event.method == "tts.sentence_end"
                            and (
                                not renewal_sentence_id
                                or str(event.params.get("sentence_id") or "")
                                == renewal_sentence_id
                            ),
                            after=renewal.event_start,
                            timeout=args.settle_timeout,
                            description="renewed Controller B2 TTS sentence completion",
                        )
                        await _wait_output_idle(
                            probe,
                            timeout=args.settle_timeout,
                        )
                        renewal.checks["b2_tts_completed_before_next_turn"] = True
                    controller_lease_ids.append(renewal_lease_id)
                    controller_generations.append(renewal_generation)
                    controller_payloads.append(dict(renewal_payload))
                    renewal.notes.append(
                        json.dumps(
                            _safe_excerpt(renewal_receipt, 1200),
                            ensure_ascii=False,
                        )
                    )
                    renewal.event_end = len(probe.state.events)
                    turns.append(renewal)
            elif isinstance(controller_oracle, dict) or scenario.get(
                "controller_takeover_required"
            ) is True:
                takeover = await _send_ui_turn(
                    product,
                    probe,
                    label="controller_takeover",
                    text="你先别操作，我自己来。",
                    chat_timeout=args.chat_timeout,
                )
                await probe.wait_event(
                    lambda event: event.method == "auip.updated"
                    and str(event.params.get("app_session_id") or "")
                    == app_session_id
                    and str(event.params.get("engagement_mode") or "").lower()
                    == "observe",
                    after=takeover.event_start,
                    timeout=args.auip_timeout,
                    description="Controller observe-mode takeover",
                )
                session_after_step = await _wait_controller_status(
                    probe,
                    app_session_id=app_session_id,
                    expected_status="idle",
                    timeout=args.auip_timeout,
                )
                takeover.checks["observe_mode_active"] = (
                    str(session_after_step.get("engagement_mode") or "").lower()
                    == "observe"
                )
                takeover.checks["controller_revoked"] = (
                    str((session_after_step.get("controller") or {}).get("status") or "")
                    == "idle"
                )
                takeover_expectations = scenario.get("takeover_state_expectations")
                takeover_expectations = (
                    takeover_expectations
                    if isinstance(takeover_expectations, dict)
                    else {}
                )
                if takeover_expectations:
                    takeover_state = session_after_step.get("state")
                    takeover.checks["sustained_intent_cleared"] = all(
                        _nested_state_fact_matches(takeover_state, key, value)
                        for key, value in takeover_expectations.items()
                    )
                takeover.event_end = len(probe.state.events)
                turns.append(takeover)

            revision_after_step = int(session_after_step.get("revision") or 0)
            query = await _send_ui_turn(
                product,
                probe,
                label="query",
                text=str(
                    scenario.get("query")
                    or "刚才真的操作了吗？现在是什么状态？"
                ),
                chat_timeout=args.chat_timeout,
            )
            await asyncio.sleep(min(5.0, args.dispatch_timeout))
            session_after_query = await probe.request(
                "auip.session.get",
                {"app_session_id": app_session_id},
                timeout=20.0,
            )
            query_revision = int(session_after_query.get("revision") or 0)
            if scenario.get("ambient_state_advances") is True:
                query.checks["ambient_revision_not_regressed"] = (
                    query_revision >= revision_after_step
                )
            else:
                query.checks["revision_unchanged"] = (
                    query_revision == revision_after_step
                )
            if str(scenario["expected_situation_kind"]) == "sequence/v1":
                query.checks["state_answer_grounded"] = _sequence_query_grounded(
                    session_after_step.get("state"),
                    query.reply,
                )
            else:
                grounding_states = [session_after_query.get("state")]
                if scenario.get("ambient_state_advances") is True:
                    # The role answers the Host snapshot visible during its
                    # turn. A continuously running app may advance again (or
                    # even reach game over) before this post-turn probe. Judge
                    # against authoritative snapshots observed during the
                    # query window, not one later future state.
                    grounding_states = [session_after_step.get("state")]
                    grounding_states.extend(
                        event.params.get("state")
                        for event in probe.state.events[query.event_start :]
                        if event.method == "auip.updated"
                        and isinstance(event.params.get("state"), dict)
                    )
                    grounding_states.append(session_after_query.get("state"))
                grounded = _query_grounded_across_states(
                    scenario=scenario,
                    states=grounding_states,
                    reply=query.reply,
                )
                if grounded is not None:
                    query.checks["state_answer_grounded"] = grounded
            query.event_end = len(probe.state.events)
            turns.append(query)

            if args.exercise_active_amendment:
                if str(session_after_query.get("status") or "").strip().lower() != "active":
                    raise RuntimeError(
                        "active amendment precondition failed: the AppSession "
                        "completed or closed before the amendment turn"
                    )
                active_session = session_after_query
                old_app_session_id = app_session_id
                old_artifact_ref = str(active_session.get("artifact_ref") or "")
                with WorkLedgerStore(
                    run_root / "state" / "work_ledger.sqlite3"
                ) as ledger:
                    amendment_attempt_ids_before = {
                        attempt.attempt_id
                        for attempt in ledger.list_attempts(seeded_work_item_id)
                    }
                amendment = await _send_ui_turn(
                    product,
                    probe,
                    label="active_app_amendment",
                    text=(
                        "把标题改成‘反应堆实验台’，改好后重新打开，"
                        "我们继续。"
                    ),
                    chat_timeout=args.chat_timeout,
                )
                created_amendment = await _wait_created_run(
                    probe,
                    after=amendment.event_start,
                    timeout=args.dispatch_timeout,
                )
                amendment.run_ids = [_event_run_id(created_amendment)]
                created_metadata = created_amendment.params.get("metadata")
                created_metadata = (
                    created_metadata if isinstance(created_metadata, dict) else {}
                )
                created_payload = created_amendment.params.get("payload")
                created_payload = (
                    created_payload if isinstance(created_payload, dict) else {}
                )
                amendment.checks["same_work_item"] = bool(
                    seeded_work_item_id
                    and _event_work_item_id(created_amendment)
                    == seeded_work_item_id
                )
                amendment.checks["amend_intent"] = (
                    str(
                        created_metadata.get("intent")
                        or created_payload.get("intent")
                        or ""
                    ).strip().lower()
                    == "amend"
                )
                amendment_terminal, amendment_item, amendment_run_ids = (
                    await _wait_provider_chain_terminal(
                        product=product,
                        probe=probe,
                        initial_created=created_amendment,
                        after=amendment.event_start,
                        run_root=run_root,
                        timeout=args.provider_timeout,
                        seen_permissions=seen_permissions,
                        permission_evidence=permission_evidence,
                    )
                )
                amendment.run_ids = amendment_run_ids
                amendment.checks["provider_succeeded"] = (
                    _provider_status(amendment_terminal) in SUCCESS_STATUSES
                    and str(amendment_item.get("execution") or "").strip().lower()
                    not in {"failed", "cancelled"}
                )
                with WorkLedgerStore(
                    run_root / "state" / "work_ledger.sqlite3"
                ) as ledger:
                    amendment_attempts_after = ledger.list_attempts(
                        seeded_work_item_id
                    )
                new_amendment_attempts = [
                    attempt
                    for attempt in amendment_attempts_after
                    if attempt.attempt_id not in amendment_attempt_ids_before
                ]
                amendment.checks["single_amendment_attempt"] = bool(
                    len(new_amendment_attempts) == 1
                    and str(new_amendment_attempts[0].provider_run_id or "")
                    in set(amendment.run_ids)
                )
                old_closed = await probe.wait_event(
                    lambda event: event.method == "auip.updated"
                    and str(event.params.get("app_session_id") or "")
                    == old_app_session_id
                    and str(event.params.get("status") or "").strip().lower()
                    == "closed",
                    after=amendment.event_start,
                    timeout=args.auip_timeout,
                    description="old AppSession closed for active-app replacement",
                )
                amendment.checks["old_app_session_closed"] = bool(old_closed)
                replacement_active = await probe.wait_event(
                    lambda event: event.method == "auip.updated"
                    and str(event.params.get("status") or "").strip().lower()
                    == "active"
                    and bool(str(event.params.get("app_session_id") or ""))
                    and str(event.params.get("app_session_id") or "")
                    != old_app_session_id,
                    after=amendment.event_start,
                    timeout=args.auip_timeout,
                    description="amended artifact attached as a fresh AppSession",
                )
                app_session_id = str(
                    replacement_active.params.get("app_session_id") or ""
                )
                active_session = await _wait_situation_kind(
                    probe,
                    app_session_id=app_session_id,
                    expected_kind=str(scenario["expected_situation_kind"]),
                    timeout=args.auip_timeout,
                )
                session_after_step = active_session
                amendment.checks["new_app_session_active"] = bool(
                    app_session_id and app_session_id != old_app_session_id
                )
                amendment.checks["new_artifact_revision_attached"] = bool(
                    str(active_session.get("artifact_ref") or "")
                    and str(active_session.get("artifact_ref") or "")
                    != old_artifact_ref
                )
                amendment.checks["engagement_mode_preserved"] = (
                    str(active_session.get("engagement_mode") or "").strip().lower()
                    == engagement_mode
                )
                current_work = await probe.request("work.list", {}, timeout=20.0)
                current_projection = _work_projection(current_work)
                current_items = [
                    item
                    for item in current_projection.get("items") or []
                    if isinstance(item, dict)
                    and str(item.get("workItemId") or item.get("id") or "")
                    == seeded_work_item_id
                ]
                amendment.checks["single_work_item_lineage"] = bool(
                    len(current_items) == 1
                    and str(current_items[0].get("attemptId") or "")
                    != str((report.get("seed") or {}).get("attempt_id") or "")
                    and str(current_items[0].get("operationIntent") or "")
                    == "amend"
                    and str(current_items[0].get("workspacePath") or "")
                    == str((report.get("seed") or {}).get("workspace") or "")
                )
                app_page = await _wait_app_page(product, timeout=args.auip_timeout)
                product._instrument_app_page(app_page)
                amended_shot = run_root / "screenshots" / "auip-amended-reattached.png"
                await app_page.screenshot(path=str(amended_shot), full_page=True)
                amendment.screenshot = str(amended_shot)
                amendment.event_end = len(probe.state.events)
                turns.append(amendment)
                failed_amendment = sorted(
                    name
                    for name, passed in amendment.checks.items()
                    if passed is not True
                )
                if failed_amendment:
                    raise RuntimeError(
                        "active AppSession amendment failed: "
                        + ", ".join(failed_amendment)
                    )

            if args.exercise_reset:
                reset = await _send_ui_turn(
                    product,
                    probe,
                    label="reset",
                    text="现在请执行重置，把应用恢复到初始状态。",
                    chat_timeout=args.chat_timeout,
                )
                reset_action = await probe.wait_event(
                    lambda event: event.method == "auip.action.requested",
                    after=reset.event_start,
                    timeout=args.auip_timeout,
                    description="live AUIP reset action request",
                )
                reset_action_id = str(
                    reset_action.params.get("action_id")
                    or (
                        (reset_action.params.get("action") or {}).get("action_id")
                        if isinstance(reset_action.params.get("action"), dict)
                        else ""
                    )
                )
                reset_receipt_event = await probe.wait_event(
                    lambda event: event.method == "auip.updated"
                    and isinstance(event.params.get("receipt"), dict)
                    and (
                        not reset_action_id
                        or str(event.params["receipt"].get("action_id") or "")
                        == reset_action_id
                    ),
                    after=reset.event_start,
                    timeout=args.auip_timeout,
                    description="live AUIP reset accepted receipt",
                )
                reset_receipt = dict(reset_receipt_event.params.get("receipt") or {})
                reset.checks["accepted_receipt"] = reset_receipt.get("accepted") is True
                reset.notes.append(
                    json.dumps(_safe_excerpt(reset_receipt, 1200), ensure_ascii=False)
                )
                reset_session = await probe.request(
                    "auip.session.get",
                    {"app_session_id": app_session_id},
                    timeout=20.0,
                )
                reset_situation = _find_situation(
                    _receipt_bound_state(
                        reset_receipt_event.params,
                        reset_session,
                    ),
                    str(scenario["expected_situation_kind"]),
                )
                reset.checks["initial_situation_restored"] = bool(
                    initial_situation is not None
                    and reset_situation == initial_situation
                )
                reset.event_end = len(probe.state.events)
                turns.append(reset)

            leave = await _send_ui_turn(
                product,
                probe,
                label="leave",
                text="先到这里，把这个小游戏关掉吧。",
                chat_timeout=args.chat_timeout,
            )
            closed = await probe.wait_event(
                lambda event: event.method == "auip.updated"
                and str(event.params.get("app_session_id") or "") == app_session_id
                and str(event.params.get("status") or "").strip().lower() == "closed",
                after=leave.event_start,
                timeout=args.auip_timeout,
                description="closed live AUIP AppSession",
            )
            leave.checks["app_session_closed"] = bool(closed)
            leave.event_end = len(probe.state.events)
            turns.append(leave)

            if args.exercise_post_leave_chat:
                post_leave_chat = await _send_ui_turn(
                    product,
                    probe,
                    label="post_leave_chat",
                    text="顺便聊点别的。你现在感觉怎么样？",
                    chat_timeout=args.chat_timeout,
                )
                await _wait_output_idle(probe, timeout=args.settle_timeout)
                post_leave_chat.event_end = len(probe.state.events)
                turns.append(post_leave_chat)

            final_work = await probe.request("work.list", {}, timeout=20.0)
            report["final_work"] = _safe_excerpt(final_work, 8000)
            final_auip_session = await probe.request(
                "auip.session.get",
                {"app_session_id": app_session_id},
                timeout=20.0,
            )
            report["final_auip_session"] = _safe_excerpt(
                final_auip_session,
                5000,
            )
            b2_evidence_session = final_auip_session
            if args.exercise_active_amendment and initial_app_session_id != app_session_id:
                b2_evidence_session = await probe.request(
                    "auip.session.get",
                    {"app_session_id": initial_app_session_id},
                    timeout=20.0,
                )
                report["replaced_auip_session"] = _safe_excerpt(
                    b2_evidence_session,
                    5000,
                )
            report["paths"]["final_screenshot"] = str(
                await product.screenshot("final")
            )

            events = list(probe.state.events)
            _populate_turn_timings(turns, events)
            review = _semantic_review(
                turns=turns,
                events=events,
                permissions=permission_evidence,
                expected_identity=identity,
                runtime_identity=runtime_identity,
                tts_required=not args.no_tts,
                journey_layer=journey_layer,
                engagement_mode=engagement_mode,
                expected_explicit_steps=explicit_steps,
                expected_human_steps=human_steps,
                expect_delegate_reactions=bool(args.expect_delegate_reactions),
            )
            final_projection = _work_projection(final_work)
            final_items = [
                item
                for item in final_projection.get("items") or []
                if isinstance(item, dict)
            ]
            final_permissions_settled = all(
                int(item.get("pendingPermissionCount") or 0) == 0
                and str(item.get("attention") or "").strip().lower() != "permission"
                for item in final_items
            )
            review["checks"]["final_work_has_no_pending_permissions"] = (
                final_permissions_settled
            )
            if not final_permissions_settled:
                review["status"] = "failed"
                review["failures"].append(
                    "the Journey ended with a pending Work permission"
                )
            report.update(
                {
                    "status": review["status"],
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "turns": [turn.to_dict() for turn in turns],
                    "permissions": permission_evidence,
                    "review": review,
                    "events": [
                        _compact_event(event, source_index=index)
                        for index, event in enumerate(events)
                        if event.method in EVIDENCE_METHODS
                    ],
                }
            )
            checks = {
                **review["checks"],
                "prepare_became_active_auip": prepare.checks.get("auip_active")
                is True,
                "prepare_business_outcome_verified": prepare.checks.get(
                    "business_outcome_verified"
                )
                is True,
                "query_respected_revision_contract": (
                    query.checks.get("ambient_revision_not_regressed") is True
                    if scenario.get("ambient_state_advances") is True
                    else query.checks.get("revision_unchanged") is True
                ),
                "app_surface_has_no_uncaught_errors": not product.app_page_errors,
            }
            if "state_answer_grounded" in query.checks:
                checks["query_answer_grounded_in_state"] = (
                    query.checks.get("state_answer_grounded") is True
                )
            report["app_surface_diagnostics"] = product.app_diagnostics()
            if explicit_steps:
                explicit_turns = [
                    item
                    for item in turns
                    if item.label == "step" or item.label.startswith("step_")
                ]
                checks["all_explicit_steps_received_accepted_receipts"] = (
                    len(explicit_turns) == explicit_steps
                    and all(
                        item.checks.get("accepted_receipt") is True
                        for item in explicit_turns
                    )
                )
                if args.require_b2:
                    b2_checks = [
                        value
                        for item in explicit_turns
                        for name, value in item.checks.items()
                        if name.startswith("b2_")
                    ]
                    capsule = (
                        b2_evidence_session.get("experience_capsule")
                        if isinstance(
                            b2_evidence_session.get("experience_capsule"),
                            dict,
                        )
                        else {}
                    )
                    role_capsule = (
                        capsule.get("role_branch")
                        if isinstance(capsule.get("role_branch"), dict)
                        else {}
                    )
                    checks["b2_foreground_contract_passed"] = bool(
                        b2_checks and all(value is True for value in b2_checks)
                    )
                    checks["b2_branch_collapsed_with_verified_action"] = bool(
                        role_capsule.get("kind")
                        == "auip_appsession_branch_capsule/v1"
                        and role_capsule.get("verified_actions")
                    )
                if controller_policy_expected and explicit_steps > 1:
                    replacements = [
                        item
                        for item in explicit_turns[1:]
                        if item.checks.get("controller_lease_replaced") is True
                        and item.checks.get("controller_policy_changed") is True
                    ]
                    checks["controller_policy_replacements_verified"] = (
                        len(replacements) == explicit_steps - 1
                    )
            if args.exercise_active_amendment:
                amendment_turn = next(
                    (
                        item
                        for item in turns
                        if item.label == "active_app_amendment"
                    ),
                    None,
                )
                checks["active_app_amendment_reused_work_and_reattached"] = bool(
                    amendment_turn
                    and amendment_turn.checks
                    and all(amendment_turn.checks.values())
                )
            if isinstance(controller_oracle, dict):
                controller_event = next(
                    (
                        item
                        for item in turns
                        if item.label == "controller_urgent_event"
                    ),
                    None,
                )
                controller_takeover = next(
                    (
                        item
                        for item in turns
                        if item.label == "controller_takeover"
                    ),
                    None,
                )
                checks["controller_urgent_response_verified"] = bool(
                    controller_event
                    and all(controller_event.checks.values())
                )
                if not args.exercise_controller_expiry:
                    checks["controller_observe_takeover_revoked_lease"] = bool(
                        controller_takeover
                        and controller_takeover.checks.get("observe_mode_active")
                        is True
                        and controller_takeover.checks.get("controller_revoked")
                        is True
                    )
            elif (
                scenario.get("controller_takeover_required") is True
                and not args.exercise_controller_expiry
            ):
                controller_takeover = next(
                    (
                        item
                        for item in turns
                        if item.label == "controller_takeover"
                    ),
                    None,
                )
                checks["controller_observe_takeover_revoked_lease"] = bool(
                    controller_takeover
                    and all(controller_takeover.checks.values())
                )
            if args.exercise_controller_expiry:
                controller_expiry = next(
                    (
                        item
                        for item in turns
                        if item.label == "controller_expiry"
                    ),
                    None,
                )
                checks["controller_expiry_cleared_lease_and_intent"] = bool(
                    controller_expiry and all(controller_expiry.checks.values())
                )
                if args.renew_controller_after_expiry:
                    controller_renewal = next(
                        (
                            item
                            for item in turns
                            if item.label == "controller_renewal_after_expiry"
                        ),
                        None,
                    )
                    checks["controller_reactivated_after_expiry"] = bool(
                        controller_renewal
                        and all(controller_renewal.checks.values())
                    )
            if controller_soak_seconds:
                controller_soak = next(
                    (
                        item for item in turns if item.label == "controller_soak"
                    ),
                    None,
                )
                checks["controller_soak_verified"] = bool(
                    controller_soak and all(controller_soak.checks.values())
                )
            if args.complete_gomoku_round:
                round_summary = (
                    report.get("gomoku_round")
                    if isinstance(report.get("gomoku_round"), dict)
                    else {}
                )
                automatic_round_turns = [
                    item for item in turns if item.label.startswith("round_b2_")
                ]
                checks["gomoku_round_reached_terminal_result"] = bool(
                    round_summary.get("completed") is True
                    and round_summary.get("lifecycle") == "round_finished"
                    and str(round_summary.get("winner") or "none") != "none"
                    and int(round_summary.get("move_count") or 0) >= 5
                )
                checks["gomoku_automatic_b2_replied_without_chat_nudges"] = bool(
                    automatic_round_turns
                    and len(automatic_round_turns)
                    == int(round_summary.get("b2_turns") or 0)
                    and all(
                        item.checks.get("accepted_receipt") is True
                        and item.checks.get("b2_action_request_path") is True
                        and item.checks.get("b2_candidate_receipt_linked") is True
                        for item in automatic_round_turns
                    )
                )
                presentation_summary = _b2_automatic_presentation_summary(events)
                report["gomoku_round"]["presentation"] = presentation_summary
                if not args.no_tts:
                    routine_count = int(
                        presentation_summary.get("narrated_routine_action_count") or 0
                    )
                    automatic_count = int(
                        presentation_summary.get("automatic_action_count") or 0
                    )
                    checks["gomoku_automatic_commentary_was_sparse"] = bool(
                        automatic_count >= 2 and routine_count < automatic_count
                    )
                    checks["gomoku_round_outcome_was_narrated_once"] = bool(
                        int(presentation_summary.get("outcome_narration_count") or 0)
                        == 1
                    )
                if args.exercise_gomoku_post_round:
                    post_round = (
                        report.get("gomoku_post_round")
                        if isinstance(report.get("gomoku_post_round"), dict)
                        else {}
                    )
                    checks["gomoku_restart_round_was_accepted"] = (
                        post_round.get("restart_accepted") is True
                    )
                    checks["gomoku_restart_triggered_automatic_b2_move"] = (
                        post_round.get("automatic_first_move_accepted") is True
                    )
                    checks["gomoku_resignation_was_accepted"] = (
                        post_round.get("resignation_accepted") is True
                    )
                    checks["gomoku_experience_finished_and_collapsed"] = bool(
                        post_round.get("finish_experience_accepted") is True
                        and post_round.get("final_status") == "completed"
                        and post_round.get("final_lifecycle")
                        == "concluded"
                    )
            if journey_layer == "full":
                checks.update(
                    {
                        "creation_provider_succeeded": bool(
                            create
                            and create.checks.get("provider_succeeded") is True
                        ),
                        "creation_business_outcome_verified": bool(
                            create
                            and create.checks.get("business_outcome_verified") is True
                        ),
                        "creation_terminal_narration_delivered": bool(
                            create
                            and create.checks.get("terminal_narration_delivered")
                            is True
                        ),
                    }
                )
            elif journey_layer == "adaptation":
                prepare_created = [
                    event
                    for event in _run_created_events(events[prepare.event_start :])
                    if _event_run_id(event) in set(prepare.run_ids)
                ]
                checks["adaptation_provider_succeeded"] = (
                    prepare.checks.get("provider_succeeded") is True
                    and (
                        len(prepare.run_ids) == 1
                        or is_bounded_progress_recovery_chain(prepare_created)
                    )
                )
            else:
                checks["interaction_launch_started_no_provider"] = not prepare.run_ids
            failed_check_names = sorted(
                name for name, passed in checks.items() if passed is not True
            )
            review["checks"] = checks
            review["status"] = "failed" if failed_check_names else "passed"
            if failed_check_names:
                existing_failures = list(review.get("failures") or [])
                existing_text = "\n".join(str(item) for item in existing_failures)
                existing_failures.extend(
                    f"acceptance check failed: {name}"
                    for name in failed_check_names
                    if name not in existing_text
                )
                review["failures"] = existing_failures
            else:
                review["failures"] = []
            report["review"] = review
            report["status"] = review["status"]
            report["semantic_evidence"] = build_evidence(
                root=ROOT,
                journey_id="J7",
                status=report["status"],
                # A no-TTS run is still the shipping Electron/backend path,
                # but it cannot claim the audible half of L4 acceptance.
                test_level="L3" if args.no_tts else "L4",
                provider="codex",
                model=str(args.model),
                report_path=report_path,
                isolation_root=run_root,
                checks=checks,
                started_at=report["started_at"],
                finished_at=report["finished_at"],
                ledger_ids={"app_session_id": app_session_id},
                manual_acceptance="pending",
                notes=(
                    "Electron started the normal backend and rendered every Chat turn.",
                    f"Journey began at the declared {journey_layer} boundary.",
                    "AI/human review still owns audible naturalness and visual polish.",
                ),
            )
            exit_code = 0 if report["status"] == "passed" else 1
    except Exception as exc:
        if product is not None:
            report["app_surface_diagnostics"] = product.app_diagnostics()
        report.update(
            {
                "status": "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
                "error_traceback": traceback.format_exc(),
                "turns": [turn.to_dict() for turn in turns],
                "permissions": permission_evidence,
                "events": (
                    [
                        _compact_event(event, source_index=index)
                        for index, event in enumerate(probe.state.events)
                        if event.method in EVIDENCE_METHODS
                    ]
                    if probe is not None
                    else []
                ),
            }
        )
    finally:
        finalizer = asyncio.create_task(
            _finalize_product_run(product, report, report_path),
            name="live-product-finalizer",
        )
        try:
            await asyncio.shield(finalizer)
        except asyncio.CancelledError:
            # Ctrl+C cancels the Journey task.  Cleanup remains a separate
            # task so Electron and its backend cannot outlive the isolated
            # run merely because the first await inherited that cancellation.
            await finalizer
            raise
    return exit_code, report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="lights")
    parser.add_argument(
        "--journey-layer",
        choices=sorted(JOURNEY_LAYERS),
        default="full",
        help=(
            "full creates the app; adaptation seeds standalone HTML but rebuilds AUIP; "
            "interaction seeds a complete AUIP bundle but starts a fresh AppSession."
        ),
    )
    parser.add_argument(
        "--seed",
        default="",
        help=(
            "Verified standalone HTML for adaptation, or verified AUIP bundle "
            "directory for interaction. Invalid for full Journeys."
        ),
    )
    parser.add_argument(
        "--natural-adaptation-request",
        action="store_true",
        help=(
            "Use the realistic short user turn '请你接入它。' and require the "
            "execution agent to derive the AUIP shape from the current artifact. "
            "Adaptation Journeys only."
        ),
    )
    parser.add_argument(
        "--report-dir",
        default=str(RUNTIME / "live_product_journeys"),
    )
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument(
        "--interaction-steps",
        type=int,
        default=1,
        help="Number of consecutive accepted AUIP actions before the state query.",
    )
    parser.add_argument(
        "--controller-lease-ms",
        type=int,
        default=0,
        help=(
            "Interaction-test-only lease duration override applied to the isolated "
            "seed bundle and synchronized embedded manifest."
        ),
    )
    parser.add_argument(
        "--controller-soak-seconds",
        type=float,
        default=0.0,
        help=(
            "After one accepted Controller policy, issue no chat turns and "
            "sample sustained app-local behavior for this duration."
        ),
    )
    parser.add_argument(
        "--controller-soak-interval",
        type=float,
        default=2.0,
        help="Host-state sample interval used by --controller-soak-seconds.",
    )
    parser.add_argument(
        "--controller-effect-timeout",
        type=float,
        default=0.0,
        help=(
            "Override how long to wait for the first lease-correlated real "
            "Controller effect. Zero uses the scenario response horizon."
        ),
    )
    parser.add_argument(
        "--exercise-controller-expiry",
        action="store_true",
        help=(
            "After the final policy effect, wait for natural lease expiry and require "
            "the app to clear sustained intent without leaving collaborate mode."
        ),
    )
    parser.add_argument(
        "--renew-controller-after-expiry",
        action="store_true",
        help=(
            "After the deliberate expiry check, send one new short policy turn "
            "and require a new generation, lease, and application effect."
        ),
    )
    parser.add_argument(
        "--refresh-host-runtime-assets",
        action="store_true",
        help=(
            "Compatibility spelling retained for existing commands. Live "
            "interaction Journeys now always replace and verify Host-owned SDK "
            "sidecars in the isolated copy."
        ),
    )
    parser.add_argument(
        "--engagement-mode",
        choices=sorted(ENGAGEMENT_MODES),
        default="collaborate",
        help="Host-owned AppSession authority mode exercised by the Journey.",
    )
    parser.add_argument(
        "--human-steps",
        type=int,
        default=0,
        help=(
            "Number of local player UI actions interleaved with Participant behavior. "
            "Scenario UI instrumentation is test-only and never enters the Host."
        ),
    )
    parser.add_argument(
        "--step-text",
        default="",
        help=(
            "Override the scenario's default explicit application step with one "
            "human-short utterance. This changes test input only."
        ),
    )
    parser.add_argument(
        "--expect-delegate-reactions",
        action="store_true",
        help=(
            "Require one automatic Participant action after each local player action. "
            "Use only when the app declares a stable participantOpportunity for that beat."
        ),
    )
    parser.add_argument(
        "--exercise-reset",
        action="store_true",
        help="After the query, reset the app and compare its situation with launch state.",
    )
    parser.add_argument(
        "--exercise-active-amendment",
        action="store_true",
        help=(
            "Interaction-test-only: amend the focused application's existing "
            "WorkItem, close its exact old AppSession, and require the successful "
            "new artifact to reattach in the shared surface."
        ),
    )
    parser.add_argument(
        "--chat-provider",
        default=os.environ.get("LLM_PROVIDER", "deepseek"),
        help="Visible Chat provider selected through the shipping renderer.",
    )
    parser.add_argument("--debug-port", type=int, default=0)
    parser.add_argument("--startup-timeout", type=float, default=180.0)
    parser.add_argument("--chat-timeout", type=float, default=120.0)
    parser.add_argument("--dispatch-timeout", type=float, default=30.0)
    parser.add_argument("--provider-timeout", type=float, default=1800.0)
    parser.add_argument("--settle-timeout", type=float, default=180.0)
    parser.add_argument("--auip-timeout", type=float, default=240.0)
    parser.add_argument(
        "--no-tts",
        action="store_true",
        help="Debug the control journey without synthesis; L4 speech assertion is omitted.",
    )
    parser.add_argument(
        "--require-b2",
        action="store_true",
        help=(
            "Require the promoted B2 foreground step route and its "
            "receipt-before-visible-delivery evidence."
        ),
    )
    parser.add_argument(
        "--complete-gomoku-round",
        action="store_true",
        help=(
            "After the B2 first move, alternate real local player clicks and "
            "automatic B2 replies until one Gomoku round reaches a result."
        ),
    )
    parser.add_argument(
        "--exercise-gomoku-post-round",
        action="store_true",
        help=(
            "After a complete Gomoku result, verify restart, the automatic "
            "opening reply, resignation, and explicit series completion."
        ),
    )
    parser.add_argument(
        "--exercise-post-leave-chat",
        action="store_true",
        help=(
            "After Host leave, send one ordinary role-only Chat turn and "
            "record UI-to-action/receipt/TTS/complete timing evidence."
        ),
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="Reuse the current Electron dist; normal acceptance rebuilds first.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    exit_code, report = asyncio.run(_run(args))
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "report": report.get("paths", {}).get("report"),
                "error": report.get("error", ""),
                "review": report.get("review", {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
