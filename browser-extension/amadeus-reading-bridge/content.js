let lastSelection = "";

function cleanInline(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function normalizeText(value) {
  return String(value || "")
    .replace(/\u00a0/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function currentSelection() {
  const selection = window.getSelection();
  const text = selection ? String(selection).trim() : "";
  if (text) lastSelection = text;
  return text || lastSelection;
}

function absoluteUrl(value, baseUrl = location.href) {
  try {
    const url = new URL(String(value || "").trim(), baseUrl);
    return /^https?:/i.test(url.href) ? url.href : "";
  } catch (_) {
    return "";
  }
}

function uniqueUrls(values) {
  return [...new Set(values.filter(Boolean))];
}

const wenku8Adapter = {
  id: "wenku8",
  contentRoot(root) {
    return root.querySelector("#content");
  },
  extraStripSelectors: "#contentdp, .ad, .ads, .advertisement",
  isIndexPage(_root, url) {
    try {
      return /\/index\.htm(?:[?#]|$)/i.test(new URL(url).pathname);
    } catch (_) {
      return false;
    }
  },
  chapterUrls(root, baseUrl) {
    const urls = [];
    for (const anchor of root.querySelectorAll("a[href]") || []) {
      const href = String(anchor.getAttribute("href") || "").trim();
      if (!href || !/(?:^|\/)\d+\.htm(?:[?#].*)?$/i.test(href)) continue;
      const label = cleanInline(anchor.textContent || "");
      if (!label) continue;
      urls.push(absoluteUrl(href, baseUrl));
    }
    return uniqueUrls(urls);
  },
  nextChapterUrl(root, baseUrl) {
    const scripts = [...root.querySelectorAll("script") || []]
      .map(script => script.textContent || "")
      .join("\n");
    const match = scripts.match(/var\s+next_page\s*=\s*["']([^"']+)["']/i);
    const candidate = match?.[1] ? absoluteUrl(match[1], baseUrl) : "";
    return candidate && !/\/index\.htm(?:[?#]|$)/i.test(new URL(candidate).pathname)
      ? candidate
      : "";
  },
  bookKey(url) {
    const parsed = new URL(url);
    const match = parsed.pathname.match(/^\/novel\/([^/]+)\/([^/]+)(?:\/|$)/i);
    return match ? `${parsed.origin}/novel/${match[1]}/${match[2]}` : "";
  }
};

const linovelibAdapter = {
  id: "linovelib",
  contentRoot(root) {
    return root.querySelector("#acontent, #chaptercontent, .read-content, .box_con");
  },
  extraStripSelectors: ".ad, .ads, .advertisement, .chapter-nav, .readpage, .read-page",
  isIndexPage(_root, url) {
    try {
      const path = new URL(url).pathname;
      return /^\/novel\/\d+\.html$/i.test(path) || /^\/novel\/\d+\/?$/i.test(path);
    } catch (_) {
      return false;
    }
  },
  chapterUrls(root, baseUrl) {
    const parsed = new URL(baseUrl);
    const book = parsed.pathname.match(/^\/novel\/(\d+)/i)?.[1] || "";
    if (!book) return [];
    const pattern = new RegExp(`^/novel/${book}/\\d+\\.html$`, "i");
    const urls = [];
    for (const anchor of root.querySelectorAll("a[href]") || []) {
      const href = absoluteUrl(anchor.getAttribute("href"), baseUrl);
      if (!href) continue;
      try {
        if (pattern.test(new URL(href).pathname)) urls.push(href);
      } catch (_) {
        // Ignore malformed links.
      }
    }
    return uniqueUrls(urls);
  },
  nextChapterUrl(root, baseUrl) {
    const next = root.querySelector("#next_url, a#next_url, a[rel='next']");
    return next ? absoluteUrl(next.getAttribute("href"), baseUrl) : "";
  },
  bookKey(url) {
    const parsed = new URL(url);
    const match = parsed.pathname.match(/^\/novel\/(\d+)/i);
    return match ? `${parsed.origin}/novel/${match[1]}` : "";
  }
};

function siteAdapterFor(_root = document, url = location.href) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    if (host === "wenku8.net" || host.endsWith(".wenku8.net") || host.endsWith(".wenku8.com")) {
      return wenku8Adapter;
    }
    if (host === "linovelib.com" || host.endsWith(".linovelib.com")) {
      return linovelibAdapter;
    }
  } catch (_) {
    // Fall through to the generic adapter.
  }
  return null;
}

function chapterTitle(root = document) {
  const selectors = [
    "meta[property='og:novel:chapter_name']",
    "meta[property='og:title']",
    "h1",
    "h2",
    "[role='heading']"
  ];
  for (const selector of selectors) {
    const element = root.querySelector?.(selector);
    const value = element?.tagName === "META"
      ? element.getAttribute("content")
      : element?.textContent;
    const title = cleanInline(value);
    if (title) return title;
  }
  return cleanInline(root.title || document.title || "");
}

const CONTENT_SELECTORS = [
  "article",
  "main",
  "[role='main']",
  ".chapter-content",
  ".read-content",
  ".reader-content",
  ".article-content",
  ".post-content",
  ".entry-content",
  "#acontent",
  "#chaptercontent",
  "#chapter-content",
  "#content",
  ".content"
].join(",");

const STRIP_SELECTORS = [
  "script", "style", "noscript", "nav", "header", "footer", "aside", "form",
  "button", ".comments", "#comments", ".comment", ".ads", ".advertisement",
  ".toolbar", ".navigation", ".menu", ".metadata"
].join(",");

function textLength(element) {
  return String(element?.textContent || "").replace(/\s+/g, "").length;
}

function contentRoot(root = document, url = location.href) {
  const adapter = siteAdapterFor(root, url);
  const preferred = adapter?.contentRoot?.(root);
  if (preferred) return preferred;
  const candidates = [];
  if (root.body) candidates.push(root.body);
  if (root.nodeType === 1) candidates.push(root);
  for (const element of root.querySelectorAll?.(CONTENT_SELECTORS) || []) candidates.push(element);
  let best = null;
  let bestScore = 0;
  for (const element of candidates) {
    const length = textLength(element);
    if (length < 200) continue;
    const paragraphs = element.querySelectorAll?.("p").length || 0;
    const links = element.querySelectorAll?.("a").length || 0;
    const score = length + Math.min(paragraphs, 80) * 120 - Math.min(links, 200) * 50;
    if (score > bestScore) {
      best = element;
      bestScore = score;
    }
  }
  return best || root.body || root.documentElement;
}

function findNextChapterUrl(root = document, baseUrl = location.href) {
  const adapter = siteAdapterFor(root, baseUrl);
  const adapted = adapter?.nextChapterUrl?.(root, baseUrl);
  if (adapted) return adapted;

  const chapterPatterns = [/下一[章篇节]/i, /下章/i, /next\s*chapter/i, /continue\s+reading/i];
  const pagePatterns = [/下一页/i, /next\s*page/i, /^next$/i];
  const chapterLinks = [];
  const pageLinks = [];
  const relNext = root.querySelector?.("link[rel='next'], a[rel='next']");
  if (relNext) pageLinks.push(relNext);
  for (const anchor of root.querySelectorAll?.("a[href]") || []) {
    const label = cleanInline(
      anchor.textContent || anchor.getAttribute("aria-label") || anchor.getAttribute("title") || ""
    );
    if (chapterPatterns.some(pattern => pattern.test(label))) chapterLinks.push(anchor);
    else if (pagePatterns.some(pattern => pattern.test(label))) pageLinks.push(anchor);
  }
  for (const element of [...chapterLinks, ...pageLinks]) {
    const candidate = absoluteUrl(element.getAttribute("href"), baseUrl);
    if (candidate && candidate !== baseUrl) return candidate;
  }
  return "";
}

function extractChapter(root = document, url = location.href) {
  const rootElement = contentRoot(root, url);
  const adapter = siteAdapterFor(root, url);
  const clone = rootElement.cloneNode(true);
  const selectors = adapter?.extraStripSelectors
    ? `${STRIP_SELECTORS}, ${adapter.extraStripSelectors}`
    : STRIP_SELECTORS;
  clone.querySelectorAll?.(selectors).forEach(element => element.remove());
  const paragraphNodes = clone.querySelectorAll ? [...clone.querySelectorAll("p")] : [];
  const paragraphs = paragraphNodes
    .map(paragraph => cleanInline(paragraph.textContent))
    .filter(text => text.length >= 2);
  let text = paragraphs.length >= 2
    ? paragraphs.join("\n\n")
    : normalizeText(clone.innerText || clone.textContent || "");
  if (text.length > 120000) text = text.slice(0, 120000);
  return {
    url,
    title: chapterTitle(root),
    text,
    next_url: findNextChapterUrl(root, url)
  };
}

function bookKeyForUrl(value) {
  const parsed = new URL(value);
  const adapter = siteAdapterFor(document, value);
  const override = adapter?.bookKey?.(value);
  if (override) return override;
  for (const key of ["book", "book_id", "bookid", "novel", "novel_id", "novelid", "bid"]) {
    const item = parsed.searchParams.get(key);
    if (item) return `${parsed.origin}?${key}=${item}`;
  }
  const parts = parsed.pathname.split("/").filter(Boolean);
  const markers = new Set(["book", "novel", "story", "read", "article"]);
  const marker = parts.findIndex(part => markers.has(part.toLowerCase()));
  if (marker >= 0) return `${parsed.origin}/${parts.slice(0, marker + 2).join("/")}`;
  if (parts.length >= 2) {
    const last = parts[parts.length - 1];
    if (/\d{1,6}/.test(last) || /chapter|chap|read|page|episode|ep/i.test(last)) {
      return `${parsed.origin}/${parts.slice(0, -1).join("/")}`;
    }
  }
  return `${parsed.origin}${parsed.pathname}`;
}

async function fetchChapter(url) {
  const response = await fetch(url, { credentials: "include" });
  if (!response.ok) throw new Error(`章节抓取失败：HTTP ${response.status}`);
  const html = await response.text();
  const parsed = new DOMParser().parseFromString(html, "text/html");
  return extractChapter(parsed, url);
}

async function captureReading(maxChapters = 2) {
  const limit = Math.max(1, Math.min(2, Number(maxChapters) || 2));
  const adapter = siteAdapterFor(document, location.href);
  let first;
  if (adapter?.isIndexPage?.(document, location.href)) {
    const urls = adapter.chapterUrls?.(document, location.href) || [];
    if (!urls.length) throw new Error("目录页没有找到章节链接。");
    first = await fetchChapter(urls[0]);
  } else {
    first = extractChapter(document, location.href);
  }
  if (!first.text || first.text.length < 40) {
    throw new Error("没有识别到正文，请确认当前页是小说、文章或 EPUB 阅读页。");
  }
  const chapters = [first];
  let nextUrl = first.next_url;
  while (chapters.length < limit && nextUrl) {
    try {
      const target = new URL(nextUrl);
      if (target.origin !== location.origin) break;
      const next = await fetchChapter(target.href);
      if (!next.text || next.text.length < 40) break;
      chapters.push(next);
      nextUrl = next.next_url;
    } catch (_) {
      break;
    }
  }
  let offset = 0;
  const chunks = chapters.map((chapter, index) => {
    const start = offset;
    offset += chapter.text.length;
    return {
      id: `chapter-${index}-${start}-${offset}`,
      chapter: chapter.title || first.title || `Chapter ${index + 1}`,
      start_offset: start,
      end_offset: offset,
      text: chapter.text,
      page: null
    };
  });
  return {
    url: first.url,
    book_key: bookKeyForUrl(first.url),
    kind: "web",
    chapter: first.title,
    cursor: first.text.length,
    spoiler_cursor: first.text.length,
    selected_start: 0,
    selected_end: first.text.length,
    text: first.text,
    chunks
  };
}

function visibleComicImages(limit = 8) {
  const candidates = [];
  const viewportHeight = Math.max(1, window.innerHeight);
  const addImage = (element, source) => {
    const rect = element.getBoundingClientRect();
    const naturalWidth = Number(element.naturalWidth || element.width || rect.width || 0);
    const naturalHeight = Number(element.naturalHeight || element.height || rect.height || 0);
    if (naturalWidth < 300 && naturalHeight < 300) return;
    if (rect.bottom < -viewportHeight || rect.top > viewportHeight * 2) return;
    candidates.push({
      url: String(source || ""),
      width: Math.round(naturalWidth || rect.width),
      height: Math.round(naturalHeight || rect.height),
      top: rect.top,
      area: Math.max(naturalWidth || rect.width, naturalHeight || rect.height)
    });
  };
  for (const image of document.images || []) {
    const source = String(image.currentSrc || image.src || "").trim();
    if (source) addImage(image, source);
  }
  for (const canvas of document.querySelectorAll("canvas") || []) {
    try {
      const dataUrl = canvas.toDataURL("image/jpeg", 0.78);
      const rect = canvas.getBoundingClientRect();
      if (dataUrl && dataUrl.length > 200) {
        candidates.push({
          url: "",
          data_url: dataUrl,
          width: canvas.width,
          height: canvas.height,
          top: rect.top,
          area: Math.max(canvas.width, canvas.height)
        });
      }
    } catch (_) {
      // Cross-origin canvas cannot be exported.
    }
  }
  candidates.sort((a, b) => a.top - b.top || b.area - a.area);
  return candidates.slice(0, Math.max(1, Math.min(limit, candidates.length)));
}

async function imageToDataUrl(url) {
  if (!url) return "";
  try {
    const response = await fetch(url, { credentials: "include" });
    if (!response.ok) return "";
    const blob = await response.blob();
    if (!blob.size || blob.size > 12 * 1024 * 1024) return "";
    return await new Promise(resolve => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => resolve("");
      reader.readAsDataURL(blob);
    });
  } catch (_) {
    return "";
  }
}

function comicNextUrl(root = document, baseUrl = location.href) {
  const selectors = ["a[rel='next']", "a.next", ".next a", ".next_page", "#next_url", "a[href*='next']", "a[href*='chapter']"];
  for (const selector of selectors) {
    for (const element of root.querySelectorAll(selector) || []) {
      const candidate = absoluteUrl(element.getAttribute("href"), baseUrl);
      if (candidate && candidate !== baseUrl) return candidate;
    }
  }
  return "";
}

async function captureComicChapter(maxPages = 8) {
  const limit = Math.max(1, Math.min(8, Number(maxPages) || 8));
  const candidates = visibleComicImages(limit);
  if (!candidates.length) throw new Error("没有识别到漫画页面图片，请确认当前页是漫画阅读页。");
  const images = [];
  for (const item of candidates) {
    images.push({
      url: item.url || "",
      data_url: item.data_url || await imageToDataUrl(item.url),
      width: item.width,
      height: item.height
    });
  }
  return {
    url: location.href,
    book_key: bookKeyForUrl(location.href),
    chapter: chapterTitle(document),
    page: null,
    images,
    next_url: comicNextUrl(document, location.href)
  };
}

document.addEventListener("mouseup", () => { currentSelection(); });
document.addEventListener("keyup", () => { currentSelection(); });

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "AMADEUS_CAPTURE_SELECTION") {
    sendResponse({
      text: currentSelection(),
      url: location.href,
      book_key: bookKeyForUrl(location.href),
      chapter: chapterTitle(document),
      page: null
    });
    return true;
  }
  if (message?.type === "AMADEUS_CAPTURE_READING") {
    captureReading(message.maxChapters)
      .then(result => sendResponse(result))
      .catch(error => sendResponse({ error: String(error) }));
    return true;
  }
  if (message?.type === "AMADEUS_CAPTURE_COMIC_CHAPTER") {
    captureComicChapter(message.maxPages)
      .then(result => sendResponse(result))
      .catch(error => sendResponse({ error: String(error) }));
    return true;
  }
  return false;
});
