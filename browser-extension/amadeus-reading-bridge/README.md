# Amadeus Reading Bridge

Chrome/Edge extension for sending reading context from web novels, browser PDFs, online articles and manga pages to Amadeus Companion.

## Load

1. Open `chrome://extensions` or `edge://extensions`.
2. Enable Developer mode.
3. Choose **Load unpacked**.
4. Select this directory.
5. Start Amadeus Companion so `127.0.0.1:17878` is listening.

## Use

### Start reading automatically

Site adapters currently cover:

- Wenku8 / 轻小说文库: directory and chapter pages
- Linovelib: book and chapter pages
- Generic article/novel pages through DOM heuristics

1. Open a chapter or article page.
2. Click the extension button.
3. Click **开始阅读**.
4. The bridge captures the current chapter and, when the site exposes a same-origin next-chapter link, the next chapter too.
5. The current chapter becomes allowed reading context. The next chapter is stored as future text and is blocked by SpoilerGuard until you advance.

### Send only a selected excerpt

1. Select text in the page.
2. Click **发送当前选中文本** or press `Ctrl+Shift+Y`.
3. Amadeus receives a `reading.selection` event and can use it as reading context.

## What it sends

- Page URL and a stable book key derived from the URL.
- Chapter title.
- Paragraph-preserving chapter text.
- `cursor` and `spoiler_cursor`.
- One chunk per captured chapter when automatic reading starts.
- Chapter chunks include `id`, `chapter`, `start_offset`, `end_offset`, `text`, and `page: null`.

## Limitations

- The extension heuristic needs a normal article/chapter DOM. Highly scripted or canvas-based readers may need a reader-specific adapter.
- It captures the next chapter only when the link is same-origin and fetchable. Cross-origin, login-protected or rate-limited pages may fall back to the current chapter only.
- The generic web client does not know exact PDF page numbers. A reader-specific client can send precise `chapter`, `page` and offsets through the same `/reading/event` endpoint.
- The adapter is loopback-only and unauthenticated. This is why the client is an extension, not a page bookmarklet.

See `docs/reading-bridge.md` for the full contract.
