from __future__ import annotations

from pathlib import Path

from core.memory import MemoryRecord, MemoryStore


def make_record(
    text: str,
    *,
    memory_id: str,
    scope: str = "user",
    namespace: str = "reading:general",
    kind: str = "preference",
    importance: int = 3,
    tags: tuple[str, ...] = (),
) -> MemoryRecord:
    return MemoryRecord.create(
        memory_id=memory_id,
        text=text,
        kind=kind,
        scope=scope,
        namespace=namespace,
        tags=tags,
        importance=importance,
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_remember_recall_and_deduplicate(tmp_path: Path) -> None:
    store = MemoryStore.open(tmp_path)
    record = make_record("用户不喜欢主动剧透", memory_id="m1", tags=("spoiler", "reading"))

    assert [item.id for item in store.remember([record])] == ["m1"]
    assert store.remember([record]) == []

    hits = store.recall("剧透", namespaces=["reading:general"], limit=5)
    assert [hit.id for hit in hits] == ["m1"]


def test_namespace_isolation(tmp_path: Path) -> None:
    store = MemoryStore.open(tmp_path)
    store.remember(
        [
            make_record("用户喜欢红莉栖", memory_id="m1", namespace="reading:steins-gate"),
            make_record("用户喜欢某个侦探", memory_id="m2", namespace="reading:other-book"),
        ]
    )

    hits = store.recall("红莉栖", namespaces=["reading:steins-gate"], limit=5)
    assert [hit.id for hit in hits] == ["m1"]
    assert store.recall("侦探", namespaces=["reading:steins-gate"]) == []


def test_supersede_retains_as_of_history(tmp_path: Path) -> None:
    store = MemoryStore.open(tmp_path)
    old = make_record("用户允许自动剧透", memory_id="old", kind="preference")
    store.remember([old])

    replacement = make_record("用户不喜欢主动剧透", memory_id="new", kind="preference")
    store.supersede("old", replacement, now="2026-02-01T00:00:00+00:00")

    current = store.recall("剧透", namespaces=["reading:general"], limit=5)
    assert [hit.id for hit in current] == ["new"]

    historical = store.recall(
        "剧透",
        namespaces=["reading:general"],
        as_of="2026-01-15T00:00:00+00:00",
        limit=5,
    )
    assert [hit.id for hit in historical] == ["old"]

    old_record = store.get("old")
    assert old_record is not None
    assert old_record.status == "superseded"
    assert old_record.superseded_by == "new"
    assert old_record.valid_to == "2026-02-01T00:00:00+00:00"


def test_rebuild_index_from_jsonl(tmp_path: Path) -> None:
    store = MemoryStore.open(tmp_path)
    store.remember([make_record("用户喜欢简洁回答", memory_id="m1", tags=("style",))])

    store.sqlite_path.unlink()
    rebuilt = MemoryStore.open(tmp_path)

    hits = rebuilt.recall("简洁", namespaces=["reading:general"])
    assert [hit.id for hit in hits] == ["m1"]


def test_compact_namespace_marks_sources_and_projects_markdown(tmp_path: Path) -> None:
    store = MemoryStore.open(tmp_path)
    records = [
        make_record("用户不喜欢剧透", memory_id="m1", tags=("spoiler",)),
        make_record("用户阅读时希望少打扰", memory_id="m2", tags=("quiet",)),
        make_record("用户喜欢简洁回复", memory_id="m3", tags=("style",)),
    ]
    store.remember(records)

    summary = store.compact_namespace(
        "reading:general",
        scope="user",
        min_records=3,
        summarizer=lambda items: "；".join(item.text for item in items),
        now="2026-03-01T00:00:00+00:00",
    )
    assert summary is not None
    assert summary.kind == "summary"
    assert summary.source_ids == ("m1", "m2", "m3")
    assert all(store.get(item.id).status == "compacted" for item in records)

    current = store.recall("剧透", namespaces=["reading:general"])
    assert [hit.id for hit in current] == [summary.id]

    store.write_projections()
    markdown = store.markdown_path.read_text(encoding="utf-8")
    assert "用户不喜欢剧透" in markdown
    assert "用户阅读时希望少打扰" in markdown


def test_revoke_hides_memory(tmp_path: Path) -> None:
    store = MemoryStore.open(tmp_path)
    store.remember([make_record("用户喜欢红莉栖", memory_id="m1")])

    revoked = store.revoke("m1", now="2026-04-01T00:00:00+00:00")

    assert revoked.status == "revoked"
    assert store.recall("红莉栖", namespaces=["reading:general"]) == []

def test_multi_token_fts_recall_returns_partial_match(tmp_path: Path) -> None:
    store = MemoryStore.open(tmp_path)
    store.remember([
        make_record("spoiler preference for the reader", memory_id="en1", tags=("spoiler",))
    ])

    hits = store.recall("spoiler cursor", scopes=["user"], namespaces=["reading:general"])

    assert [hit.id for hit in hits] == ["en1"]


def test_chinese_sentence_recall_uses_shared_bigram(tmp_path: Path) -> None:
    store = MemoryStore.open(tmp_path)
    store.remember([make_record("用户不喜欢主动剧透。", memory_id="cjk1")])

    hits = store.recall(
        "接下来会不会剧透？",
        scopes=["user"],
        namespaces=["reading:general"],
    )

    assert [hit.id for hit in hits] == ["cjk1"]
