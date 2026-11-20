from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from server.handlers import composition
from server.protocol import Method
from server.ws_handler import ConnectionManager, RequestHandler


@pytest.fixture
def services(monkeypatch):
    # Composition must not probe user-installed agents or credentials in a unit test.
    from server.handlers import provider_handler

    monkeypatch.setattr(provider_handler, "load_mcp_connections", lambda: ())
    monkeypatch.setattr(provider_handler.ProviderHandler, "_ensure_registered", lambda self: None)
    return composition.HandlerServices(
        capability_catalog=Mock(), work_ledger=Mock(), work_ledger_store=Mock(),
        work_preview=Mock(), attention=Mock(), auip_launch=SimpleNamespace(),
        provider_activity=Mock(), provider_runtime=Mock(),
        current_session_id=lambda: "fixture-session",
        auip_app_websocket_url="ws://127.0.0.1:17777/auip/ws",
        extra_capability_packages=lambda: (),
    )


def test_builtin_registry_preserves_shared_owners_and_registers_every_handler(services):
    bundle = composition.create_builtin_handlers(services)
    handlers = bundle.instances
    manager = ConnectionManager()
    bundle.register(manager)
    expected_methods = {method for handler in handlers.values() for method in handler.methods}
    assert set(manager._request_handlers) == expected_methods
    assert {Method.CHAT_SEND, Method.CHARACTER_LIST, Method.VISUAL_GET,
            Method.SYSTEM_GET_CONFIG, Method.WORK_LIST, Method.AUIP_ATTACH_PREPARE} <= expected_methods
    assert handlers["session"]._work_coordinator is services.work_ledger
    assert handlers["session"]._is_chat_busy == handlers["chat"].is_busy
    assert handlers["provider"]._work_control is services.work_ledger
    assert handlers["work"]._provider_run == handlers["provider"].run_provider
    assert handlers["work"]._preview_open == handlers["work_preview"].open_from_work_action
    assert handlers["work"]._provider_permission == services.provider_runtime.resolve_permission
    assert services.auip_launch.before_result_entry == handlers["auip"].prepare_result_entry
    assert handlers["asr"] is bundle.voice.asr
    assert handlers["wake"].service() is None
    assert handlers["vn"]._runtime is None


def test_new_factory_is_constructed_and_registered_without_an_app_list(monkeypatch, services):
    class FixtureHandler(RequestHandler):
        methods = ["fixture.catalog"]

        async def handle(self, method, params):
            return {"received": params["value"]}

    original = composition.builtin_handler_factories
    def extended(shared, voice):
        return {**original(shared, voice), "fixture": FixtureHandler}
    monkeypatch.setattr(composition, "builtin_handler_factories", extended)
    bundle = composition.create_builtin_handlers(services)
    manager = ConnectionManager()
    bundle.register(manager)
    handler = manager._request_handlers["fixture.catalog"]
    assert isinstance(handler, FixtureHandler)
    assert asyncio.run(handler.handle("fixture.catalog", {"value": "roundtrip"})) == {"received": "roundtrip"}


def test_conflicting_factory_cannot_replace_an_existing_method(monkeypatch, services):
    class ConflictingHandler(RequestHandler):
        methods = [Method.SYSTEM_GET_CONFIG]
    original = composition.builtin_handler_factories
    monkeypatch.setattr(composition, "builtin_handler_factories", lambda shared, voice: {
        **original(shared, voice), "conflict": ConflictingHandler,
    })
    bundle = composition.create_builtin_handlers(services)
    manager = ConnectionManager()
    with pytest.raises(ValueError, match="already registered"):
        bundle.register(manager)
    assert manager._request_handlers[Method.SYSTEM_GET_CONFIG] is bundle.instances["system"]
