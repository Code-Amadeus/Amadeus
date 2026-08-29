"""Run a standard stdio MCP Provider Journey through Host authority and Ledger."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.adapters.mcp_provider import (  # noqa: E402
    MCP_OPERATION_INPUT_KEY,
    McpProviderAdapter,
    McpToolBinding,
)
from agent_host.provider_contract import ProviderRequirements  # noqa: E402
from agent_host.provider_runtime import ProviderRuntime  # noqa: E402
from agent_host.provider_types import (  # noqa: E402
    ProviderPermissionResponse,
    ProviderRunRequest,
)
from agent_host.work_ledger_store import WorkLedgerStore  # noqa: E402
from server.outcome_verification import (  # noqa: E402
    assess_provider_outcome,
    localize_outcome_verdict,
)
from server.work_ledger_coordinator import WorkLedgerCoordinator  # noqa: E402


SCHEMA = "amadeus.standard-mcp-provider-journey.v1"


async def _wait_permission(record: Any, *, timeout: float = 5.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        event = next(
            (
                item
                for item in record.events
                if item.get("type") == "permission.requested"
            ),
            None,
        )
        if event is not None:
            return dict(event["payload"]["permissionRequest"])
        await asyncio.sleep(0.01)
    raise TimeoutError("MCP permission request")


def _request(
    *,
    operation: str,
    operation_input: dict[str, Any],
    session_id: str,
) -> ProviderRunRequest:
    return ProviderRunRequest(
        provider="warehouse_mcp",
        task=f"Exercise standard MCP operation {operation}",
        mode=operation,
        requirements=ProviderRequirements(
            task_kind="inventory",
            preferred_provider="warehouse_mcp",
            preference_policy="require",
        ),
        metadata={
            "session_id": session_id,
            "action": operation,
            MCP_OPERATION_INPUT_KEY: operation_input,
        },
    )


async def _run() -> tuple[int, dict[str, Any]]:
    fixture = ROOT / "tools" / "probes" / "standard_mcp_fixture_server.py"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-X", "utf8", str(fixture)],
        cwd=ROOT,
    )
    adapter = McpProviderAdapter(
        provider_id="warehouse_mcp",
        display_name="Warehouse MCP",
        server=params,
        task_kinds=("inventory",),
        client_factory=lambda configuration: Client(
            stdio_client(configuration),
            raise_exceptions=False,
        ),
        permission_timeout_s=5,
        bindings=(
            McpToolBinding(
                "lookup_inventory",
                "lookup_inventory",
                argument_mapper=lambda value: {"sku": value["stock_code"]},
                resource_uris=("warehouse://inventory/A-17",),
                expected={
                    "structured_content": {"sku": "A-17", "available": 7}
                },
            ),
            McpToolBinding("reserve_part", "reserve_part"),
            McpToolBinding("explode", "explode"),
            McpToolBinding("empty_result", "empty_result"),
        ),
    )
    runtime = ProviderRuntime()
    runtime.register(adapter)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "provider_manifest": adapter.manifest.to_dict(),
        "runs": [],
        "checks": {},
    }

    with tempfile.TemporaryDirectory(prefix="amadeus_standard_mcp_") as temp:
        store = WorkLedgerStore(Path(temp) / "ledger.sqlite3")
        coordinator = WorkLedgerCoordinator(store)
        runtime.set_request_preparer(coordinator.prepare_request)
        coordinator.configure()
        try:
            success = await runtime.start(
                _request(
                    operation="lookup_inventory",
                    operation_input={"stock_code": "A-17"},
                    session_id="mcp-success",
                )
            )
            assert success.task_handle is not None
            await success.task_handle
            success_verdict = assess_provider_outcome(
                execution_status=success.status,
                provider_report=success.result,
                metadata=success.metadata,
            )
            success_work = success.metadata["work"]
            success_attempt = store.get_attempt(success_work["attempt_id"])
            completions = store.list_completions(success_work["work_item_id"])
            report["runs"].append(success.to_dict())
            report["checks"].update(
                {
                    "stdio_protocol_negotiated": success.metadata.get("mcp", {}).get(
                        "protocol_version"
                    )
                    == "2026-07-28",
                    "server_owned_tool_discovered_and_called": any(
                        item.get("type") == "tool.call"
                        and item.get("payload", {}).get("tool")
                        == "lookup_inventory"
                        for item in success.events
                    ),
                    "resource_became_host_observation": bool(
                        success_verdict
                        and success_verdict.observed.get("resources")
                        and success_verdict.observed["resources"][0].get("uri")
                        == "warehouse://inventory/A-17"
                    ),
                    "host_identity_not_overwritten": bool(
                        success_work["work_item_id"]
                        and success_work["attempt_id"]
                        and success_work["work_item_id"] != "external-session"
                        and success_work["attempt_id"] != "external-attempt"
                    ),
                    "host_outcome_verified": bool(
                        success_verdict and success_verdict.verified
                    ),
                    "work_ledger_recorded_verified_outcome_without_auto_accept": bool(
                        success_attempt
                        and success_attempt.metadata.get("outcome_verdict", {}).get(
                            "verified"
                        )
                        is True
                        and completions
                        and completions[-1].work_item_state == "review_ready"
                        and completions[-1].attention == "review"
                    ),
                    "narration_lane_can_localize_host_verdict": bool(
                        success_attempt
                        and localize_outcome_verdict(
                            success_attempt.metadata.get("outcome_verdict"),
                            execution_status=success.status,
                            display_language="japanese",
                        )
                    ),
                }
            )

            malformed = await runtime.start(
                _request(
                    operation="reserve_part",
                    operation_input={"part_id": "P-9"},
                    session_id="mcp-malformed",
                )
            )
            assert malformed.task_handle is not None
            await malformed.task_handle
            report["runs"].append(malformed.to_dict())
            report["checks"]["malformed_input_failed_before_call"] = bool(
                malformed.status == "error"
                and "mcp_invalid_tool_arguments" in str(malformed.error or "")
                and not any(
                    item.get("type") == "tool.call" for item in malformed.events
                )
            )

            exploded = await runtime.start(
                _request(
                    operation="explode",
                    operation_input={},
                    session_id="mcp-error",
                )
            )
            assert exploded.task_handle is not None
            await exploded.task_handle
            report["runs"].append(exploded.to_dict())
            report["checks"]["tool_error_did_not_complete"] = bool(
                exploded.status == "error"
                and "mcp_tool_error" in str(exploded.error or "")
            )

            empty = await runtime.start(
                _request(
                    operation="empty_result",
                    operation_input={},
                    session_id="mcp-empty",
                )
            )
            assert empty.task_handle is not None
            await empty.task_handle
            report["runs"].append(empty.to_dict())
            report["checks"]["missing_result_did_not_complete"] = bool(
                empty.status == "error"
                and "mcp_result_missing" in str(empty.error or "")
            )

            denied = await runtime.start(
                _request(
                    operation="reserve_part",
                    operation_input={"part_id": "P-9", "quantity": 2},
                    session_id="mcp-denied",
                )
            )
            denied_permission = await _wait_permission(denied)
            await runtime.resolve_permission(
                denied.run_id,
                ProviderPermissionResponse(
                    request_id=denied_permission["request_id"],
                    allow=False,
                ),
            )
            assert denied.task_handle is not None
            await denied.task_handle
            report["runs"].append(denied.to_dict())
            report["checks"]["denied_permission_prevented_tool_call"] = bool(
                denied.status == "error"
                and any(
                    item.get("type") == "permission.denied"
                    for item in denied.events
                )
                and not any(item.get("type") == "tool.call" for item in denied.events)
            )

            allowed = await runtime.start(
                _request(
                    operation="reserve_part",
                    operation_input={"part_id": "P-9", "quantity": 2},
                    session_id="mcp-allowed",
                )
            )
            allowed_permission = await _wait_permission(allowed)
            await runtime.resolve_permission(
                allowed.run_id,
                ProviderPermissionResponse(
                    request_id=allowed_permission["request_id"],
                    allow=True,
                ),
            )
            assert allowed.task_handle is not None
            await allowed.task_handle
            report["runs"].append(allowed.to_dict())
            report["checks"]["allowed_permission_called_exact_tool"] = bool(
                allowed.status == "done"
                and any(
                    item.get("type") == "permission.allowed"
                    for item in allowed.events
                )
                and any(
                    item.get("type") == "tool.call"
                    and item.get("payload", {}).get("tool") == "reserve_part"
                    for item in allowed.events
                )
            )
        finally:
            coordinator.close()
            await runtime.close()

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["status"] = (
        "passed" if all(report["checks"].values()) else "failed"
    )
    output_root = ROOT / "runtime" / "e2e_reports" / "mcp_provider"
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_root / f"standard_mcp_provider_{stamp}.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report["report_path"] = str(output_path)
    return (0 if report["status"] == "passed" else 1), report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    code, report = asyncio.run(_run())
    rendered = (
        report
        if args.pretty
        else {
            "status": report["status"],
            "report_path": report["report_path"],
            "checks": report["checks"],
        }
    )
    print(
        json.dumps(
            rendered,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
