"""Hard process loss preserves uncertainty without disabling later interaction.

The child really exits without cleanup; provider execution is a scripted adapter
that writes a marker into a real directory and emits a native session handle.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from core import session_manager as sm
from test_cooperative_context_recovery import host_factory as make_factory


CHILD = r'''
import asyncio,json,os,sys
from pathlib import Path
from pytest import MonkeyPatch
sys.path.insert(0,str(Path.cwd()/"tests"))
from test_cooperative_context_recovery import host_factory
root=Path(sys.argv[1])
root.mkdir()
async def main():
    patch=MonkeyPatch()
    host=host_factory.__wrapped__(root,patch)()
    host.adapter.release.clear()
    receipt=await host.send("检查目录。","crashed-source")
    await asyncio.wait_for(host.adapter.started.wait(),3)
    child=host.loop.children[host.loop.bound_context_id]
    (root/"crash-evidence.json").write_text(json.dumps({"receipt":receipt,
        "context_id":child.child_id,"workspace":child.workspace}),encoding="utf-8")
    os._exit(77)
asyncio.run(main())
'''


async def test_actual_process_loss_keeps_replay_chat_and_independent_execution_separate(tmp_path, monkeypatch):
    root = tmp_path / "crashed host 真实目录"
    result = await asyncio.to_thread(subprocess.run,
        [sys.executable, "-X", "utf8", "-c", CHILD, str(root)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        encoding="utf-8", timeout=40, env=dict(os.environ, PYTHONUTF8="1"))
    assert result.returncode == 77, result.stdout + result.stderr
    evidence = json.loads((root / "crash-evidence.json").read_text(encoding="utf-8"))
    marker = Path(evidence["workspace"]) / "context.txt"
    assert marker.is_file()
    original_marker = marker.read_bytes()
    create_session = sm.create_session
    monkeypatch.setattr(sm, "create_session", sm.load_session)
    factory = make_factory.__wrapped__(root, monkeypatch)
    monkeypatch.setattr(sm, "create_session", create_session)
    host = factory()
    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] != "user":
            return "收到。"
        text = frame["current"]["text"]
        return json.dumps({"action": ({"op": "delegate", "provider": host.adapter.provider_id}
            if text == "另做独立检查。" else {"op": "send"} if text == "继续原来的检查。" else None), "say": "收到。"})
    host.loop.query = query
    try:
        assert (await host.send("检查目录。", "crashed-source"))["status"] == "replayed"
        assert not host.adapter.requests
        assert (await host.send("继续原来的检查。", "uncertain-continuation"))["state"] == "unknown"
        assert not host.adapter.requests
        assert (await host.send("先聊聊。", "chat"))["state"] == "no_action"
        independent = await host.send("另做独立检查。", "independent")
        assert independent["state"] == "started"
        await host.loop.wait()
        assert len(host.adapter.requests) == 1
        assert host.adapter.requests[0].cwd != evidence["workspace"]
        assert marker.read_bytes() == original_marker
    finally:
        await host.close()
