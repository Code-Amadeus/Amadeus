from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

from agent_host.provider_authoring import (
    materialize_auip_runtime_assets,
    official_auip_runtime_assets,
    stage_auip_authoring_bundle,
)
from server.auip_bundle_validation import validate_auip_web_bundle_execution
from tools import validate_auip_entry as entry_preflight
from tools.sync_auip_manifest import sync_manifest
from tools.validate_auip_manifest import validate_file


class _ConnectionGuard:
    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen()
        self._socket.settimeout(0.05)
        self.port = int(self._socket.getsockname()[1])
        self.connections = 0
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)

    def _accept(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            connection.close()

    def __enter__(self) -> "_ConnectionGuard":
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stopped.set()
        self._socket.close()
        self._thread.join(timeout=1)


def _manifest() -> dict[str, object]:
    return {
        "schema": "amadeus.auip/v0",
        "app": {
            "id": "entry-preflight-fixture",
            "title": "Entry preflight fixture",
            "version": "1.0.0",
            "objective": "Choose one available assistance option.",
            "interactionSummary": (
                "The participant can use preflight.choose for the currently available "
                "assistance option. Example: 'assist' chooses the published option."
            ),
        },
        "events": {"preflight.ready": {"beat": True}},
        "actions": {
            "preflight.choose": {
                "description": (
                    "Choose payload id only when the matching state.choice option is "
                    "available."
                ),
                "risk": "local_execution",
                "inputSchema": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "enum": ["assist"]}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
            }
        },
        "stances": ["spectator", "participant"],
        "situationKinds": ["choice/v1"],
    }


def _write_fixture(
    root: Path,
    *,
    option_available: bool,
    websocket_url: str = "",
) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    materialize_auip_runtime_assets(root)
    manifest_path = root / "auip.manifest.json"
    entry_path = root / "index.html"
    manifest = _manifest()
    manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)
    transport_probe = (
        f'window.transportProbe = new WebSocket({json.dumps(websocket_url)});'
        if websocket_url
        else ""
    )
    entry_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Entry preflight fixture</title></head>
<body>
  <output id="status">standalone ready</output>
  <script id="auip-manifest" type="application/json">
{manifest_json}
  </script>
  <script src="sdk/auip-core/managed-v0.js"></script>
  <script src="sdk/auip-core/situations-v0.js"></script>
  <script src="sdk/auip-web/auip-v0.js"></script>
  <script>
    console.warn(new Error("optional feature unavailable"));
    const manifest = JSON.parse(document.getElementById("auip-manifest").textContent);
    const state = {{selected: null}};
    const auip = AmadeusAUIP.createManagedApp({{
      manifest,
      snapshot: () => ({{
        choice: AmadeusAUIPSituations.choiceSituation({{
          compact: true,
          action: "preflight.choose",
          options: [{{
            id: "assist",
            label: "Assist",
            payload: {{id: "assist"}},
            available: {str(option_available).lower()},
          }}],
        }}),
      }}),
      initialEvents: () => [{{type: "preflight.ready", actor: "app", payload: {{}}}}],
      actions: {{
        "preflight.choose": (payload, tx) => tx.commit({{
          mutate: () => {{ state.selected = payload.id; }},
          effects: () => ({{selected: state.selected}}),
          events: () => [],
        }}),
      }},
    }});
    auip.start();
    {transport_probe}
    document.documentElement.dataset.booted = "true";
  </script>
</body>
</html>
""",
        encoding="utf-8",
    )
    manifest_path.write_text(manifest_json, encoding="utf-8")
    return manifest_path, entry_path


def _write_swallowed_grid_snapshot_fixture(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    materialize_auip_runtime_assets(root)
    manifest = _manifest()
    manifest["situationKinds"] = ["grid/v1", "choice/v1"]
    manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)
    manifest_path = root / "auip.manifest.json"
    entry_path = root / "index.html"
    manifest_path.write_text(manifest_json, encoding="utf-8")
    entry_path.write_text(
        f"""<!doctype html>
<script id="auip-manifest" type="application/json">{manifest_json}</script>
<script src="sdk/auip-core/managed-v0.js"></script>
<script src="sdk/auip-core/situations-v0.js"></script>
<script src="sdk/auip-web/auip-v0.js"></script>
<script>
try {{
  const manifest = JSON.parse(document.getElementById("auip-manifest").textContent);
  const app = AmadeusAUIP.createManagedApp({{
    manifest,
    snapshot: () => ({{
      board: AmadeusAUIPSituations.gridSituation({{
        width: 3,
        height: 3,
        empty: "o",
        legend: {{o: "off", x: "on"}},
        cell: (x, y) => ["xox", "oxo", "xoo"][y][x],
      }}),
    }}),
    actions: {{
      "preflight.choose": (_payload, tx) => tx.reject("not used", "not_used"),
    }},
  }});
  app.start();
}} catch (error) {{
  console.warn("AUIP unavailable; standalone play remains active.", error);
}}
document.documentElement.dataset.booted = "true";
</script>
""",
        encoding="utf-8",
    )
    return manifest_path, entry_path


def _run_preflight(manifest_path: Path, entry_path: Path) -> subprocess.CompletedProcess[str]:
    # Execute the actual staged tool outside the repository's import directory,
    # as the Provider does. A repo-local subprocess can hide missing bundle files.
    bundle = manifest_path.parent / "authoring-bundle"
    stage_auip_authoring_bundle(bundle, include_opaque_dependencies=False)
    return subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(bundle / "tools" / "validate_auip_entry.py"),
            str(manifest_path),
            str(entry_path),
            "--settle-ms",
            "100",
            "--json",
        ],
        cwd=entry_path.parent,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_real_sdk_entry_boot_succeeds_without_live_host_traffic(tmp_path: Path) -> None:
    with _ConnectionGuard() as guard:
        guarded_url = f"ws://127.0.0.1:{guard.port}/daily-host"
        manifest_path, entry_path = _write_fixture(
            tmp_path / "valid",
            option_available=True,
            websocket_url=guarded_url,
        )

        validate_file(manifest_path)
        sync_manifest(manifest_path, entry_path, check=True)
        completed = _run_preflight(manifest_path, entry_path)

    assert completed.returncode == 0, completed.stderr or completed.stdout
    result = json.loads(completed.stdout)
    assert result["ok"] is True
    assert result["kind"] == "ok"
    assert result["diagnostics"] == []
    assert result["browser"]["freshEphemeralContext"] is True
    assert result["transportIsolation"]["liveHost"] is False
    assert result["transportIsolation"]["externalNetwork"] is False
    attempts = result["transportIsolation"]["isolatedTransportAttempts"]
    assert {item["url"] for item in attempts} >= {guarded_url}
    assert guard.connections == 0
    assert "gameplay" in result["scope"]["doesNotProve"]


def test_real_sdk_boot_rejects_invalid_initial_choice_snapshot_static_checks_miss(
    tmp_path: Path,
) -> None:
    manifest_path, entry_path = _write_fixture(
        tmp_path / "invalid-initial-snapshot",
        option_available=False,
    )

    # These current static authoring checks both accept the completed files.
    validate_file(manifest_path)
    sync_manifest(manifest_path, entry_path, check=True)

    completed = _run_preflight(manifest_path, entry_path)

    assert completed.returncode == 2, completed.stderr or completed.stdout
    result = json.loads(completed.stdout)
    assert result["ok"] is False
    assert result["kind"] == "app_error"
    assert any(
        item["source"] == "pageerror"
        and "choice_compact_unavailable_option" in item["message"]
        and "assist" in item["message"]
        for item in result["diagnostics"]
    ), result
    assert not any(
        item["code"] in {"browser_unavailable", "playwright_unavailable"}
        for item in result["diagnostics"]
    )


async def test_shared_boot_rejects_caught_managed_initial_snapshot_error(
        tmp_path: Path) -> None:
    manifest_path, entry_path = _write_swallowed_grid_snapshot_fixture(
        tmp_path / "caught-invalid-grid")
    expected = official_auip_runtime_assets()

    # Static packaging is valid; the real Managed Core owns this state error.
    validate_file(manifest_path)
    sync_manifest(manifest_path, entry_path, check=True)
    result = await validate_auip_web_bundle_execution(
        manifest_path.parent,
        entry_filename=entry_path.name,
        materialized_files=tuple(expected),
        expected_assets=expected,
    )

    assert result["verified"] is False
    assert result["kind"] == "app_error"
    assert result["code"] == "snapshot_failed"
    assert "ManagedCommitError" in result["detail"]
    assert "snapshot_failed: grid_empty_symbol_in_legend: o" in result["detail"]
    assert result["boot"]["ok"] is False
    assert result["boot"]["diagnostics"][0]["source"] == "console"


def test_unavailable_browser_environment_is_never_reported_as_app_valid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_path, entry_path = _write_fixture(
        tmp_path / "missing-browser",
        option_available=True,
    )
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)

    result = asyncio.run(
        entry_preflight.validate_entry(
            manifest_path,
            entry_path,
            timeout_seconds=1,
            settle_milliseconds=0,
        )
    )

    assert result["ok"] is False
    assert result["kind"] == "browser_environment_error"
    assert result["diagnostics"][0]["code"] == "playwright_unavailable"


def test_missing_host_materialized_sdk_is_a_distinct_non_app_blocker(
    tmp_path: Path,
) -> None:
    manifest_path, entry_path = _write_fixture(
        tmp_path / "missing-materialized-sdk",
        option_available=True,
    )
    (manifest_path.parent / "sdk" / "auip-core" / "managed-v0.js").unlink()

    result = asyncio.run(
        entry_preflight.validate_entry(
            manifest_path,
            entry_path,
            timeout_seconds=1,
            settle_milliseconds=0,
        )
    )

    assert result["ok"] is False
    assert result["kind"] == "host_materialization_error"
    assert result["diagnostics"][0]["code"] == "host_runtime_asset_missing"
    assert "Do not edit, copy, or recreate the SDK" in result["diagnostics"][0][
        "message"
    ]


async def test_shared_bundle_execution_runs_real_valid_and_invalid_entry_boots(
        tmp_path: Path) -> None:
    expected = official_auip_runtime_assets()
    valid_manifest, valid_entry = _write_fixture(
        tmp_path / "shared-valid", option_available=True)
    invalid_manifest, invalid_entry = _write_fixture(
        tmp_path / "shared-invalid", option_available=False)

    valid = await validate_auip_web_bundle_execution(
        valid_manifest.parent,
        entry_filename=valid_entry.name,
        materialized_files=tuple(expected),
        expected_assets=expected,
    )
    invalid = await validate_auip_web_bundle_execution(
        invalid_manifest.parent,
        entry_filename=invalid_entry.name,
        materialized_files=tuple(expected),
        expected_assets=expected,
    )

    assert valid["verified"] is True
    assert valid["kind"] == valid["code"] == "ok"
    assert valid["checks"] == [
        "manifest", "embedded_manifest_sync", "runtime_asset_integrity",
        "entry_wiring", "entry_boot"]
    assert valid["boot"]["scope"]["doesNotProve"].startswith("live Host attach")
    assert invalid["verified"] is False
    assert invalid["kind"] == "app_error"
    assert invalid["code"] == "javascript_page_error"
    assert "choice_compact_unavailable_option" in invalid["detail"]
    assert "assist" in invalid["detail"]
    assert invalid["boot"]["diagnostics"]
