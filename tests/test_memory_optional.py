"""Base installation behavior does not depend on importing the Eidema package."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from config.environment import EnvironmentReader
from config.memory import MemoryConfig
from core import session_manager as sm
from server.handlers.memory_handler import MemoryHandler
from server.memory_service import HostMemoryService, MemoryRequestError, _SingleFlight
from server.protocol import Method


def test_memory_configuration_uses_existing_reader_without_loading_optional_models():
    value = MemoryConfig.from_environment(EnvironmentReader({}))
    assert value.enabled and value.semantic_enabled and value.semantic_threshold is None
    assert MemoryConfig.from_environment(EnvironmentReader({"AMADEUS_MEMORY_ENABLED":"false"})).enabled is False


async def test_disabled_new_install_does_not_import_or_submit_workers(tmp_path, monkeypatch):
    service = HostMemoryService(MemoryConfig(enabled=False, db_path=tmp_path / "absent.sqlite3"))
    monkeypatch.setattr("server.memory_service.importlib.import_module", Mock(side_effect=AssertionError("disabled import")))
    for lane in (service._db, service._query, service._model):
        monkeypatch.setattr(lane._executor, "submit", Mock(side_effect=AssertionError("disabled worker")))
    service.start()
    await service._startup
    assert service._state == "disabled" and service.project_history("session", [{"role":"user", "content":"ordinary"}])
    assert await service.recall_for_session("", "ordinary") is None
    await service.close()
    await service.close()


async def test_missing_package_is_truthful_and_preserves_new_install_history(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    sm.create_session("A")
    service = HostMemoryService(MemoryConfig(db_path=tmp_path / "missing.sqlite3"))
    monkeypatch.setattr("server.memory_service.importlib.import_module", Mock(side_effect=ModuleNotFoundError("private path")))
    service.start()
    try:
        await service._startup
        result = await MemoryHandler(service).handle(Method.MEMORY_STATUS, {"session_id":"A"})
        assert result["ok"] and result["status"]["state"] == "unavailable"
        assert result["status"]["semantic_reason"] == "dependencies_missing"
        assert "private path" not in str(result)
        assert service.project_history("A", [{"role":"user", "content":"ordinary"}])
    finally:
        await service.close()


async def test_startup_completion_owns_late_store_and_idempotent_shutdown(tmp_path):
    started, release = threading.Event(), threading.Event()
    closed = []
    class Store:
        def __init__(self, path, **kwargs):
            started.set()
            assert release.wait(3)
        def source_policy(self, access):
            return SimpleNamespace(exclusions=(), retirements=(), change_seq=0)
        def close(self):
            closed.append(True)
    api = SimpleNamespace(MemoryStore=Store, AccessScope=lambda *args:args)
    service = HostMemoryService(MemoryConfig(semantic_enabled=False, db_path=tmp_path / "late.sqlite3"), api=api)
    service.start()
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await service.close()  # Bounded caller; startup owner stays alive.
        release.set()
        await service._close_task
        await service.close()
        assert closed == [True]
    finally:
        release.set()


async def test_single_flight_stays_owned_until_host_completion_publication():
    lane = _SingleFlight("synthetic-publication")
    completed = threading.Event()
    published = []
    def work():
        completed.set()
        return "one"
    task = asyncio.create_task(lane.run(work, on_completed=published.append))
    # Let submission occur, then synchronously wait for the worker while the
    # Host loop cannot yet execute the publication callback.
    await asyncio.sleep(0)
    assert completed.wait(1)
    assert lane._future.result(timeout=1) == "one"
    assert lane._future is not None and lane._future.done() and published == []
    try:
        await lane.run(lambda:"two")
        raise AssertionError("A second job started before Host publication")
    except MemoryRequestError as exc:
        assert exc.code == "busy"
    assert await task == "one" and published == ["one"]
    assert await lane.run(lambda:"two") == "two"
    lane.close()
