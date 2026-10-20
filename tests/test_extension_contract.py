"""Author diagnostics and real installed Notes through the native Pi Provider."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from test_pi_runtime import native_pi as native_pi

ROOT = Path(__file__).resolve().parents[1]


def test_explicit_execution_and_missing_input_fail_without_secret_output(tmp_path):
    fixture_value = "synthetic-do-not-print"
    source = tmp_path / "record.json"
    source.write_text(json.dumps({"secrets": {"token": fixture_value}}))
    scenario = tmp_path / "scenario.json"
    scenario.write_text("{}")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / "tools/check_extension_contract.py"),
            "--kind",
            "mcp",
            "--record",
            str(source),
            "--scenario",
            str(scenario),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert not report["ok"] and report["stage"] == "input"
    assert set(report["layers"].values()) == {"not_run"}
    assert fixture_value not in result.stdout + result.stderr


@pytest.fixture
def installed():
    value = os.environ.get("AMADEUS_TEST_AUTHOR_SNAPSHOTS")
    if not value:
        pytest.skip("run the explicit installer-backed author journey")
    return Path(value)


@pytest.mark.parametrize("defect", ["unbound", "missing_resource", "timeout"])
def test_author_failure_identifies_stage_and_exits_nonzero(installed, tmp_path, defect):
    name = "notes" if defect == "unbound" else "handoff"
    distribution = (
        "amadeus_example_notes_mcp" if name == "notes" else "amadeus_example_handoff_skill"
    )
    record = json.loads((installed / f"{name}-0.2.0.json").read_text(encoding="utf-8"))
    scenario = json.loads(
        (ROOT / f"examples/extensions/contract-scenarios/{name}.json").read_text(encoding="utf-8")
    )
    if defect == "unbound":
        record["providers"] = []
    if defect == "missing_resource":
        scenario["reads"].append("skills/handoff-brief/missing.txt")
    source = tmp_path / "record.json"
    source.write_text(json.dumps(record))
    plan = tmp_path / "scenario.json"
    plan.write_text(json.dumps(scenario))
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / "tools/check_extension_contract.py"),
            "--execute",
            "--kind",
            "mcp" if name == "notes" else "skill",
            "--wheel",
            str(installed / f"wheels/{distribution}-0.2.0-py3-none-any.whl"),
            "--record",
            str(source),
            "--scenario",
            str(plan),
            "--timeout",
            "1" if defect == "timeout" else "90",
        ],
        capture_output=True,
        text=True,
        timeout=100,
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert not report["ok"] and report["layers"]["static"] == "passed"
    assert report["stage"] != "complete" and report["error"]


async def test_installed_notes_calls_use_real_pi_approval_and_native_transport(
    installed, native_pi, tmp_path
):
    from agent_host.extension_mcp import project_extension_mcp_connections
    from agent_host.provider_contract import ProviderRequirements
    from agent_host.provider_types import ProviderRunRequest, ProviderPermissionResponse

    record = json.loads((installed / "notes-0.2.0.json").read_text(encoding="utf-8"))
    record["data_path"] = str(tmp_path / "notes-data")
    Path(record["data_path"]).mkdir()
    adapter, requests, selected, _ = native_pi
    adapter.mcp_connections = project_extension_mcp_connections(record)

    def call(tool, arguments):
        def step(data):
            # Only one configured server. Pi preserves the original tool name in the bounded description.
            choices = [
                t["function"]["name"]
                for t in data["tools"]
                if tool in t["function"]["description"] or tool in t["function"]["name"]
            ]
            assert len(choices) == 1, [
                (t["function"]["name"], t["function"]["description"][:160]) for t in data["tools"]
            ]
            return {"tool": choices[0], "args": arguments}

        return step

    selected["steps"] = [
        call("save_note", {"name": "native-author", "text": "Synthetic native Notes round trip"}),
        call("read_note", {"name": "native-author"}),
    ]
    permissions = []
    events = []

    async def emit(event):
        events.append(event)
        if event.type == "permission.requested":
            permission = event.payload["permissionRequest"]
            permissions.append(permission)
            assert permission["capability"] == "mcp.tool.call"
            await adapter.resolve_permission(
                "author-notes", ProviderPermissionResponse(permission["request_id"], True)
            )

    request = ProviderRunRequest(
        "pi",
        "Exercise the selected synthetic Notes calls.",
        cwd=str(tmp_path),
        requirements=ProviderRequirements(workspace_access="none", workspace_ownership="caller"),
    )
    result = await adapter.run(request, "author-notes", emit)
    assert result.status == "done", result
    assert len(permissions) == 2
    assert len([e for e in events if e.type == "tool.result" and e.payload.get("ok")]) == 2
    assert "Synthetic native Notes round trip" in str(requests[-1]["messages"])


def test_native_theme_accepts_inherited_background_with_valid_overrides(installed, tmp_path):
    from server.extensions.package import validate_manifest
    from tools.check_extension_contract import check_theme

    record = json.loads((installed / "theme-0.2.0.json").read_text(encoding="utf-8"))
    theme = record["manifest"]["contributes"]["theme"][0]
    theme["tokens"].pop("--bg", None)
    theme.pop("background", None)
    record["manifest"]["contributes"]["theme"] = [theme]
    validate_manifest(record["manifest"])
    result = check_theme(record, tmp_path, lambda stage: None)
    assert result["nativeDecode"] and result["painted"] and result["builtinRestored"]
