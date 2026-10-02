# Reading bridge — client contract and limitations

The reading side of Companion has a working loopback adapter and one shipped
client. This note records the contract between them, how to verify it, and the
limitations that are real rather than pending work.

## Pieces

| Piece | Location | Role |
|---|---|---|
| Adapter | `core/reading/server.py` (`ReadingEventServer`) | Loopback HTTP receiver on `127.0.0.1:17878` |
| Client | `browser-extension/amadeus-reading-bridge/` | Chrome/Edge MV3 extension for web novels, browser PDFs and comic pages |
| Consumer | `core/companion/runtime.py` (`CompanionRuntime`) | Feeds `context_block()` into the chat prompt |

## Adapter contract

| Method | Path | Body / query | Response |
|---|---|---|---|
| `GET` | `/health` | — | `{"ok": true}` |
| `GET` | `/reading/session` | `?book_id=<id>` | `{"ok": true, "context": {...}}`, `404` when unknown |
| `POST` | `/reading/event` | a `reading.selection` event | `{"ok": true, "context": {...}}`, `400` on bad input |
| `POST` | `/reading/turn` | `book_id`, `user_message`, `assistant_message`, `selected_excerpt`, `referenced_chunks` | `{"ok": true}` |

Event fields the store actually reads: `book_id` (required), `kind`, `chapter`,
`page`, `cursor`, `spoiler_cursor`, `selected_start`, `selected_end`, `text`,
`chunks`. The canonical shape is in the master plan:

```json
{
  "type": "reading.selection",
  "app": "browser|obsidian|zotero|calibre|koreader",
  "book_id": "book:xxx",
  "chapter": "第三章",
  "page": 42,
  "cursor": 12345,
  "text": "选中内容"
}
```

## What the shipped client sends

`background.js` builds the event; `content.js` extracts either a selection or one to two chapters.

| Field | Source | Notes |
|---|---|---|
| `type` | constant | `reading.selection` |
| `app` | constant | `browser` |
| `book_id` | FNV-1a of a URL-derived book key | Heuristic; query keys such as `book_id`/`novel_id` win, otherwise common book/read path prefixes are kept |
| `chapter` | meta/heading/title | heuristic |
| `cursor`, `selected_start`, `selected_end` | current chapter length | automatic chapter mode uses the current chapter boundary |
| `spoiler_cursor` | current chapter length | next chapter chunks remain future text and are blocked |
| `page` | omitted when unknown | see the limitation below |
| `text` | current selection or current chapter body | automatic mode refuses an empty extraction |
| `chunks` | one chunk per captured chapter | current + next chapter when the next link is fetchable |

Entry points: popup **开始阅读** for one to two chapters, popup **发送当前选中文本**,
selection context menu, and `Ctrl+Shift+Y` for selected text.

## Site adapters

The shipped content script has explicit adapters for:

- `wenku8.net`: directory pages (`index.htm`) and chapter pages (`div#content`, `next_page`)
- `linovelib.com`: book/chapter pages (`#acontent`, `#next_url`)

Other sites use the generic DOM scoring and next-link heuristics.

## Limitations (real, not pending)

1. **The adapter has no CORS support.** It sends no `Access-Control-Allow-*`
   headers and has no `OPTIONS` handler, so a preflighted page `fetch` fails
   (`501`). The extension works because `host_permissions` exempts its service
   worker. A user script or bookmarklet cannot post here without a privileged
   request context (for example `GM_xmlhttpRequest`).
2. **An omitted `page` clears the stored page.** `update_from_event` reads
   `event.get("page")`, so an absent key is indistinguishable from an explicit
   `null` and `current_page` becomes `None`. The generic client therefore has no
   page number on most sites; a reader-specific client should send a real one.
3. **The cursor is a chapter/character boundary, not a calibrated book progress.**
   Automatic mode sets it to the end of the current chapter. It is accurate for
   the extracted DOM, but not for PDF pages, scrolling, or readers that render
   only part of the chapter.
4. **`book_id` is still heuristic.** The client removes common chapter suffixes
   and recognizes common query parameters, but unusual sites can still split one
   book or merge two books. Reader-specific adapters can send an exact id.
5. **PDF and manga pages need their own handling.** The extension captures page
   text selection only; image-only manga pages have no text layer to select.

## Verify

```powershell
# Contract tests: manifest, payload shape, live round-trip, CORS boundary
python -m pytest tests/test_reading_bridge_client.py -q

# Manual round-trip against a running Companion backend
Invoke-RestMethod http://127.0.0.1:17878/health
$body = '{"type":"reading.selection","app":"browser","book_id":"browser:test","chapter":"第三章","cursor":20,"text":"她推开了那扇门。"}'
Invoke-RestMethod http://127.0.0.1:17878/reading/event -Method Post -Body $body -ContentType 'application/json'
Invoke-RestMethod 'http://127.0.0.1:17878/reading/session?book_id=browser:test'
```

Then in a browser: load the extension unpacked, open a chapter, click the
extension and choose **开始阅读**. Confirm the session shows the current chapter,
its cursor, and one or two stored chapter chunks. The next chapter is stored as
future text behind `spoiler_cursor` until you advance.
