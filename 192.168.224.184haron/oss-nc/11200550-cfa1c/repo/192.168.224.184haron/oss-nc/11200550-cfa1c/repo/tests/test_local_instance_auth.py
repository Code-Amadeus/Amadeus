from __future__ import annotations

import pytest

from server.local_auth import (
    AUTH_MODE_ENV,
    AUTH_PROTOCOL,
    AUTH_SUBPROTOCOL_PREFIX,
    AUTH_TOKEN_ENV,
    INSTANCE_NONCE_ENV,
    LocalAuthConfigurationError,
    LocalAuthPolicy,
    clear_inherited_auth_environment,
)


TOKEN = "desktop-token-abcdefghijklmnopqrstuvwxyz-0123456789"
NONCE = "instance-nonce-abcdefghijklmnop"


def _required_policy() -> LocalAuthPolicy:
    return LocalAuthPolicy.from_environment(
        {
            AUTH_MODE_ENV: "required",
            AUTH_TOKEN_ENV: TOKEN,
            INSTANCE_NONCE_ENV: NONCE,
        }
    )


def test_auto_mode_preserves_direct_loopback_development() -> None:
    policy = LocalAuthPolicy.from_environment({})

    assert policy.required is False
    principal = policy.authenticate({})
    assert principal is not None
    assert principal.scheme == "loopback-development"
    assert policy.health_fields() == {
        "instance_nonce": "",
        "auth": {"required": False, "scheme": "disabled"},
    }


def test_required_mode_rejects_partial_or_weak_credentials() -> None:
    with pytest.raises(LocalAuthConfigurationError, match="configured together"):
        LocalAuthPolicy.from_environment(
            {AUTH_MODE_ENV: "required", AUTH_TOKEN_ENV: TOKEN}
        )
    with pytest.raises(LocalAuthConfigurationError, match="32-512"):
        LocalAuthPolicy.from_environment(
            {
                AUTH_MODE_ENV: "required",
                AUTH_TOKEN_ENV: "too-short",
                INSTANCE_NONCE_ENV: NONCE,
            }
        )
    with pytest.raises(LocalAuthConfigurationError, match="URL-safe"):
        LocalAuthPolicy.from_environment(
            {
                AUTH_MODE_ENV: "required",
                AUTH_TOKEN_ENV: TOKEN + " invalid",
                INSTANCE_NONCE_ENV: NONCE,
            }
        )


def test_required_policy_authenticates_header_or_websocket_subprotocol() -> None:
    policy = _required_policy()

    header_principal = policy.authenticate({"X-Amadeus-Token": TOKEN})
    protocol_principal = policy.authenticate(
        {
            "sec-websocket-protocol": (
                f"{AUTH_PROTOCOL}, {AUTH_SUBPROTOCOL_PREFIX}{TOKEN}"
            )
        },
        allow_websocket_protocol=True,
    )

    for principal in (header_principal, protocol_principal):
        assert principal is not None
        assert principal.scheme == AUTH_PROTOCOL
        assert principal.subject == "electron-desktop"
        assert principal.instance_nonce == NONCE
    assert policy.authenticate({"x-amadeus-token": "wrong-token"}) is None
    assert policy.authenticate(
        {"sec-websocket-protocol": f"{AUTH_SUBPROTOCOL_PREFIX}{TOKEN}"}
    ) is None
    assert policy.selected_websocket_subprotocol(
        {
            "sec-websocket-protocol": (
                f"{AUTH_PROTOCOL}, {AUTH_SUBPROTOCOL_PREFIX}{TOKEN}"
            )
        }
    ) == AUTH_PROTOCOL
    assert policy.selected_websocket_subprotocol(
        {"sec-websocket-protocol": f"{AUTH_SUBPROTOCOL_PREFIX}{TOKEN}"}
    ) is None


def test_public_health_identity_never_contains_the_credential() -> None:
    fields = _required_policy().health_fields()

    assert fields["instance_nonce"] == NONCE
    assert fields["auth"] == {"required": True, "scheme": AUTH_PROTOCOL}
    assert TOKEN not in repr(fields)


def test_backend_consumes_credentials_before_spawning_child_processes() -> None:
    environ = {
        AUTH_MODE_ENV: "required",
        AUTH_TOKEN_ENV: TOKEN,
        INSTANCE_NONCE_ENV: NONCE,
        "UNRELATED_SETTING": "kept",
    }

    policy = LocalAuthPolicy.from_environment(environ)
    clear_inherited_auth_environment(environ)

    assert policy.authenticate({"x-amadeus-token": TOKEN}) is not None
    assert environ == {"UNRELATED_SETTING": "kept"}
