"""Controlled ingress for an approved AUIP application opened by the user.

Self-attach is deliberately only a way to ask.  Approved external artifacts
remain the durable application registration, Attention owns the user's mode
choice, and ``AuipRuntime`` still creates identity only after its ordinary
single-use attach ticket is consumed on the restricted app socket.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from server.attention_request import (
    AttentionOption,
    AttentionRequestCoordinator,
    opaque_option_id,
)
from server.auip_app_source import ArtifactSource, discover_exported_auip_apps
from server.auip_contract import AuipProtocolError, parse_manifest
from server.auip_runtime import AuipRuntime


SELF_ATTACH_TIMEOUT_S = 5 * 60.0
_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_-]{8,160}$")


class AuipSelfAttachCoordinator:
    """Turn one untrusted app hello into the existing ticket path."""

    def __init__(
        self,
        *,
        runtime: AuipRuntime,
        artifacts: ArtifactSource,
        attention: AttentionRequestCoordinator,
        current_session_id: Callable[[], str],
        timeout_s: float = SELF_ATTACH_TIMEOUT_S,
    ) -> None:
        self.runtime = runtime
        self.artifacts = artifacts
        self.attention = attention
        self.current_session_id = current_session_id
        self.timeout_s = max(1.0, float(timeout_s))

    async def request(
        self,
        *,
        manifest: dict[str, Any],
        instance_id: str,
        entry_url: str,
    ) -> dict[str, Any]:
        parsed = parse_manifest(manifest)
        clean_instance = str(instance_id or "").strip()
        if not _INSTANCE_ID.fullmatch(clean_instance):
            raise AuipProtocolError("invalid_app_instance")
        session_id = str(self.current_session_id() or "").strip()
        if not session_id:
            raise AuipProtocolError("host_session_unavailable")

        matches = discover_exported_auip_apps(self.artifacts, parsed.to_dict())
        if not matches:
            raise AuipProtocolError("unregistered_app")
        if len(matches) != 1:
            raise AuipProtocolError("ambiguous_app_registration")
        app = matches[0]
        if _entry_path(entry_url) != Path(str(app["entry_path"])).resolve():
            raise AuipProtocolError("app_entry_mismatch")

        loop = asyncio.get_running_loop()
        decision: asyncio.Future[dict[str, Any]] = loop.create_future()
        choices: list[tuple[str, str, str]] = []
        stances = {str(value) for value in app.get("stances") or []}
        if "spectator" in stances:
            choices.append(
                (
                    "observe",
                    "Observe",
                    "Let Amadeus watch and comment without operating the app.",
                )
            )
        if "participant" in stances:
            choices.extend(
                (
                    (
                        "collaborate",
                        "Play together",
                        "Allow bounded AUIP actions while you remain in the interaction.",
                    ),
                    (
                        "delegate",
                        "Let Kurisu play",
                        "Let the participant lane take bounded AUIP actions for this session.",
                    ),
                )
            )
        choices.append(("deny", "Not now", "Keep the application standalone."))
        option_modes = {opaque_option_id(): choice for choice in choices}

        async def continue_attach(option_id: str) -> dict[str, Any]:
            mode = option_modes[option_id][0]
            if mode == "deny":
                outcome = {"approved": False, "mode": "deny"}
            else:
                ticket = self.runtime.issue_attach_ticket(
                    conversation_id=session_id,
                    artifact_ref=str(app["artifact_ref"]),
                    engagement_mode=mode,
                )
                outcome = {"approved": True, "mode": mode, **ticket}
            if not decision.done():
                decision.set_result(outcome)
            return {"approved": outcome["approved"], "mode": mode}

        request = await self.attention.create_selection(
            session_id=session_id,
            title=f"Connect {parsed.title}?",
            prompt=(
                f"{parsed.title} was opened outside Amadeus and requested an AUIP "
                "connection. Choose how it may join this conversation."
            ),
            options=[
                AttentionOption(
                    option_id=option_id,
                    label=label,
                    entity_kind="other",
                    description=description,
                    metadata={"scope": "this app session"},
                )
                for option_id, (_mode, label, description) in option_modes.items()
            ],
            continuation=continue_attach,
            ttl_s=self.timeout_s,
            dedupe_key=f"auip-self-attach:{clean_instance}",
        )
        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(decision),
                timeout=self.timeout_s + 1.0,
            )
        except TimeoutError as exc:
            await self.attention.cancel_matching(
                session_id=session_id,
                dedupe_key=f"auip-self-attach:{clean_instance}",
            )
            raise AuipProtocolError("attach_request_expired") from exc
        if outcome.get("approved") is not True:
            raise AuipProtocolError("attach_denied")
        return {
            "ok": True,
            "request_id": str(request["id"]),
            "mode": str(outcome["mode"]),
            "attach_ticket": str(outcome["attach_ticket"]),
            "expires_at": float(outcome["expires_at"]),
        }


def _entry_path(value: str) -> Path:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme != "file" or parsed.query or parsed.username or parsed.password:
        raise AuipProtocolError("invalid_app_entry_url")
    if parsed.netloc not in {"", "localhost"}:
        raise AuipProtocolError("invalid_app_entry_url")
    raw_path = unquote(parsed.path)
    if re.match(r"^/[A-Za-z]:/", raw_path):
        raw_path = raw_path[1:]
    try:
        path = Path(url2pathname(raw_path)).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise AuipProtocolError("invalid_app_entry_url") from exc
    if not path.is_file():
        raise AuipProtocolError("invalid_app_entry_url")
    return path
