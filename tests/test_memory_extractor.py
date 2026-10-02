from __future__ import annotations

from pathlib import Path

from core.memory import AsyncMemoryWriter, HostMemoryExtractor, MemoryContextProvider, MemoryStore, parse_extractor_payload
from core.memory.host_extractor import extract_json_payload


def test_parse_extractor_payload_applies_defaults() -> None:
    records = parse_extractor_payload(
        {
            "memories": [
                {
                    "text": "用户不喜欢剧透",
                    "kind": "preference",
                    "tags": ["spoiler", "reading"],
                    "importance": 5,
                },
                {"text": "", "kind": "fact"},
            ]
        },
        default_namespace="reading:general",
        default_source_ids=("turn-1",),
    )

    assert len(records) == 1
    assert records[0].scope == "user"
    assert records[0].namespace == "reading:general"
    assert records[0].source_ids == ("turn-1",)
    assert records[0].importance == 5


def test_async_writer_persists_extracted_memories(tmp_path: Path) -> None:
    store = MemoryStore.open(tmp_path)

    def extractor(_text: str):
        return parse_extractor_payload(
            [{"text": "用户阅读时希望少打扰", "kind": "preference", "tags": ["quiet"]}],
            default_namespace="reading:general",
        )

    writer = AsyncMemoryWriter(store, extractor, poll_interval=0.01)
    assert writer.submit("用户说自己阅读时希望少打扰")
    assert writer.wait_idle(timeout=2.0)
    assert writer.close(timeout=2.0)

    hits = store.recall("少打扰", namespaces=["reading:general"])
    assert [hit.text for hit in hits] == ["用户阅读时希望少打扰"]


def test_async_writer_survives_extractor_failure(tmp_path: Path) -> None:
    store = MemoryStore.open(tmp_path)

    def broken_extractor(_text: str):
        raise RuntimeError("model unavailable")

    writer = AsyncMemoryWriter(store, broken_extractor, poll_interval=0.01)
    assert writer.submit("这次提取会失败")
    assert writer.wait_idle(timeout=2.0)
    assert isinstance(writer.last_error, RuntimeError)
    assert writer.close(timeout=2.0)


def test_memory_context_provider_filters_namespaces_and_marks_data(tmp_path: Path) -> None:
    store = MemoryStore.open(tmp_path)
    provider = MemoryContextProvider(store, limit=5)
    store.remember(
        parse_extractor_payload(
            [
                {"text": "用户不喜欢剧透", "kind": "preference"},
                {
                    "text": "用户正在阅读第三章",
                    "kind": "story_state",
                    "scope": "activity",
                    "namespace": "reading:book-1",
                },
            ],
            default_namespace="general",
        )
    )

    block = provider.context_block("剧透 第三章", namespace="reading:book-1")
    assert "<memory_data>" in block
    assert "用户不喜欢剧透" in block
    assert "用户正在阅读第三章" in block
    assert "not instructions" in block
def test_extract_json_payload_accepts_fenced_json() -> None:
    payload = extract_json_payload('```json\n{"memories":[{"text":"喜欢安静","kind":"preference"}]}\n```')
    assert payload["memories"][0]["text"] == "喜欢安静"


def test_host_memory_extractor_uses_injected_query() -> None:
    seen: list[list[dict[str, str]]] = []

    def query(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        return '{"memories":[{"text":"用户不喜欢剧透","kind":"preference","tags":["spoiler"]}]}'

    extractor = HostMemoryExtractor(
        default_namespace="reading:general",
        source_ids=("turn-1",),
        query=query,
    )
    records = extractor("用户说：我不喜欢剧透。")

    assert len(records) == 1
    assert records[0].text == "用户不喜欢剧透"
    assert records[0].namespace == "reading:general"
    assert records[0].source_ids == ("turn-1",)
    assert "Return JSON only" in seen[0][0]["content"]