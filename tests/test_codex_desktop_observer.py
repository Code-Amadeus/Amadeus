import json
import sqlite3
from datetime import datetime, timezone, timedelta

from server.codex_desktop_observer import ACTIVE_WINDOW_MS, CodexDesktopObserver, CodexRolloutTask, _records_backwards

THREAD = "00000000-0000-4000-8000-000000000001"


def event(kind, **data):
    return {"type": "event_msg", "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "payload": {"type": kind, **data}}


def question(call="q1", asynchronous=True):
    return {"type": "response_item", "payload": {
        "type": "function_call", "call_id": call,
        "name": "request_user_input_async" if asynchronous else "request_user_input",
        "arguments": json.dumps({"questions": [{"title": "原文首问？", "options": ["不要读选项"]}, {"title": "不要读第二题"}]})}}


def reply(call="q1", index=0):
    text = "<send_user_message_question_reply>" + json.dumps([{
        "questionItemId": json.dumps(["request_user_input_async", call, index]), "answer": "是"}]) + "</send_user_message_question_reply>"
    return {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": text}]}}


def test_question_identity_and_async_ack_are_not_an_answer():
    task = CodexRolloutTask(THREAD, "任务")
    task.consume(event("task_started", turn_id="t1"))
    task.consume(question())
    task.consume({"type": "response_item", "payload": {"type": "function_call_output", "call_id": "q1", "output": "sent"}})
    assert task.snapshot()["detail"] == "原文首问？"
    task.consume(reply(index=1))
    task.consume(reply(call="different"))
    assert task.snapshot()["phase"] == "attention"
    task.consume(reply())
    assert task.snapshot()["phase"] == "running"
    task.consume(question(call="q2"))
    assert task.snapshot()["key"].endswith(":q2")


def test_turn_completion_requires_matching_codex_event_and_has_no_fake_read_receipt():
    task = CodexRolloutTask(THREAD, "任务")
    task.consume(event("task_started", turn_id="t2"))
    task.consume(event("task_complete", turn_id="t1", last_agent_message="旧结果"))
    assert task.snapshot()["phase"] == "running"
    task.consume(event("task_complete", turn_id="t2", last_agent_message="真实产出"))
    assert task.snapshot()["phase"] == "ready"
    assert task.snapshot()["detail"] == "真实产出"
    assert task.snapshot()["repeatable"] is True  # bounded local reminder, not an unread claim
    assert "unread" not in task.snapshot()


def test_tool_output_or_quoted_question_cannot_create_notification():
    task = CodexRolloutTask(THREAD, "任务")
    task.consume({"type": "response_item", "payload": {"type": "function_call_output", "output": json.dumps(question())}})
    assert task.snapshot()["phase"] == "running"


def setup_home(tmp_path, status="completed", hours_ago=9):
    path = tmp_path / "rollout.jsonl"
    records = [event("task_started", turn_id="old")]
    if status == "completed":
        records.append(event("task_complete", turn_id="old"))
        for record in records:
            record["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT, name TEXT, title TEXT, archived INTEGER, source TEXT)")
        db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, 0, 'vscode')", (THREAD, str(path), "正式任务名", "最初输入"))
    return path


def test_no_history_replay_partial_records_and_future_turns(tmp_path):
    path = setup_home(tmp_path)
    observer = CodexDesktopObserver(tmp_path, [THREAD])
    assert observer.poll()["tasks"] == []
    with path.open("a") as f:
        f.write(json.dumps(event("task_started", turn_id="new")) + "\n")
        f.write(json.dumps(question()))
    assert observer.poll()["tasks"][0]["phase"] == "running"
    with path.open("a") as f:
        f.write("\n")
    assert observer.poll()["tasks"][0]["phase"] == "attention"
    with path.open("a") as f:
        f.write(json.dumps(reply()) + "\n")
    assert observer.poll()["tasks"][0]["phase"] == "running"
    with path.open("a") as f:
        f.write(json.dumps(event("task_complete", turn_id="new", last_agent_message="新结果")) + "\n")
    assert observer.poll()["tasks"][0]["detail"] == "新结果"


def test_missing_source_is_observable_error_not_finished_task(tmp_path):
    snapshot = CodexDesktopObserver(tmp_path, [THREAD]).poll()
    assert snapshot["status"] == "error"
    assert snapshot["tasks"] == []


def test_project_name_uses_explicit_membership_and_not_task_title(tmp_path):
    setup_home(tmp_path, status="inProgress")
    state = {"local-projects": {"project": {"name": "项目名称", "rootPaths": [str(tmp_path)]}},
             "thread-project-assignments": {THREAD: {"projectKind": "local", "projectId": "project"}}}
    state_path = tmp_path / ".codex-global-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    snapshot = CodexDesktopObserver(tmp_path, [THREAD]).poll()["tasks"][0]
    assert snapshot["projectName"] == "项目名称"
    assert snapshot["title"] == "正式任务名"
    state["thread-project-assignments"] = {}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    snapshot = CodexDesktopObserver(tmp_path, [THREAD]).poll()["tasks"][0]
    assert snapshot["projectName"] == ""


def test_all_projects_discover_future_and_resumed_tasks_without_replaying_history(tmp_path):
    old_path = setup_home(tmp_path)
    observer = CodexDesktopObserver(tmp_path)
    assert observer.poll()["tasks"] == []
    # A stale database projection cannot bring back the completed raw turn.
    (tmp_path / "thread_history_1.sqlite").write_text("stale unused history")
    assert observer.poll()["tasks"] == []
    future_id = "00000000-0000-4000-8000-000000000002"
    future_path = tmp_path / "future.jsonl"
    future_path.write_text("".join(json.dumps(row) + "\n" for row in [
        event("task_started", turn_id="fast"), event("task_complete", turn_id="fast", last_agent_message="另一项目结果"),
    ]))
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, 0, 'vscode')", (future_id, str(future_path), "其他项目任务", "original"))
        db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, 0, ?)", ("guardian", str(future_path), "后台子代理", "guardian", '{"subagent":{"other":"guardian"}}'))
    observer.discover_at = 0
    task = observer.poll()["tasks"][0]
    assert task["id"] == future_id and task["phase"] == "ready"
    with old_path.open("a") as stream:
        stream.write(json.dumps(event("task_started", turn_id="resumed")) + "\n" + json.dumps(question()) + "\n")
    tasks = observer.poll()["tasks"]
    assert len(tasks) == 2
    assert next(task for task in tasks if task["id"] == THREAD)["phase"] == "attention"


def test_reverse_baseline_handles_large_utf8_records_and_partial_last_line(tmp_path):
    path = tmp_path / "large.jsonl"
    rows = [event("task_started", turn_id="one"), {"text": "中文" * 40000}, event("task_complete", turn_id="one")]
    encoded = [(json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows]
    path.write_bytes(b"".join(encoded) + b'{"incomplete":')
    reversed_rows = list(_records_backwards(path))
    assert [row for at, row in reversed_rows] == list(reversed(rows))
    assert [at for at, row in reversed_rows] == [len(encoded[0]) + len(encoded[1]), len(encoded[0]), 0]


def test_projectless_title_uses_desktop_index_and_updates_without_a_new_notification(tmp_path):
    setup_home(tmp_path, status="inProgress")
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET name=NULL, title='第一轮的完整提问'")
    index = tmp_path / "session_index.jsonl"
    index.write_text(json.dumps({"id": THREAD, "thread_name": "桌面任务名"}) + "\n", encoding="utf-8")
    observer = CodexDesktopObserver(tmp_path)
    before = observer.poll()["tasks"][0]
    assert before["projectName"] == "" and before["title"] == "桌面任务名"
    # The index is append-only. Ignore a partial new entry until it is complete.
    with index.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"id": THREAD, "thread_name": "修改后的任务名"}))
    observer.discover_at = 0
    assert observer.poll()["tasks"][0]["title"] == "桌面任务名"
    with index.open("a", encoding="utf-8") as stream:
        stream.write("\n")
    observer.discover_at = 0
    after = observer.poll()["tasks"][0]
    assert after["title"] == "修改后的任务名"
    assert after["key"] == before["key"]
    assert after["announce"] is False


def test_missing_saved_title_does_not_turn_the_first_prompt_into_a_heading(tmp_path):
    setup_home(tmp_path, status="inProgress")
    # New-format database names remain usable without a title index.
    assert CodexDesktopObserver(tmp_path).poll()["tasks"][0]["title"] == "正式任务名"
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET name=NULL, title='第一轮的完整提问'")
    assert CodexDesktopObserver(tmp_path).poll()["tasks"][0]["title"] == "Codex 任务"


def test_recent_completed_work_restores_silently_with_original_activity_time(tmp_path):
    path = setup_home(tmp_path, hours_ago=7)
    observer = CodexDesktopObserver(tmp_path)
    task = observer.poll()["tasks"][0]
    assert task["phase"] == "ready" and task["announce"] is False
    assert observer.connected_at_ms - ACTIVE_WINDOW_MS < task["lastActivityAt"] < observer.connected_at_ms
    assert observer.poll()["tasks"][0]["lastActivityAt"] == task["lastActivityAt"]
    with path.open("a") as stream:
        stream.write(json.dumps(event("task_started", turn_id="new")) + "\n")
        stream.write(json.dumps(event("task_complete", turn_id="new", last_agent_message="新结果")) + "\n")
    updated = observer.poll()["tasks"][0]
    assert updated["announce"] is True and updated["lastActivityAt"] > task["lastActivityAt"]


def test_only_visible_task_activity_moves_retention_clock():
    task = CodexRolloutTask(THREAD, "任务")
    start = event("task_started", turn_id="t1")
    start["timestamp"] = "2026-09-06T01:00:00Z"
    task.consume(start)
    original = task.snapshot()["lastActivityAt"]
    for record_type, payload in [
        ("event_msg", {"type": "token_count"}),
        ("turn_context", {}),
        ("response_item", {"type": "reasoning"}),
        ("response_item", {"type": "message", "role": "assistant", "phase": "analysis"}),
        ("event_msg", {"type": "task_complete", "turn_id": "wrong-turn"}),
    ]:
        task.consume({"type": record_type, "timestamp": "2026-09-06T02:00:00Z", "payload": payload})
    assert task.snapshot()["lastActivityAt"] == original
    task.consume({"type": "response_item", "timestamp": "2026-09-06T03:00:00Z",
                  "payload": {"type": "function_call_output", "output": "command result"}})
    assert task.snapshot()["lastActivityAt"] == original + 2 * 60 * 60 * 1000
    assert task.snapshot()["announce"] is False


def test_public_timeline_tracks_tools_and_result_without_reasoning_or_raw_tool_arguments():
    task = CodexRolloutTask(THREAD, "任务")
    task.consume(event("task_started", turn_id="one"))
    task.consume({"type":"response_item","payload":{"type":"message","role":"assistant","phase":"commentary","content":[{"text":"开始检查"}]}})
    task.consume({"type":"response_item","payload":{"type":"function_call","name":"exec_command","call_id":"tool-1","arguments":"private command args"}})
    before = task.snapshot()
    task.consume({"type":"response_item","payload":{"type":"function_call_output","call_id":"tool-1","output":"raw result"}})
    assert before["activities"][-1]["status"] == "running"
    task.consume(event("task_complete", turn_id="one", last_agent_message="已完成检查"))
    history = task.snapshot()["activities"]
    assert [item["kind"] for item in history] == ["progress","tool","result"]
    assert history[1]["status"] == "returned"
    assert "private command args" not in json.dumps(history)
    task.consume(event("task_started", turn_id="two"))
    assert task.snapshot()["activities"] == []


def test_subagents_inherit_proven_parent_project_and_are_visual_only(tmp_path):
    setup_home(tmp_path, status="inProgress")
    child_id = "00000000-0000-4000-8000-000000000002"
    child_path = tmp_path / "child.jsonl"
    child_path.write_text(json.dumps(event("task_started", turn_id="child")) + "\n")
    source = json.dumps({"subagent":{"thread_spawn":{"parent_thread_id":THREAD,"agent_nickname":"Tester"}}})
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("INSERT INTO threads VALUES (?,?,?,?,0,?)", (child_id,str(child_path),"检查测试","first prompt",source))
    (tmp_path / ".codex-global-state.json").write_text(json.dumps({"local-projects":{"one":{"name":"Amadeus"}},
        "thread-project-assignments":{THREAD:{"projectKind":"local","projectId":"one"}}}))
    tasks = CodexDesktopObserver(tmp_path).poll()["tasks"]
    child = next(item for item in tasks if item["id"] == child_id)
    assert child["projectId"] == "one" and child["projectName"] == "Amadeus"
    assert child["parentTaskId"] == THREAD and child["sourceKind"] == "subagent"
    assert not child["announce"] and not child["repeatable"]
