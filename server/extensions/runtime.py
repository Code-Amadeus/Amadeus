"""Read-only startup activation of Electron-owned desired extension records."""
from __future__ import annotations

import atexit
import copy
import re
import threading
import time
from pathlib import Path
from typing import Any

from server.extensions.package import validate_config, validate_installed, validate_manifest
from server.extensions.runner import RunnerClient
from server.extensions.tts import ExtensionTTSBackend
from server.extensions.profile import contribution_profile
from agent_host.skills import SkillCatalog, configure_skills

_current: ExtensionRuntime | None = None


def localized_text(value: Any, fallback: str = "") -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("en-US") or value.get("en") or next(iter(value.values()), fallback))
    return fallback


class ExtensionRuntime:
    def __init__(self, records: list[dict[str, Any]], *, provider_manifest_lookup=None) -> None:
        self.records: list[dict[str, Any]] = []
        self.clients: dict[str, RunnerClient] = {}
        self.backends: dict[str, ExtensionTTSBackend] = {}
        self._observed: dict[str, dict[str, Any]] = {}
        self._mcp_errors: dict[str, str] = {}
        self._blocked: set[str] = set()
        self._provider_manifest_lookup = provider_manifest_lookup
        self._lock = threading.RLock()
        self._started = False
        self._closed = False
        duplicate_ids = {str(record.get("id")) for record in records if sum(other.get("id") == record.get("id") for other in records) > 1}
        for raw in records:
            package_id = str(raw.get("id") or "unknown")
            try:
                if package_id in duplicate_ids:
                    raise ValueError("duplicate extension startup identity")
                record, missing = self._validate_record(raw)
                self.records.append(record)
                enabled = record["enabled"]
                state = "inactive" if not enabled else "needs_config" if missing else "starting" if "entry" in record["manifest"]["runtime"] else "active"
                detail = "Package is disabled" if not enabled else "Required configuration is missing: " + ", ".join(missing) if missing else "Runner is starting" if state == "starting" else "Declarative contributions are registered at startup"
                self._observed[package_id] = {"id": package_id, "version": record["version"], "revision": record['revision'], "startup": "rejected" if missing else "accepted", "enabled": enabled, "state": state, "detail": detail, "contributions": []}
                client = None
                if enabled and not missing and "entry" in record["manifest"]["runtime"]:
                    client = RunnerClient(record)
                    self.clients[package_id] = client
                for contribution in record["manifest"]["contributes"].get("voice_backend", []):
                    backend = ExtensionTTSBackend(package_id, contribution, client)
                    self.backends[backend.backend_id] = backend
            except (ValueError, KeyError, TypeError, OSError):
                # The failing boundary may contain paths or secrets; return one safe signal.
                self._observed[package_id] = {"id": package_id, "version": str(raw.get("version") or ""), "enabled": False,
                                              "state": "incompatible", "detail": "Extension startup record is invalid or incompatible", "contributions": []}
                # A terminal rejection acknowledges a unique received identity/revision,
                # not a loaded package. Malformed identities/revisions remain unconfirmed.
                revision = raw.get("revision", 1)
                if (isinstance(raw.get("id"), str) and raw["id"] and package_id not in duplicate_ids
                        and type(revision) is int and 1 <= revision <= 2**53 - 1):
                    self._observed[package_id].update(revision=revision, startup="rejected")
                self._blocked.add(package_id)
                # Retain a valid manifest's saved intent and unavailable contribution
                # identities while refusing to execute unavailable installation paths.
                try:
                    manifest = validate_manifest(raw["manifest"])
                    if (package_id in duplicate_ids or manifest["id"] != package_id or raw["version"] != manifest["version"] or
                            type(raw.get("enabled")) is not bool or not isinstance(raw.get("digest"), str) or
                            not re.fullmatch(r"[a-f0-9]{64}", raw["digest"])):
                        continue
                    safe_record = {**copy.deepcopy(raw), "config": {}, "secrets": {}, "providers": []}
                    self.records.append(safe_record)
                    self._observed[package_id].update(enabled=bool(raw.get("enabled")), state="error", detail="Installed extension environment or configuration is unavailable")
                    for contribution in manifest["contributes"].get("voice_backend", []):
                        backend = ExtensionTTSBackend(package_id, contribution, None)
                        self.backends[backend.backend_id] = backend
                except (ValueError, KeyError, TypeError):
                    pass

        self.skills = SkillCatalog(self.activation_records)

    @staticmethod
    def _validate_record(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        record = copy.deepcopy(raw)
        record['revision'] = record.get('revision', 1)
        if type(record['revision']) is not int or not 1 <= record['revision'] <= 2**53 - 1:
            raise ValueError('invalid extension revision')
        if record.get("startup_error"):
            raise ValueError("desktop blocked extension startup")
        manifest = record["manifest"]
        if (record["id"] != manifest["id"] or record["version"] != manifest["version"] or
                not isinstance(record["digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", record["digest"]) or
                type(record["enabled"]) is not bool or not isinstance(record.get("config", {}), dict) or
                not isinstance(record.get("secrets", {}), dict)):
            raise ValueError("invalid extension record identity")
        for key in ("package_path", "data_path"):
            if not isinstance(record[key], str) or not Path(record[key]).is_absolute():
                raise ValueError("absolute extension paths required")
        if manifest["runtime"]["kind"] == "python" and (
                not isinstance(record["python_path"], str) or not Path(record["python_path"]).is_absolute() or not Path(record["python_path"]).is_file()):
            raise ValueError("private extension Python is unavailable")
        package_path = Path(record["package_path"])
        validate_installed(manifest, package_path)
        fields = manifest.get("config", {})
        secrets = record.get("secrets", {})
        values = record.get("config", {})
        if any(key not in fields or not fields[key].get("secret") or not isinstance(value, str) for key, value in secrets.items()):
            raise ValueError("invalid extension secret scope")
        if any(fields.get(key, {}).get("secret") for key in values):
            raise ValueError("secret fields cannot be in public config")
        resolved, missing = validate_config(manifest, {**values, **secrets}, allow_incomplete=True)
        record["config"] = {key: value for key, value in resolved.items() if not fields[key].get("secret")}
        record["secrets"] = {key: value for key, value in resolved.items() if fields[key].get("secret")}
        providers = record.get("providers", [])
        if not isinstance(providers, list) or any(not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) for value in providers):
            raise ValueError("invalid extension Work Provider selection")
        record["providers"] = list(dict.fromkeys(providers))
        return record, missing

    @property
    def activation_records(self) -> list[dict[str, Any]]:
        return copy.deepcopy([record for record in self.records if record["enabled"] and record["id"] not in self._blocked and self._observed[record["id"]]["state"] not in {"needs_config", "incompatible"}])

    def start(self) -> None:
        global _current
        if self._started:
            return
        self._started = True
        _current = self
        configure_skills(self.skills)
        atexit.register(self.close)
        from tts.registry import TTSBackendDescriptor, register_tts_backend

        for record in self.records:
            for contribution in record["manifest"]["contributes"].get("voice_backend", []):
                backend = self.backends[f"{record['id']}/{contribution['id']}"]
                register_tts_backend(TTSBackendDescriptor(
                    backend.backend_id, localized_text(contribution.get("title"), localized_text(record["manifest"]["display_name"])),
                    backend.deployment, lambda backend=backend: backend,
                    lambda package_id=record["id"], contribution_id=contribution["id"]: self.voice_probe(package_id, contribution_id),
                    localized_text(contribution.get("summary"), localized_text(record["manifest"]["description"])),
                    supports_streaming=backend.supports_streaming,
                ))
        for client in self.clients.values():
            client.start()

    def voice_probe(self, package_id: str, contribution_id: str) -> tuple[str, str]:
        observed = self._observed[package_id]
        client = self.clients.get(package_id)
        if client is None:
            return observed["state"], observed["detail"]
        with client._lock:
            health, detail = client.health[contribution_id]
            if client.state == "active" and health in {"ready", "degraded"}:
                # Existing TTS availability enum: installed/remote mean usable.
                backend = self.backends[f"{package_id}/{contribution_id}"]
                return "remote" if backend.deployment == "remote" else "installed", detail or client.detail
            return client.state if client.state != "active" else health, detail or client.detail

    def statuses(self) -> list[dict[str, Any]]:
        statuses = copy.deepcopy(list(self._observed.values()))
        records = {record["id"]: record for record in self.records}
        for status in statuses:
            record = records.get(status["id"])
            if record is None:
                continue
            client = self.clients.get(status["id"])
            if client:
                with client._lock:
                    status["state"], status["detail"] = client.state, client.detail
            elif status["id"] in self._mcp_errors:
                status["state"], status["detail"] = "error", self._mcp_errors[status["id"]]
            for kind, contributions in record["manifest"]["contributes"].items():
                for contribution in contributions:
                    if (contribution_profile(kind, contribution.get('role')) or {}).get('apply') == 'frontend':
                        continue
                    health = "disabled" if not status["enabled"] else "error" if status["state"] in {"needs_config", "incompatible", "error"} else "starting" if status["state"] == "starting" else "ready"
                    detail = status["detail"]
                    if kind == "voice_backend" and client:
                        with client._lock:
                            own_health, own_detail = client.health[contribution["id"]]
                        if status["state"] == "active":
                            health, detail = own_health, own_detail
                    provider_targets = None
                    if kind == "skill" and record['id'] in self.skills.errors:
                        health, detail = 'error', self.skills.errors[record['id']]
                    elif kind in {"mcp_server", "skill"}:
                        if record["id"] in self._mcp_errors:
                            health, detail = "error", self._mcp_errors[record["id"]]
                        elif record["enabled"] and record["id"] not in self._blocked and self._observed[record["id"]]["state"] != "needs_config":
                            health, detail, provider_targets = self._provider_health(record["providers"], 'agent_skill' if kind == 'skill' else 'mcp_connection',
                                f"{record['id']}/{contribution['id']}" if kind == 'skill' else '')
                    status["contributions"].append({"kind": kind, "id": contribution["id"], "qualified_id": f"{record['id']}/{contribution['id']}", "health": health, "detail": detail,
                                                  **({"provider_targets": provider_targets} if provider_targets is not None else {})})
            if client is None and status["state"] == "active" and status['contributions'] and all(item["health"] == "error" for item in status["contributions"]):
                status["state"], status["detail"] = "error", status["contributions"][0]["detail"]
        return statuses

    def _provider_health(self, provider_ids: list[str], projection: str, skill_id: str = '') -> tuple[str, str, list[dict[str, str]]]:
        if self._provider_manifest_lookup is None:
            from agent_host.provider_runtime import runtime as provider_runtime
            lookup = provider_runtime.get_manifest
        else:
            lookup = self._provider_manifest_lookup
        targets = []
        for provider_id in provider_ids:
            native_error = self.skills.native_errors.get((provider_id, skill_id))
            if native_error:
                targets.append({"id": provider_id, "state": "error", "detail": native_error})
                continue
            manifest = lookup(provider_id)
            state = "unavailable" if manifest is None else "ready" if projection in manifest.capabilities.capability_projections else "incompatible"
            targets.append({"id": provider_id, "state": state})
        compatible = [target for target in targets if target["state"] == "ready"]
        if not targets:
            return "degraded", "No Work Provider selected", targets
        if not compatible:
            return "error", next((target['detail'] for target in targets if target.get('detail')), "Selected Work Providers are unavailable or do not support this contribution"), targets
        if len(compatible) != len(targets):
            return "degraded", next((f"{target['id']}: {target['detail']}" for target in targets if target.get('detail')),
                "Some selected Work Providers are unavailable or do not support this contribution"), targets
        return "ready", "Contribution is available to compatible selected Work Providers", targets

    def mark_mcp_projection_failed(self, package_id: str) -> None:
        self._mcp_errors[package_id] = "Declarative MCP contribution could not be registered"

    def capability_packages(self):
        from server.capability_composition import extension_capability_packages
        return extension_capability_packages(self.records, self.statuses())

    def close(self) -> None:
        global _current
        if self._closed:
            return
        self._closed = True
        deadline = time.monotonic() + 8
        threads = [threading.Thread(target=client.close, daemon=True) for client in self.clients.values()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        from tts.registry import unregister_tts_backend
        for backend_id in self.backends:
            unregister_tts_backend(backend_id)
        if _current is self:
            _current = None
            configure_skills(SkillCatalog())
        atexit.unregister(self.close)


def current_runtime() -> ExtensionRuntime | None:
    return _current


def extension_statuses() -> list[dict[str, Any]]:
    return _current.statuses() if _current else []
