"""Normal Host startup delivers committed Session originals into its SDK."""

import asyncio
import sqlite3

import eidema as memory

from config.memory import MemoryConfig
from core import session_manager as sessions
from core import session_source_journal as journal
from server.memory_service import HostMemoryService
from test_session_source_lifecycle import MODE, USER, room as room


def save(session, message, text, *, role="user", turn=None):
    assert sessions.append_session_message(
        session, role=role, content=text, turn_id=turn or message, message_id=message
    )


async def ready(host):
    host.start()
    await host._startup
    assert host._sources is not None


def find(host, access, query):
    return {
        item.text: item.source
        for item in memory.Retrieval(host._store).lookup_sources(access, query).items
    }


async def wait_for(operation, predicate):
    async with asyncio.timeout(5):
        while True:
            value = operation()
            if predicate(value):
                return value
            await asyncio.sleep(.01)


async def test_committed_raw_delivery_scope_revision_and_restart(room, tmp_path, monkeypatch):
    monkeypatch.setenv(MODE, "0")
    sessions.create_session("legacy")
    save("legacy", "old", "archived-kite")

    projects = {}
    config = MemoryConfig(
        db_path=tmp_path / "memory.sqlite3", user_id=USER, semantic_enabled=False,
        source_runtime_enabled=True,
    )
    role = sessions.conversation_history.character_id
    private = memory.AccessScope(USER, role)
    project = memory.AccessScope(USER, role, "project-a")
    host = HostMemoryService(config, project_for_session=projects.get)
    await ready(host)
    try:
        assert sessions.load_session("legacy")[0]
        save("legacy", "new", "silver-mallet")
        sessions.create_session("next")
        await wait_for(lambda: find(host, private, "silver"), bool)
        assert not find(host, private, "archived")

        sessions.create_session("work")
        await wait_for(lambda: host._sources.state, lambda state: state == "idle")
        projects["work"] = "project-a"
        save("work", "user", "supplies for the east gallery", turn="live")
        save("work", "reply", "copper-signal", role="assistant", turn="live")
        old = (await wait_for(lambda: find(host, project, "copper"), bool))["copper-signal"]
        assert not find(host, private, "copper")
        assert not find(host, memory.AccessScope(USER, "other-role", "project-a"), "copper")
        assert not find(host, memory.AccessScope(USER, role, "project-b"), "copper")

        maintenance = memory.Maintenance(host._store)
        pin = maintenance.lookup_input(project, "m0:work", old)
        assert host._sources._current_text(project, pin) == "copper-signal"
        assert sessions.persist_interrupted_assistant_turn("work", turn_id="live", heard_content="bronze-marker")
        # Same event-loop tick, before the receiver processes the new revision.
        assert host._sources._current_text(project, pin) is None
        new = (await wait_for(lambda: find(host, project, "bronze"), bool))
        assert next(iter(new.values())).revision == old.revision + 1
        assert not find(host, project, "copper")

        # A control exchange can be evidence for a saved note without becoming
        # independently discoverable as conversation history.
        save("work", "control", "audit-brass", turn="control")
        control = (await wait_for(lambda: find(host, project, "audit"), bool))["audit-brass"]
        sessions.mark_memory_control_sources("work", [("user", journal.coordinate("work", "control", 1))])
        await wait_for(lambda: find(host, project, "audit"), lambda result: not result)
        assert maintenance.lookup_input(project, "m0:work", control).source == control

        # Changing project does not grant the old stream to the new authority.
        projects["work"] = "project-b"
        save("work", "ungranted", "unbound-compass")
        assert not find(host, memory.AccessScope(USER, role, "project-b"), "bronze")
        journal_data = journal.read(sessions._session_path("work"))[journal.FIELD]
        assert len(journal_data["generations"]) == 1
        assert all("ungranted" not in event["source"]["record_key"]
                   for event in journal_data["generations"][0]["events"])
    finally:
        await host.close()

    assert not journal.enabled()
    assert sessions.delete_session("work")
    # No observer was running during deletion. The journal must recover it.
    reopened = HostMemoryService(config, project_for_session=projects.get)
    await ready(reopened)
    try:
        await wait_for(lambda: find(reopened, project, "bronze"), lambda value: not value)
        assert set(find(reopened, private, "silver")) == {"silver-mallet"}
        assert not find(reopened, project, "compass")
    finally:
        await reopened.close()
    with sqlite3.connect(config.db_path) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


class LocalTestEmbedder:
    version = "test-embedding-v1"
    dimensions = 2

    @staticmethod
    def split_passage(text):
        return [text]

    @staticmethod
    def embed_query(text):
        return [1, 0] if any(term in text for term in ("面接", "面试")) else [0, 1]

    def embed_passages(self, texts):
        return [self.embed_query(text) for text in texts]


async def test_source_indexes_warm_in_existing_model_lane(room, tmp_path, monkeypatch):
    monkeypatch.setenv(MODE, "0")
    config = MemoryConfig(db_path=tmp_path / "memory.sqlite3", user_id=USER,
                          source_runtime_enabled=True)
    host = HostMemoryService(config, embedder_factory=LocalTestEmbedder)
    await ready(host)
    try:
        sessions.create_session("prior")
        save("prior", "reply", "面接では自己紹介を短くします。", role="assistant")
        save("prior", "other", "明日は雨が降ります。")
        sessions.create_session("current")
        context = host.context("current")
        access = host._access(context)
        retrieval = memory.Retrieval(host._store)
        result = await wait_for(
            lambda: retrieval.lookup_sources(access, "面试建议是什么"),
            lambda result: result.coverage.indexed_documents == 2,
        )
        assert result.items[0].text == "面接では自己紹介を短くします。"
        assert not result.items[0].lexical_match
        assert result.coverage.embedding_version == LocalTestEmbedder.version
        assert (await host.status(context))["automatic_admission"] == "gate_required"
    finally:
        await host.close()


async def test_source_runtime_off_preserves_saved_only_host(room, tmp_path, monkeypatch):
    monkeypatch.setenv(MODE, "0")
    config = MemoryConfig(db_path=tmp_path / "memory.sqlite3", user_id=USER, semantic_enabled=False)
    host = HostMemoryService(config)
    try:
        host.start()
        await host._startup
        assert not journal.enabled()
        assert host._sources is None
        sessions.create_session("plain")
        save("plain", "one", "current-lookup-example")
        assert journal.FIELD not in journal.read(sessions._session_path("plain"))
        context = host.context("plain")
        status = await host.status(context)
        assert "source_delivery" not in status
        assert not find(host, host._access(context), "lookup")
    finally:
        await host.close()
