"""Explicit local HTTPS/installer/recovery qualification with real reference wheels.

Requires cryptography for disposable loopback TLS certificates; never changes OS trust.
The Electron driver pins only this generated certificate in its isolated test session.
"""

from __future__ import annotations
import argparse
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server.extensions.catalog import policy
from tools.extension_package import host_profile
from config.process_tree import ProcessTree
from config.subprocess_environment import isolated_child_environment


def create_certificate(base: Path):
    """Create disposable loopback TLS material without changing OS trust."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    (base / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (base / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--wheels", type=Path, required=True)
    parser.add_argument(
        "--store-ui",
        action="store_true",
        help="Run the real store UI journey with both reference versions",
    )
    parser.add_argument("--prepare-only", action="store_true", help="Prepare an isolated TLS/catalog fixture without starting Electron")
    parser.add_argument("--failure-fixture", action="store_true", help="Add a deliberately failing runtime version for shipping-shell qualification")
    args = parser.parse_args()
    base = ROOT / "build/extension-catalog-journey" / uuid.uuid4().hex[:12]
    base.mkdir(parents=True)
    create_certificate(base)
    entries = []
    wheels = {}
    for name, version in [
        (name, version)
        for name in ["tone_tts", "notes_mcp", "handoff_skill", "theme"]
        for version in (["0.1.0", "0.2.0"] if args.store_ui else ["0.1.0"])
    ]:
        wheel = args.wheels.resolve() / f"amadeus_example_{name}-{version}-py3-none-any.whl"
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        source = dict(
            wheel=dict(url="https://artifact.test/" + wheel.name, sha256=digest),
            publisher=dict(
                id="fixture-author",
                maintainers=["fixture-author"],
                package_ids=[
                    {
                        "tone_tts": "com.example.tone-tts",
                        "notes_mcp": "com.example.notes-mcp",
                        "handoff_skill": "com.example.handoff-skill",
                        "theme": "com.example.desktop-theme",
                    }[name]
                ],
                evidence="https://fixture.test/ownership",
            ),
            review=dict(
                sha256=digest,
                policy="curated-v1",
                reviewer="fixture-reviewer",
                date="2026-10-21",
                evidence="https://fixture.test/review",
            ),
            withdrawal=None,
        )
        entries.append(
            policy().inspect_entry(source, wheel, ROOT, Path(sys.executable), host_profile())
        )
        wheels[wheel.name] = str(wheel)
    index = dict(
        format="amadeus.catalog/v1",
        sequence=1,
        source_commit="a" * 40,
        source_time="2026-10-21T00:00:00Z",
        generator_commit=policy().PIN["host_commit"],
        host_commit=policy().PIN["host_commit"],
        entries=entries,
        security=[],
    )
    policy().validate_index(index)
    fixture = dict(index=index, wheels=wheels)
    if args.failure_fixture:
        from tools.probes.extension_release_failures import prepare_startup_failure
        failure, wheel = prepare_startup_failure(base, entries[0])
        fixture["startup_failure"] = failure
        wheels[wheel.name] = str(wheel)
    (base / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
    if args.prepare_only:
        print(json.dumps({"fixture": str(base)}))
        return 0
    app = base / "app"
    app.mkdir()
    (app / "package.json").write_text(
        json.dumps(
            dict(
                name="catalog-qualification",
                version="0.0.0",
                type="module",
                main=str(
                    ROOT
                    / (
                        "electron/tests/extensionStore.smoke.mjs"
                        if args.store_ui
                        else "electron/tests/extensionCatalog.smoke.mjs"
                    )
                ),
            )
        )
    )
    env = isolated_child_environment(
        extra={
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "AMADEUS_CATALOG_JOURNEY_ROOT": str(base),
            "AMADEUS_TEST_PYTHON": sys.executable,
        }
    )
    command = [str(ROOT / "electron/node_modules/electron/dist/electron.exe"), str(app)]

    def run(phase, arguments, timeout):
        with (base / (phase + ".log")).open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                arguments,
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=(0x4 | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            tree = ProcessTree(process)
            try:
                tree.resume()
                return process.wait(timeout=timeout)
            finally:
                tree.terminate()

    if args.store_ui:
        subprocess.run(
            ["node", "--check", str(ROOT / "electron/tests/extensionStore.smoke.mjs")],
            cwd=ROOT,
            env=env,
            check=True,
            timeout=30,
        )
    code = run("driver", command, 600)
    if code == 0 and not args.store_ui:
        code = run("recovery", command + ["--recover"], 240)
    print(json.dumps(dict(ok=code == 0, report=str(base / "report.json"), returncode=code)))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
