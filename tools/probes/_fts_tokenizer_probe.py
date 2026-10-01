"""Scratch probe: does the memory store reach a record by a shared CJK word?

The 2026-09-30 integration run found that a Chinese query such as
"接下来会不会剧透？" does not surface the remembered preference
"用户不喜欢主动剧透。". This probe isolates why, without touching
`core/memory`:

1. FTS5 tokenizes a Chinese run as one token, so a phrase search for a
   substring never matches; and
2. once the store hands more than one search token to the FTS index, the
   query fails while ordering by `bm25(f)` and the fallback LIKE path has no
   record to return.

Run from the worktree root:

    python tools/probes/_fts_tokenizer_probe.py
"""

import os
import sqlite3

DB = "runtime/_fts/_t.sqlite3"
os.makedirs("runtime/_fts", exist_ok=True)
if os.path.exists(DB):
    os.remove(DB)

connection = sqlite3.connect(DB)
connection.execute("create virtual table memory_fts using fts5(id, text, tags, namespace)")
connection.execute("insert into memory_fts values ('1', ?, '', 'general')", ("用户不喜欢主动剧透。",))

print("stored: 用户不喜欢主动剧透。")
for query in [
    '"用户不喜欢主动剧透"',
    '"用户不喜欢主动剧透。"',
    '"剧透"',
    '"用户"',
    "用户不喜欢主动剧透",
]:
    count = connection.execute(
        "select count(*) from memory_fts where memory_fts match ?", (query,)
    ).fetchone()[0]
    print(f"  fts match {query!r:32s} -> {count}")

connection.execute(
    "insert into memory_fts values ('2', ?, '', 'general')", ("spoiler preference for the reader",)
)
print()
print("stored: spoiler preference for the reader")
for query in ('"spoiler" OR "preference"', '"spoiler cursor"', '"spoiler" OR "cursor"'):
    count = connection.execute(
        "select count(*) from memory_fts where memory_fts match ?", (query,)
    ).fetchone()[0]
    print(f"  fts match {query!r:32s} -> {count}")

connection.close()
