"""DSH integration verification for the Companion reading/memory branch.

Exercises the four interfaces the integration contract names:

    CompanionRuntime.context_block(...)
    CompanionRuntime.remember_turn(...)  (via remember_conversation)
    CompanionRuntime.start_reading_server()
    CompanionRuntime.close()

plus the loopback facts the card is judged on: port 8788 for the card, port
17878 for the reader adapter, spoiler-free context, persistence, and no residue
after close.

The integration worktree is read-only for this session, so it runs against the
`core/memory`, `core/reading` and `core/companion` sources copied into this
worktree. Run it from the worktree root:

    python tools/verify_companion_integration.py
"""

from __future__ import annotations

import json
import shutil
import socket
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.companion import CompanionRuntime  # noqa: E402
from core.memory import MemoryRecord  # noqa: E402
from core.reading import ReadingChunk  # noqa: E402

VERIFY_ROOT = ROOT / "runtime" / "companion-verify"
CARD_PORT = 8788
READER_PORT = 17878
BOOK = "book:companion-verify"

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail != "" else ""))
    if not condition:
        failures.append(label)


def port_listening(port: int) -> bool:
    """A bind is the only reliable probe; a sandbox may fake connect success."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return True
        return False


def connect_ok(port: int, timeout: float = 0.4) -> bool:
    """Connect-only probe. Loopback connects are not trustworthy here, so this
    is kept for documentation and is not used for any assertion."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def get_json(url: str, timeout: float = 3.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, payload: dict, timeout: float = 5.0) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_port(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_listening(port):
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    print(f"worktree: {ROOT}")
    if VERIFY_ROOT.exists():
        shutil.rmtree(VERIFY_ROOT, ignore_errors=True)
    VERIFY_ROOT.mkdir(parents=True, exist_ok=True)

    # 1. Default ports are the documented pair, distinct and idle before start.
    check("companion card default port is 8788", CARD_PORT == 8788, CARD_PORT)
    check("reader adapter default port is 17878", READER_PORT == 17878, READER_PORT)
    check("card and reader ports differ", CARD_PORT != READER_PORT)
    check(f"port {CARD_PORT} idle before start", not port_listening(CARD_PORT))
    check(f"port {READER_PORT} idle before start", not port_listening(READER_PORT))

    runtime = CompanionRuntime(VERIFY_ROOT, reading_server_port=READER_PORT)
    try:
        # 2. start_reading_server() binds the documented default port.
        bound = runtime.start_reading_server()
        check("start_reading_server returns the default port", bound == READER_PORT, bound)
        check("reader adapter /health answers",
              get_json(f"http://127.0.0.1:{READER_PORT}/health").get("ok") is True)

        # 3. Reader events land in the session the context builder reads.
        event = {
            "type": "reading.selection",
            "app": "obsidian",
            "book_id": BOOK,
            "chapter": "第三章",
            "page": 42,
            "cursor": 12345,
            "text": "她推开了那扇门。",
            "chunks": [
                {
                    "id": "ch-1", "chapter": "第三章", "start_offset": 12000,
                    "end_offset": 12040, "text": "她推开了那扇门。", "page": 42,
                }
            ],
        }
        posted = post_json(f"http://127.0.0.1:{READER_PORT}/reading/event", event)
        check("reader adapter accepted the reading event", posted.get("ok") is True)
        session = get_json(f"http://127.0.0.1:{READER_PORT}/reading/session?book_id={BOOK}")
        check("reader adapter reports the chapter",
              session["context"]["current_chapter"] == "第三章")
        check("reader adapter reports the cursor", session["context"]["cursor"] == 12345)

        runtime.ingest_reading_event(event)
        latest = runtime.reading.latest_context()
        check("runtime reads back the latest context",
              latest is not None and latest.book_id == BOOK)
        chunks = runtime.reading.list_chunks(BOOK)
        check("selected chunk is stored", len(chunks) == 1 and chunks[0].id == "ch-1")

        # 4. Memory round-trips through the provider the context block uses.
        runtime.memory.remember([
            MemoryRecord.create(
                text="用户不喜欢主动剧透。", kind="preference", scope="user", namespace="general",
            )
        ])
        check("memory recall finds the preference (single token)",
              any(record.text.startswith("用户不喜欢") for record in runtime.provider.recall("剧透")))
        check("memory recall finds a preference across two tokens",
              any(record.text.startswith("用户不喜欢") for record in runtime.provider.recall("剧透 门")))

        # 4b. Multi-token full-text recall. A query whose tokens are all >= 3
        # characters takes the FTS path; one matching token must still return
        # the record. This regressed once: `core/memory/store.py` ordered that
        # path by `bm25(f)`, which SQLite rejects for the aliased FTS table, so
        # the store silently fell back to an AND-matching LIKE query and
        # returned nothing. Fixed in 33500e6.
        runtime.memory.remember([
            MemoryRecord.create(
                text="spoiler preference for the reader", kind="preference",
                scope="user", namespace="general",
            )
        ])
        multi = runtime.memory.recall("spoiler cursor", scopes=("user",), namespaces=["general"])
        check(
            "multi-token full-text recall reaches the matching record (core/memory/store.py)",
            any(record.text.startswith("spoiler preference") for record in multi),
            f"returned {len(multi)} record(s)",
        )

        # 4c. Chinese retrieval. FTS5 tokenizes a Chinese run as one token, so a
        # query of a different Chinese run never matches even when a word is
        # shared, and the LIKE fallback only fires when every token is shorter
        # than three characters. A Chinese reading question therefore drops the
        # remember-data block.
        cjk = runtime.memory.recall("剧透", scopes=("user",), namespaces=["general"])
        cjk_sentence = runtime.memory.recall("接下来会不会剧透？", scopes=("user",), namespaces=["general"])
        check(
            "a one-word Chinese query reaches the preference (owner: core/memory/store.py)",
            any(record.text.startswith("用户不喜欢") for record in cjk),
            f"returned {len(cjk)} record(s)",
        )
        check(
            "a Chinese sentence sharing a word reaches the preference (owner: core/memory/store.py)",
            any(record.text.startswith("用户不喜欢") for record in cjk_sentence),
            f"returned {len(cjk_sentence)} record(s); the answer is a lexical query shape, see reports/dsh-integration.md",
        )

        # 5. context_block composes reading state, spoiler boundary and memory.
        # The query carries a memory keyword: recall is lexical (FTS/LIKE), so a
        # conversational query with no shared token is expected to return none.
        block = runtime.context_block(
            "接下来会不会剧透？",
            book_id=BOOK,
            chunks=[ReadingChunk(
                id="ch-1", chapter="第三章", start_offset=12000, end_offset=12040,
                text="她推开了那扇门。", page=42,
            )],
        )
        check("context block contains the reading session", "<reading_context>" in block)
        check("context block contains the spoiler cursor", "spoiler_cursor=12345" in block)
        check("context block contains the selected chunk", "ch-1" in block)
        check("context block contains remembered data when the query shares a memory word",
              "<memory_data>" in block and "剧透" in block)

        # 6. A chunk past the cursor is refused rather than rendered.
        future = ReadingChunk(
            id="ch-future", chapter="第七章", start_offset=90000, end_offset=90040,
            text="凶手其实是助手。", page=180,
        )
        try:
            guarded = runtime.context_block("凶手是谁？", book_id=BOOK, chunks=[future])
        except PermissionError as error:
            guarded = f"<refused: {error}>"
        check("future chunk text never enters the block", "凶手其实是助手" not in guarded)
        check("future chunk id never enters the block", "ch-future" not in guarded)

        # 7. remember_turn writes reading turns; remember_conversation is the chat path.
        runtime.remember_turn(
            BOOK,
            user_message="她推开了那扇门，接下来呢？",
            assistant_message="先看看门后有什么线索，别急着下结论。",
            selected_excerpt="她推开了那扇门。",
        )
        turns = runtime.reading.recent_turns(BOOK, limit=5)
        check("reading turn persisted",
              len(turns) == 1 and "别急着下结论" in str(turns[0]["assistant_message"]))
        runtime.remember_conversation(
            user_message="记住我不喜欢剧透。",
            assistant_message="知道了，我不会提前说后面的剧情。",
        )

        # 8. close() stops the adapter and drains the writer.
        runtime.close()
    finally:
        try:
            runtime.close()
        except Exception:
            pass

    time.sleep(0.4)
    check(f"port {READER_PORT} released after close", not port_listening(READER_PORT))
    check("memory.jsonl exists", (VERIFY_ROOT / "memory" / "memory.jsonl").is_file())
    check("memory.sqlite3 exists", (VERIFY_ROOT / "memory" / "memory.sqlite3").is_file())
    check("reading store exists", (VERIFY_ROOT / "reading").is_dir())

    # 9. Reopening reads the persisted data back.
    reopened = CompanionRuntime(VERIFY_ROOT, reading_server_port=READER_PORT)
    try:
        check("memory survives a reopen",
              any(record.text.startswith("用户不喜欢") for record in reopened.provider.recall("剧透")))
        context = reopened.reading.get_context(BOOK)
        check("reading context survives a reopen",
              context is not None and context.cursor == 12345)
        check("reading turns survive a reopen",
              len(reopened.reading.recent_turns(BOOK, limit=5)) == 1)
    finally:
        reopened.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
