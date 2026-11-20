"""Build the one-shot launch descriptor for an external AUIP web app.

The launcher does not own a browser process and does not serve application
bytes.  It joins two facts already established by their owning layers:

* ``auip_app_source`` verified one local entry document; and
* ``AuipRuntime`` issued one short-lived Attach ticket.

The resulting file URL carries connection material in its fragment.  A URL
fragment is not sent to an HTTP server, and the SDK consumes it immediately.
The ticket is still a secret, so it is single-use and short-lived rather than
being treated as durable application configuration.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from server.auip_contract import AuipProtocolError


AUIP_LAUNCH_SCHEMA = "amadeus.auip/launch-v0"
AUIP_LAUNCH_FRAGMENT = "amadeus-auip"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def build_app_launch_url(
    *,
    entry_path: str,
    websocket_url: str,
    attach_ticket: str,
    expires_at: float,
) -> str:
    """Return a local file URL carrying one bounded Attach descriptor."""

    entry = _entry_file(entry_path)
    endpoint = _app_websocket_url(websocket_url)
    ticket = str(attach_ticket or "").strip()
    if not ticket or len(ticket) > 256:
        raise AuipProtocolError("invalid_attach_ticket")
    try:
        expiry = float(expires_at)
    except (TypeError, ValueError) as exc:
        raise AuipProtocolError("invalid_attach_expiry") from exc
    if expiry <= 0:
        raise AuipProtocolError("invalid_attach_expiry")

    descriptor = {
        "schema": AUIP_LAUNCH_SCHEMA,
        "webSocketUrl": endpoint,
        "attachTicket": ticket,
        "expiresAt": expiry,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(
            descriptor,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{entry.as_uri()}#{AUIP_LAUNCH_FRAGMENT}={encoded}"


def parse_app_launch_url(value: str) -> dict[str, Any]:
    """Decode a launch URL for deterministic probes and diagnostics.

    Product application code uses the Web SDK helper.  Keeping a Python
    decoder alongside the encoder lets tests assert the exact cross-process
    contract without launching a visible browser.
    """

    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme != "file":
        raise AuipProtocolError("invalid_app_launch_url", "entry scheme")
    prefix = f"{AUIP_LAUNCH_FRAGMENT}="
    if not parsed.fragment.startswith(prefix):
        raise AuipProtocolError("invalid_app_launch_url", "missing descriptor")
    encoded = parsed.fragment[len(prefix) :]
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
        descriptor = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuipProtocolError("invalid_app_launch_url", "descriptor") from exc
    if not isinstance(descriptor, dict) or descriptor.get("schema") != AUIP_LAUNCH_SCHEMA:
        raise AuipProtocolError("invalid_app_launch_url", "schema")
    descriptor["entryPath"] = unquote(parsed.path)
    return descriptor


def _entry_file(value: str) -> Path:
    try:
        path = Path(str(value or "").strip()).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise AuipProtocolError("invalid_app_entry") from exc
    if not path.is_file():
        raise AuipProtocolError("invalid_app_entry")
    return path


def _app_websocket_url(value: str) -> str:
    endpoint = str(value or "").strip()
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"ws", "wss"}
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.path != "/auip/ws"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise AuipProtocolError("invalid_auip_endpoint")
    try:
        if parsed.port is None:
            raise ValueError("missing port")
    except ValueError as exc:
        raise AuipProtocolError("invalid_auip_endpoint") from exc
    return endpoint
