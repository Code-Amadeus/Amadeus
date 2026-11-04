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


def test_parse_unified_diff_keeps_spaced_paths_without_marker_lines() -> None:
    parsed = parse_unified_diff(
        "\n".join(
            [
                "diff --git a/old notes.txt b/old notes.txt",
                "deleted file mode 100644",
                "index 587be6b..0000000",
                "--- a/old notes.txt\t",
                "+++ /dev/null",
                "@@ -1 +0,0 @@",
                "-x",
                "diff --git a/cover art.png b/cover art.png",
                "index bdc955b..8835708 100644",
                "Binary files a/cover art.png and b/cover art.png differ",
                "diff --git a/run me.sh b/run me.sh",
                "old mode 100644",
                "new mode 100755",
            ]
        ),
        changed_files=["old notes.txt", "cover art.png", "run me.sh"],
    )

    assert [(item["path"], item["status"]) for item in parsed["files"]] == [
        ("old notes.txt", "deleted"),
        ("cover art.png", "modified"),
        ("run me.sh", "modified"),
    ]
    assert parsed["files"][0]["oldPath"] == "old notes.txt"
    assert parsed["files"][0]["deletions"] == 1
    assert parsed["files"][1]["oldPath"] == "cover art.png"
    assert parsed["files"][1]["newPath"] == "cover art.png"


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


def test_parse_unified_diff_splits_hunk_lines_only_at_newlines() -> None:
    parsed = parse_unified_diff(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,4 +1,4 @@\n"
        " def a():\n"
        "-    s = 'x\f-y'\n"
        "+    s = 'x\u2028+y'\n"
        " \f\n"
        " windows\r\n"
    )

    file = parsed["files"][0]
    assert file["additions"] == 1
    assert file["deletions"] == 1
    assert file["hunks"][0]["lines"] == [
        {"kind": "context", "oldLine": 1, "newLine": 1, "text": "def a():"},
        {"kind": "remove", "oldLine": 2, "text": "    s = 'x\f-y'"},
        {"kind": "add", "newLine": 2, "text": "    s = 'x\u2028+y'"},
        {"kind": "context", "oldLine": 3, "newLine": 3, "text": "\f"},
        {"kind": "context", "oldLine": 4, "newLine": 4, "text": "windows"},
    ]


def test_parse_unified_diff_reads_empty_lines_inside_hunks_as_blank_context() -> None:
    # Output of `git -c diff.suppressBlankEmpty=true diff`: blank context
    # lines lose their leading space.
    parsed = parse_unified_diff(
        "diff --git a/notes.txt b/notes.txt\n"
        "--- a/notes.txt\n"
        "+++ b/notes.txt\n"
        "@@ -1,4 +1,4 @@\n"
        " a\n"
        "\n"
        "-b\n"
        "+B\n"
        "\n"
        "diff --git a/next.txt b/next.txt\n"
        "--- a/next.txt\n"
        "+++ b/next.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "\n"
    )

    first, second = parsed["files"]
    assert first["additions"] == 1
    assert first["deletions"] == 1
    assert first["hunks"][0]["lines"] == [
        {"kind": "context", "oldLine": 1, "newLine": 1, "text": "a"},
        {"kind": "context", "oldLine": 2, "newLine": 2, "text": ""},
        {"kind": "remove", "oldLine": 3, "text": "b"},
        {"kind": "add", "newLine": 3, "text": "B"},
        {"kind": "context", "oldLine": 4, "newLine": 4, "text": ""},
    ]
    assert second["hunks"][0]["lines"] == [
        {"kind": "remove", "oldLine": 1, "text": "old"},
        {"kind": "add", "newLine": 1, "text": "new"},
    ]
    assert parsed["lineCount"] == 7


if __name__ == "__main__":
    test_parse_unified_diff_preserves_dual_line_numbers()
    test_parse_unified_diff_keeps_untracked_files_without_hunks()
    test_parse_unified_diff_renders_an_untracked_new_file_patch()
    test_parse_unified_diff_keeps_marker_like_content_inside_hunks()
    test_parse_unified_diff_keeps_spaced_paths_without_marker_lines()
    test_diff_canvas_payload_keeps_report_for_local_mode_switch()
    test_parse_unified_diff_caps_files_without_merging_hunks()
    test_parse_unified_diff_splits_hunk_lines_only_at_newlines()
    test_parse_unified_diff_reads_empty_lines_inside_hunks_as_blank_context()
    print("unified diff canvas tests passed")
