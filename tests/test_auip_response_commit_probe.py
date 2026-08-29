from __future__ import annotations

from tools.probes.probe_auip_response_commit_abc import _runtime_for


def test_response_commit_probe_fixtures_match_current_manifests() -> None:
    for app_kind in ("bullet", "gomoku"):
        runtime, app_session_id = _runtime_for(
            app_kind,
            f"fixture-contract-{app_kind}",
        )

        projection = runtime.get(app_session_id)

        assert projection["status"] == "active"
        assert projection["revision"] == 1
