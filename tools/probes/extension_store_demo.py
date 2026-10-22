"""Local publisher demonstration using unchanged, pinned catalog CLI tools.

No remote pushes, real approval identities, OS trust changes or production test APIs.
The disposable Git history models the input-review/publication boundary, not GitHub
authorization. Every served snapshot is the exact successfully checked CLI output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PACKAGES = {"notes_mcp": "com.example.notes-mcp", "theme": "com.example.desktop-theme"}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Publisher:
    def __init__(self, root, config):
        from tools.probes.extension_catalog_journey import create_certificate
        from tools.smoke_electron_model_less import model_less_backend_environment

        self.root = root
        self.root.mkdir(parents=True)
        self.config = config
        self.repo = root / "repository"
        self.artifacts = root / "artifact-cache"
        self.phases = []
        self.requests = []
        self.offline = False
        self.sequence = 0
        self.current = None
        self.command_number = 0
        self.wheels = {}
        self.environment = model_less_backend_environment(os.environ, python_executable=sys.executable)
        self.environment.update(
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_NAME="Local demo author", GIT_AUTHOR_EMAIL="demo@example.invalid",
            GIT_COMMITTER_NAME="Local demo maintainer", GIT_COMMITTER_EMAIL="demo@example.invalid",
        )
        create_certificate(root)
        # Only maintenance subprocesses trust this disposable certificate. No OS change.
        self.environment["SSL_CERT_FILE"] = str(root / "cert.pem")
        self.environment["NO_PROXY"] = "localhost,127.0.0.1"
        for name in PACKAGES:
            for version in ("0.1.0", "0.2.0"):
                filename = f"amadeus_example_{name}-{version}-py3-none-any.whl"
                source = Path(config["wheels"]) / filename
                destination = root / "wheels" / filename
                destination.parent.mkdir(exist_ok=True)
                shutil.copyfile(source, destination)
                self.wheels[filename] = destination
        self.clone(Path(config["protocol_repo"]), self.repo)
        self.generator = self.git(self.repo, "rev-parse", "HEAD")
        if json.loads((self.repo / "catalog/index.json").read_text())["entries"]:
            raise ValueError("Demo expects the experimental empty bootstrap")
        self.current = root / "bootstrap.json"
        shutil.copyfile(self.repo / "catalog/index.json", self.current)
        publisher = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                route = urlsplit(self.path).path
                file = None
                if route == "/index.json":
                    file = publisher.current
                elif route == "/candidate-index.json":
                    file = publisher.repo / "catalog/index.json"
                elif route.startswith(("/artifacts/", "/mirror/")):
                    candidate = publisher.wheels.get(route.rsplit("/", 1)[-1])
                    if candidate and route in (
                        "/artifacts/" + candidate.name,
                        "/mirror/" + sha256(candidate) + "/" + candidate.name,
                    ):
                        file = candidate
                status = 503 if publisher.offline else 200 if file else 404
                body = file.read_bytes() if status == 200 else b"unavailable"
                publisher.requests.append({
                    "sequence": publisher.sequence, "path": route, "status": status,
                    "sha256": hashlib.sha256(body).hexdigest(), "size": len(body),
                    "client": "maintainer" if self.headers.get("User-Agent", "").startswith("Python-urllib") else "desktop",
                })
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(root / "cert.pem", root / "key.pem")
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.origin = f"https://localhost:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def command(self, argv, *, expected=0):
        self.command_number += 1
        result = subprocess.run(
            [str(x) for x in argv], cwd=ROOT, env=self.environment,
            capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
        evidence = {
            "argv": [str(x) for x in argv], "exit_code": result.returncode,
            "expected_exit": expected, "stdout": result.stdout, "stderr": result.stderr,
        }
        (self.root / f"command-{self.command_number:03}.json").write_text(
            json.dumps(evidence, indent=2), encoding="utf-8"
        )
        if result.returncode != expected:
            raise RuntimeError(f"Publisher command {self.command_number} failed its expected outcome: {result.stdout} {result.stderr}")
        return result.stdout.strip()

    def git(self, repo, *args):
        return self.command(["git", "-C", repo, *args])

    def clone(self, source, destination):
        self.command(["git", "clone", "-c", "core.longpaths=true", "--shared", "--no-checkout", source, destination])
        self.git(destination, "checkout", "--detach", self.git(source, "rev-parse", "HEAD"))
        self.git(destination, "remote", "remove", "origin")

    def commit(self, message):
        self.git(self.repo, "add", "catalog")
        self.git(self.repo, "commit", "-m", message)
        return self.git(self.repo, "rev-parse", "HEAD")

    def tool(self, checkout, name, *args, expected=0):
        return self.command([self.config["checker_python"], checkout / "tools" / name, *args], expected=expected)

    def facts(self, name, version):
        wheel = self.wheels[f"amadeus_example_{name}-{version}-py3-none-any.whl"]
        digest = sha256(wheel)
        return {
            "wheel": {"url": self.origin + "/artifacts/" + wheel.name, "sha256": digest},
            "publisher": {
                "id": "demo-author", "maintainers": ["demo-author"],
                "package_ids": [PACKAGES[name]], "evidence": self.origin + "/demo-only/ownership",
            },
            "review": {
                "sha256": digest, "policy": "curated-v1", "reviewer": "demo-reviewer",
                "date": datetime.now(timezone.utc).date().isoformat(),
                "evidence": self.origin + "/demo-only/simulated-review",
            },
            "withdrawal": None,
        }

    def publish(self, label, *, version=None, withdrawal=None, decision=None):
        number = self.sequence + 1
        phase = self.root / f"{number:02}-{label}"
        phase.mkdir()
        base = phase / "protected-base"
        self.clone(self.repo, base)
        previous_commit = self.git(base, "rev-parse", "HEAD")
        previous = base / "catalog/index.json"
        if version:
            for name in PACKAGES:
                path = self.repo / "catalog/packages" / f"{name}-{version}.json"
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps(self.facts(name, version), indent=2), encoding="utf-8")
                self.tool(base, "preflight_submission.py", "--wheel",
                          self.wheels[f"amadeus_example_{name}-{version}-py3-none-any.whl"],
                          "--host", self.config["checker_host"], "--python", self.config["checker_python"],
                          "--output", phase / f"{name}-preflight.json")
                args = ["--wheel", self.wheels[f"amadeus_example_{name}-{version}-py3-none-any.whl"],
                        "--output", phase / f"{name}-review.json"]
                if version == "0.2.0":
                    args += ["--previous-wheel", self.wheels[f"amadeus_example_{name}-0.1.0-py3-none-any.whl"]]
                self.tool(base, "check_catalog.py", *args)
        if withdrawal is not None:
            path = self.repo / "catalog/packages/notes_mcp-0.2.0.json"
            facts = json.loads(path.read_text())
            facts["withdrawal"] = (
                {"reason": "copyright", "evidence": self.origin + "/demo-only/copyright"}
                if withdrawal else None
            )
            path.write_text(json.dumps(facts, indent=2), encoding="utf-8")
        if decision:
            safety = [
                {"id": PACKAGES[name], "version": "0.2.0",
                 "sha256": self.facts(name, "0.2.0")["wheel"]["sha256"],
                 "status": decision, "sequence": number,
                 "reason": "security" if decision == "blocked" else "security-resolved",
                 "evidence": self.origin + "/demo-only/" + decision}
                for name in PACKAGES
            ]
            (self.repo / "catalog/security.json").write_text(json.dumps(safety, indent=2), encoding="utf-8")
        common = [
            "--host", self.config["checker_host"], "--python", self.config["checker_python"],
            "--artifacts", self.artifacts,
        ]
        revision = None
        if number == 1:
            path = self.repo / "catalog/packages/notes_mcp-0.1.0.json"
            correct = path.read_bytes()
            wrong = json.loads(correct)
            wrong["wheel"]["sha256"] = "0" * 64
            path.write_text(json.dumps(wrong), encoding="utf-8")
            rejected_head = self.commit("DEMO ONLY: submission with incorrect artifact digest")
            self.tool(base, "check_catalog_ci.py", "--proposal", self.repo, "--base", previous_commit,
                      "--evidence", phase / "rejected-input-evidence", *common, expected=1)
            path.write_bytes(correct)
            revision = {"rejected_head": rejected_head, "reason": "wrong wheel digest",
                        "same_submission": True, "approval_was_not_granted": True}
        input_commit = self.commit(f"DEMO ONLY: {label} input and simulated review")
        self.tool(base, "check_catalog_ci.py", "--proposal", self.repo, "--base", previous_commit,
                  "--evidence", phase / "input-evidence", *common)
        # Simulate reviewed-input merge locally; never alter any public branch.
        self.git(self.repo, "update-ref", "refs/heads/demo-approved", input_commit)
        build = [
            "--source-repo", self.repo, "--source-commit", input_commit,
            "--approved-ref", "demo-approved", "--sequence", str(number),
            "--previous", previous, "--output", self.repo / "catalog/index.json",
            "--fetch", *common,
        ]
        # Always run the original reviewed generator revision, not submitted input code.
        generator = self.root / "generator"
        if not generator.exists():
            self.clone(Path(self.config["protocol_repo"]), generator)
            self.git(generator, "checkout", "--detach", self.generator)
        self.tool(generator, "build_catalog.py", *build)
        self.tool(generator, "build_catalog.py", *build, "--check")
        self.tool(base, "check_catalog.py", "--index", self.repo / "catalog/index.json", "--previous", previous)
        publication = self.commit(f"DEMO ONLY: publish catalog sequence {number}")
        self.tool(base, "check_catalog_ci.py", "--proposal", self.repo, "--base", input_commit,
                  "--evidence", phase / "publication-evidence", *common)
        self.tool(generator, "verify_publication.py", "--index-url", self.origin + "/candidate-index.json",
                  "--expected", self.repo / "catalog/index.json", "--previous", previous,
                  "--output", phase / "candidate-delivery.json")
        snapshot = phase / "index.json"
        shutil.copyfile(self.repo / "catalog/index.json", snapshot)
        self.current = snapshot
        self.sequence = number
        self.tool(generator, "verify_publication.py", "--index-url", self.origin + "/index.json",
                  "--expected", snapshot, "--previous", previous,
                  "--output", phase / "live-delivery.json")
        data = json.loads(snapshot.read_text())
        evidence = {
            "label": label, "sequence": number, "input_commit": input_commit,
            "publication_commit": publication, "generator_commit": data["generator_commit"],
            "index_sha256": sha256(snapshot), "entries": len(data["entries"]),
            "input_ci": True, "publication_ci": True, "exact_rebuild": True,
            "simulated_review_only": True, "submission_revision": revision,
            "candidate_delivery": True, "live_delivery": True,
        }
        self.phases.append(evidence)
        (phase / "publication.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        self.save()
        return data

    def verify_rejections(self):
        """Publication CI must reject hand-edited output; prior safety cannot vanish."""
        before = sha256(self.current)
        proposal = self.root / "rejected-proposal"
        self.clone(self.repo, proposal)
        index = json.loads(self.current.read_text())
        index["sequence"] += 1
        index["entries"][0]["display"]["description"] = "DEMO tampered output"
        (proposal / "catalog/index.json").write_text(json.dumps(index), encoding="utf-8")
        self.tool(self.repo, "check_catalog_ci.py", "--proposal", proposal,
                  "--base", self.git(self.repo, "rev-parse", "HEAD"),
                  "--host", self.config["checker_host"], "--python", self.config["checker_python"],
                  "--artifacts", self.artifacts, "--evidence", self.root / "rejected-evidence",
                  expected=1)
        index = json.loads(self.current.read_text())
        index["sequence"] += 1
        index["security"] = []
        rejected = self.root / "rejected-missing-safety.json"
        rejected.write_text(json.dumps(index), encoding="utf-8")
        self.tool(self.repo, "check_catalog.py", "--index", rejected,
                  "--previous", self.current, expected=1)
        assert sha256(self.current) == before

    def save(self):
        (self.root / "report.json").write_text(json.dumps({
            "simulated_authority": True, "public_release": False,
            "phases": self.phases, "requests": self.requests,
            "wheels": [{"file": p.name, "sha256": sha256(p)} for p in self.wheels.values()],
        }, indent=2), encoding="utf-8")

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--protocol-repo", type=Path, required=True)
    parser.add_argument("--checker-host", type=Path, required=True)
    parser.add_argument("--checker-python", type=Path, required=True)
    parser.add_argument("--wheels", type=Path, required=True)
    parser.add_argument("--publisher-only", action="store_true", help="Run local Git/preflight/revision/distribution tools without launching the desktop")
    parser.add_argument("--packaged-executable", type=Path)
    parser.add_argument("--report-dir", type=Path, default=ROOT / "build/extension-store-demo")
    args = parser.parse_args()
    directory = args.report_dir.resolve() / uuid.uuid4().hex[:12]
    directory.mkdir(parents=True)
    config = {name: str(getattr(args, name).resolve())
              for name in ("protocol_repo", "checker_host", "checker_python", "wheels")}
    invocation = directory / "invocation.json"
    invocation.write_text(json.dumps(config, indent=2), encoding="utf-8")
    if args.publisher_only:
        publisher = Publisher(directory / "publisher", config)
        try:
            for label, changes in (
                ("initial", {"version": "0.1.0"}),
                ("update", {"version": "0.2.0"}),
                ("copyright-withdrawal", {"withdrawal": True}),
                ("security-block", {"decision": "blocked"}),
                ("security-unblock", {"decision": "allowed"}),
                ("restore-distribution", {"withdrawal": False}),
            ):
                publisher.publish(label, **changes)
            publisher.verify_rejections()
            result = {"ok": True, "scope": "publisher-only", "desktop": "not_run",
                      "phases": publisher.phases, "publication_rejections": "passed"}
            (directory / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps({"ok": True, "report": str(directory / "result.json"), "desktop": "not_run"}))
            return 0
        finally:
            publisher.close()
    command = [sys.executable, ROOT / "tools/smoke_electron_model_less.py",
               "--extension-publication", invocation, "--report-dir", directory]
    if args.packaged_executable:
        command += ["--packaged-executable", args.packaged_executable.resolve()]
    return subprocess.run([str(x) for x in command], cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
