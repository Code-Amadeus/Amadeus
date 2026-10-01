const ENDPOINT = "http://127.0.0.1:17878/reading/event";
const COMIC_ENDPOINT = "http://127.0.0.1:17878/comic/chapter";

function stableBookId(value) {
  let hash = 2166136261;
  for (const ch of String(value || "")) {
    hash ^= ch.codePointAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return `browser:${(hash >>> 0).toString(16)}`;
}

function bookIdForCapture(captured, fallbackUrl = "") {
  return stableBookId(captured?.book_key || captured?.url || fallbackUrl);
}

async function captureSelection(tabId, fallbackUrl = "", fallbackText = "") {
  let captured = { text: fallbackText, url: fallbackUrl, chapter: "", page: null };
  try {
    const response = await chrome.tabs.sendMessage(tabId, { type: "AMADEUS_CAPTURE_SELECTION" });
    if (response?.text) captured = { ...captured, ...response };
  } catch (_) {
    // Restricted browser pages cannot run content scripts; use selectionText if available.
  }
  return captured;
}

async function postReadingEvent(payload) {
  const response = await fetch(ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!response.ok) throw new Error(`Amadeus adapter HTTP ${response.status}`);
  return response.json().catch(() => ({ ok: true }));
}

async function sendSelection(tab, fallbackText = "") {
  const url = tab?.url || "";
  const captured = await captureSelection(tab?.id, url, fallbackText);
  const text = String(captured.text || "").trim();
  if (!text) throw new Error("请先选中要发送的文字。");

  const bookId = bookIdForCapture(captured, url);
  const storageKey = `cursor:${bookId}`;
  const prior = await chrome.storage.local.get(storageKey);
  const start = Number(prior[storageKey] || 0);
  const end = start + text.length;

  const payload = {
    type: "reading.selection",
    app: "browser",
    book_id: bookId,
    chapter: String(captured.chapter || ""),
    // Omitted, not zero: the adapter reads a missing/null page as "unknown" and
    // keeps the stored page, while 0 would claim the reader is on page zero.
    ...(Number(captured.page) > 0 ? { page: Number(captured.page) } : {}),
    cursor: end,
    selected_start: start,
    selected_end: end,
    text
  };

  await postReadingEvent(payload);
  await chrome.storage.local.set({
    [storageKey]: end,
    lastReadingStatus: { ok: true, chars: text.length, bookId, at: Date.now() }
  });
  return { ok: true, chars: text.length };
}

async function sendReading(tab) {
  const url = tab?.url || "";
  if (!tab?.id) throw new Error("未找到当前网页。");
  const captured = await chrome.tabs.sendMessage(tab.id, {
    type: "AMADEUS_CAPTURE_READING",
    maxChapters: 2
  });
  const text = String(captured?.text || "").trim();
  if (!text) throw new Error("没有识别到正文，请确认当前页是小说、文章或 EPUB 阅读页。");
  const bookId = bookIdForCapture(captured, url);
  const chunks = Array.isArray(captured.chunks)
    ? captured.chunks.filter(chunk => chunk && String(chunk.text || "").trim())
    : [];
  const payload = {
    type: "reading.selection",
    app: "browser",
    kind: "web",
    book_id: bookId,
    chapter: String(captured.chapter || ""),
    cursor: Number(captured.cursor || text.length),
    spoiler_cursor: Number(captured.spoiler_cursor || text.length),
    selected_start: Number(captured.selected_start || 0),
    selected_end: Number(captured.selected_end || text.length),
    text,
    ...(chunks.length ? { chunks } : {})
  };
  await postReadingEvent(payload);
  await chrome.storage.local.set({
    [`cursor:${bookId}`]: payload.cursor,
    lastReadingStatus: {
      ok: true,
      chars: text.length,
      chapters: chunks.length || 1,
      bookId,
      at: Date.now()
    }
  });
  return { ok: true, chars: text.length, chapters: chunks.length || 1 };
}

async function postComicChapter(payload) {
  const response = await fetch(COMIC_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!response.ok) throw new Error(`Amadeus comic adapter HTTP ${response.status}`);
  return response.json().catch(() => ({ ok: true }));
}

async function sendComic(tab) {
  const url = tab?.url || "";
  if (!tab?.id) throw new Error("未找到当前网页。");
  const captured = await chrome.tabs.sendMessage(tab.id, {
    type: "AMADEUS_CAPTURE_COMIC_CHAPTER",
    maxPages: 8
  });
  const images = Array.isArray(captured?.images)
    ? captured.images.filter(item => item && (item.data_url || item.url))
    : [];
  if (!images.length) throw new Error("没有识别到漫画页面图片，请确认当前页是漫画阅读页。");
  const bookId = bookIdForCapture(captured, url);
  const payload = {
    type: "comic.chapter",
    app: "browser",
    book_id: bookId,
    chapter: String(captured.chapter || ""),
    url: String(captured.url || url),
    page: null,
    images,
    next_url: String(captured.next_url || "")
  };
  const result = await postComicChapter(payload);
  await chrome.storage.local.set({
    lastComicStatus: { ok: true, pages: images.length, bookId, at: Date.now() }
  });
  return { ok: true, pages: images.length, summary: String(result?.summary || "") };
}

async function startComicActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("未找到当前网页。");
  return await sendComic(tab);
}

async function sendActiveTab(fallbackText = "") {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("未找到当前网页。");
  return await sendSelection(tab, fallbackText);
}

async function startReadingActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("未找到当前网页。");
  return await sendReading(tab);
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "amadeus-send-selection",
      title: "发送选中文字到 Amadeus",
      contexts: ["selection"]
    });
    chrome.contextMenus.create({
      id: "amadeus-start-reading",
      title: "从当前章节开始阅读",
      contexts: ["page"]
    });
    chrome.contextMenus.create({
      id: "amadeus-start-comic",
      title: "从当前章节开始看漫画",
      contexts: ["page"]
    });
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  try {
    if (info.menuItemId === "amadeus-send-selection") {
      await sendSelection(tab, info.selectionText || "");
    } else if (info.menuItemId === "amadeus-start-reading") {
      await sendReading(tab);
    } else if (info.menuItemId === "amadeus-start-comic") {
      await sendComic(tab);
    }
  } catch (error) {
    await chrome.storage.local.set({ lastReadingStatus: { ok: false, error: String(error), at: Date.now() } });
  }
});

chrome.commands.onCommand.addListener(async command => {
  if (command !== "send-selection") return;
  try {
    await sendActiveTab();
  } catch (error) {
    await chrome.storage.local.set({ lastReadingStatus: { ok: false, error: String(error), at: Date.now() } });
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "AMADEUS_SEND_ACTIVE_SELECTION") {
    sendActiveTab()
      .then(result => sendResponse(result))
      .catch(error => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type === "AMADEUS_START_READING") {
    startReadingActiveTab()
      .then(result => sendResponse(result))
      .catch(error => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type === "AMADEUS_START_COMIC") {
    startComicActiveTab()
      .then(result => sendResponse(result))
      .catch(error => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  return false;
});
