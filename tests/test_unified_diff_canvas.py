from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.ai_os_schema import diff_canvas_payload, normalize_canvas_mode
from server.unified_diff import parse_unified_diff


def test_parse_unified_diff_preserves_dual_line_numbers() -> None:
    parsed = parse_unified_diff(
        "\n".join(
            [
                "diff --git a/app.py b/app.py",
                "--- a/app.py",
                "+++ b/app.py",
                "@@ -10,3 +10,4 @@ def run():",
                " context",
                "-old value",
                "+new value",
                "+extra value",
                " tail",
            ]
        )
    )

    assert parsed["fileCount"] == 1
    assert parsed["additions"] == 2
    assert parsed["deletions"] == 1
    file = parsed["files"][0]
    assert file["path"] == "app.py"
    lines = file["hunks"][0]["lines"]
    assert lines[0] == {"kind": "context", "oldLine": 10, "newLine": 10, "text": "context"}
    assert lines[1] == {"kind": "remove", "oldLine": 11, "text": "old value"}
    assert lines[2] == {"kind": "add", "newLine": 11, "text": "new value"}
    assert lines[3] == {"kind": "add", "newLine": 12, "text": "extra value"}
    assert lines[4] == {"kind": "context", "oldLine": 12, "newLine": 13, "text": "tail"}


def test_parse_unified_diff_keeps_untracked_files_without_hunks() -> None:
    parsed = parse_unified_diff(
        "",
        changed_files=["tracked.txt", "new file.md"],
        untracked=["new file.md"],
    )

    assert parsed["clean"] is False
    assert parsed["files"] == [
        {
            "path": "tracked.txt",
            "oldPath": "",
            "newPath": "tracked.txt",
            "status": "modified",
            "additions": 0,
            "deletions": 0,
            "hunks": [],
        },
        {
            "path": "new file.md",
            "oldPath": "",
            "newPath": "new file.md",
            "status": "untracked",
            "additions": 0,
            "deletions": 0,
            "hunks": [],
        },
    ]


def test_parse_unified_diff_renders_an_untracked_new_file_patch() -> None:
    parsed = parse_unified_diff(
        "\n".join(
            [
                "diff --git a/index.html b/index.html",
                "new file mode 100644",
                "--- /dev/null",
                "+++ b/index.html",
                "@@ -0,0 +1,2 @@",
                "+<main>",
                "+</main>",
            ]
        ),
        changed_files=["index.html"],
        untracked=["index.html"],
    )

    assert parsed["fileCount"] == 1
    assert parsed["files"][0]["status"] == "added"
    assert parsed["files"][0]["additions"] == 2
    assert parsed["files"][0]["hunks"][0]["lines"][0]["text"] == "<main>"


def test_parse_unified_diff_keeps_marker_like_content_inside_hunks() -> None:
    parsed = parse_unified_diff(
        "\n".join(
            [
                "diff --git a/schema.sql b/schema.sql",
                "--- a/schema.sql",
                "+++ b/schema.sql",
                "@@ -1,3 +1,3 @@",
                " CREATE TABLE t (id INT);",
                "--- legacy index",
                "+++ counter",
                " SELECT 1;",
                "diff --git a/next.sql b/next.sql",
                "--- a/next.sql",
                "+++ b/next.sql",
                "@@ -1 +1 @@",
                "-old",
                "+new",
            ]
        )
    )

    assert parsed["fileCount"] == 2
    assert parsed["additions"] == 2
    assert parsed["deletions"] == 2
    first, second = parsed["files"]
    assert first["path"] == "schema.sql"
    assert first["oldPath"] == "schema.sql"
    assert first["newPath"] == "schema.sql"
    assert first["hunks"][0]["lines"][1:3] == [
        {"kind": "remove", "oldLine": 2, "text": "-- legacy index"},
        {"kind": "add", "newLine": 2, "text": "++ counter"},
    ]
    assert first["hunks"][0]["lines"][3] == {
        "kind": "context",
        "oldLine": 3,
        "newLine": 3,
        "text": "SELECT 1;",
    }
    assert second["path"] == "next.sql"
    assert second["additions"] == 1
    assert second["deletions"] == 1


def test_diff_canvas_payload_keeps_report_for_local_mode_switch() -> None:
    structured = parse_unified_diff(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    payload = diff_canvas_payload(
        phase="Preview",
        title="Diff",
        lead="1 file: +1 / -1",
        diff=structured,
        report_markdown="### Result\nDone.",
        report_view={"phase": "Result", "title": "Result report", "lead": "Done.", "progress": 100},
    )

    assert normalize_canvas_mode("diff") == "diff"
    assert payload["mode"] == "diff"
    assert payload["artifact"]["kind"] == "code.diff"
    assert payload["artifact"]["content"]["diff"] == structured
    assert payload["reportMarkdown"] == "### Result\nDone."
    assert payload["reportView"]["title"] == "Result report"


def test_parse_unified_diff_caps_files_without_merging_hunks() -> None:
    patch = "\n".join(
        [
            "diff --git a/a.py b/a.py",
            "--- a/a.py",
            "+++ b/a.py",
            "@@ -1 +1 @@",
            "-a",
            "+b",
            "diff --git a/c.py b/c.py",
            "--- a/c.py",
            "+++ b/c.py",
            "@@ -1 +1 @@",
            "-c",
            "+d",
        ]
    )
    parsed = parse_unified_diff(patch, max_files=1)

    assert parsed["truncated"] is True
    assert parsed["fileCount"] == 1
    assert parsed["files"][0]["path"] == "a.py"
    assert parsed["files"][0]["additions"] == 1
    assert parsed["files"][0]["deletions"] == 1


if __name__ == "__main__":
    test_parse_unified_diff_preserves_dual_line_numbers()
    test_parse_unified_diff_keeps_untracked_files_without_hunks()
    test_parse_unified_diff_renders_an_untracked_new_file_patch()
    test_parse_unified_diff_keeps_marker_like_content_inside_hunks()
    test_diff_canvas_payload_keeps_report_for_local_mode_switch()
    test_parse_unified_diff_caps_files_without_merging_hunks()
    print("unified diff canvas tests passed")
