from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_electron_owns_ephemeral_backend_identity_without_url_secrets() -> None:
    main = (ROOT / "electron" / "src" / "main" / "index.ts").read_text(
        encoding="utf-8"
    )

    assert "randomBytes" in main
    assert "AMADEUS_BACKEND_AUTH_MODE: 'required'" in main
    assert "AMADEUS_BACKEND_TOKEN: BACKEND_TOKEN" in main
    assert "AMADEUS_BACKEND_INSTANCE_NONCE: BACKEND_INSTANCE_NONCE" in main
    assert "headers: { [BACKEND_TOKEN_HEADER]: BACKEND_TOKEN }" in main
    assert "payload.instance_nonce !== BACKEND_INSTANCE_NONCE" in main
    assert "owned by another backend instance" in main
    assert "`ws://127.0.0.1:${BACKEND_PORT}/ws`" in main
    assert "BACKEND_WS = `ws://127.0.0.1:${BACKEND_PORT}/ws?" not in main


def test_preload_exposes_a_bounded_connection_descriptor() -> None:
    preload = (ROOT / "electron" / "src" / "preload" / "index.mts").read_text(
        encoding="utf-8"
    )
    renderer = (
        ROOT / "electron" / "src" / "renderer" / "hooks" / "useBackend.ts"
    ).read_text(encoding="utf-8")
    main = (ROOT / "electron" / "src" / "main" / "index.ts").read_text(
        encoding="utf-8"
    )

    assert "getBackendConnection" in preload
    assert "getBackendUrl" not in preload
    assert "isTrustedBackendRenderer(event.sender)" in main
    assert "project-directory.select" in preload
    assert "isTrustedAmadeusRenderer(event.sender)" in main
    assert "dialog.showOpenDialog" in main
    assert "new WebSocket(" in renderer
    assert "connection.protocols" in renderer
    assert "backend instance is not authenticated" in renderer


def test_backend_keeps_desktop_and_auip_authentication_realms_separate() -> None:
    app = (ROOT / "server" / "app.py").read_text(encoding="utf-8")

    assert "auth_policy=auth_policy" in app
    assert "_http_request_authenticated(request.headers, auth_policy)" in app
    assert "auth_policy.health_fields()" in app
    assert "clear_inherited_auth_environment(os.environ)" in app
    assert "AUIP applications authenticate with a one-time attach ticket" in app
    assert "auth_policy=LocalAuthPolicy.disabled()" in app


def test_embedded_render_surface_reuses_the_authenticated_parent_socket() -> None:
    chat = (
        ROOT / "electron" / "src" / "renderer" / "components" / "ChatPage.tsx"
    ).read_text(encoding="utf-8")
    renderer = (ROOT / "render" / "web" / "renderer.js").read_text(
        encoding="utf-8"
    )

    assert "RENDER_EVENT_METHODS" in chat
    assert "send('render.ready', {})" in chat
    assert "amadeus.render.event" in chat
    assert "window.parent !== window" in renderer
    assert "event.source !== window.parent" in renderer
    assert "using authenticated parent event channel" in renderer
    assert "amadeus.render.event" in renderer


def test_work_preview_journey_uses_the_owned_backend_identity() -> None:
    preview_journey = (ROOT / "tools" / "e2e_work_preview.py").read_text(
        encoding="utf-8"
    )

    assert "subprotocols=product.backend_websocket_protocols" in preview_journey
