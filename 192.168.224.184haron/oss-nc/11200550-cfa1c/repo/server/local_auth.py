"""Authentication boundary for the local Electron/backend control plane.

This module authenticates one desktop process instance. It deliberately does
not decide product permissions: Work scopes, AUIP attach tickets, and user
approval remain owned by their existing host services.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Literal, Mapping, MutableMapping, Protocol


AUTH_MODE_ENV = "AMADEUS_BACKEND_AUTH_MODE"
AUTH_TOKEN_ENV = "AMADEUS_BACKEND_TOKEN"
INSTANCE_NONCE_ENV = "AMADEUS_BACKEND_INSTANCE_NONCE"
AUTH_TOKEN_HEADER = "x-amadeus-token"
AUTH_PROTOCOL = "amadeus.local.v1"
AUTH_SUBPROTOCOL_PREFIX = "amadeus.auth."

_CREDENTIAL_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


class LocalAuthConfigurationError(ValueError):
    """The local authentication environment is incomplete or unsafe."""


class HeaderMapping(Protocol):
    def get(self, key: str, default: str | None = None) -> str | None: ...


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Minimal identity returned to future authorization layers."""

    scheme: str
    subject: str
    instance_nonce: str


@dataclass(frozen=True, slots=True)
class LocalAuthPolicy:
    """Authenticate the desktop instance without introducing user accounts."""

    mode: Literal["required", "disabled"]
    token: str = ""
    instance_nonce: str = ""

    @property
    def required(self) -> bool:
        return self.mode == "required"

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
    ) -> "LocalAuthPolicy":
        requested = str(environ.get(AUTH_MODE_ENV) or "auto").strip().lower()
        if requested not in {"auto", "required", "disabled"}:
            raise LocalAuthConfigurationError(
                f"{AUTH_MODE_ENV} must be auto, required, or disabled"
            )

        token = str(environ.get(AUTH_TOKEN_ENV) or "").strip()
        nonce = str(environ.get(INSTANCE_NONCE_ENV) or "").strip()
        if requested == "disabled":
            return cls(mode="disabled")
        if requested == "auto" and not token and not nonce:
            return cls(mode="disabled")
        if not token or not nonce:
            raise LocalAuthConfigurationError(
                f"{AUTH_TOKEN_ENV} and {INSTANCE_NONCE_ENV} must be configured together"
            )
        _validate_credential(AUTH_TOKEN_ENV, token, minimum=32)
        _validate_credential(INSTANCE_NONCE_ENV, nonce, minimum=16)
        return cls(mode="required", token=token, instance_nonce=nonce)

    @classmethod
    def disabled(cls) -> "LocalAuthPolicy":
        return cls(mode="disabled")

    def authenticate(
        self,
        headers: HeaderMapping | Mapping[str, str],
        *,
        allow_websocket_protocol: bool = False,
    ) -> AuthenticatedPrincipal | None:
        if not self.required:
            return AuthenticatedPrincipal(
                scheme="loopback-development",
                subject="unauthenticated-local-client",
                instance_nonce="",
            )

        candidate = _header_value(headers, AUTH_TOKEN_HEADER)
        if not candidate and allow_websocket_protocol:
            candidate = _websocket_protocol_token(
                _header_value(headers, "sec-websocket-protocol")
            )
        if not candidate or not secrets.compare_digest(candidate, self.token):
            return None
        return AuthenticatedPrincipal(
            scheme=AUTH_PROTOCOL,
            subject="electron-desktop",
            instance_nonce=self.instance_nonce,
        )

    def health_fields(self) -> dict[str, object]:
        """Return public instance identity without exposing the credential."""

        return {
            "instance_nonce": self.instance_nonce,
            "auth": {
                "required": self.required,
                "scheme": AUTH_PROTOCOL if self.required else "disabled",
            },
        }

    def selected_websocket_subprotocol(
        self,
        headers: HeaderMapping | Mapping[str, str],
    ) -> str | None:
        """Select only the public protocol marker, never the credential value."""

        if not self.required:
            return None
        offered = {
            item.strip()
            for item in _header_value(headers, "sec-websocket-protocol").split(",")
            if item.strip()
        }
        return AUTH_PROTOCOL if AUTH_PROTOCOL in offered else None


def _validate_credential(name: str, value: str, *, minimum: int) -> None:
    if len(value) < minimum or len(value) > 512 or not _CREDENTIAL_RE.fullmatch(value):
        raise LocalAuthConfigurationError(
            f"{name} must be {minimum}-512 URL-safe characters"
        )


def _header_value(
    headers: HeaderMapping | Mapping[str, str],
    name: str,
) -> str:
    direct = headers.get(name)
    if direct is not None:
        return str(direct).strip()
    expected = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == expected:
            return str(value).strip()
    return ""


def _websocket_protocol_token(raw: str) -> str:
    for item in str(raw or "").split(","):
        protocol = item.strip()
        if protocol.startswith(AUTH_SUBPROTOCOL_PREFIX):
            return protocol[len(AUTH_SUBPROTOCOL_PREFIX) :]
    return ""


def clear_inherited_auth_environment(environ: MutableMapping[str, str]) -> None:
    """Keep the desktop credential out of Provider/model child processes."""

    for name in (AUTH_MODE_ENV, AUTH_TOKEN_ENV, INSTANCE_NONCE_ENV):
        environ.pop(name, None)
