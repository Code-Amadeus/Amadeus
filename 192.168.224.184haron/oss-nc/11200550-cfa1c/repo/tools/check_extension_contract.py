"""Explicitly execute a trusted installed package in disposable data/session directories.

Requires a wheel and ONE record exported from an isolated desktop installer snapshot.
This is an author diagnostic, not an OS sandbox or catalog approval. No implicit pip.
"""

from __future__ import annotations
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.extension_package import check_wheel, host_profile
from config.subprocess_environment import isolated_child_environment
from config.process_tree import ProcessTree


def read_json(path):
    with Path(path).open("rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("input exceeds 1 MiB")
    return json.loads(raw)


def checkpoint(path, report, stage):
    report["stage"] = stage
    path.write_text(json.dumps(report, ensure_ascii=True), encoding="utf-8")


def check_tts(record, scenario, mark):
    from server.extensions.runner import RunnerClient
    from server.extensions.tts import ExtensionTTSBackend
    from tts.backend import TTSSynthesisRequest, TTSBackendError

    contribution = next(
        c
        for c in record["manifest"]["contributes"]["voice_backend"]
        if c["id"] == scenario["contribution"]
    )
    client = RunnerClient(record)
    try:
        mark("tts.initialize_ready")
        client.start()
        expires = time.monotonic() + 35
        while client.state == "starting" and time.monotonic() < expires:
            time.sleep(0.02)
        mark("tts.initialize_ready", runtime_state=client.state)
        if client.state != "active":
            raise RuntimeError("Runner not ready")
        backend = ExtensionTTSBackend(record["id"], contribution, client)
        mark("tts.synthesize")
        audio = list(
            backend.synthesize_stream(
                TTSSynthesisRequest(text=scenario["text"], language=scenario["language"])
            )
        )
        if not audio or not any(chunk.audio.size for chunk in audio):
            raise RuntimeError("No audio")
        mark("tts.undeclared_language")
        try:
            list(
                backend.synthesize_stream(
                    TTSSynthesisRequest(text="Synthetic negative check", language="zz")
                )
            )
        except TTSBackendError:
            pass
        else:
            raise RuntimeError("Undeclared language admitted")
        mark("tts.cancel")
        stream = client.stream(
            contribution["id"],
            text=scenario["cancel_text"],
            language=scenario["language"],
            speed=1,
            deadline_ms=5000,
        )
        next(stream)
        stream.close()  # same cooperative cancellation path as the Host stream owner
        mark("tts.close")
    finally:
        client.close()
    return {
        "audio_chunks": len(audio),
        "cancellation": "Host stream closed; adversarial late-audio cases are separate regressions",
    }


async def check_mcp(record, scenario, mark):
    from agent_host.extension_mcp import (
        project_extension_mcp_connections,
        extension_mcp_connection_id,
    )
    from agent_host.mcp_connections import open_mcp_connection

    mark("mcp.provider_binding")
    provider = scenario["provider"]
    connections = project_extension_mcp_connections(record)
    connection = next(
        c
        for c in connections
        if c.connection_id == extension_mcp_connection_id(record["id"], scenario["contribution"])
    )
    if not connection.enabled or not connection.supports_provider(provider):
        raise ValueError("Selected Provider is not bound")
    mark("mcp.discover")
    async with open_mcp_connection(connection) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        for call in scenario["calls"]:
            if call["tool"] not in tools:
                raise ValueError("Tool not discovered")
            mark("mcp.call")
            result = await client.call_tool(call["tool"], call.get("arguments", {}))
            if result.is_error:
                raise RuntimeError("Tool returned an error")
            if call.get("contains") and call["contains"] not in str(result):
                raise RuntimeError("Tool output differs from synthetic expectation")
        mark("mcp.close")
    return {"tools": len(tools), "calls": len(scenario["calls"])}


async def check_skill(record, scenario, base, mark):
    from tools.probes.extension_contract_pi import read_skill_with_pi

    return await read_skill_with_pi(record, scenario, base, mark)


def check_theme(record, base, mark):
    executable = ROOT / "electron/node_modules/electron/dist/electron.exe"
    if sys.platform != "win32" or not executable.is_file():
        raise RuntimeError("Windows Electron build required")
    app = base / "theme-app"
    app.mkdir()
    (app / "package.json").write_text(
        json.dumps(
            {
                "name": "extension-contract-theme",
                "version": "0.0.0",
                "type": "module",
                "main": str(ROOT / "electron/scripts/check-extension-theme.mjs"),
            }
        ),
        encoding="utf-8",
    )
    selected = base / "theme-record.json"
    selected.write_text(
        json.dumps({"manifest": record["manifest"], "package_path": record["package_path"]}),
        encoding="utf-8",
    )
    mark("theme.native_decode_and_paint")
    result = subprocess.run(
        [str(executable), str(app)],
        env=isolated_child_environment(
            extra={
                "AMADEUS_CONTRACT_THEME_RECORD": str(selected),
                "AMADEUS_CONTRACT_THEME_ROOT": str(base),
            }
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=40,
    )
    if result.returncode:
        raise RuntimeError("Native theme validation failed")
    return read_json(base / "theme-result.json")


def worker(args):
    base = args.report.parent
    report = read_json(args.report)

    def mark(stage, **facts):
        report.update(facts)
        checkpoint(args.report, report, stage)

    try:
        from server.extensions.runtime import ExtensionRuntime

        record, missing = ExtensionRuntime._validate_record(read_json(args.record))
        if missing or not record["enabled"]:
            raise ValueError("Enable and configure the isolated package first")
        scenario = read_json(args.scenario)
        mark("record.validated")
        if args.kind == "tts":
            detail = check_tts(record, scenario, mark)
            layer = "process_contract"
        elif args.kind == "mcp":
            detail = asyncio.run(check_mcp(record, scenario, mark))
            layer = "process_contract"
        elif args.kind == "skill":
            detail = asyncio.run(check_skill(record, scenario, base, mark))
            layer = "native_agent"
        else:
            detail = check_theme(record, base, mark)
            layer = "gui"
        report["layers"][layer] = "passed"
        report.update(ok=True, detail=detail)
        mark("complete")
        return 0
    except Exception as error:
        # Arbitrary plugin exceptions/tool output may contain tokens. Report only controlled stages/classes.
        report.update(ok=False, error=type(error).__name__)
        checkpoint(args.report, report, report["stage"])
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--kind", choices=["tts", "mcp", "skill", "theme"], required=True)
    parser.add_argument(
        "--execute", action="store_true", help="Acknowledge that this trusted package will execute"
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow an explicitly selected remote service; it may be billable",
    )
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return worker(args)
    report = {
        "ok": False,
        "stage": "input",
        "kind": args.kind,
        "layers": dict.fromkeys(
            ["static", "process_contract", "native_agent", "gui", "remote_service"], "not_run"
        ),
    }
    try:
        if not args.execute or not args.wheel or not 1 <= args.timeout <= 300:
            raise ValueError("explicit execution and a wheel are required; timeout must be 1..300")
        report["stage"] = "static"
        inspection = check_wheel(args.wheel)
        record = read_json(args.record)
        if (
            record["manifest"] != inspection["manifest"]
            or record["digest"] != inspection["digest"]
            or record["id"] != inspection["manifest"]["id"]
            or record["version"] != inspection["manifest"]["version"]
        ):
            raise ValueError("record does not identify this wheel")
        contribution_kind = {
            "tts": "voice_backend",
            "mcp": "mcp_server",
            "skill": "skill",
            "theme": "theme",
        }[args.kind]
        contributions = record["manifest"]["contributes"].get(contribution_kind, [])
        if not contributions:
            raise ValueError("selected contribution kind is absent")
        remote = any(
            c.get("deployment") == "remote" or c.get("transport") == "http" for c in contributions
        )
        if remote and not args.allow_remote:
            raise ValueError("remote-service execution requires --allow-remote")
        report.update(
            identity={"id": record["id"], "version": record["version"], "sha256": record["digest"]},
            host=host_profile(),
        )
        report["layers"]["static"] = "passed"
        with tempfile.TemporaryDirectory(prefix="amadeus-contract-") as temporary:
            base = Path(temporary)
            record = copy.deepcopy(record)
            record["data_path"] = str(base / "data")
            Path(record["data_path"]).mkdir()
            selected = base / "record.json"
            selected.write_text(json.dumps(record), encoding="utf-8")
            scenario = base / "scenario.json"
            scenario.write_text(json.dumps(read_json(args.scenario)), encoding="utf-8")
            progress = base / "report.json"
            checkpoint(progress, report, "worker.start")
            command = [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--worker",
                "--record",
                str(selected),
                "--scenario",
                str(scenario),
                "--kind",
                args.kind,
                "--report",
                str(progress),
            ]
            env = isolated_child_environment(extra={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
            process = subprocess.Popen(
                command,
                cwd=base,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=(0x4 | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            tree = ProcessTree(process)
            try:
                tree.resume()
                try:
                    code = process.wait(timeout=args.timeout)
                    report = read_json(progress)
                    report["worker_exit_code"] = code
                    if code or report.get("stage") != "complete":
                        report["ok"] = False
                except subprocess.TimeoutExpired:
                    report = read_json(progress)
                    report.update(ok=False, error="TimeoutExpired")
            finally:
                tree.terminate()
        if remote and report["ok"]:
            report["layers"]["remote_service"] = "passed"
    except Exception as error:
        report.update(ok=False, error=type(error).__name__)
    encoded = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
