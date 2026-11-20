"""Verify real AUIP authoring through the official Codex SDK adapter.

The probe starts from a standalone interactive app in a disposable Git
workspace.  The Host stages the AUIP skill through normal Work intake, Codex
performs the integration, and the Host independently validates the result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.adapters.codex_app_server import CodexAppServerAdapter
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerStore
from server.work_ledger_coordinator import WorkLedgerCoordinator
from tools.e2e_direct_codex_conversation import _create_sandbox_accessible_root


FIXTURE = """<!doctype html>
<meta charset="utf-8">
<title>Increment Garden</title>
<main>
  <h1>Increment Garden</h1>
  <output id="value">0</output>
  <button id="increment" type="button">Increment</button>
</main>
<script>
let value = 0;
const output = document.querySelector('#value');
document.querySelector('#increment').onclick = () => {
  value += 1;
  output.textContent = String(value);
};
</script>
"""


def _git(cwd: Path, *args: str) -> None:
    process = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr or process.stdout)


def _initialize(workspace: Path) -> None:
    workspace.mkdir()
    _git(workspace, "init", "--quiet")
    _git(workspace, "config", "user.email", "amadeus-e2e@example.invalid")
    _git(workspace, "config", "user.name", "Amadeus E2E")
    (workspace / "index.html").write_text(FIXTURE, encoding="utf-8")
    _git(workspace, "add", "index.html")
    _git(workspace, "commit", "--quiet", "-m", "standalone fixture")


def _validate_manifest(workspace: Path) -> dict[str, Any]:
    manifest_path = workspace / "auip.manifest.json"
    process = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(ROOT / "tools" / "validate_auip_manifest.py"),
            str(manifest_path),
            "--print-canonical",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    parsed: dict[str, Any] = {}
    if process.returncode == 0:
        try:
            parsed = json.loads(process.stdout)
        except json.JSONDecodeError:
            parsed = {}
    return {
        "ok": process.returncode == 0,
        "returncode": process.returncode,
        "manifest": parsed,
        "diagnostics": (process.stderr or "").strip()[-2000:],
    }


def _remove_sandbox_root(root: Path) -> None:
    """Return Windows sandbox ACLs before deleting a disposable probe root."""

    if os.name == "nt" and root.exists():
        subprocess.run(
            ["icacls", str(root), "/inheritance:e", "/T", "/C", "/Q"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        subprocess.run(
            ["icacls", str(root), "/reset", "/T", "/C", "/Q"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    def onerror(function, path, _exc_info) -> None:
        os.chmod(path, stat.S_IWRITE)
        function(path)

    shutil.rmtree(root, onerror=onerror)


async def _run(timeout_s: float, keep: bool) -> dict[str, Any]:
    parent = (ROOT / "runtime" / "e2e_workspaces").resolve()
    parent.mkdir(parents=True, exist_ok=True)
    root = _create_sandbox_accessible_root(parent)
    workspace = root / "project"
    ledger_path = root / "work_ledger.sqlite3"
    adapter = CodexAppServerAdapter(
        approval_mode="auto_review",
        turn_timeout_s=timeout_s,
    )
    runtime = ProviderRuntime()
    coordinator: WorkLedgerCoordinator | None = None
    report: dict[str, Any] = {
        "status": "failed",
        "workspace": str(workspace),
        "kept": keep,
    }
    try:
        _initialize(workspace)
        store = WorkLedgerStore(ledger_path)
        coordinator = WorkLedgerCoordinator(store)
        coordinator.configure()
        runtime.set_request_preparer(coordinator.prepare_request)
        runtime.register(adapter)
        record = await runtime.start(
            ProviderRunRequest(
                provider="codex",
                task=(
                    "把现有 Increment Garden 网页接成可由 Amadeus 观察和参与的 "
                    "AUIP 应用。保留原按钮和离线玩法；公开当前计数状态、一次递增的"
                    "语义事件，并提供一个受限的本地递增动作。完成所有协议验证，"
                    "不要启动应用。"
                ),
                cwd=str(workspace),
                mode="agent",
                requirements=ProviderRequirements(
                    task_kind="workspace_mutation",
                    workspace_access="write",
                    workspace_ownership="caller",
                    preferred_provider="codex",
                    preference_policy="require",
                ),
                metadata={
                    "source": "auip_prepare",
                    # Production receives this frozen Host contract from
                    # build_delegate_metadata after AUIP control adjudication.
                    # The probe bypasses Chat but must not bypass that authority.
                    "host_outcome_requirement": {
                        "operation": "prepare",
                        "facet": "auip.application",
                        "expected": {"current_attempt_contribution": True},
                    },
                    "source_user_text": (
                        "让这个小游戏能和你一起玩，也能在旁边看着评论；接好以后先别打开。"
                    ),
                    "provider_manifest": CODEX_APP_SERVER_MANIFEST.to_dict(),
                },
            )
        )
        if record.task_handle is None:
            raise RuntimeError("ProviderRuntime did not start the Codex turn")
        await record.task_handle
        events = list(record.events)
        skill_path = Path(str(record.metadata.get("auip_authoring_skill_path") or ""))
        work = record.metadata.get("work") if isinstance(record.metadata.get("work"), dict) else {}
        work_item_id = str(work.get("work_item_id") or "")
        attempt_id = str(work.get("attempt_id") or "")
        attempt = store.get_attempt(attempt_id)
        completions = store.list_completions(work_item_id) if work_item_id else []
        verdict = (
            attempt.metadata.get("outcome_verdict")
            if attempt is not None and isinstance(attempt.metadata.get("outcome_verdict"), dict)
            else {}
        )
        artifacts = (
            store.list_artifacts(work_item_id, attempt_id=attempt_id)
            if work_item_id and attempt_id
            else []
        )
        provider_session = record.metadata.get("provider_session")

        manifest_validation = _validate_manifest(workspace)
        html = (workspace / "index.html").read_text(encoding="utf-8")
        manifest = manifest_validation.get("manifest") or {}
        actions = manifest.get("actions") if isinstance(manifest, dict) else {}
        events_contract = manifest.get("events") if isinstance(manifest, dict) else {}
        checks = {
            "host_staged_attempt_local_skill": skill_path.is_file()
            and skill_path.is_relative_to(workspace),
            "real_codex_turn_succeeded": record.status == "done",
            "native_tool_events_visible": any(
                event.get("type") == "tool.call" for event in events
            )
            and any(event.get("type") == "tool.result" for event in events),
            "manifest_is_valid": manifest_validation["ok"],
            "bounded_action_declared": isinstance(actions, dict) and bool(actions),
            "mcp_compatible_action_schema_generated": isinstance(actions, dict)
            and bool(actions)
            and all(
                isinstance(spec, dict)
                and isinstance(spec.get("inputSchema"), dict)
                and spec["inputSchema"].get("type") == "object"
                and isinstance(spec["inputSchema"].get("properties"), dict)
                for spec in actions.values()
            ),
            "semantic_event_declared": isinstance(events_contract, dict)
            and bool(events_contract),
            "official_sdk_is_referenced": "auip-v0.js" in html,
            "standalone_control_preserved": 'id="increment"' in html,
            "provider_did_not_launch": not any(
                token in record.result.casefold()
                for token in ("opened the app", "launched the app")
            ),
            "host_verified_current_attempt_delivery": verdict.get("verified") is True
            and verdict.get("facet") == "auip.application",
            "host_registered_launchable_artifacts": {
                artifact.title
                for artifact in artifacts
                if artifact.kind == "business.file" and artifact.status == "registered"
            }.issuperset({"index.html", "auip.manifest.json"}),
            "ledger_terminal_is_reviewable": bool(completions)
            and completions[-1].completeness == "partial"
            and completions[-1].attention == "review",
        }
        report.update(
            {
                "status": "passed" if all(checks.values()) else "failed",
                "checks": checks,
                "work_item_id": work_item_id,
                "attempt_id": attempt_id,
                "provider_status": record.status,
                "provider_result": record.result,
                "provider_error": record.error or "",
                "provider_session": (
                    dict(provider_session) if isinstance(provider_session, dict) else {}
                ),
                "outcome_verdict": verdict,
                "completion": (
                    {
                        "completeness": completions[-1].completeness,
                        "attention": completions[-1].attention,
                        "state": completions[-1].work_item_state,
                    }
                    if completions
                    else {}
                ),
                "artifacts": [
                    {
                        "kind": artifact.kind,
                        "title": artifact.title,
                        "path": artifact.path,
                        "status": artifact.status,
                        "attempt_id": artifact.attempt_id,
                        "sha256": artifact.sha256,
                    }
                    for artifact in artifacts
                ],
                "event_types": sorted({event.get("type") for event in events}),
                "manifest_validation": manifest_validation,
            }
        )
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        await runtime.close()
        if coordinator is not None:
            coordinator.close()
        if not keep:
            try:
                _remove_sandbox_root(root)
                report["cleanup"] = {"removed": not root.exists()}
            except OSError as exc:
                report["cleanup"] = {"removed": False, "error": str(exc)}
                report["status"] = "failed"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"status": "dry_run", "live": False}, indent=2))
        return 0
    report = asyncio.run(_run(args.timeout, args.keep))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
