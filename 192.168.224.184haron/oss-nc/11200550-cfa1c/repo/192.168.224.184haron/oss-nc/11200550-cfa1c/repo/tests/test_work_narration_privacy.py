"""Work narration keeps runtime locations out of chat and speech."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.work_observer import WorkObserverCoordinator


def test_absolute_locations_are_removed_but_artifact_basename_survives() -> None:
    raw = (
        '進捗をまとめると、Updated project files: gomoku.html. / '
        'Codex applied 1 file change. / '
        '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" / '
        'F:\\Computer_Science\\Amadeus\\amadeus / '
        'C:\\Users\\Lucas\\project\\gomoku.html'
    )

    bounded = WorkObserverCoordinator._bounded_role_line(raw, limit=500)

    assert "gomoku.html" in bounded
    for private_fragment in (
        "C:\\",
        "F:\\",
        "Computer_Science",
        "Users",
        "WindowsPowerShell",
        "powershell.exe",
    ):
        assert private_fragment not in bounded
    assert " /  / " not in bounded


def test_urls_relative_identifiers_and_plain_prose_are_not_paths() -> None:
    raw = "结果在 src/provider_result.json，也可查看 https://example.test/report。"
    bounded = WorkObserverCoordinator._bounded_role_line(raw, limit=500)

    assert bounded == raw


def test_posix_unc_and_file_url_locations_are_removed() -> None:
    raw = (
        "Artifacts: /home/user/private/report.md / "
        "\\\\server\\private\\tool.exe / "
        "file:///C:/Users/user/private/result.json"
    )

    bounded = WorkObserverCoordinator._bounded_role_line(raw, limit=500)

    assert "report.md" in bounded
    assert "result.json" in bounded
    assert "user" not in bounded.casefold()
    assert "server" not in bounded.casefold()
    assert "tool.exe" not in bounded.casefold()
    assert "file:///" not in bounded


def main() -> None:
    test_absolute_locations_are_removed_but_artifact_basename_survives()
    test_urls_relative_identifiers_and_plain_prose_are_not_paths()
    test_posix_unc_and_file_url_locations_are_removed()
    print("all work narration privacy tests passed")


if __name__ == "__main__":
    main()
