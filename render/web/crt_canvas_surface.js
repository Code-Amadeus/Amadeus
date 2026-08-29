(function () {
  "use strict";

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function escapeAttr(value) {
    return escapeHtml(value).replace(/'/g, "&#39;");
  }

  function stripTrailingPathPunctuation(value) {
    let path = String(value || "").trim();
    let suffix = "";
    while (/[.,;:!?，。；：！？、)\]}\uFF09\u3011\u300B]$/.test(path)) {
      suffix = path.slice(-1) + suffix;
      path = path.slice(0, -1);
    }
    return { path, suffix };
  }

  function normalizeLocalPathCandidate(value) {
    let text = String(value || "").trim().replace(/[\u200B-\u200D\uFEFF]/g, "");
    if ((text.startsWith("<") && text.endsWith(">")) || (text.startsWith("[") && text.endsWith("]"))) {
      text = text.slice(1, -1).trim();
    }
    if ((text.startsWith("\"") && text.endsWith("\"")) || (text.startsWith("'") && text.endsWith("'"))) {
      text = text.slice(1, -1).trim();
    }
    text = stripTrailingPathPunctuation(text).path;
    if (/^file:\/\//i.test(text)) {
      try {
        const parsed = new URL(text);
        const decodedPath = decodeURIComponent(parsed.pathname || "");
        if (parsed.hostname) {
          return "\\\\" + parsed.hostname + decodedPath.replace(/\//g, "\\");
        }
        return decodedPath.replace(/^\/([A-Za-z]:)/, "$1").replace(/\//g, "\\");
      } catch (err) {
        return text.replace(/^file:\/\/\/?/i, "").replace(/\//g, "\\");
      }
    }
    return text;
  }

  function fileLabelFromPath(value) {
    const text = String(value || "").trim().replace(/[\\\/]+$/, "");
    const parts = text.split(/[\\\/]/).filter(Boolean);
    return parts[parts.length - 1] || text;
  }

  function fileRefHtml(path, label) {
    const text = normalizeLocalPathCandidate(path);
    const display = String(label || "").trim() || fileLabelFromPath(text);
    const attrPath = escapeAttr(text);
    return [
      "<span class=\"crt-canvas-file-ref\" data-file-path=\"" + attrPath + "\">",
      "<button type=\"button\" class=\"crt-canvas-file-main\" data-file-action=\"open\" data-file-path=\"" + attrPath + "\" aria-label=\"Open file\">" + escapeHtml(display) + "</button>",
      "<button type=\"button\" class=\"crt-canvas-file-toggle\" data-file-action=\"menu\" data-file-path=\"" + attrPath + "\" aria-label=\"File actions\">v</button>",
      "<span class=\"crt-canvas-file-menu\" role=\"menu\">",
      "<button type=\"button\" data-file-action=\"copy\" data-file-path=\"" + attrPath + "\">Copy path</button>",
      "<button type=\"button\" data-file-action=\"folder\" data-file-path=\"" + attrPath + "\">Show folder</button>",
      "<button type=\"button\" data-file-action=\"open_with\" data-file-path=\"" + attrPath + "\">Open with</button>",
      "</span>",
      "</span>",
    ].join("");
  }

  function escapeInlineText(value) {
    return escapeHtml(value).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  }

  function normalizeWebUrl(value) {
    const text = stripTrailingUrlPunctuation(String(value || "").trim()).url;
    if (/^www\./i.test(text)) return "https://" + text;
    return text;
  }

  function stripTrailingUrlPunctuation(value) {
    let url = String(value || "");
    let suffix = "";
    let changed = true;
    while (changed) {
      changed = false;
      const markdownCloser = url.match(/(\*\*|__|\*)$/);
      if (markdownCloser) {
        url = url.slice(0, -markdownCloser[1].length);
        changed = true;
        continue;
      }
      while (/[.,;:!?，。；：！？、)\]}\uFF09\u3011\u300B]$/.test(url)) {
        suffix = url.slice(-1) + suffix;
        url = url.slice(0, -1);
        changed = true;
      }
    }
    return { url, suffix };
  }

  function webLabelFromUrl(url, label) {
    const explicit = String(label || "").trim();
    if (explicit) return explicit;
    try {
      const parsed = new URL(normalizeWebUrl(url));
      const host = parsed.hostname.replace(/^www\./i, "");
      const path = parsed.pathname && parsed.pathname !== "/" ? parsed.pathname.replace(/\/$/, "") : "";
      const loopback = /^(127\.0\.0\.1|localhost|\[::1\]|::1)$/i.test(parsed.hostname);
      return ((loopback ? "local" : host) + path).slice(0, 72);
    } catch (_) {
      return String(url || "").replace(/^https?:\/\//i, "").slice(0, 72);
    }
  }

  function webLinkHtml(url, label, options) {
    const opts = options || {};
    const normalizedUrl = normalizeWebUrl(url);
    const display = webLabelFromUrl(normalizedUrl, label);
    const attrUrl = escapeAttr(normalizedUrl);
    const sessionId = String(opts.browserSessionId || "").trim();
    const attrSession = escapeAttr(sessionId);
    const main = sessionId
      ? "<button type=\"button\" class=\"crt-canvas-web-main\" data-web-action=\"browser_open\" data-web-url=\"" + attrUrl + "\" data-browser-session-id=\"" + attrSession + "\" title=\"" + attrUrl + "\" aria-label=\"Open in browser session\">" + escapeHtml(display) + "</button>"
      : "<a class=\"crt-canvas-web-main\" href=\"" + attrUrl + "\" target=\"_blank\" rel=\"noreferrer\" title=\"" + attrUrl + "\" aria-label=\"Open source\">" + escapeHtml(display) + "</a>";
    return [
      "<span class=\"crt-canvas-web-ref\" data-web-url=\"" + attrUrl + "\">",
      main,
      "<button type=\"button\" class=\"crt-canvas-web-toggle\" data-web-action=\"menu\" data-web-url=\"" + attrUrl + "\" data-browser-session-id=\"" + attrSession + "\" aria-label=\"Source actions\">&gt;</button>",
      "<span class=\"crt-canvas-web-menu\" role=\"menu\">",
      "<button type=\"button\" data-web-action=\"open\" data-web-url=\"" + attrUrl + "\">Open external</button>",
      "<button type=\"button\" data-web-action=\"copy\" data-web-url=\"" + attrUrl + "\">Copy URL</button>",
      "<button type=\"button\" data-web-action=\"source\" data-web-url=\"" + attrUrl + "\">Save source</button>",
      "</span>",
      "</span>",
    ].join("");
  }

  function providerActionHtml(item) {
    const metadata = item && item.metadata && typeof item.metadata === "object" ? item.metadata : {};
    const action = String(item.defaultAction || item.action || metadata.action || "open_details");
    const provider = String(item.provider || metadata.provider || "provider");
    const runId = String(item.runId || item.run_id || metadata.run_id || metadata.runId || item.ref || "");
    const cwd = String(item.cwd || metadata.cwd || "");
    const ref = String(item.ref || metadata.ref || runId);
    const label = String(item.label || metadata.label || (action === "view_diff" ? "View diff" : "Open Provider details"));
    return [
      "<span class=\"crt-canvas-provider-ref\" data-provider=\"" + escapeAttr(provider) + "\" data-provider-run-id=\"" + escapeAttr(runId) + "\">",
      "<button type=\"button\" class=\"crt-canvas-provider-main\" data-provider-action=\"" + escapeAttr(action) + "\" data-provider=\"" + escapeAttr(provider) + "\" data-provider-run-id=\"" + escapeAttr(runId) + "\" data-provider-cwd=\"" + escapeAttr(cwd) + "\" data-provider-ref=\"" + escapeAttr(ref) + "\" aria-label=\"" + escapeAttr(label) + "\">" + escapeHtml(label) + "</button>",
      "</span>",
    ].join("");
  }

  function linkifyInlineText(value) {
    const text = String(value || "");
    const re = new RegExp(
      "\\[([^\\]\\n]{1,140})\\]\\(([^)\\n]{1,2048})\\)"
      + "|<([^<>\\n]{1,2048})>"
      + "|((?:https?:\\/\\/|www\\.)[^\\s<>\"'`]+)",
      "gi"
    );
    let html = "";
    let lastIndex = 0;
    let match;
    while ((match = re.exec(text)) !== null) {
      html += escapeInlineText(text.slice(lastIndex, match.index));
      if (match[1] && match[2]) {
        const label = match[1];
        const target = match[2].trim();
        const stripped = stripTrailingUrlPunctuation(target);
        if (/^(https?:\/\/|www\.)/i.test(target)) {
          html += webLinkHtml(stripped.url, label) + escapeInlineText(stripped.suffix);
        } else {
          html += escapeInlineText(match[0]);
        }
      } else if (match[3]) {
        const target = match[3].trim();
        const urlStripped = stripTrailingUrlPunctuation(target);
        if (/^(https?:\/\/|www\.)/i.test(target)) {
          html += webLinkHtml(urlStripped.url) + escapeInlineText(urlStripped.suffix);
        } else {
          html += escapeInlineText(match[0]);
        }
      } else if (match[4]) {
        const stripped = stripTrailingUrlPunctuation(match[4]);
        html += webLinkHtml(stripped.url) + escapeInlineText(stripped.suffix);
      }
      lastIndex = match.index + match[0].length;
    }
    html += escapeInlineText(text.slice(lastIndex));
    return html;
  }

  function looksLikeCommand(value) {
    const text = String(value || "").trim();
    if (!text || text.length > 1200 || /[<>]/.test(text)) return false;
    return /^(npm|pnpm|yarn|bun|node|npx|python|py|uv|pytest|git|cargo|go|dotnet|mvn|gradle|ruff|tsc|vite|electron|powershell|pwsh|cmd|\.\\|\.\/)/i.test(text);
  }

  function commandRefHtml(command) {
    const text = String(command || "").trim();
    const attrCommand = escapeAttr(text);
    const label = text.split(/\r?\n/)[0].slice(0, 72);
    return [
      "<span class=\"crt-canvas-command-ref\" data-command=\"" + attrCommand + "\">",
      "<button type=\"button\" class=\"crt-canvas-command-main\" data-command-action=\"copy\" data-command=\"" + attrCommand + "\" aria-label=\"Copy command\">" + escapeHtml(label) + "</button>",
      "<button type=\"button\" class=\"crt-canvas-command-toggle\" data-command-action=\"menu\" data-command=\"" + attrCommand + "\" aria-label=\"Command actions\">v</button>",
      "<span class=\"crt-canvas-command-menu\" role=\"menu\">",
      "<button type=\"button\" data-command-action=\"make_bat\" data-command=\"" + attrCommand + "\">Make .bat chip</button>",
      "</span>",
      "</span>",
    ].join("");
  }

  function codeBlockHtml(code, lang) {
    const text = String(code || "").trim();
    const language = String(lang || "").trim().toLowerCase();
    const commandLang = /^(cmd|bat|batch|powershell|ps1|pwsh|shell|sh|bash|zsh|terminal|console)$/.test(language);
    if (commandLang || looksLikeCommand(text)) {
      return [
        "<div class=\"crt-canvas-command-block\">",
        commandRefHtml(text),
        "<pre>" + escapeHtml(text) + "</pre>",
        "</div>",
      ].join("");
    }
    return "<pre class=\"crt-canvas-code-block\"><code>" + escapeHtml(code) + "</code></pre>";
  }

  function markdownLite(value) {
    const lines = normalizeMarkdownForCrt(value).split(/\r?\n/);
    const out = [];
    let inList = false;
    let inFence = false;
    let fenceLang = "";
    let fenceLines = [];
    const closeList = () => {
      if (inList) {
        out.push("</ul>");
        inList = false;
      }
    };
    const tableCells = (line) => {
      let text = String(line || "").trim();
      if (text.startsWith("|")) text = text.slice(1);
      if (text.endsWith("|")) text = text.slice(0, -1);
      return text.split("|").map((cell) => cell.trim());
    };
    const isTableSeparator = (line) => {
      const cells = tableCells(line);
      return cells.length >= 2 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
    };
    const isTableRow = (line) => {
      const text = String(line || "").trim();
      if (!text.includes("|") || isTableSeparator(text)) return false;
      if (!text.startsWith("|") && !text.endsWith("|")) return false;
      return tableCells(text).length >= 2;
    };
    const isTableLine = (line) => isTableRow(line) || isTableSeparator(line);
    const tableHtml = (tableLines) => {
      const rows = tableLines.filter((line) => !isTableSeparator(line)).map(tableCells);
      if (!rows.length) return "";
      const width = Math.max(...rows.map((row) => row.length));
      const padRow = (row) => row.concat(Array(Math.max(0, width - row.length)).fill(""));
      const header = padRow(rows[0]);
      const body = rows.slice(1).map(padRow);
      const headHtml = "<thead><tr>" + header.map((cell) => "<th>" + inlineMarkdown(cell) + "</th>").join("") + "</tr></thead>";
      const bodyHtml = body.length
        ? "<tbody>" + body.map((row) => "<tr>" + row.map((cell) => "<td>" + inlineMarkdown(cell) + "</td>").join("") + "</tr>").join("") + "</tbody>"
        : "";
      return "<div class=\"crt-canvas-md-table-wrap\"><table>" + headHtml + bodyHtml + "</table></div>";
    };
    for (let i = 0; i < lines.length; i += 1) {
      const raw = lines[i];
      const line = raw.trim();
      if (line.startsWith("```")) {
        if (inFence) {
          out.push(codeBlockHtml(fenceLines.join("\n"), fenceLang));
          inFence = false;
          fenceLang = "";
          fenceLines = [];
        } else {
          closeList();
          inFence = true;
          fenceLang = line.slice(3).trim();
          fenceLines = [];
        }
        continue;
      }
      if (inFence) {
        fenceLines.push(raw);
        continue;
      }
      if (isTableRow(line)) {
        const tableLines = [];
        let j = i;
        while (j < lines.length) {
          const candidate = lines[j].trim();
          if (!isTableLine(candidate)) break;
          tableLines.push(candidate);
          j += 1;
        }
        const hasSeparator = tableLines.some(isTableSeparator);
        const rowCount = tableLines.filter(isTableRow).length;
        if (hasSeparator || rowCount >= 2) {
          closeList();
          out.push(tableHtml(tableLines));
          i = j - 1;
          continue;
        }
      }
      if (!line) {
        closeList();
        continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (/^-{3,}$/.test(line)) {
        closeList();
        out.push("<hr>");
      } else if (heading) {
        closeList();
        const depth = Math.min(6, heading[1].length);
        const tag = depth <= 1 ? "h2" : (depth === 2 ? "h3" : "h4");
        out.push("<" + tag + " class=\"crt-canvas-md-heading depth-" + depth + "\">" + inlineMarkdown(heading[2]) + "</" + tag + ">");
      } else if (line.startsWith("- ")) {
        if (!inList) {
          out.push("<ul>");
          inList = true;
        }
        out.push("<li>" + inlineMarkdown(line.slice(2)) + "</li>");
      } else {
        closeList();
        out.push("<p>" + inlineMarkdown(line) + "</p>");
      }
    }
    if (inFence) out.push(codeBlockHtml(fenceLines.join("\n"), fenceLang));
    closeList();
    return out.join("");
  }

  function normalizeMarkdownForCrt(value) {
    return expandCompactTableRows(String(value || "")
      .replace(/\r\n/g, "\n")
      .replace(/\uFF5C/g, "|")
      .replace(/[ \t]+(-{3,})[ \t]+/g, "\n\n$1\n\n")
      .replace(/([^\n])\s+(#{1,6})\s+([0-9A-Za-z\u3040-\u30ff\u3400-\u9fff])/g, "$1\n\n$2 $3")
      .replace(/([^\n])\s+(-\s+\[[^\]\n]{1,140}\]\([^)]+\))/g, "$1\n$2"));
  }

  function expandCompactTableRows(value) {
    return String(value || "").split("\n").map((line) => {
      const pipeCount = (line.match(/\|/g) || []).length;
      if (pipeCount < 4) return line;
      let expanded = line;
      if (!expanded.trimStart().startsWith("|")) {
        expanded = expanded.replace(/^(.+?)\s+(\|[^|\n]+(?:\|[^|\n]*){2,})$/, "$1\n$2");
      }
      return expanded
        // Common compressed form: "| head | |---|---| | row |".
        .replace(/\|\s+(?=\|)/g, "|\n")
        // Same as above, but without the separating space.
        .replace(/\|(?=\|)/g, "|\n");
    }).join("\n");
  }

  function inlineMarkdown(value) {
    const text = String(value || "");
    return text.split(/(`[^`]*`)/g).map((chunk) => {
      if (chunk.startsWith("`") && chunk.endsWith("`")) {
        const code = chunk.slice(1, -1);
        if (looksLikeCommand(code)) return commandRefHtml(code);
        return "<code>" + escapeHtml(code) + "</code>";
      }
      return linkifyInlineText(chunk);
    }).join("");
  }

  const COMPACT_SIZE = { width: 270, height: 530 };
  const WIDE_SIZE = { width: 410, height: 530 };
  // Nominal oversize; clampSize caps it to the available surface area.
  const FULL_SIZE = { width: 4096, height: 2560 };
  const SIZE_KEY = "amadeus.wallpaperCrtCanvas.size.v3";
  const PRESET_KEY = "amadeus.wallpaperCrtCanvas.preset.v3";
  const DEMO_LOOP_MS = 26000;
  const DEMO_FLOW = [
    {
      atMs: 0,
      expanded: false,
      phase: "Ready",
      title: "Waiting for work signal",
      lead: "Standing by for a provider task.",
      progress: 4,
      sizePreset: "compact",
      signals: [
        { label: "idle", text: "OpenClaw host is connected and quiet.", detail: "no active run" },
      ],
    },
    {
      atMs: 1400,
      expanded: true,
      phase: "Intake",
      title: "Work request received",
      lead: "A user request entered the host. The card wakes before tools begin.",
      progress: 14,
      sizePreset: "compact",
      signals: [
        { label: "request", text: "Created a run envelope and task contract.", detail: "run.openclaw.local" },
        { label: "input", text: "User intent compressed into a short work goal.", detail: "no raw chat stream" },
      ],
    },
    {
      atMs: 5200,
      expanded: true,
      phase: "Route",
      title: "Routing task to provider",
      lead: "The host picks the narrowest provider and keeps broad OS authority outside the tool.",
      progress: 28,
      sizePreset: "compact",
      signals: [
        { label: "route", text: "Coding scope routed to the selected workspace provider.", detail: "capability: workspace.write" },
        { label: "policy", text: "Workspace write remains permission-gated.", detail: "level 2 reversible" },
      ],
    },
    {
      atMs: 9000,
      expanded: true,
      phase: "Context",
      title: "Context packet prepared",
      lead: "Only the task contract, current workspace, and recent turn capsule are passed down.",
      progress: 42,
      sizePreset: "compact",
      signals: [
        { label: "context", text: "Loaded project capsule and current branch state.", detail: "3 evidence refs" },
        { label: "plan", text: "Plan preview reduced to three checkpoints.", detail: "interruptable" },
      ],
    },
    {
      atMs: 13200,
      expanded: true,
      phase: "Work",
      title: "Provider run streaming",
      lead: "Tool calls are grouped into work signals so the CRT shows progress, not raw logs.",
      progress: 63,
      sizePreset: "compact",
      signals: [
        { label: "tool", text: "Inspecting files and mapping likely edit points.", detail: "read grouped" },
        { label: "edit", text: "Potential changes staged as reversible deltas.", detail: "+42 / -11" },
        { label: "run", text: "Validation command queued behind the edit signal.", detail: "npm run build" },
      ],
    },
    {
      atMs: 17600,
      expanded: true,
      phase: "Check",
      title: "Permission checkpoint",
      lead: "The card floats the decision only when a scoped side effect is needed.",
      progress: 72,
      permissionVisible: true,
      sizePreset: "compact",
      signals: [
        { label: "permission", text: "Requesting reversible workspace write.", detail: "2 files inside cwd" },
        { label: "pause", text: "Provider waits for a lease instead of assuming authority.", detail: "allow once / queue" },
      ],
    },
    {
      atMs: 21200,
      expanded: true,
      phase: "Artifact",
      title: "Result preview ready",
      lead: "The card widens only for previewable output, leaving the character side visible.",
      progress: 91,
      mode: "html",
      sizePreset: "wide",
      signals: [
        { label: "artifact", text: "HTML preview is ready inside the CRT canvas.", detail: "no IDE takeover" },
        { label: "review", text: "Diff, command log, and audit trace stay one layer deeper.", detail: "foldable" },
      ],
    },
  ];

  function demoFrame(elapsedMs) {
    const t = elapsedMs % DEMO_LOOP_MS;
    let frame = DEMO_FLOW[0];
    for (const item of DEMO_FLOW) {
      if (t >= item.atMs) frame = item;
    }
    return frame;
  }

  function presetSize(preset) {
    if (preset === "full") return FULL_SIZE;
    return preset === "wide" ? WIDE_SIZE : COMPACT_SIZE;
  }

  function demoEnabledByUrl() {
    try {
      const value = String(new URLSearchParams(window.location.search || "").get("canvasDemo") || "").toLowerCase();
      return value === "1" || value === "true" || value === "yes" || value === "on";
    } catch (err) {
      return false;
    }
  }

  function loadSavedPreset() {
    try {
      const saved = localStorage.getItem(PRESET_KEY);
      if (saved === "compact" || saved === "wide" || saved === "full" || saved === "custom") return saved;
    } catch (err) {}
    return "compact";
  }

  function loadSavedSize() {
    try {
      const saved = JSON.parse(localStorage.getItem(SIZE_KEY) || "null");
      if (saved && Number.isFinite(saved.width) && Number.isFinite(saved.height)) {
        return { width: Number(saved.width), height: Number(saved.height) };
      }
    } catch (err) {}
    return presetSize(loadSavedPreset());
  }

  function injectStyle() {
    if (document.getElementById("crt-canvas-surface-style")) return;
    const style = document.createElement("style");
    style.id = "crt-canvas-surface-style";
    style.textContent = `
      .crt-canvas-surface {
        position: absolute;
        z-index: 8;
        overflow: hidden;
        pointer-events: none;
        color: rgba(198, 255, 239, 0.92);
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      }

      .crt-canvas-surface-dot {
        position: absolute;
        top: max(10px, 3.2%);
        right: max(12px, 3.4%);
        width: 16px;
        height: 16px;
        border: 1px solid rgba(94, 242, 216, 0.48);
        border-radius: 999px;
        background:
          radial-gradient(circle at 50% 50%, rgba(198, 255, 239, 0.96) 0 18%, rgba(62, 238, 198, 0.42) 19% 38%, transparent 39%),
          rgba(4, 27, 30, 0.34);
        box-shadow: 0 0 8px rgba(84, 255, 221, 0.34), 0 0 24px rgba(37, 233, 202, 0.16);
        opacity: 0.82;
        cursor: pointer;
        pointer-events: auto;
      }

      .crt-canvas-surface-status {
        position: absolute;
        top: max(9px, 3%);
        right: calc(max(12px, 3.4%) + 22px);
        min-height: 18px;
        padding: 2px 7px;
        border: 1px solid rgba(94, 242, 216, 0.18);
        border-radius: 999px;
        background: rgba(4, 23, 28, 0.42);
        color: rgba(190, 255, 239, 0.72);
        font: inherit;
        font-size: 9px;
        line-height: 1;
        letter-spacing: 0;
        text-transform: uppercase;
        box-shadow: 0 0 18px rgba(37, 233, 202, 0.1);
        cursor: pointer;
        opacity: 0.72;
        pointer-events: auto;
      }

      .crt-canvas-surface:not(.has-content):not(.expanded) .crt-canvas-surface-status {
        display: none;
      }

      .crt-canvas-surface-status.ws-locked {
        border-color: rgba(255, 199, 122, 0.46);
        box-shadow: 0 0 18px rgba(255, 186, 108, 0.16);
        opacity: 0.92;
      }

      .crt-canvas-surface-status .crt-canvas-ws-lock {
        color: rgba(255, 218, 156, 0.96);
        font-style: normal;
        font-weight: 600;
        text-shadow: 0 0 6px rgba(255, 190, 110, 0.35);
      }

      .crt-canvas-surface-dot span {
        position: absolute;
        inset: 5px;
        border-radius: inherit;
        background: rgba(210, 255, 242, 0.96);
        box-shadow: 0 0 7px rgba(137, 255, 225, 0.8);
      }

      .crt-canvas-surface-dot:hover,
      .crt-canvas-surface.expanded .crt-canvas-surface-dot,
      .crt-canvas-surface-status:hover,
      .crt-canvas-surface.expanded .crt-canvas-surface-status {
        opacity: 1;
        transform: translateY(-1px) scale(1.04);
      }

      .crt-canvas-surface-card {
        position: absolute;
        top: max(32px, 9%);
        left: 4%;
        width: var(--crt-canvas-w, min(72%, 430px));
        height: var(--crt-canvas-h, min(74%, 560px));
        max-width: calc(100% - max(24px, 7%));
        max-height: calc(91% - 18px);
        min-width: 220px;
        min-height: 180px;
        display: flex;
        flex-direction: column;
        gap: 6px;
        padding: 7px;
        border: 1px solid rgba(91, 239, 215, 0.42);
        border-radius: 16px;
        background:
          linear-gradient(135deg, rgba(3, 26, 30, 0.88), rgba(5, 20, 28, 0.74)),
          repeating-linear-gradient(0deg, rgba(148, 255, 230, 0.055) 0 1px, transparent 1px 5px);
        box-shadow: 0 16px 48px rgba(0, 12, 18, 0.34), 0 0 46px rgba(50, 238, 207, 0.13);
        backdrop-filter: blur(10px) saturate(1.08);
        overflow: hidden;
        pointer-events: auto;
        resize: both;
      }

      .crt-canvas-surface-card[hidden],
      .crt-canvas-surface:not(.expanded) .crt-canvas-surface-card {
        display: none !important;
      }

      .crt-canvas-semantic-header {
        position: relative;
        z-index: 1;
        flex: 0 0 auto;
        min-height: 34px;
        padding: 4px 72px 7px 8px;
        border-bottom: 1px solid rgba(105, 230, 214, 0.12);
        cursor: default;
      }

      .crt-canvas-semantic-header span {
        display: block;
        margin-bottom: 3px;
        color: rgba(157, 234, 219, 0.64);
        font-size: 8px;
        line-height: 1;
        text-transform: uppercase;
      }

      .crt-canvas-semantic-header strong {
        display: block;
        overflow: hidden;
        color: rgba(207, 255, 241, 0.94);
        font-size: clamp(12px, 2.2vw, 15px);
        line-height: 1.16;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .crt-canvas-task-dock {
        position: relative;
        z-index: 3;
        flex: 0 0 auto;
        display: grid;
        gap: 6px;
        min-width: 0;
        padding: 6px 7px 7px;
        border: 1px solid rgba(105, 230, 214, 0.14);
        border-radius: 12px;
        background: rgba(3, 23, 29, 0.36);
      }

      .crt-canvas-task-dock-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 4px;
        min-width: 0;
      }

      .crt-canvas-task-counts {
        overflow: hidden;
        color: rgba(179, 240, 225, 0.68);
        font-size: 8px;
        line-height: 1;
        text-overflow: ellipsis;
        text-transform: uppercase;
        white-space: nowrap;
      }

      .crt-canvas-task-focus {
        flex: 1 1 auto;
        display: inline-flex;
        align-items: center;
        justify-content: flex-end;
        gap: 3px;
        min-width: 0;
      }

      .crt-canvas-task-focus span {
        max-width: min(190px, 34vw);
        overflow: hidden;
        color: rgba(179, 240, 225, 0.62);
        font-size: 7px;
        line-height: 1.15;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .crt-canvas-task-focus span.is-locked {
        color: rgba(255, 218, 156, 0.88);
      }

      .crt-canvas-task-focus button {
        min-height: 19px;
        padding: 0 7px;
        border-color: rgba(112, 225, 211, 0.14);
        background: transparent;
        color: rgba(164, 220, 210, 0.48);
        font-size: 7px;
        letter-spacing: 0.06em;
        text-transform: uppercase;
      }

      .crt-canvas-task-focus button.active {
        border-color: rgba(113, 255, 222, 0.42);
        background: rgba(21, 74, 70, 0.48);
        color: rgba(221, 255, 246, 0.92);
      }

      .crt-canvas-task-action-error {
        margin: 2px 0 0;
        padding: 3px 5px;
        border: 1px solid rgba(255, 119, 135, 0.34);
        border-radius: 4px;
        background: rgba(91, 22, 35, 0.24);
        color: rgba(255, 190, 198, 0.92);
        font-size: 7px;
        line-height: 1.3;
      }

      .crt-canvas-task-filters {
        flex: 1 1 auto;
        display: flex;
        align-items: center;
        gap: 2px;
        min-width: 0;
        overflow-x: auto;
        scrollbar-width: none;
      }

      .crt-canvas-task-filters::-webkit-scrollbar {
        display: none;
      }

      .crt-canvas-task-filters button {
        flex: 0 0 auto;
        min-height: 19px;
        padding: 0 5px;
        border-color: rgba(112, 225, 211, 0.12);
        background: rgba(3, 23, 29, 0.26);
        color: rgba(164, 220, 210, 0.48);
        font-size: 7px;
        letter-spacing: 0.02em;
        text-transform: uppercase;
      }

      .crt-canvas-task-filters button.active {
        border-color: rgba(113, 255, 222, 0.38);
        background: rgba(21, 74, 70, 0.4);
        color: rgba(221, 255, 246, 0.88);
      }

      .crt-canvas-task-rail {
        display: flex;
        gap: 5px;
        min-width: 0;
        overflow-x: auto;
        overflow-y: visible;
        overscroll-behavior: contain;
      }

      .crt-canvas-task-item {
        width: 100%;
        min-height: 48px !important;
        display: grid;
        grid-template-columns: 7px minmax(0, 1fr);
        align-items: center;
        gap: 6px;
        padding: 5px 27px 5px 7px !important;
        overflow: hidden;
        border-radius: 9px !important;
        text-align: left;
      }

      .crt-canvas-project-item {
        grid-template-columns: 7px minmax(0, 1fr) auto;
        padding-right: 7px !important;
      }

      .crt-canvas-project-item > b {
        color: rgba(179, 240, 225, 0.68);
        font-size: 7px;
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        white-space: nowrap;
      }

      .crt-canvas-task-card {
        position: relative;
        flex: 0 0 min(150px, 44%);
        min-width: 112px;
      }

      .crt-canvas-task-disposition {
        position: relative;
        z-index: 5;
        flex: 0 0 auto;
      }

      .crt-canvas-task-card > .crt-canvas-task-disposition {
        position: absolute;
        top: 5px;
        right: 5px;
      }

      .crt-canvas-task-disposition-toggle {
        width: 17px;
        min-width: 17px;
        height: 17px;
        min-height: 17px;
        padding: 0 !important;
        display: grid;
        place-items: center;
        border-color: rgba(112, 225, 211, 0.2) !important;
        border-radius: 999px !important;
        background: rgba(3, 23, 29, 0.72) !important;
      }

      .crt-canvas-task-disposition-toggle i {
        width: 3px;
        height: 3px;
        border-radius: 999px;
        background: rgba(207, 255, 241, 0.84);
        box-shadow: 0 0 5px rgba(113, 255, 222, 0.42);
      }

      .crt-canvas-task-disposition-menu {
        position: absolute;
        top: calc(100% + 3px);
        right: 0;
        z-index: 8;
        width: max-content;
        min-width: 98px;
        display: grid;
        gap: 2px;
        padding: 3px;
        border: 1px solid rgba(112, 225, 211, 0.3);
        border-radius: 7px;
        background: rgba(3, 20, 27, 0.97);
        box-shadow: 0 8px 22px rgba(0, 0, 0, 0.38);
      }

      .crt-canvas-task-disposition-overlay {
        z-index: 24;
      }

      .crt-canvas-task-disposition-menu button {
        min-height: 21px;
        padding: 3px 8px;
        border-color: transparent;
        background: transparent;
        color: rgba(221, 255, 246, 0.9);
        font-size: 8px;
        text-align: left;
        white-space: nowrap;
      }

      .crt-canvas-task-disposition-menu button:hover,
      .crt-canvas-task-disposition-menu button:focus-visible {
        border-color: rgba(113, 255, 222, 0.32);
        background: rgba(21, 74, 70, 0.58);
      }

      .crt-canvas-task-item.active {
        border-color: rgba(113, 255, 222, 0.52);
        background: rgba(20, 70, 68, 0.56);
        box-shadow: inset 0 0 16px rgba(76, 235, 205, 0.07);
      }

      .crt-canvas-task-item.action-ok,
      .crt-canvas-task-focus button.action-ok {
        border-color: rgba(113, 255, 174, 0.68);
      }

      .crt-canvas-task-item.action-error,
      .crt-canvas-task-focus button.action-error {
        border-color: rgba(255, 113, 145, 0.68);
      }

      .crt-canvas-task-control {
        display: flex;
        justify-content: flex-end;
        gap: 5px;
      }

      .crt-canvas-task-control button.is-secondary {
        border-color: rgba(112, 225, 211, 0.16);
        background: transparent;
        color: rgba(184, 236, 224, 0.66);
      }

      .crt-canvas-task-control button {
        min-height: 20px;
        padding: 0 8px;
        border-color: rgba(113, 255, 222, 0.38);
        background: rgba(21, 74, 70, 0.42);
        color: rgba(221, 255, 246, 0.9);
        font-size: 8px;
        letter-spacing: 0.05em;
        text-transform: uppercase;
        white-space: nowrap;
      }

      .crt-canvas-task-control button.action-ok {
        border-color: rgba(113, 255, 174, 0.68);
      }

      .crt-canvas-task-control button.action-error {
        border-color: rgba(255, 113, 145, 0.68);
      }

      .crt-canvas-task-dot {
        width: 6px;
        height: 6px;
        border-radius: 999px;
        background: rgba(153, 207, 198, 0.42);
        box-shadow: 0 0 7px rgba(127, 237, 215, 0.12);
      }

      .crt-canvas-task-item.is-running .crt-canvas-task-dot,
      .crt-canvas-task-strip.is-running .crt-canvas-task-dot {
        background: rgba(109, 255, 218, 0.92);
        box-shadow: 0 0 8px rgba(82, 255, 211, 0.46);
      }

      .crt-canvas-task-item.needs-attention .crt-canvas-task-dot,
      .crt-canvas-task-strip.needs-attention .crt-canvas-task-dot {
        background: rgba(232, 177, 255, 0.92);
        box-shadow: 0 0 8px rgba(210, 126, 255, 0.42);
      }

      .crt-canvas-task-item.is-stalled .crt-canvas-task-dot,
      .crt-canvas-task-strip.is-stalled .crt-canvas-task-dot {
        background: rgba(255, 204, 117, 0.94);
        box-shadow: 0 0 8px rgba(255, 190, 82, 0.42);
      }

      .crt-canvas-task-item.is-complete .crt-canvas-task-dot,
      .crt-canvas-task-strip.is-complete .crt-canvas-task-dot {
        background: rgba(144, 224, 178, 0.62);
      }

      .crt-canvas-task-striprow {
        display: flex;
        align-items: stretch;
        gap: 5px;
        min-width: 0;
      }

      .crt-canvas-task-railrow {
        display: flex;
        align-items: stretch;
        gap: 5px;
        min-width: 0;
      }

      .crt-canvas-task-railrow .crt-canvas-task-rail {
        flex: 1 1 auto;
        min-width: 0;
      }

      .crt-canvas-task-railrow .crt-canvas-task-control {
        flex: 0 0 auto;
        align-self: stretch;
        flex-direction: column;
        justify-content: center;
        gap: 4px;
      }

      .crt-canvas-task-railrow .crt-canvas-task-control button {
        flex: 1 1 auto;
      }

      .crt-canvas-task-railrow .crt-canvas-task-control button,
      .crt-canvas-task-striprow .crt-canvas-task-control button {
        min-height: 0;
        max-width: 92px;
        padding: 3px 8px;
        border-radius: 7px;
        border-color: rgba(112, 225, 211, 0.18);
        background: rgba(3, 23, 29, 0.22);
        font-size: 8px;
        white-space: normal;
        line-height: 1.5;
        text-align: center;
      }

      .crt-canvas-task-striprow > .crt-canvas-task-disposition {
        align-self: center;
      }


      .crt-canvas-task-strip {
        flex: 1 1 auto;
        display: flex;
        align-items: center;
        gap: 6px;
        min-width: 0;
        min-height: 20px;
        padding: 2px 7px !important;
        border-color: rgba(112, 225, 211, 0.1) !important;
        border-radius: 7px !important;
        background: rgba(3, 23, 29, 0.22);
        text-align: left;
      }

      .crt-canvas-task-strip .crt-canvas-task-dot {
        flex: 0 0 auto;
      }

      .crt-canvas-task-strip span {
        flex: 1 1 auto;
        min-width: 0;
        overflow: hidden;
        color: rgba(207, 255, 241, 0.86);
        font-size: 8px;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .crt-canvas-task-strip em {
        flex: 0 0 auto;
        overflow: hidden;
        color: rgba(157, 234, 219, 0.5);
        font-size: 7px;
        font-style: normal;
        text-transform: uppercase;
        white-space: nowrap;
      }

      .crt-canvas-task-rail-toggle {
        flex: 0 0 auto;
        width: 15px;
        min-width: 15px;
        min-height: 19px;
        padding: 0 !important;
        border-color: rgba(112, 225, 211, 0.14);
        background: transparent;
        color: rgba(184, 236, 224, 0.6);
        font-size: 8px;
        line-height: 1;
      }

      .crt-canvas-task-copy {
        min-width: 0;
      }

      .crt-canvas-task-copy small,
      .crt-canvas-task-copy strong,
      .crt-canvas-task-copy em {
        display: block;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .crt-canvas-task-copy small {
        color: rgba(157, 234, 219, 0.52);
        font-size: 7px;
        font-style: normal;
        line-height: 1;
        text-transform: uppercase;
      }

      .crt-canvas-task-copy strong {
        margin: 3px 0;
        color: rgba(211, 255, 244, 0.88);
        font-size: 9px;
        line-height: 1.08;
      }

      .crt-canvas-task-copy em {
        color: rgba(157, 218, 207, 0.46);
        font-size: 7px;
        font-style: normal;
        line-height: 1;
      }

      .crt-canvas-actions,
      .crt-canvas-permission div {
        display: flex;
        align-items: center;
        gap: 7px;
      }

      .crt-canvas-permission span {
        display: block;
        margin-bottom: 4px;
        color: rgba(157, 234, 219, 0.72);
        font-size: 9px;
        line-height: 1;
        text-transform: uppercase;
      }

      .crt-canvas-surface-card button {
        min-height: 27px;
        padding: 0 9px;
        border: 1px solid rgba(110, 231, 215, 0.3);
        border-radius: 999px;
        background: rgba(9, 38, 44, 0.64);
        color: rgba(202, 255, 242, 0.86);
        font: inherit;
        font-size: 10px;
        cursor: pointer;
      }

      .crt-canvas-surface-card button.action-pending {
        cursor: progress;
        opacity: 0.68;
        filter: saturate(0.72);
      }

      .crt-canvas-overlay-controls {
        position: absolute;
        top: 10px;
        right: 10px;
        z-index: 4;
        display: flex;
        align-items: center;
        gap: 6px;
        opacity: 0;
        transition: opacity 140ms ease, transform 140ms ease;
        transform: translateY(-2px);
      }

      .crt-canvas-surface-card:hover .crt-canvas-overlay-controls,
      .crt-canvas-overlay-controls:focus-within,
      .crt-canvas-surface-card:focus-within .crt-canvas-overlay-controls {
        opacity: 1;
        transform: translateY(0);
      }

      .crt-canvas-actions button {
        width: 24px;
        min-height: 24px;
        padding: 0;
        border-color: rgba(127, 245, 224, 0.24);
        background: rgba(4, 24, 29, 0.68);
        font-size: 9px;
      }

      .crt-canvas-pane {
        flex: 1 1 auto;
        min-height: 0;
        overflow: hidden;
        border: 1px solid rgba(105, 230, 214, 0.18);
        border-radius: 13px;
        background:
          linear-gradient(180deg, rgba(8, 33, 39, 0.58), rgba(2, 18, 25, 0.42)),
          repeating-linear-gradient(0deg, rgba(150, 255, 232, 0.035) 0 1px, transparent 1px 6px);
      }

      .crt-canvas-pane.workflow,
      .crt-canvas-pane.browser,
      .crt-canvas-pane.markdown,
      .crt-canvas-pane.image,
      .crt-canvas-pane.table,
      .crt-canvas-pane.code,
      .crt-canvas-pane.diff {
        padding: 11px;
      }

      .crt-canvas-mode-tabs {
        flex: 0 0 auto;
        display: flex;
        align-items: center;
        gap: 5px;
        padding: 0 4px;
      }

      .crt-canvas-mode-tabs button {
        min-height: 23px;
        padding: 0 10px;
        border-color: rgba(112, 225, 211, 0.18);
        background: rgba(3, 23, 29, 0.42);
        color: rgba(164, 220, 210, 0.62);
        font-size: 8px;
        letter-spacing: 0.09em;
        text-transform: uppercase;
      }

      .crt-canvas-mode-tabs button.active {
        border-color: rgba(113, 255, 222, 0.5);
        background: rgba(21, 74, 70, 0.6);
        color: rgba(221, 255, 246, 0.96);
      }

      .crt-canvas-pane p {
        margin: 0 0 9px;
        color: rgba(178, 229, 218, 0.76);
        font-size: 11px;
        line-height: 1.42;
      }

      .crt-canvas-progress {
        height: 4px;
        overflow: hidden;
        border-radius: 999px;
        background: rgba(125, 225, 213, 0.13);
        margin-bottom: 9px;
      }

      .crt-canvas-progress i {
        display: block;
        height: 100%;
        border-radius: inherit;
        background: linear-gradient(90deg, rgba(88, 255, 220, 0.26), rgba(184, 255, 236, 0.94));
      }

      .crt-canvas-pane.browser {
        display: grid;
        grid-template-rows: auto minmax(0, 1fr) auto;
        gap: 9px;
        overflow: hidden;
      }

      .crt-canvas-browser-shot {
        min-height: 112px;
        max-height: 210px;
        overflow: hidden;
        border: 1px solid rgba(119, 245, 223, 0.18);
        border-radius: 11px;
        background:
          radial-gradient(circle at 28% 22%, rgba(103, 244, 221, 0.18), transparent 34%),
          linear-gradient(135deg, rgba(6, 28, 35, 0.94), rgba(12, 18, 32, 0.92));
      }

      .crt-canvas-browser-shot img {
        display: block;
        width: 100%;
        height: 100%;
        object-fit: cover;
        opacity: 0.82;
        filter: saturate(0.86) contrast(1.04);
      }

      .crt-canvas-browser-placeholder {
        display: grid;
        height: 100%;
        min-height: 112px;
        place-items: center;
        color: rgba(184, 255, 238, 0.58);
        font-size: 10px;
        text-transform: uppercase;
      }

      .crt-canvas-browser-copy {
        min-height: 0;
        overflow: hidden;
      }

      .crt-canvas-browser-copy p {
        display: -webkit-box;
        margin-bottom: 6px;
        overflow: hidden;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 4;
      }

      .crt-canvas-browser-source {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
        min-height: 24px;
        overflow: hidden;
      }

      .crt-canvas-browser-session {
        display: inline-flex;
        align-items: center;
        height: 22px;
        padding: 0 8px;
        border: 1px solid rgba(119, 245, 223, 0.17);
        border-radius: 999px;
        color: rgba(178, 229, 218, 0.56);
        background: rgba(4, 22, 28, 0.34);
        font-size: 9px;
      }

      .crt-canvas-signals {
        display: grid;
        gap: 7px;
        min-height: 0;
        overflow: hidden;
      }

      .crt-canvas-signal {
        box-sizing: border-box;
        width: 100%;
        height: 58px;
        min-height: 58px;
        max-height: 58px;
        padding: 7px 8px !important;
        overflow: hidden;
        text-align: left;
        border-radius: 11px !important;
      }

      .crt-canvas-signal span,
      .crt-canvas-signal small {
        display: block;
        color: rgba(126, 227, 212, 0.7);
        font-size: 9px;
        text-transform: uppercase;
      }

      .crt-canvas-signal strong {
        display: -webkit-box;
        margin: 3px 0 0;
        overflow: hidden;
        color: rgba(214, 255, 244, 0.9);
        font-size: 11px;
        line-height: 1.32;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
        overflow-wrap: anywhere;
        word-break: break-word;
      }

      .crt-canvas-signal small {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .crt-canvas-pane.workflow {
        display: flex;
        flex-direction: column;
        gap: 8px;
        overflow-x: hidden;
        overflow-y: auto;
        overscroll-behavior: contain;
      }

      .crt-canvas-pane.workflow > p {
        margin-bottom: 0;
        overflow-wrap: anywhere;
        word-break: break-word;
      }

      .crt-canvas-pane.workflow::-webkit-scrollbar {
        width: 5px;
        height: 5px;
      }

      .crt-canvas-pane.workflow::-webkit-scrollbar-track {
        background: transparent;
        border: 0;
      }

      .crt-canvas-pane.workflow::-webkit-scrollbar-thumb {
        min-height: 28px;
        border-radius: 999px;
        background:
          linear-gradient(180deg, rgba(189, 255, 239, 0.08), rgba(88, 245, 219, 0.18));
        box-shadow: 0 0 7px rgba(88, 245, 219, 0.08);
      }

      .crt-canvas-pane.workflow:hover::-webkit-scrollbar-thumb,
      .crt-canvas-pane.workflow:focus-within::-webkit-scrollbar-thumb {
        background:
          linear-gradient(180deg, rgba(219, 255, 247, 0.26), rgba(88, 245, 219, 0.38));
        box-shadow: 0 0 10px rgba(88, 245, 219, 0.18);
      }

      .crt-canvas-pane.workflow::-webkit-scrollbar-corner,
      .crt-canvas-pane.workflow::-webkit-scrollbar-button {
        width: 0;
        height: 0;
        display: none;
        background: transparent;
      }

      .crt-canvas-pane.markdown {
        display: flex;
        height: 100%;
        min-height: 0;
        max-height: 100%;
        overflow: hidden;
        flex-direction: column;
        color: rgba(212, 255, 244, 0.88);
        font-size: 11px;
        line-height: 1.45;
      }

      .crt-canvas-markdown-body {
        flex: 1 1 auto;
        min-height: 0;
        max-height: 100%;
        overflow-x: hidden;
        overflow-y: auto;
        overscroll-behavior: contain;
        padding-right: 2px;
      }

      .crt-canvas-markdown-body::-webkit-scrollbar,
      .crt-canvas-source-tail-list::-webkit-scrollbar {
        width: 5px;
        height: 5px;
      }

      .crt-canvas-markdown-body::-webkit-scrollbar-track,
      .crt-canvas-source-tail-list::-webkit-scrollbar-track {
        background: transparent;
        border: 0;
      }

      .crt-canvas-markdown-body::-webkit-scrollbar-thumb,
      .crt-canvas-source-tail-list::-webkit-scrollbar-thumb {
        min-height: 28px;
        border-radius: 999px;
        background:
          linear-gradient(180deg, rgba(189, 255, 239, 0.08), rgba(88, 245, 219, 0.18));
        box-shadow: 0 0 7px rgba(88, 245, 219, 0.08);
      }

      .crt-canvas-pane.markdown:hover .crt-canvas-markdown-body::-webkit-scrollbar-thumb,
      .crt-canvas-pane.markdown:focus-within .crt-canvas-markdown-body::-webkit-scrollbar-thumb,
      .crt-canvas-source-tail-list:hover::-webkit-scrollbar-thumb,
      .crt-canvas-source-tail-list:focus-within::-webkit-scrollbar-thumb {
        background:
          linear-gradient(180deg, rgba(219, 255, 247, 0.26), rgba(88, 245, 219, 0.38));
        box-shadow: 0 0 10px rgba(88, 245, 219, 0.18);
      }

      .crt-canvas-markdown-body::-webkit-scrollbar-corner,
      .crt-canvas-markdown-body::-webkit-scrollbar-button,
      .crt-canvas-source-tail-list::-webkit-scrollbar-corner,
      .crt-canvas-source-tail-list::-webkit-scrollbar-button {
        width: 0;
        height: 0;
        display: none;
        background: transparent;
      }

      .crt-canvas-pane.markdown h1,
      .crt-canvas-pane.markdown h2,
      .crt-canvas-pane.markdown h3,
      .crt-canvas-pane.markdown h4 {
        margin: 9px 0 5px;
        color: rgba(225, 255, 248, 0.98);
        font-weight: 680;
        line-height: 1.25;
        overflow-wrap: anywhere;
        word-break: break-word;
      }

      .crt-canvas-pane.markdown h1:first-child,
      .crt-canvas-pane.markdown h2:first-child,
      .crt-canvas-pane.markdown h3:first-child,
      .crt-canvas-pane.markdown h4:first-child {
        margin-top: 0;
      }

      .crt-canvas-pane.markdown h1,
      .crt-canvas-pane.markdown h2 {
        font-size: 12px;
      }

      .crt-canvas-pane.markdown h3 {
        font-size: 11.2px;
      }

      .crt-canvas-pane.markdown h4 {
        color: rgba(197, 246, 235, 0.9);
        font-size: 10.7px;
      }

      .crt-canvas-pane.markdown p,
      .crt-canvas-pane.markdown li {
        color: rgba(204, 248, 238, 0.82);
        font-size: 10.2px;
        line-height: 1.48;
        overflow-wrap: anywhere;
        word-break: break-word;
      }

      .crt-canvas-pane.markdown p {
        margin: 0 0 7px;
      }

      .crt-canvas-pane.markdown strong {
        color: rgba(225, 255, 248, 0.98);
        font-weight: 660;
      }

      .crt-canvas-pane.markdown code {
        display: inline-block;
        margin: 2px 0 8px;
        padding: 2px 5px;
        border-radius: 6px;
        background: rgba(2, 16, 22, 0.54);
        color: rgba(172, 255, 232, 0.94);
      }

      .crt-canvas-pane.markdown hr {
        height: 1px;
        margin: 10px 0;
        border: 0;
        background: linear-gradient(90deg, transparent, rgba(118, 244, 222, 0.24), transparent);
      }

      .crt-canvas-md-table-wrap {
        max-width: 100%;
        margin: 8px 0 10px;
        overflow-x: auto;
        overscroll-behavior: contain;
        border: 1px solid rgba(105, 230, 214, 0.16);
        border-radius: 10px;
        background: rgba(2, 16, 22, 0.34);
      }

      .crt-canvas-md-table-wrap::-webkit-scrollbar {
        width: 5px;
        height: 5px;
      }

      .crt-canvas-md-table-wrap::-webkit-scrollbar-track {
        background: transparent;
        border: 0;
      }

      .crt-canvas-md-table-wrap::-webkit-scrollbar-thumb {
        border-radius: 999px;
        background:
          linear-gradient(90deg, rgba(189, 255, 239, 0.08), rgba(88, 245, 219, 0.18));
      }

      .crt-canvas-md-table-wrap table {
        width: 100%;
        min-width: 360px;
        border-collapse: collapse;
        table-layout: fixed;
        color: rgba(204, 248, 238, 0.82);
        font-size: 9.6px;
        line-height: 1.38;
      }

      .crt-canvas-md-table-wrap th,
      .crt-canvas-md-table-wrap td {
        padding: 5px 6px;
        border-right: 1px solid rgba(105, 230, 214, 0.12);
        border-bottom: 1px solid rgba(105, 230, 214, 0.1);
        text-align: left;
        vertical-align: top;
        white-space: normal;
        overflow-wrap: anywhere;
        word-break: break-word;
      }

      .crt-canvas-md-table-wrap th {
        color: rgba(225, 255, 248, 0.96);
        background: rgba(36, 118, 112, 0.12);
      }

      .crt-canvas-md-table-wrap tr:last-child td {
        border-bottom: 0;
      }

      .crt-canvas-md-table-wrap th:last-child,
      .crt-canvas-md-table-wrap td:last-child {
        border-right: 0;
      }

      .crt-canvas-source-tail {
        flex: 0 0 auto;
        display: grid;
        gap: 5px;
        max-height: 104px;
        margin-top: 7px;
        padding-top: 8px;
        border-top: 1px solid rgba(105, 230, 214, 0.13);
        background:
          linear-gradient(180deg, rgba(4, 22, 28, 0.12), rgba(4, 22, 28, 0.74)),
          repeating-linear-gradient(0deg, rgba(148, 255, 230, 0.025) 0 1px, transparent 1px 5px);
      }

      .crt-canvas-source-tail > span:first-child {
        color: rgba(157, 234, 219, 0.58);
        font-size: 8px;
        line-height: 1;
        text-transform: uppercase;
      }

      .crt-canvas-source-tail-list {
        display: flex;
        flex-wrap: wrap;
        gap: 3px;
        max-height: 73px;
        min-height: 0;
        overflow-x: hidden;
        overflow-y: auto;
        overscroll-behavior: contain;
        padding-right: 1px;
      }

      .crt-canvas-file-ref,
      .crt-canvas-web-ref,
      .crt-canvas-provider-ref {
        position: relative;
        display: inline-flex;
        max-width: 100%;
        margin: 2px 2px 3px 0;
        vertical-align: middle;
        filter: drop-shadow(0 0 7px rgba(86, 250, 221, 0.08));
      }

      .crt-canvas-file-ref,
      .crt-canvas-web-ref,
      .crt-canvas-provider-ref,
      .crt-canvas-command-ref {
        flex-wrap: wrap;
        pointer-events: auto;
      }

      .crt-canvas-file-ref button,
      .crt-canvas-web-ref button,
      .crt-canvas-web-ref a,
      .crt-canvas-provider-ref button {
        appearance: none;
        min-height: 22px;
        border: 1px solid rgba(109, 239, 217, 0.2);
        background: rgba(3, 22, 28, 0.64);
        color: rgba(214, 255, 246, 0.92);
        font: inherit;
        font-size: 10px;
        line-height: 1;
        cursor: pointer;
        text-decoration: none;
      }

      .crt-canvas-file-main,
      .crt-canvas-web-main,
      .crt-canvas-provider-main {
        max-width: min(100%, 250px);
        overflow: hidden;
        padding: 4px 7px;
        border-radius: 7px 0 0 7px;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .crt-canvas-web-main {
        max-width: min(100%, 280px);
      }

      .crt-canvas-provider-main {
        max-width: min(100%, 230px);
        border-radius: 7px;
        background: rgba(17, 29, 45, 0.68);
      }

      .crt-canvas-file-toggle,
      .crt-canvas-web-toggle {
        width: 22px;
        margin-left: -1px;
        padding: 0;
        border-radius: 0 7px 7px 0;
        color: rgba(160, 246, 226, 0.72);
      }

      .crt-canvas-file-ref button:hover,
      .crt-canvas-file-ref.menu-open button,
      .crt-canvas-file-ref.action-ok .crt-canvas-file-main,
      .crt-canvas-web-ref a:hover,
      .crt-canvas-web-ref a:focus-visible,
      .crt-canvas-web-ref button:hover,
      .crt-canvas-web-ref.menu-open button,
      .crt-canvas-web-ref.action-ok .crt-canvas-web-main,
      .crt-canvas-provider-ref button:hover,
      .crt-canvas-provider-ref.action-ok .crt-canvas-provider-main {
        border-color: rgba(168, 255, 238, 0.44);
        background: rgba(9, 47, 52, 0.74);
        color: rgba(232, 255, 249, 0.98);
      }

      .crt-canvas-file-ref.action-error .crt-canvas-file-main,
      .crt-canvas-web-ref.action-error .crt-canvas-web-main,
      .crt-canvas-provider-ref.action-error .crt-canvas-provider-main {
        border-color: rgba(255, 171, 153, 0.42);
        background: rgba(64, 18, 18, 0.48);
      }

      .crt-canvas-file-menu,
      .crt-canvas-web-menu {
        position: static;
        z-index: 12;
        display: none;
        flex: 1 0 100%;
        min-width: 104px;
        margin-top: 4px;
        padding: 5px;
        border: 1px solid rgba(104, 239, 219, 0.2);
        border-radius: 9px;
        background:
          linear-gradient(180deg, rgba(6, 32, 38, 0.96), rgba(2, 16, 22, 0.96)),
          repeating-linear-gradient(0deg, rgba(150, 255, 232, 0.035) 0 1px, transparent 1px 6px);
        box-shadow: 0 10px 22px rgba(0, 0, 0, 0.34), 0 0 18px rgba(87, 248, 221, 0.1);
      }

      .crt-canvas-web-menu {
        min-width: 112px;
        pointer-events: auto;
      }

      .crt-canvas-file-ref.menu-open .crt-canvas-file-menu,
      .crt-canvas-web-ref.menu-open .crt-canvas-web-menu {
        display: grid;
        gap: 4px;
      }

      .crt-canvas-file-menu button,
      .crt-canvas-web-menu button {
        width: 100%;
        min-height: 21px;
        border-radius: 6px;
        padding: 0 6px;
        text-align: left;
        white-space: nowrap;
      }

      .crt-canvas-command-block {
        display: grid;
        gap: 6px;
        margin: 5px 0 9px;
        padding: 8px;
        border: 1px solid rgba(104, 239, 219, 0.16);
        border-radius: 10px;
        background: rgba(2, 15, 22, 0.42);
      }

      .crt-canvas-command-block pre,
      .crt-canvas-code-block {
        max-height: 118px;
        margin: 0;
        overflow: auto;
        color: rgba(174, 225, 216, 0.72);
        font: inherit;
        font-size: 10px;
        line-height: 1.44;
        white-space: pre-wrap;
      }

      .crt-canvas-command-ref {
        position: relative;
        display: inline-flex;
        max-width: 100%;
        vertical-align: middle;
      }

      .crt-canvas-command-ref button {
        appearance: none;
        min-height: 22px;
        border: 1px solid rgba(190, 177, 255, 0.2);
        background: rgba(16, 17, 40, 0.64);
        color: rgba(221, 234, 255, 0.92);
        font: inherit;
        font-size: 10px;
        line-height: 1;
        cursor: pointer;
      }

      .crt-canvas-command-main {
        max-width: min(100%, 270px);
        overflow: hidden;
        padding: 4px 7px;
        border-radius: 7px 0 0 7px;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .crt-canvas-command-toggle {
        width: 22px;
        margin-left: -1px;
        padding: 0;
        border-radius: 0 7px 7px 0;
      }

      .crt-canvas-command-ref button:hover,
      .crt-canvas-command-ref.menu-open button,
      .crt-canvas-command-ref.action-ok .crt-canvas-command-main {
        border-color: rgba(218, 207, 255, 0.46);
        background: rgba(31, 28, 73, 0.78);
        color: rgba(240, 245, 255, 0.98);
      }

      .crt-canvas-command-ref.action-error .crt-canvas-command-main {
        border-color: rgba(255, 171, 153, 0.42);
        background: rgba(64, 18, 18, 0.48);
      }

      .crt-canvas-command-menu {
        position: static;
        z-index: 12;
        display: none;
        flex: 1 0 100%;
        min-width: 124px;
        margin-top: 4px;
        padding: 5px;
        border: 1px solid rgba(190, 177, 255, 0.2);
        border-radius: 9px;
        background:
          linear-gradient(180deg, rgba(12, 19, 42, 0.96), rgba(4, 13, 24, 0.96)),
          repeating-linear-gradient(0deg, rgba(200, 220, 255, 0.03) 0 1px, transparent 1px 6px);
        box-shadow: 0 10px 22px rgba(0, 0, 0, 0.34), 0 0 18px rgba(154, 132, 255, 0.1);
      }

      .crt-canvas-command-ref.menu-open .crt-canvas-command-menu {
        display: grid;
        gap: 4px;
      }

      .crt-canvas-command-menu button {
        width: 100%;
        min-height: 21px;
        border-radius: 6px;
        padding: 0 6px;
        text-align: left;
        white-space: nowrap;
      }

      .crt-canvas-generated-file {
        display: inline-flex;
        margin: 3px 0 0 6px;
      }

      .crt-canvas-pane.markdown ul {
        margin: 6px 0 0 16px;
        padding: 0;
      }

      .crt-canvas-metric-grid {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 8px;
        margin-bottom: 10px;
      }

      .crt-canvas-metric {
        min-height: 68px;
        padding: 10px;
        border: 1px solid rgba(116, 237, 218, 0.2);
        border-radius: 12px;
        background: rgba(3, 24, 30, 0.54);
      }

      .crt-canvas-metric strong {
        display: block;
        margin-bottom: 6px;
        color: rgba(219, 255, 246, 0.95);
        font-size: 18px;
      }

      .crt-canvas-metric span,
      .crt-canvas-row span,
      .crt-canvas-code-head span {
        color: rgba(168, 231, 219, 0.68);
        font-size: 9px;
        text-transform: uppercase;
      }

      .crt-canvas-rows {
        display: grid;
        gap: 6px;
      }

      .crt-canvas-row {
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 8px;
        align-items: center;
        min-height: 34px;
        padding: 8px 9px;
        border: 1px solid rgba(116, 237, 218, 0.14);
        border-radius: 10px;
        background: rgba(2, 18, 24, 0.46);
      }

      .crt-canvas-row strong {
        overflow: hidden;
        color: rgba(213, 255, 244, 0.9);
        font-size: 11px;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .crt-canvas-code-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 8px;
      }

      .crt-canvas-code-list {
        display: grid;
        gap: 3px;
        font-size: 10px;
      }

      .crt-canvas-code-line {
        display: grid;
        grid-template-columns: 32px 16px 1fr;
        gap: 6px;
        align-items: baseline;
        min-height: 20px;
        color: rgba(177, 215, 207, 0.62);
      }

      .crt-canvas-code-line.add {
        color: rgba(177, 255, 224, 0.92);
      }

      .crt-canvas-code-line.remove {
        color: rgba(255, 178, 190, 0.72);
        text-decoration: line-through;
      }

      .crt-canvas-code-line b {
        font-weight: 500;
      }

      .crt-canvas-pane.diff {
        display: grid;
        grid-template-columns: minmax(108px, 0.28fr) minmax(0, 1fr);
        gap: 8px;
        overflow: hidden;
      }

      .crt-canvas-diff-files,
      .crt-canvas-diff-content {
        min-width: 0;
        min-height: 0;
        overflow: auto;
      }

      .crt-canvas-diff-files {
        display: flex;
        flex-direction: column;
        gap: 5px;
        padding-right: 5px;
        border-right: 1px solid rgba(105, 230, 214, 0.12);
      }

      .crt-canvas-diff-summary {
        display: grid;
        gap: 3px;
        padding: 5px 6px 7px;
        color: rgba(169, 226, 215, 0.7);
        font-size: 8px;
        line-height: 1.3;
        text-transform: uppercase;
      }

      .crt-canvas-diff-summary strong {
        color: rgba(214, 255, 244, 0.94);
        font-size: 10px;
      }

      .crt-canvas-diff-file {
        width: 100% !important;
        min-height: 42px !important;
        display: grid;
        grid-template-columns: minmax(0, 1fr);
        gap: 2px;
        align-content: center;
        padding: 5px 7px !important;
        border-radius: 8px !important;
        text-align: left;
      }

      .crt-canvas-diff-file.active {
        border-color: rgba(111, 255, 222, 0.52);
        background: rgba(24, 76, 72, 0.62);
      }

      .crt-canvas-diff-file span {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .crt-canvas-diff-file small {
        color: rgba(168, 235, 218, 0.68);
        font-size: 7px;
        white-space: nowrap;
      }

      .crt-canvas-diff-file small b {
        color: rgba(143, 255, 200, 0.88);
        font-weight: 500;
      }

      .crt-canvas-diff-file small i {
        color: rgba(255, 158, 178, 0.88);
        font-style: normal;
      }

      .crt-canvas-diff-content {
        font-family: "Cascadia Mono", "SFMono-Regular", Consolas, monospace;
      }

      .crt-canvas-diff-file-head {
        position: sticky;
        top: 0;
        z-index: 2;
        display: flex;
        justify-content: space-between;
        gap: 8px;
        padding: 3px 5px 7px;
        background: rgba(5, 25, 31, 0.96);
        color: rgba(211, 255, 244, 0.92);
        font-size: 9px;
      }

      .crt-canvas-diff-hunk {
        margin-bottom: 7px;
        overflow: hidden;
        border: 1px solid rgba(104, 220, 207, 0.12);
        border-radius: 8px;
      }

      .crt-canvas-diff-hunk > header {
        padding: 5px 7px;
        background: rgba(55, 103, 117, 0.22);
        color: rgba(155, 221, 228, 0.76);
        font-size: 8px;
        white-space: pre-wrap;
      }

      .crt-canvas-diff-line {
        display: grid;
        grid-template-columns: 28px 28px 12px minmax(0, 1fr);
        min-height: 19px;
        align-items: stretch;
        color: rgba(194, 225, 218, 0.74);
        font-size: 9px;
        line-height: 1.45;
      }

      .crt-canvas-diff-line > span {
        padding: 3px 4px;
        border-right: 1px solid rgba(104, 220, 207, 0.08);
        color: rgba(139, 185, 178, 0.48);
        text-align: right;
        user-select: none;
      }

      .crt-canvas-diff-line > i {
        padding: 3px 2px;
        font-style: normal;
        text-align: center;
        user-select: none;
      }

      .crt-canvas-diff-line > code {
        display: block;
        min-width: max-content;
        padding: 3px 7px;
        color: inherit;
        font: inherit;
        white-space: pre;
      }

      .crt-canvas-diff-line.add {
        background: rgba(37, 151, 102, 0.15);
        color: rgba(183, 255, 218, 0.94);
      }

      .crt-canvas-diff-line.add > i {
        color: rgba(101, 255, 173, 0.92);
      }

      .crt-canvas-diff-line.remove {
        background: rgba(174, 55, 78, 0.16);
        color: rgba(255, 192, 203, 0.9);
      }

      .crt-canvas-diff-line.remove > i {
        color: rgba(255, 126, 153, 0.92);
      }

      .crt-canvas-diff-line.remove > code {
        text-decoration: line-through;
        text-decoration-color: rgba(255, 121, 148, 0.42);
      }

      .crt-canvas-diff-line.meta {
        grid-template-columns: 1fr;
        padding: 4px 7px;
        color: rgba(151, 202, 194, 0.58);
        font-style: italic;
      }

      .crt-canvas-diff-empty {
        grid-column: 1 / -1;
        display: grid;
        place-content: center;
        min-height: 100%;
        padding: 24px;
        text-align: center;
      }

      .crt-canvas-diff-empty strong {
        margin-bottom: 7px;
        color: rgba(213, 255, 244, 0.95);
        font-size: 13px;
      }

      .crt-canvas-diff-empty p {
        margin: 0;
      }

      @media (max-width: 520px) {
        .crt-canvas-task-item {
          flex-basis: min(180px, 78%);
        }

        .crt-canvas-task-counts {
          max-width: 58%;
        }

        .crt-canvas-pane.diff {
          grid-template-columns: 1fr;
          grid-template-rows: auto minmax(0, 1fr);
        }

        .crt-canvas-diff-files {
          max-height: 92px;
          flex-direction: row;
          border-right: 0;
          border-bottom: 1px solid rgba(105, 230, 214, 0.12);
        }

        .crt-canvas-diff-file {
          min-width: 128px;
        }
      }

      .crt-canvas-pane.html iframe {
        width: 100%;
        height: 100%;
        min-height: 100%;
        border: 0;
        border-radius: 12px;
      }

      .crt-canvas-image {
        min-height: 170px;
        border-radius: 12px;
        padding: 18px;
        display: flex;
        flex-direction: column;
        justify-content: flex-end;
        background:
          radial-gradient(circle at 28% 28%, rgba(170, 255, 231, 0.32), transparent 23%),
          radial-gradient(circle at 72% 34%, rgba(180, 135, 255, 0.24), transparent 26%),
          linear-gradient(135deg, rgba(8, 38, 43, 0.96), rgba(22, 14, 38, 0.94));
        box-shadow: inset 0 0 34px rgba(94, 255, 222, 0.08);
      }

      .crt-canvas-image strong {
        color: rgba(228, 255, 249, 0.98);
        font-size: 15px;
      }

      .crt-canvas-image span {
        color: rgba(204, 247, 238, 0.68);
        font-size: 10px;
      }

      .crt-canvas-permission {
        position: absolute;
        left: 12px;
        right: 12px;
        bottom: 12px;
        z-index: 2;
        padding: 11px;
        border: 1px solid rgba(192, 145, 255, 0.44);
        border-radius: 14px;
        background:
          linear-gradient(135deg, rgba(34, 18, 52, 0.94), rgba(5, 24, 30, 0.9)),
          repeating-linear-gradient(0deg, rgba(219, 190, 255, 0.065) 0 1px, transparent 1px 5px);
        box-shadow: 0 -10px 36px rgba(6, 0, 16, 0.34), 0 0 38px rgba(178, 113, 255, 0.16);
      }

      .crt-canvas-permission strong {
        display: block;
        color: rgba(231, 219, 255, 0.92);
        font-size: 12px;
      }

      .crt-canvas-permission p {
        margin: 6px 0 9px;
        color: rgba(211, 197, 237, 0.72);
        font-size: 10px;
      }

      .crt-canvas-permission ul {
        max-height: 74px;
        margin: 6px 0 9px;
        padding-left: 16px;
        overflow: auto;
        color: rgba(211, 197, 237, 0.82);
        font-size: 10px;
      }

      .crt-canvas-permission code {
        overflow-wrap: anywhere;
      }

      .crt-canvas-permission .crt-canvas-permission-error {
        color: rgba(255, 142, 153, 0.96);
      }

      .crt-canvas-attention {
        position: absolute;
        inset: 52px 12px 12px;
        z-index: 6;
        display: flex;
        flex-direction: column;
        min-height: 0;
        padding: 14px;
        border: 1px solid rgba(105, 239, 220, 0.5);
        border-radius: 14px;
        background:
          linear-gradient(145deg, rgba(5, 34, 39, 0.98), rgba(17, 15, 39, 0.97)),
          repeating-linear-gradient(0deg, rgba(148, 255, 235, 0.055) 0 1px, transparent 1px 5px);
        box-shadow: 0 16px 44px rgba(0, 8, 15, 0.52), 0 0 40px rgba(87, 232, 211, 0.16);
      }

      .crt-canvas-attention > span {
        color: rgba(138, 245, 224, 0.74);
        font-size: 9px;
        letter-spacing: 0.12em;
        text-transform: uppercase;
      }

      .crt-canvas-attention h3 {
        margin: 7px 0 5px;
        color: rgba(225, 255, 248, 0.96);
        font-size: 14px;
      }

      .crt-canvas-attention > p {
        margin: 0 0 11px;
        color: rgba(196, 235, 227, 0.72);
        font-size: 10px;
        line-height: 1.45;
      }

      .crt-canvas-attention-options {
        display: grid;
        min-height: 0;
        gap: 8px;
        overflow: auto;
      }

      .crt-canvas-attention-option {
        display: grid;
        grid-template-columns: minmax(68px, auto) 1fr;
        gap: 3px 10px;
        width: 100%;
        min-height: 56px !important;
        padding: 9px 11px !important;
        border-radius: 11px !important;
        text-align: left;
      }

      .crt-canvas-attention-option span {
        grid-row: 1 / span 2;
        align-self: center;
        color: rgba(125, 237, 216, 0.6);
        font-size: 8px;
        text-transform: uppercase;
      }

      .crt-canvas-attention-option strong {
        overflow: hidden;
        color: rgba(224, 255, 248, 0.94);
        font-size: 11px;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .crt-canvas-attention-option small {
        color: rgba(183, 225, 216, 0.58);
        font-size: 9px;
        line-height: 1.3;
      }

      .crt-canvas-attention .crt-canvas-attention-error {
        color: rgba(255, 142, 153, 0.96);
      }

      /* Electron/Chromium is the Slice host. Keep scrollbar geometry under
         one WebKit rule so hover and active scrolling cannot switch between
         the native Windows rail and a second standards-based width. */
      .crt-canvas-surface *:not(.crt-canvas-task-filters)::-webkit-scrollbar {
        width: 3px;
        height: 3px;
      }

      .crt-canvas-surface *:not(.crt-canvas-task-filters)::-webkit-scrollbar-track {
        border: 0;
        border-radius: 999px;
        background: transparent;
      }

      .crt-canvas-surface *:not(.crt-canvas-task-filters)::-webkit-scrollbar-thumb {
        min-height: 24px;
        border: 0;
        border-radius: 999px;
        background: rgba(122, 238, 218, 0.09);
        box-shadow: none;
      }

      .crt-canvas-surface *:not(.crt-canvas-task-filters):hover::-webkit-scrollbar-thumb,
      .crt-canvas-surface *:not(.crt-canvas-task-filters):focus-within::-webkit-scrollbar-thumb {
        background: rgba(151, 247, 227, 0.24);
      }

      .crt-canvas-surface *:not(.crt-canvas-task-filters)::-webkit-scrollbar-corner,
      .crt-canvas-surface *:not(.crt-canvas-task-filters)::-webkit-scrollbar-button {
        display: none;
        width: 0;
        height: 0;
        border: 0;
        background: transparent;
      }

    `;
    document.head.appendChild(style);
  }

  function createCrtCanvasSurface() {
    injectStyle();

    const presentationText = {
      "en-US": {
        expand: "Expand canvas", fold: "Fold canvas", ready: "Ready",
        waitingTitle: "Waiting for work signal", waitingLead: "Waiting for an artifact or work signal.",
        work: "Work", web: "Web", doc: "Doc", diff: "Diff", image: "Image", table: "Table", code: "Code",
        report: "Report", current: "Current", history: "History", projects: "Projects",
        accept: "Accept", archive: "Archive", reopen: "Reopen", keepProject: "Keep as project",
        permission: "Permission", recovery: "Recovery", providerBlocked: "Provider blocked",
        choice: "Choice", selectionFailed: "Selection could not be applied",
        allowOnce: "Allow once", deny: "Deny", dismiss: "Dismiss",
      },
      "zh-CN": {
        expand: "展开面板", fold: "收起面板", ready: "就绪",
        waitingTitle: "等待工作信号", waitingLead: "正在等待产物或工作信号。",
        work: "工作", web: "网页", doc: "文档", diff: "差异", image: "图像", table: "表格", code: "代码",
        report: "报告", current: "当前", history: "历史", projects: "项目",
        accept: "接受", archive: "归档", reopen: "重新打开", keepProject: "保留为项目",
        permission: "权限", recovery: "恢复", providerBlocked: "执行受阻",
        choice: "请选择", selectionFailed: "无法应用这次选择",
        allowOnce: "仅允许一次", deny: "拒绝", dismiss: "关闭",
      },
      "ja-JP": {
        expand: "パネルを開く", fold: "パネルを閉じる", ready: "待機",
        waitingTitle: "作業シグナルを待機中", waitingLead: "成果物または作業シグナルを待っている。",
        work: "作業", web: "ウェブ", doc: "文書", diff: "差分", image: "画像", table: "表", code: "コード",
        report: "報告", current: "現在", history: "履歴", projects: "プロジェクト",
        accept: "受け入れる", archive: "アーカイブ", reopen: "再開", keepProject: "プロジェクトとして保存",
        permission: "権限", recovery: "復旧", providerBlocked: "実行ブロック",
        choice: "選択", selectionFailed: "選択を適用できませんでした",
        allowOnce: "今回のみ許可", deny: "拒否", dismiss: "閉じる",
      },
    };

    function normalizePresentationLocale(value) {
      const raw = String(value || "").trim().toLowerCase().replace(/_/g, "-");
      if (["ja", "jp", "ja-jp", "japanese"].includes(raw)) return "ja-JP";
      if (["zh", "zh-cn", "chinese", "simplified-chinese"].includes(raw)) return "zh-CN";
      return "en-US";
    }

    const host = document.getElementById("canvas-container") || document.body;
    const root = document.createElement("div");
    root.className = "crt-canvas-surface";
    root.setAttribute("aria-label", "CRT canvas surface");

    const dot = document.createElement("button");
    dot.type = "button";
    dot.className = "crt-canvas-surface-dot";
    dot.setAttribute("aria-label", "Expand canvas");
    dot.innerHTML = "<span></span>";

    const status = document.createElement("button");
    status.type = "button";
    status.className = "crt-canvas-surface-status";
    status.setAttribute("aria-label", "Canvas status");

    const card = document.createElement("section");
    card.className = "crt-canvas-surface-card";

    const state = {
      expanded: false,
      hasContent: false,
      mode: "workflow",
      phase: "Ready",
      startedAt: Date.now(),
      title: "Waiting for work signal",
      lead: "Waiting for an artifact or work signal.",
      progress: 18,
      markdown: "",
      diff: null,
      activeDiffFile: 0,
      reportView: null,
      diffView: null,
      html: "",
      url: "",
      browserSessionId: "",
      pageTitle: "",
      excerpt: "",
      screenshot: "",
      links: [],
      actions: [],
      workContext: null,
      taskDock: null,
      taskFilter: "current",
      taskRailExpanded: false,
      workExecutionSubmitting: false,
      workPreviewSubmitting: false,
      workDispositionSubmitting: false,
      workDispositionMenu: "",
      workActionError: "",
      permissionVisible: false,
      permissionRequest: null,
      permissionSubmitting: false,
      permissionError: "",
      attentionRequest: null,
      attentionSubmitting: false,
      attentionError: "",
      attentionPresentedId: "",
      presentationLocale: "en-US",
      presentationKey: "",
      preset: loadSavedPreset(),
      size: loadSavedSize(),
      demoEnabled: demoEnabledByUrl(),
      lastDemoAt: -1,
      signals: [
        { label: "idle", text: "No canvas artifact is active.", detail: "manual open" },
      ],
    };
    let resizeObserver = null;
    let lifecycleTimer = null;
    let fileBridgePortPromise = null;
    let renderedCardHtml = "";

    function ui(key) {
      const catalog = presentationText[state.presentationLocale] || presentationText["en-US"];
      return catalog[key] || presentationText["en-US"][key] || key;
    }

    function closeFileMenus(except) {
      card.querySelectorAll(".crt-canvas-file-ref.menu-open").forEach((ref) => {
        if (ref !== except) ref.classList.remove("menu-open");
      });
      closeWebMenus();
    }

    function closeCommandMenus(except) {
      card.querySelectorAll(".crt-canvas-command-ref.menu-open").forEach((ref) => {
        if (ref !== except) ref.classList.remove("menu-open");
      });
      closeWebMenus();
    }

    function closeWebMenus(except) {
      card.querySelectorAll(".crt-canvas-web-ref.menu-open").forEach((ref) => {
        if (ref !== except) ref.classList.remove("menu-open");
      });
    }

    function markFileAction(ref, className) {
      if (!ref) return;
      ref.classList.remove("action-ok", "action-error");
      ref.classList.add(className);
      window.setTimeout(() => ref.classList.remove(className), 900);
    }

    function markActionPending(button, pending) {
      if (!(button instanceof HTMLElement)) return;
      button.classList.toggle("action-pending", !!pending);
      if (pending) {
        button.setAttribute("aria-busy", "true");
        button.setAttribute("disabled", "disabled");
      } else {
        button.removeAttribute("aria-busy");
        button.removeAttribute("disabled");
      }
    }

    async function resolveFileBridgePort(forceRefresh) {
      if (forceRefresh) {
        fileBridgePortPromise = null;
        window.__amadeusBridgePort = "";
        window.__amadeusBridgeToken = "";
      }
      if (!forceRefresh && window.__amadeusBridgePort && window.__amadeusBridgeToken) return String(window.__amadeusBridgePort);
      if (!forceRefresh && fileBridgePortPromise) return fileBridgePortPromise;
      fileBridgePortPromise = fetch(window.location.origin + "/wallpaper/bridge-info", { cache: "no-store" })
        .then((res) => res.json())
        .then(async (info) => {
          if (info && Number.isFinite(Number(info.bridgePort))) {
            window.__amadeusBridgePort = String(info.bridgePort);
            if (info.bridgeToken) window.__amadeusBridgeToken = String(info.bridgeToken);
            if (!window.__amadeusBridgeToken && Number.isFinite(Number(info.assetPort))) {
              try {
                const assetOrigin = "http://127.0.0.1:" + String(info.assetPort);
                if (assetOrigin !== window.location.origin) {
                  const assetInfo = await fetch(assetOrigin + "/wallpaper/bridge-info", { cache: "no-store" }).then((res) => res.json());
                  if (assetInfo && assetInfo.bridgeToken) window.__amadeusBridgeToken = String(assetInfo.bridgeToken);
                  if (assetInfo && Number.isFinite(Number(assetInfo.bridgePort))) {
                    window.__amadeusBridgePort = String(assetInfo.bridgePort);
                  }
                }
              } catch (err) {
                if (typeof window.__weBridgeLog === "function") {
                  window.__weBridgeLog("canvas.bridge_token_fallback_failed", {
                    error: String(err && (err.stack || err)),
                  }, "warning");
                }
              }
            }
            return window.__amadeusBridgePort;
          }
          const params = new URLSearchParams(window.location.search || "");
          return params.get("bridgePort") || "17797";
        })
        .catch(() => {
          const params = new URLSearchParams(window.location.search || "");
          return params.get("bridgePort") || "17797";
        });
      return fileBridgePortPromise;
    }

    function copyText(text) {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        return navigator.clipboard.writeText(text);
      }
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "readonly");
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      document.body.appendChild(textarea);
      textarea.select();
      try {
        document.execCommand("copy");
      } finally {
        document.body.removeChild(textarea);
      }
      return Promise.resolve();
    }

    async function postCanvasAction(target, action, payload) {
      const body = JSON.stringify(Object.assign({ target, action }, payload || {}));
      const send = async (forceRefresh) => {
        const port = await resolveFileBridgePort(forceRefresh);
        const token = String(window.__amadeusBridgeToken || "");
        if (!token) throw new Error("canvas bridge token unavailable");
        const headers = {
          "Content-Type": "application/json",
          "X-Amadeus-Bridge-Token": token,
        };
        return fetch("http://127.0.0.1:" + port + "/wallpaper/canvas-action", {
          method: "POST",
          headers,
          body,
        });
      };
      let res = await send(false);
      if (res.status === 401 || res.status === 403) {
        if (typeof window.__weBridgeLog === "function") {
          window.__weBridgeLog("canvas.action_auth_retry", { target, action }, "warning");
        }
        res = await send(true);
      }
      const responsePayload = await res.json().catch(() => ({}));
      applyWorkProjectionResponse(responsePayload);
      if (!res.ok || (responsePayload && responsePayload.ok === false)) {
        const code = String(responsePayload && responsePayload.error || "canvas_action_failed");
        const message = code === "canvas_action_failed"
          ? "Canvas action failed (HTTP " + Number(res.status || 0) + ")."
          : code;
        const error = new Error(message);
        error.code = code;
        error.status = Number(res.status || 0);
        error.payload = responsePayload;
        throw error;
      }
      return responsePayload || {};
    }

    async function runFileAction(action, path) {
      if (action === "copy") {
        await copyText(path);
        return {};
      }
      return postCanvasAction("file", action, { path });
    }

    async function handleFileAction(button) {
      const action = button.getAttribute("data-file-action");
      const path = button.getAttribute("data-file-path") || "";
      const ref = button.closest(".crt-canvas-file-ref");
      if (action === "menu") {
        const open = !ref.classList.contains("menu-open");
        closeFileMenus(ref);
        ref.classList.toggle("menu-open", open);
        return;
      }
      closeFileMenus();
      try {
        await runFileAction(action, path);
        markFileAction(ref, "action-ok");
      } catch (err) {
        markFileAction(ref, "action-error");
        if (typeof window.__weBridgeLog === "function") {
          window.__weBridgeLog("canvas.file_action_failed", {
            action,
            path,
            error: String(err && (err.stack || err)),
          }, "warning");
        }
      }
    }

    function attachGeneratedFileChip(ref, path) {
      let slot = ref.nextElementSibling;
      if (!slot || !slot.classList || !slot.classList.contains("crt-canvas-generated-file")) {
        slot = document.createElement("span");
        slot.className = "crt-canvas-generated-file";
        ref.insertAdjacentElement("afterend", slot);
      }
      slot.innerHTML = fileRefHtml(path);
    }

    async function runWebAction(action, url) {
      if (action === "copy") {
        await copyText(url);
        return {};
      }
      return postCanvasAction("url", action, { url });
    }

    async function runBrowserAction(action, options) {
      const opts = options || {};
      return postCanvasAction("browser", action, {
        url: opts.url || "",
        text: opts.text || "",
        label: opts.label || "",
        browserSessionId: opts.browserSessionId || "",
      });
    }

    async function handleWebAction(button) {
      const action = button.getAttribute("data-web-action");
      const url = button.getAttribute("data-web-url") || "";
      const browserSessionId = button.getAttribute("data-browser-session-id") || state.browserSessionId || "";
      const ref = button.closest(".crt-canvas-web-ref");
      if (action === "menu") {
        if (!ref) return;
        const open = !ref.classList.contains("menu-open");
        closeWebMenus(ref);
        card.querySelectorAll(".crt-canvas-file-ref.menu-open").forEach((item) => item.classList.remove("menu-open"));
        card.querySelectorAll(".crt-canvas-command-ref.menu-open").forEach((item) => item.classList.remove("menu-open"));
        ref.classList.toggle("menu-open", open);
        return;
      }
      closeWebMenus();
      try {
        const browserAction = action === "browser_open"
          ? "open"
          : action === "browser_observe"
            ? "observe"
            : action === "browser_click_text"
              ? "click_text"
              : "";
        const payload = browserAction
          ? await runBrowserAction(browserAction, {
            url,
            browserSessionId,
            text: button.getAttribute("data-browser-text") || "",
            label: button.textContent || "",
          })
          : await runWebAction(action, url);
        markFileAction(ref, "action-ok");
        if (payload && payload.sourcePath) attachGeneratedFileChip(ref, payload.sourcePath);
      } catch (err) {
        markFileAction(ref, "action-error");
        if (typeof window.__weBridgeLog === "function") {
          window.__weBridgeLog("canvas.web_action_failed", {
            action,
            url,
            error: String(err && (err.stack || err)),
          }, "warning");
        }
      }
    }

    async function runCommandAction(action, command) {
      if (action === "copy") {
        await copyText(command);
        return {};
      }
      return postCanvasAction("command", action, { command });
    }

    async function handleCommandAction(button) {
      const action = button.getAttribute("data-command-action");
      const command = button.getAttribute("data-command") || "";
      const ref = button.closest(".crt-canvas-command-ref");
      if (action === "menu") {
        const open = !ref.classList.contains("menu-open");
        closeFileMenus();
        closeCommandMenus(ref);
        ref.classList.toggle("menu-open", open);
        return;
      }
      closeCommandMenus();
      try {
        const payload = await runCommandAction(action, command);
        markFileAction(ref, "action-ok");
        if (payload && payload.batPath) attachGeneratedFileChip(ref, payload.batPath);
      } catch (err) {
        markFileAction(ref, "action-error");
        if (typeof window.__weBridgeLog === "function") {
          window.__weBridgeLog("canvas.command_action_failed", {
            action,
            command,
            error: String(err && (err.stack || err)),
          }, "warning");
        }
      }
    }

    async function handleProviderAction(button) {
      const action = button.getAttribute("data-provider-action") || "";
      const ref = button.closest(".crt-canvas-provider-ref");
      try {
        await postCanvasAction("provider", action, {
          provider: button.getAttribute("data-provider") || "",
          run_id: button.getAttribute("data-provider-run-id") || "",
          cwd: button.getAttribute("data-provider-cwd") || "",
          ref: button.getAttribute("data-provider-ref") || "",
          label: button.textContent || "",
        });
        markFileAction(ref, "action-ok");
      } catch (err) {
        markFileAction(ref, "action-error");
        if (typeof window.__weBridgeLog === "function") {
          window.__weBridgeLog("canvas.provider_action_failed", {
            action,
            provider: button.getAttribute("data-provider") || "",
            run_id: button.getAttribute("data-provider-run-id") || "",
            error: String(err && (err.stack || err)),
          }, "warning");
        }
      }
    }

    function workItemActionPayload(extra) {
      const context = state.workContext || {};
      const dock = state.taskDock || {};
      return Object.assign({
        project_id: String(context.projectId || ""),
        work_item_id: String(context.workItemId || dock.selectedWorkItemId || ""),
        run_id: String(context.runId || ""),
        attempt_id: String(context.attemptId || ""),
        revision: String(dock.revision || ""),
      }, extra || {});
    }

    function workActionErrorText(error, action) {
      const code = String(error && error.code || error && error.message || "canvas_action_failed");
      if (code === "stale_revision") {
        return "Task state changed, so I refreshed the task list. Review it and try again.";
      }
      if (code === "work_item_not_selected" || code === "permission_work_item_not_selected") {
        return "The selected task changed. I refreshed the task list; select the task again and retry.";
      }
      if (code === "work_attempt_not_current") {
        return "That run is no longer the current attempt. I refreshed the task list so you can review its latest state.";
      }
      if (code === "work_action_not_available") {
        return "That task action is no longer available. I refreshed the task list so you can choose the current action.";
      }
      const label = {
        select: "open that task",
        set_focus: "update workspace routing",
        retry: "retry the failed run",
        resume: "resume the interrupted run",
        accept: "accept the reviewed task",
        archive: "archive the task",
        reopen: "reopen that finished task",
        promote_to_project: "keep that task as a project",
        open_preview: "open that task's preview",
        exit_project: "return this conversation to Drafts",
        open_project: "open that project's Chat",
        open_work_item: "open that task's Chat",
      }[String(action || "")] || "complete that task action";
      return "Unable to " + label + ": " + code.replace(/_/g, " ") + ".";
    }

    async function handleWorkItemSelect(button) {
      const workItemId = button.getAttribute("data-work-item-id") || "";
      if (!workItemId) return;
      state.workActionError = "";
      markActionPending(button, true);
      try {
        await postCanvasAction("work_item", "select", workItemActionPayload({ work_item_id: workItemId }));
        markFileAction(button, "action-ok");
      } catch (err) {
        state.workActionError = workActionErrorText(err, "select");
        markFileAction(button, "action-error");
        if (typeof window.__weBridgeLog === "function") {
          window.__weBridgeLog("canvas.work_item_select_failed", {
            work_item_id: workItemId,
            error: String(err && (err.stack || err)),
          }, "warning");
        }
      } finally {
        markActionPending(button, false);
        render();
      }
    }

    async function handleWorkItemFocus(button) {
      const focusMode = button.getAttribute("data-work-focus-mode") === "pinned" ? "pinned" : "auto";
      const focusWorkItemId = String(button.getAttribute("data-work-focus-item-id") || "");
      state.workActionError = "";
      markActionPending(button, true);
      try {
        await postCanvasAction("work_item", "set_focus", workItemActionPayload({
          focus_mode: focusMode,
          work_item_id: focusWorkItemId,
        }));
        markFileAction(button, "action-ok");
      } catch (err) {
        state.workActionError = workActionErrorText(err, "set_focus");
        markFileAction(button, "action-error");
        if (typeof window.__weBridgeLog === "function") {
          window.__weBridgeLog("canvas.work_item_focus_failed", {
            focus_mode: focusMode,
            error: String(err && (err.stack || err)),
          }, "warning");
        }
      } finally {
        markActionPending(button, false);
        render();
      }
    }

    async function handleDestinationExit(button) {
      state.workActionError = "";
      markActionPending(button, true);
      try {
        await postCanvasAction("work_destination", "exit_project", workItemActionPayload());
        markFileAction(button, "action-ok");
      } catch (err) {
        state.workActionError = workActionErrorText(err, "exit_project");
        markFileAction(button, "action-error");
        if (typeof window.__weBridgeLog === "function") {
          window.__weBridgeLog("canvas.work_destination_exit_failed", {
            error: String(err && (err.stack || err)),
          }, "warning");
        }
      } finally {
        markActionPending(button, false);
        render();
      }
    }

    async function handleConversationOpen(button) {
      const projectId = String(button.getAttribute("data-conversation-project-id") || "");
      const workItemId = String(button.getAttribute("data-conversation-work-item-id") || "");
      const action = workItemId ? "open_work_item" : "open_project";
      if (!projectId) return;
      state.workActionError = "";
      markActionPending(button, true);
      try {
        await postCanvasAction("conversation", action, {
          project_id: projectId,
          work_item_id: workItemId,
        });
        markFileAction(button, "action-ok");
      } catch (err) {
        state.workActionError = workActionErrorText(err, action);
        markFileAction(button, "action-error");
        if (typeof window.__weBridgeLog === "function") {
          window.__weBridgeLog("canvas.conversation_context_failed", {
            action,
            project_id: projectId,
            work_item_id: workItemId,
            error: String(err && (err.stack || err)),
          }, "warning");
        }
      } finally {
        markActionPending(button, false);
        render();
      }
    }

    async function handleWorkItemExecution(button) {
      const action = String(button.getAttribute("data-work-execution-action") || "").toLowerCase();
      const workItemId = String(button.getAttribute("data-work-action-item-id") || "");
      const attemptId = String(button.getAttribute("data-work-action-attempt-id") || "");
      const authorizationRequestId = String(button.getAttribute("data-work-authorization-request-id") || "");
      if (
        state.workExecutionSubmitting
        || !["retry", "resume"].includes(action)
        || !workItemId
        || !attemptId
      ) return;
      state.workExecutionSubmitting = true;
      state.workActionError = "";
      markActionPending(button, true);
      try {
        await postCanvasAction("work_item", action, workItemActionPayload({
          work_item_id: workItemId,
          attempt_id: attemptId,
          ...(authorizationRequestId
            ? { authorization_permission_request_id: authorizationRequestId }
            : {}),
        }));
        markFileAction(button, "action-ok");
      } catch (err) {
        state.workActionError = workActionErrorText(err, action);
        markFileAction(button, "action-error");
        if (typeof window.__weBridgeLog === "function") {
          window.__weBridgeLog("canvas.work_item_execution_failed", {
            action,
            work_item_id: workItemId,
            attempt_id: attemptId,
            error: String(err && (err.stack || err)),
          }, "warning");
        }
      } finally {
        state.workExecutionSubmitting = false;
        markActionPending(button, false);
        render();
      }
    }

    async function handleWorkItemPreview(button) {
      const workItemId = String(button.getAttribute("data-work-preview-item-id") || "");
      const attemptId = String(button.getAttribute("data-work-preview-attempt-id") || "");
      const revision = String(state.taskDock && state.taskDock.revision || "");
      if (state.workPreviewSubmitting || !workItemId || !attemptId || !revision) return;
      state.workPreviewSubmitting = true;
      state.workActionError = "";
      markActionPending(button, true);
      try {
        // A preview request is identity-only. The trusted Host resolves the
        // workspace, entry point, server, and window from the ledger.
        await postCanvasAction("work_item", "open_preview", {
          work_item_id: workItemId,
          attempt_id: attemptId,
          revision,
        });
        markFileAction(button, "action-ok");
      } catch (err) {
        state.workActionError = workActionErrorText(err, "open_preview");
        markFileAction(button, "action-error");
        if (typeof window.__weBridgeLog === "function") {
          window.__weBridgeLog("canvas.work_item_preview_failed", {
            work_item_id: workItemId,
            attempt_id: attemptId,
            error: String(err && (err.stack || err)),
          }, "warning");
        }
      } finally {
        state.workPreviewSubmitting = false;
        markActionPending(button, false);
        render();
      }
    }

    function selectedWorkPreviewTarget() {
      const dock = state.taskDock && typeof state.taskDock === "object"
        ? state.taskDock
        : {};
      const workItemId = String(dock.selectedWorkItemId || "");
      const items = Array.isArray(dock.items) ? dock.items : [];
      const item = workItemId
        ? items.find((candidate) => String(candidate && candidate.id || "") === workItemId)
        : null;
      const attemptId = String(item && item.attemptId || "");
      const revision = String(dock.revision || "");
      return {
        workItemId,
        attemptId,
        ready: Boolean(item && item.workspacePath && attemptId && revision),
      };
    }

    function workPreviewLaunchButton() {
      const target = selectedWorkPreviewTarget();
      const title = target.ready
        ? "Open the live preview for this task"
        : "Live preview will be available after the selected task has a workspace and an active attempt";
      return [
        "<button type=\"button\" class=\"crt-canvas-preview-launch\" data-work-preview-item-id=\"" + escapeAttr(target.workItemId) + "\" data-work-preview-attempt-id=\"" + escapeAttr(target.attemptId) + "\" title=\"" + escapeAttr(title) + "\" aria-label=\"" + escapeAttr(title) + "\"",
        ((!target.ready || state.workPreviewSubmitting) ? " disabled" : ""),
        ">W</button>",
      ].join("");
    }

    // A draft reaches only as far as the conversation that made it, so this is
    // the one place it can be given a name the model routes to later. The host
    // decides whether it is offered; the surface only reads the projection.
    const WORK_DISPOSITION_ACTIONS = new Set(["accept", "archive", "reopen", "promote_to_project"]);
    function workDispositionLabel(action) {
      return {
        accept: ui("accept"),
        archive: ui("archive"),
        reopen: ui("reopen"),
        promote_to_project: ui("keepProject"),
      }[action] || action;
    }
    const WORK_DISPOSITION_TITLES = {
      accept: "Accept this reviewed WorkItem and clear its attention badge.",
      archive: "Archive this WorkItem and remove it from current work.",
      reopen: "Reopen this finished task so it can be worked on again.",
      promote_to_project: "Keep this scratch task as a project so later instructions can be sent to it by name.",
    };

    function taskDispositionActions(item) {
      const stateValue = String(item && item.state || "").toLowerCase();
      const running = taskIsRunning(item);
      const actions = [];
      if (stateValue === "review_ready") actions.push("accept", "archive");
      else if (stateValue === "open" && !running) actions.push("archive");
      // Finished is not the same as finished with. Without this, an accepted or
      // archived task had no action at all here and could only be picked back
      // up from the desktop app.
      if (item && item.canReopen && !running) actions.push("reopen");
      if (item && item.canPromoteToProject && !running) actions.push("promote_to_project");
      return actions;
    }

    function taskMenuHasSelectedActions(item, dock) {
      const itemId = String(item && item.id || "");
      const selectedId = String(dock && dock.selectedWorkItemId || "");
      if (!itemId || itemId !== selectedId) return false;
      const workspacePath = String(item && (item.workspacePath || item.workspaceLabel) || "");
      const workspaceUnlocked = String(dock && dock.workspaceFocusMode || "auto") !== "pinned";
      return (workspaceUnlocked && !!workspacePath) || !!String(item && item.projectId || "");
    }

    function taskDispositionToggleHtml(item, dock) {
      const actions = taskDispositionActions(item);
      if (!actions.length && !taskMenuHasSelectedActions(item, dock)) return "";
      const itemId = String(item && item.id || "");
      const open = state.workDispositionMenu === itemId;
      return [
        "<span class=\"crt-canvas-task-disposition" + (open ? " is-open" : "") + "\">",
        "<button type=\"button\" class=\"crt-canvas-task-disposition-toggle\" data-work-disposition-toggle=\"" + escapeAttr(itemId) + "\" aria-haspopup=\"menu\" aria-expanded=\"" + (open ? "true" : "false") + "\" aria-label=\"Task actions\" title=\"Task actions\"><i aria-hidden=\"true\"></i></button>",
        "</span>",
      ].join("");
    }

    function taskDispositionOverlayHtml(items, dock) {
      const item = (Array.isArray(items) ? items : []).find(
        (candidate) => String(candidate && candidate.id || "") === state.workDispositionMenu
      );
      if (!item) return "";
      const actions = taskDispositionActions(item);
      const selectedActions = taskMenuHasSelectedActions(item, dock);
      if (!actions.length && !selectedActions) return "";
      const itemId = String(item.id || "");
      const menuButtons = [];
      if (selectedActions && item.projectId) {
        menuButtons.push(
          "<button type=\"button\" role=\"menuitem\" data-conversation-open=\"work-item\" data-conversation-project-id=\"" + escapeAttr(item.projectId) + "\" data-conversation-work-item-id=\"" + escapeAttr(itemId) + "\" title=\"Open this task as the main Chat context\">Open in Chat</button>"
        );
      }
      const workspacePath = String(item.workspacePath || item.workspaceLabel || "");
      const workspaceUnlocked = String(dock && dock.workspaceFocusMode || "auto") !== "pinned";
      if (selectedActions && workspaceUnlocked && workspacePath) {
        menuButtons.push(
          "<button type=\"button\" role=\"menuitem\" data-work-focus-mode=\"pinned\" data-work-focus-item-id=\"" + escapeAttr(itemId) + "\" title=\"" + escapeAttr("Restore this workspace: pin future tasks to " + workspacePath + ". Browsing task history stays view-only until a workspace is restored.") + "\">Restore</button>"
        );
      }
      menuButtons.push(...actions.map((action) => {
        const title = WORK_DISPOSITION_TITLES[action] || "";
        const label = workDispositionLabel(action);
        return "<button type=\"button\" role=\"menuitem\" data-work-disposition-action=\"" + action + "\" data-work-disposition-item-id=\"" + escapeAttr(itemId) + "\" title=\"" + escapeAttr(title) + "\"" + (state.workDispositionSubmitting ? " disabled" : "") + ">" + escapeHtml(label) + "</button>";
      }));
      return "<span class=\"crt-canvas-task-disposition-menu crt-canvas-task-disposition-overlay\" role=\"menu\" data-work-disposition-overlay=\"" + escapeAttr(itemId) + "\">" + menuButtons.join("") + "</span>";
    }

    async function handleWorkItemDisposition(button) {
      const action = String(button.getAttribute("data-work-disposition-action") || "").toLowerCase();
      const workItemId = String(button.getAttribute("data-work-disposition-item-id") || "");
      if (state.workDispositionSubmitting || !WORK_DISPOSITION_ACTIONS.has(action) || !workItemId) return;
      state.workDispositionSubmitting = true;
      state.workActionError = "";
      markActionPending(button, true);
      try {
        await postCanvasAction("work_item", action, workItemActionPayload({ work_item_id: workItemId }));
        state.workDispositionMenu = "";
        markFileAction(button, "action-ok");
      } catch (err) {
        state.workActionError = workActionErrorText(err, action);
        markFileAction(button, "action-error");
        if (typeof window.__weBridgeLog === "function") {
          window.__weBridgeLog("canvas.work_item_disposition_failed", {
            action,
            work_item_id: workItemId,
            error: String(err && (err.stack || err)),
          }, "warning");
        }
      } finally {
        state.workDispositionSubmitting = false;
        markActionPending(button, false);
        render();
      }
    }

    function positionWorkDispositionMenu() {
      const menu = card.querySelector("[data-work-disposition-overlay]");
      if (!(menu instanceof HTMLElement)) return;
      const itemId = String(menu.getAttribute("data-work-disposition-overlay") || "");
      const toggle = Array.from(card.querySelectorAll("[data-work-disposition-toggle]"))
        .find((candidate) => String(candidate.getAttribute("data-work-disposition-toggle") || "") === itemId);
      const dock = menu.closest(".crt-canvas-task-dock");
      if (!(toggle instanceof HTMLElement) || !(dock instanceof HTMLElement)) return;

      const dockRect = dock.getBoundingClientRect();
      const cardRect = card.getBoundingClientRect();
      const toggleRect = toggle.getBoundingClientRect();
      const menuRect = menu.getBoundingClientRect();
      const gap = 3;
      const inset = 5;
      const maxLeft = Math.max(inset, dockRect.width - menuRect.width - inset);
      const left = Math.min(
        maxLeft,
        Math.max(inset, toggleRect.right - dockRect.left - menuRect.width)
      );
      let top = toggleRect.bottom - dockRect.top + gap;
      const maxBottom = cardRect.bottom - dockRect.top - inset;
      if (top + menuRect.height > maxBottom) {
        top = Math.max(inset, toggleRect.top - dockRect.top - menuRect.height - gap);
      }
      menu.style.left = Math.round(left) + "px";
      menu.style.top = Math.round(top) + "px";
      menu.style.right = "auto";
    }

    function canScrollElement(element, axis, delta) {
      if (!(element instanceof HTMLElement)) return false;
      if (axis === "x") {
        if (element.scrollWidth <= element.clientWidth + 1) return false;
        if (delta < 0) return element.scrollLeft > 0;
        if (delta > 0) return element.scrollLeft + element.clientWidth < element.scrollWidth - 1;
        return true;
      }
      if (element.scrollHeight <= element.clientHeight + 1) return false;
      if (delta < 0) return element.scrollTop > 0;
      if (delta > 0) return element.scrollTop + element.clientHeight < element.scrollHeight - 1;
      return true;
    }

    function scrollCandidatesFrom(target) {
      const candidates = [];
      let node = target instanceof Element ? target : null;
      while (node && node !== card) {
        if (node instanceof HTMLElement) candidates.push(node);
        node = node.parentElement;
      }
      [
        ".crt-canvas-task-rail",
        ".crt-canvas-source-tail-list",
        ".crt-canvas-md-table-wrap",
        ".crt-canvas-markdown-body",
        ".crt-canvas-pane.workflow",
        ".crt-canvas-pane.browser",
        ".crt-canvas-pane.table",
        ".crt-canvas-pane.code",
        ".crt-canvas-diff-files",
        ".crt-canvas-diff-content",
      ].forEach((selector) => {
        const item = card.querySelector(selector);
        if (item instanceof HTMLElement && !candidates.includes(item)) candidates.push(item);
      });
      return candidates;
    }

    function handleCanvasWheel(event) {
      // Native Electron input should stay on Chromium's compositor path. The
      // manual fallback is only for untrusted WheelEvents forwarded by a
      // wallpaper host that cannot receive wheel input itself.
      if (event.isTrusted) return;
      if (!state.expanded) return;
      const deltaY = Number(event.deltaY) || 0;
      const deltaX = Number(event.deltaX) || 0;
      const wantsHorizontal = Math.abs(deltaX) > Math.abs(deltaY) || event.shiftKey;
      const primaryAxis = wantsHorizontal ? "x" : "y";
      const primaryDelta = wantsHorizontal ? (deltaX || deltaY) : deltaY;
      const fallbackAxis = primaryAxis === "x" ? "y" : "x";
      const fallbackDelta = primaryAxis === "x" ? deltaY : deltaX;
      const candidates = scrollCandidatesFrom(event.target);
      let target = candidates.find((item) => canScrollElement(item, primaryAxis, primaryDelta));
      let axis = primaryAxis;
      let delta = primaryDelta;
      if (!target && fallbackDelta) {
        target = candidates.find((item) => canScrollElement(item, fallbackAxis, fallbackDelta));
        axis = fallbackAxis;
        delta = fallbackDelta;
      }
      if (!target) return;
      event.preventDefault();
      event.stopPropagation();
      if (axis === "x") {
        target.scrollLeft += delta;
      } else {
        target.scrollTop += delta;
      }
    }

    function own(object, key) {
      return Object.prototype.hasOwnProperty.call(object || {}, key);
    }

    function compactText(value, limit) {
      return String(value == null ? "" : value).trim().slice(0, limit);
    }

    function normalizeProviderDisplayTitle(value) {
      return String(value == null ? "" : value)
        .replace(/^Codex App Server(?=\s|$)/, "Codex")
        .replace(/^Direct Codex(?=\s|$)/, "Codex");
    }

    function normalizeProviderDisplayMarkdown(value) {
      return String(value == null ? "" : value)
        .replace(/^###\s+Codex App Server(?=\s|$)/, "### Codex")
        .replace(/^###\s+Direct Codex(?=\s|$)/, "### Codex");
    }

    function nonNegativeCount(value) {
      const count = Number(value);
      return Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0;
    }

    function normalizeWorkContext(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return null;
      return {
        projectId: compactText(value.projectId || value.project_id, 160),
        workItemId: compactText(value.workItemId || value.work_item_id, 160),
        runId: compactText(value.runId || value.run_id, 160),
        attemptId: compactText(value.attemptId || value.attempt_id, 160),
      };
    }

    function normalizeTaskDock(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return null;
      const counts = value.counts && typeof value.counts === "object" ? value.counts : {};
      const rawDestinationFeedback = value.destinationFeedback && typeof value.destinationFeedback === "object"
        ? value.destinationFeedback
        : (value.destination_feedback && typeof value.destination_feedback === "object"
          ? value.destination_feedback
          : null);
      const items = (Array.isArray(value.items) ? value.items : [])
        .filter((item) => item && typeof item === "object" && !Array.isArray(item))
        .map((item) => ({
          id: compactText(item.id || item.workItemId || item.work_item_id, 160),
          title: compactText(item.title || "Untitled task", 160),
          execution: compactText(item.execution, 40).toLowerCase(),
          liveness: compactText(item.liveness, 40).toLowerCase(),
          livenessStage: compactText(item.livenessStage || item.liveness_stage, 40).toLowerCase(),
          probeStatus: compactText(item.probeStatus || item.probe_status, 40).toLowerCase(),
          silentForSeconds: nonNegativeCount(
            item.silentForSeconds == null ? item.silent_for_seconds : item.silentForSeconds
          ),
          completion: compactText(item.completion, 40).toLowerCase(),
          attention: compactText(item.attention, 40).toLowerCase(),
          state: compactText(item.state || item.disposition, 40).toLowerCase(),
          workspaceLabel: compactText(item.workspaceLabel || item.workspace_label, 160),
          workspacePath: compactText(item.workspacePath || item.workspace_path, 2048),
          updatedAt: compactText(item.updatedAt || item.updated_at, 80),
          attemptId: compactText(item.attemptId || item.attempt_id, 160),
          sessionId: compactText(item.sessionId || item.session_id, 160),
          projectId: compactText(item.projectId || item.project_id, 160),
          projectName: compactText(item.projectName || item.project_name, 160),
          canRetry: item.canRetry === true || item.can_retry === true,
          canResume: item.canResume === true || item.can_resume === true,
          canReopen: item.canReopen === true || item.can_reopen === true,
          canPromoteToProject: item.canPromoteToProject === true || item.can_promote_to_project === true,
          retryAuthorizationRequestId: compactText(
            item.retryAuthorizationRequestId || item.retry_authorization_request_id,
            240
          ),
        }))
        .filter((item) => item.id)
        .slice(0, 24);
      const projects = (Array.isArray(value.projects) ? value.projects : [])
        .filter((project) => project && typeof project === "object" && !Array.isArray(project))
        .map((project) => {
          const projectCounts = project.counts && typeof project.counts === "object"
            ? project.counts
            : {};
          return {
            id: compactText(project.projectId || project.project_id || project.id, 160),
            name: compactText(project.name || project.projectName || "Untitled project", 160),
            workspacePath: compactText(project.workspacePath || project.workspace_path, 2048),
            latestWorkItemId: compactText(project.latestWorkItemId || project.latest_work_item_id, 160),
            latestTaskTitle: compactText(project.latestTaskTitle || project.latest_task_title, 160),
            counts: {
              current: nonNegativeCount(projectCounts.current),
              needsYou: nonNegativeCount(projectCounts.needsYou == null ? projectCounts.needs_you : projectCounts.needsYou),
              running: nonNegativeCount(projectCounts.running),
              history: nonNegativeCount(projectCounts.history),
            },
          };
        })
        .filter((project) => project.id)
        .slice(0, 100);
      return {
        revision: compactText(value.revision, 160),
        currentSessionId: compactText(value.currentSessionId || value.current_session_id, 160),
        selectedWorkItemId: compactText(value.selectedWorkItemId || value.selected_work_item_id, 160),
        workspaceFocusMode: String(value.workspaceFocusMode || value.workspace_focus_mode || "auto").toLowerCase() === "pinned" ? "pinned" : "auto",
        workspaceFocusPath: compactText(value.workspaceFocusPath || value.workspace_focus_path, 2048),
        destinationLabel: compactText(value.destinationLabel || value.destination_label, 160),
        destinationProjectId: compactText(value.destinationProjectId || value.destination_project_id, 160),
        destinationFeedback: rawDestinationFeedback
          ? {
            status: compactText(rawDestinationFeedback.status, 40).toLowerCase(),
            message: compactText(rawDestinationFeedback.message, 320),
          }
          : null,
        workspaceFocusWorkItemId: compactText(value.workspaceFocusWorkItemId || value.workspace_focus_work_item_id, 160),
        counts: {
          running: nonNegativeCount(counts.running),
          needsAttention: nonNegativeCount(counts.needsAttention == null ? counts.needs_attention : counts.needsAttention),
          active: nonNegativeCount(counts.active),
        },
        projects,
        items,
      };
    }

    function applyWorkProjectionResponse(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return false;
      const projection = value.work && typeof value.work === "object" && !Array.isArray(value.work)
        ? value.work
        : (value.projection && typeof value.projection === "object" && !Array.isArray(value.projection)
          ? value.projection
          : null);
      const taskDock = normalizeTaskDock(projection);
      if (!taskDock) return false;
      state.taskDock = taskDock;
      const selected = projection && projection.selected && typeof projection.selected === "object"
        ? projection.selected
        : null;
      if (selected && state.permissionRequest && state.permissionRequest.id) {
        const recovery = Array.isArray(state.permissionRequest.options)
          && state.permissionRequest.options.some((option) => option === "retry_export" || option === "abandon_export");
        const currentId = String(
          selected[recovery ? "recoverableExportRequestId" : "pendingPermissionRequestId"] || ""
        );
        if (currentId !== String(state.permissionRequest.id)) {
          state.permissionVisible = false;
          state.permissionRequest = null;
          state.permissionSubmitting = false;
          state.permissionError = "";
        }
      }
      return true;
    }

    function normalizePermissionRequest(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return null;
      const rawScope = Array.isArray(value.scope_paths)
        ? value.scope_paths
        : (Array.isArray(value.scope) ? value.scope : (value.scope ? [value.scope] : []));
      const scope = rawScope
        .map((item) => compactText(item && typeof item === "object" ? item.path : item, 2048))
        .filter(Boolean)
        .slice(0, 32);
      const rawOptions = Array.isArray(value.options) ? value.options : [];
      const options = rawOptions
        .map((item) => compactText(item && typeof item === "object" ? (item.kind || item.id) : item, 80).toLowerCase().replace(/-/g, "_"))
        .map((item) => item === "approve_once" || item === "allow" ? "allow_once" : (item === "reject" || item === "reject_once" ? "deny" : item))
        .filter((item, index, values) => ["allow_once", "deny", "retry_export", "abandon_export"].includes(item) && values.indexOf(item) === index);
      const previews = (Array.isArray(value.previews) ? value.previews : [])
        .filter((item) => item && typeof item === "object" && !Array.isArray(item))
        .map((item) => ({
          path: compactText(item.path, 2048),
          status: compactText(item.status, 80),
          mediaType: compactText(item.mediaType || item.media_type, 160),
          sizeBytes: nonNegativeCount(item.sizeBytes == null ? item.size_bytes : item.sizeBytes),
          sha256: compactText(item.sha256, 128),
        }))
        .filter((item) => item.path && item.status === "binary_identity" && item.sha256)
        .slice(0, 128);
      return {
        id: compactText(value.id || value.requestId || value.request_id, 240),
        workItemId: compactText(value.workItemId || value.work_item_id, 160),
        attemptId: compactText(value.attemptId || value.attempt_id, 160),
        capability: compactText(value.capability || "permission", 120),
        action: compactText(value.action || "scoped_action", 120),
        scope,
        reason: compactText(value.reason || "Explicit user approval is required.", 1000),
        reversibility: compactText(value.reversibility || "Unknown reversibility", 300),
        status: compactText(value.status || "pending", 40).toLowerCase(),
        options: options.length ? options : ["deny"],
        retryRequired: value.retryRequired === true || value.retry_required === true,
        diagnosticOnly: value.diagnosticOnly === true || value.diagnostic_only === true,
        previewComplete: value.previewComplete === true || value.preview_complete === true,
        previewVersion: nonNegativeCount(value.previewVersion == null ? value.preview_version : value.previewVersion),
        previews,
      };
    }

    function normalizeAttentionRequest(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return null;
      const status = compactText(value.status || "pending", 40).toLowerCase();
      if (status !== "pending") return null;
      const options = (Array.isArray(value.options) ? value.options : [])
        .filter((item) => item && typeof item === "object" && !Array.isArray(item))
        .map((item) => ({
          id: compactText(item.id || item.optionId || item.option_id, 240),
          label: compactText(item.label, 160),
          description: compactText(item.description, 240),
          parentLabel: compactText(item.parentLabel || item.parent_label, 160),
          entityKind: ["project", "work_item"].includes(String(item.entityKind || item.entity_kind || "").toLowerCase())
            ? String(item.entityKind || item.entity_kind).toLowerCase()
            : "other",
        }))
        .filter((item) => item.id && item.label)
        .slice(0, 12);
      const request = {
        id: compactText(value.id || value.requestId || value.request_id, 240),
        title: compactText(value.title, 160),
        prompt: compactText(value.prompt, 520),
        options,
      };
      return request.id && request.options.length ? request : null;
    }

    function attentionRequestFromEnvelope(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return null;
      const requests = Array.isArray(value.requests) ? value.requests : [];
      for (const request of requests) {
        const normalized = normalizeAttentionRequest(request);
        if (normalized) return normalized;
      }
      return null;
    }

    function stablePresentationValue(value, seen) {
      if (value === null || typeof value === "string" || typeof value === "boolean") return value;
      if (typeof value === "number") return Number.isFinite(value) ? value : String(value);
      if (typeof value !== "object") return undefined;
      if (seen.has(value)) return "[circular]";
      seen.add(value);
      let normalized;
      if (Array.isArray(value)) {
        normalized = value.map((item) => {
          const entry = stablePresentationValue(item, seen);
          return entry === undefined ? null : entry;
        });
      } else {
        normalized = {};
        Object.keys(value).sort().forEach((key) => {
          const entry = stablePresentationValue(value[key], seen);
          if (entry !== undefined) normalized[key] = entry;
        });
      }
      seen.delete(value);
      return normalized;
    }

    function presentationKey(data) {
      if (!data || typeof data !== "object" || Array.isArray(data)) return "";
      const ignored = new Set([
        "taskDock", "workContext", "permissionRequest", "permissionVisible",
        "open", "expanded", "visible", "ttlMs", "timeoutMs", "lifecycle",
        "size", "sizePreset", "revision", "clear", "action",
      ]);
      const presentation = {};
      Object.keys(data).sort().forEach((key) => {
        if (ignored.has(key)) return;
        const value = stablePresentationValue(data[key], new Set());
        if (value !== undefined) presentation[key] = value;
      });
      return Object.keys(presentation).length ? JSON.stringify(presentation) : "";
    }

    function taskDockCountText() {
      const dock = state.taskDock;
      if (!dock) return "";
      const counts = dock.counts || {};
      const items = Array.isArray(dock.items) ? dock.items : [];
      const workspacePinned = String(dock.workspaceFocusMode || "auto") === "pinned";
      const destination = workspacePinned
        ? workspaceDirName(dock.workspaceFocusPath || "")
        : (String(dock.destinationLabel || "").trim() || "Drafts");
      const needsAttention = items.filter((item) => (
        taskBelongsToCurrentSession(item, dock)
        && !taskIsHistory(item)
        && taskNeedsAttention(item)
      )).length;
      const parts = ["Destination", destination];
      if ((counts.running || 0) > 0) parts.push((counts.running || 0) + " running");
      if (needsAttention > 0) parts.push(needsAttention + " action required");
      return parts.join(" · ");
    }

    function statusText() {
      const dockText = taskDockCountText();
      if (dockText) return dockText;
      const elapsedMinutes = Math.max(0, Math.floor((Date.now() - state.startedAt) / 60000));
      const phase = String(state.phase || "Ready").toUpperCase();
      return phase + " - " + elapsedMinutes + "m";
    }

    function workspaceDirName(path) {
      const text = String(path || "");
      if (/^https?:\/\//i.test(text)) return "workspace";
      const segments = text.split(/[\\/]/).filter((segment) => segment && segment !== "." && segment !== "..");
      return segments.pop() || "workspace";
    }

    function workspaceLockInfo() {
      const dock = state.taskDock;
      if (!dock || dock.workspaceFocusMode !== "pinned") return null;
      const path = String(dock.workspaceFocusPath || "");
      return { path, name: workspaceDirName(path) };
    }

    function updateStatus() {
      const text = statusText();
      const lock = workspaceLockInfo();
      status.classList.toggle("ws-locked", !!lock);
      if (lock) {
        status.innerHTML = "<b class=\"crt-canvas-ws-lock\">Locked &middot; " + escapeHtml(lock.name) + "</b> &middot; " + escapeHtml(text);
        status.title = "Workspace locked: " + lock.path + " - future tasks stay in this directory. Click to open the canvas.";
        status.setAttribute("aria-label", "Workspace locked to " + lock.name + ". " + text);
      } else {
        status.textContent = text;
        status.removeAttribute("title");
        status.setAttribute("aria-label", text);
      }
    }

    function clampSize(size) {
      const rootWidth = Math.max(1, root.clientWidth || 560);
      const rootHeight = Math.max(1, root.clientHeight || 420);
      const leftOffset = Math.max(16, rootWidth * 0.04);
      const topOffset = Math.max(32, rootHeight * 0.09);
      const maxWidth = Math.max(220, rootWidth - leftOffset - 22);
      const maxHeight = Math.max(180, rootHeight - topOffset - 18);
      const minWidth = Math.min(280, maxWidth);
      const minHeight = Math.min(260, maxHeight);
      return {
        width: Math.min(Math.max(Number(size.width) || 360, minWidth), maxWidth),
        height: Math.min(Math.max(Number(size.height) || 300, minHeight), maxHeight),
      };
    }

    function saveSize() {
      try {
        localStorage.setItem(SIZE_KEY, JSON.stringify(state.size));
      } catch (err) {}
    }

    function savePreset() {
      try {
        localStorage.setItem(PRESET_KEY, state.preset);
      } catch (err) {}
    }

    function applySize() {
      state.size = clampSize(state.size);
      root.style.setProperty("--crt-canvas-w", Math.round(state.size.width) + "px");
      root.style.setProperty("--crt-canvas-h", Math.round(state.size.height) + "px");
    }

    function setPreset(preset) {
      state.preset = preset === "wide" || preset === "full" ? preset : "compact";
      state.size = clampSize(presetSize(state.preset));
      savePreset();
      saveSize();
      applySize();
    }

    function applyDemo(frame) {
      if (!frame || frame.atMs === state.lastDemoAt) return;
      state.lastDemoAt = frame.atMs;
      state.hasContent = true;
      state.expanded = !!frame.expanded;
      state.phase = frame.phase || state.phase;
      state.title = frame.title || state.title;
      state.lead = frame.lead || state.lead;
      state.mode = normalizeMode(frame.mode) || "workflow";
      state.progress = Number.isFinite(Number(frame.progress)) ? Number(frame.progress) : state.progress;
      state.permissionVisible = !!frame.permissionVisible;
      if (!state.permissionVisible) state.permissionRequest = null;
      if (Array.isArray(frame.signals)) state.signals = frame.signals;
      if (["compact", "wide", "full"].includes(frame.sizePreset)) setPreset(frame.sizePreset);
      render();
    }

    function normalizeMode(value) {
      const mode = String(value || "").trim().toLowerCase();
      if (!mode) return "";
      if (mode === "work" || mode === "work_signal" || mode === "provider_work") return "workflow";
      if (mode === "permission") return "workflow";
      if (mode === "web" || mode === "webview" || mode === "page" || mode === "browser.snapshot") return "browser";
      if (["workflow", "browser", "markdown", "diff", "html", "image", "table", "code"].includes(mode)) return mode;
      return "";
    }

    function payloadLooksBrowser(data) {
      if (!data || typeof data !== "object") return false;
      if (typeof data.browserSessionId === "string" || typeof data.browser_session_id === "string") return true;
      if (typeof data.screenshot === "string" && data.screenshot) return true;
      if (typeof data.url === "string" && /^https?:\/\//i.test(data.url.trim())) return true;
      if (typeof data.pageTitle === "string" && data.pageTitle && Array.isArray(data.links)) return true;
      if (Array.isArray(data.links) && data.links.some((item) => item && typeof item.url === "string")) return true;
      if (Array.isArray(data.signals)) {
        return data.signals.some((signal) => {
          const label = String(signal && signal.label || "").toLowerCase();
          const detail = String(signal && signal.detail || "").toLowerCase();
          return label === "source" || label === "links" || detail.includes("bilibili.com") || detail.includes("http");
        });
      }
      return false;
    }

    function inferPayloadMode(data) {
      const explicit = normalizeMode(data && (data.mode || data.canvasMode || data.kind || data.artifact_type));
      if (explicit) return explicit;
      if (payloadLooksBrowser(data)) return "browser";
      return "";
    }

    function modeLabel() {
      if (state.mode === "browser") return ui("web");
      if (state.mode === "markdown") return ui("doc");
      if (state.mode === "diff") return ui("diff");
      if (state.mode === "html") return "HTML";
      if (state.mode === "image") return ui("image");
      if (state.mode === "table") return ui("table");
      if (state.mode === "code") return ui("code");
      return ui("work");
    }

    function surfaceTitle() {
      if (state.title && state.title !== "Provider canvas") return String(state.title);
      if (state.mode === "browser") return state.pageTitle || "Browser Source";
      if (state.mode === "markdown") return "AUIP Runtime Note";
      if (state.mode === "diff") return "Workspace Diff Review";
      if (state.mode === "html") return "Provider Runtime Snapshot";
      if (state.mode === "image") return "AUIP Card Map";
      if (state.mode === "table") return "Provider Run Metrics";
      if (state.mode === "code") return "Diff and Terminal Evidence";
      return ui("waitingTitle");
    }

    function surfaceKicker() {
      const phase = String(state.phase || "Ready");
      const localizedPhase = phase.toLowerCase() === "ready" ? ui("ready") : phase;
      return localizedPhase.toUpperCase() + " / " + String(modeLabel()).toUpperCase();
    }

    function workflowPane() {
      const signals = (state.signals || []).slice(0, 3).map((signal) => [
        "<button type=\"button\" class=\"crt-canvas-signal\">",
        "<span>" + escapeHtml(signal.label || "signal") + "</span>",
        "<strong>" + escapeHtml(signal.text || signal.summary || "") + "</strong>",
        signal.detail ? "<small>" + escapeHtml(signal.detail) + "</small>" : "",
        "</button>",
      ].join("")).join("");
      return [
        "<div class=\"crt-canvas-pane workflow\">",
        "<p>" + escapeHtml(state.lead) + "</p>",
        "<div class=\"crt-canvas-progress\"><i style=\"width:" + Math.round(state.progress) + "%\"></i></div>",
        "<div class=\"crt-canvas-signals\">" + signals + "</div>",
        "</div>",
      ].join("");
    }

    function browserPane() {
      const url = String(state.url || "");
      const browserOptions = { browserSessionId: state.browserSessionId };
      const source = url ? webLinkHtml(url, state.pageTitle || webLabelFromUrl(url), browserOptions) : "";
      const session = state.browserSessionId
        ? "<button type=\"button\" class=\"crt-canvas-browser-session\" data-web-action=\"browser_observe\" data-browser-session-id=\"" + escapeAttr(state.browserSessionId) + "\" aria-label=\"Observe browser session\">session " + escapeHtml(String(state.browserSessionId).slice(-6)) + "</button>"
        : "";
      const related = (Array.isArray(state.links) ? state.links : []).slice(0, 3).map((item) => {
        if (!item || !item.url) return "";
        return webLinkHtml(item.url, item.title || webLabelFromUrl(item.url), browserOptions);
      }).join("");
      const shot = state.screenshot
        ? "<div class=\"crt-canvas-browser-shot\"><img src=\"" + escapeAttr(state.screenshot) + "\" alt=\"Browser snapshot\"></div>"
        : "<div class=\"crt-canvas-browser-shot\"><div class=\"crt-canvas-browser-placeholder\">browser snapshot</div></div>";
      return [
        "<div class=\"crt-canvas-pane browser\">",
        shot,
        "<div class=\"crt-canvas-browser-copy\">",
        "<p>" + linkifyInlineText(state.excerpt || state.lead || "Browser source captured.") + "</p>",
        "</div>",
        "<div class=\"crt-canvas-browser-source\">",
        session,
        source,
        related,
        "</div>",
        "</div>",
      ].join("");
    }

    function markdownPane() {
      const sources = sourceTailHtml();
      if (state.markdown) {
        return [
          "<div class=\"crt-canvas-pane markdown crt-canvas-markdown-dynamic\">",
          "<div class=\"crt-canvas-markdown-body\">",
          markdownLite(state.markdown),
          "</div>",
          sources,
          "</div>",
        ].join("");
      }
      return [
        "<div class=\"crt-canvas-pane markdown\">",
        "<div class=\"crt-canvas-markdown-body\">",
        "<h4>AUIP Runtime Note</h4>",
        "<p>The current turn should expose <strong>state</strong>, not raw logs.</p>",
        "<code>WorkSignal = raw provider events -&gt; compact evidence</code>",
        "<p><strong>Formula draft:</strong> trust = validation * reversibility / risk</p>",
        "<ul><li>render provider summaries as markdown</li><li>preview generated HTML artifacts</li><li>show visual outputs without opening an IDE pane</li></ul>",
        "</div>",
        sources,
        "</div>",
      ].join("");
    }

    function sourceTailHtml() {
      const actions = Array.isArray(state.actions) ? state.actions : [];
      const sourceItems = actions
        .filter((item) => item && (item.url || item.uri))
        .slice(0, 4)
        .map((item) => webLinkHtml(String(item.url || item.uri || ""), String(item.label || ""), { browserSessionId: state.browserSessionId }))
        .join("");
      const providerItems = actions
        .filter((item) => item && (String(item.kind || "").toLowerCase() === "provider" || String(item.target || "").toLowerCase() === "provider" || (item.metadata && String(item.metadata.target || "").toLowerCase() === "provider")))
        .slice(0, 4)
        .map(providerActionHtml)
        .join("");
      const items = sourceItems + providerItems;
      if (!items) return "";
      const label = sourceItems && providerItems ? "Sources / Actions" : providerItems ? "Actions" : "Sources";
      return [
        "<section class=\"crt-canvas-source-tail\">",
        "<span>" + label + "</span>",
        "<div class=\"crt-canvas-source-tail-list\">",
        items,
        "</div>",
        "</section>",
      ].join("");
    }

    function htmlPane() {
      if (state.html) {
        return "<div class=\"crt-canvas-pane html\"><iframe aria-label=\"HTML artifact preview\" sandbox=\"\" srcdoc=\"" + escapeAttr(state.html) + "\"></iframe></div>";
      }
      const doc = [
        "<!doctype html><html><head><style>",
        "body{margin:0;min-height:100vh;font-family:system-ui,sans-serif;color:#d8fff2;background:linear-gradient(135deg,#061b20,#10172a 62%,#211533);}",
        "main{padding:16px}h1{margin:0 0 8px;font-size:20px}p{margin:0 0 14px;color:rgba(216,255,242,.72);line-height:1.4}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.cell{border:1px solid rgba(147,255,229,.28);border-radius:12px;padding:10px;background:rgba(4,28,34,.52)}strong{display:block;font-size:18px}span{font-size:10px;text-transform:uppercase;color:rgba(216,255,242,.62)}",
        "</style></head><body><main><h1>Provider Runtime Snapshot</h1><p>Generated HTML can live inside the CRT canvas.</p><section class=\"grid\"><div class=\"cell\"><strong>04</strong><span>signals</span></div><div class=\"cell\"><strong>2</strong><span>files</span></div><div class=\"cell\"><strong>1</strong><span>permission</span></div></section></main></body></html>",
      ].join("");
      return "<div class=\"crt-canvas-pane html\"><iframe aria-label=\"HTML artifact preview\" sandbox=\"\" srcdoc=\"" + escapeAttr(doc) + "\"></iframe></div>";
    }

    function imagePane() {
      return [
        "<div class=\"crt-canvas-pane image\">",
        "<div class=\"crt-canvas-image\"><strong>AUIP Card Map</strong><span>manifest -&gt; event -&gt; response -&gt; action</span></div>",
        "<p>Image artifacts should open here first, with full detail one click away.</p>",
        "</div>",
      ].join("");
    }

    function tablePane() {
      return [
        "<div class=\"crt-canvas-pane table\">",
        "<div class=\"crt-canvas-metric-grid\">",
        "<div class=\"crt-canvas-metric\"><strong>04</strong><span>signals</span></div>",
        "<div class=\"crt-canvas-metric\"><strong>2</strong><span>files</span></div>",
        "<div class=\"crt-canvas-metric\"><strong>91%</strong><span>ready</span></div>",
        "</div>",
        "<div class=\"crt-canvas-rows\">",
        "<div class=\"crt-canvas-row\"><strong>Context packet</strong><span>sent</span></div>",
        "<div class=\"crt-canvas-row\"><strong>Workspace write lease</strong><span>pending</span></div>",
        "<div class=\"crt-canvas-row\"><strong>HTML artifact preview</strong><span>ready</span></div>",
        "</div>",
        "</div>",
      ].join("");
    }

    function codePane() {
      const lines = [
        ["context", "128", " ", "const run = await provider.open(task)"],
        ["remove", "129", "-", "renderRawToolEvents(events)"],
        ["add", "129", "+", "renderWorkSignals(compact(events))"],
        ["add", "130", "+", "await permissionBroker.request(scope)"],
        ["context", "131", " ", "return createArtifactPreview(run)"],
      ];
      return [
        "<div class=\"crt-canvas-pane code\">",
        "<div class=\"crt-canvas-code-head\"><span>work-surface.ts</span><span>+2 / -1</span></div>",
        "<div class=\"crt-canvas-code-list\">",
        lines.map((line) => "<div class=\"crt-canvas-code-line " + line[0] + "\"><span>" + line[1] + "</span><span>" + line[2] + "</span><b>" + escapeHtml(line[3]) + "</b></div>").join(""),
        "</div>",
        "</div>",
      ].join("");
    }

    function taskNeedsAttention(item) {
      const value = String(item && item.attention || "").toLowerCase();
      return !!value && !["none", "clear", "resolved", "dismissed"].includes(value);
    }

    function taskAttentionLabel(item) {
      const value = String(item && item.attention || "").toLowerCase();
      if (value === "permission") return "Approval required";
      if (value === "input") return "Input required";
      if (value === "conflict") return "Resolve conflict";
      if (value === "review") return "Review ready";
      if (value === "error") return "Inspect failure";
      return taskNeedsAttention(item) ? "Action required" : "";
    }

    function taskIsRunning(item) {
      return ["queued", "running", "active", "working"].includes(String(item && item.execution || "").toLowerCase());
    }

    function taskSilenceLabel(item) {
      const seconds = nonNegativeCount(item && item.silentForSeconds);
      if (seconds < 60) return seconds + "s";
      const minutes = Math.floor(seconds / 60);
      return minutes + "m" + (seconds % 60 ? " " + (seconds % 60) + "s" : "");
    }

    function taskIsHistory(item) {
      return ["accepted", "archived", "closed"].includes(String(item && item.state || "").toLowerCase());
    }

    function taskBelongsToCurrentSession(item, dock) {
      const currentSessionId = String(dock && dock.currentSessionId || "");
      if (!currentSessionId) return !taskIsHistory(item);
      return String(item && item.sessionId || "") === currentSessionId;
    }

    function taskMatchesFilter(item, filter, dock) {
      if (filter === "history") return !taskBelongsToCurrentSession(item, dock);
      return taskBelongsToCurrentSession(item, dock);
    }

    function taskDockPane() {
      const dock = state.taskDock;
      if (!dock) return "";
      const canvasWorkItemId = String(state.workContext && state.workContext.workItemId || "");
      const selectedId = String(dock.selectedWorkItemId || canvasWorkItemId);
      const allItems = Array.isArray(dock.items) ? dock.items : [];
      const projects = Array.isArray(dock.projects) ? dock.projects : [];
      const filter = ["current", "history", "projects"].includes(state.taskFilter) ? state.taskFilter : "current";
      const items = filter === "projects" ? [] : allItems.filter((item) => taskMatchesFilter(item, filter, dock));
      const selectedItem = selectedId ? allItems.find((item) => item.id === selectedId) : null;
      if (filter !== "projects" && selectedItem && !items.some((item) => item.id === selectedId)) {
        items.unshift(selectedItem);
      }
      const itemHtml = items.map((item) => {
        const selected = item.id === selectedId;
        const outsideFilter = selected && !taskMatchesFilter(item, filter, dock);
        const running = taskIsRunning(item);
        const stalled = item.liveness === "stalled";
        const cancelPending = item.liveness === "cancel_pending";
        const history = !taskBelongsToCurrentSession(item, dock);
        const attention = taskNeedsAttention(item);
        const categories = [];
        if (outsideFilter) categories.push("Viewing outside filter");
        else if (selected) categories.push("Viewing");
        if (stalled) categories.push("Stalled " + taskSilenceLabel(item));
        else if (cancelPending) categories.push("Stopping");
        else if (running) categories.push("Running");
        if (attention) categories.push(taskAttentionLabel(item));
        if (history) categories.push("History");
        if (!categories.length) categories.push("Current");
        const category = categories.join(" + ");
        const stateParts = [stalled ? "stalled" : (cancelPending ? "stopping" : item.execution), item.completion]
          .filter((value, index, values) => value && values.indexOf(value) === index);
        const stateLabel = stateParts.join(" + ") || "open";
        const detailParts = [];
        if (item.workspaceLabel) detailParts.push(item.workspaceLabel);
        else if (item.updatedAt) detailParts.push(item.updatedAt);
        if (item.canResume) detailParts.push("interrupted run can be resumed");
        else if (item.canRetry) detailParts.push("failed run can be retried");
        if (stalled && item.probeStatus) detailParts.push("Provider reports " + item.probeStatus);
        const classes = [
          "crt-canvas-task-item",
          selected ? "active" : "",
          running ? "is-running" : "",
          stalled || cancelPending ? "is-stalled" : "",
          attention ? "needs-attention" : "",
          history ? "is-complete" : "",
        ].filter(Boolean).join(" ");
        return [
          "<div class=\"crt-canvas-task-card\">",
          "<button type=\"button\" class=\"" + classes + "\" data-work-item-id=\"" + escapeAttr(item.id) + "\" aria-current=\"" + (selected ? "true" : "false") + "\" title=\"" + escapeAttr(item.title + (item.workspaceLabel ? " - " + item.workspaceLabel : "")) + "\">",
          "<i class=\"crt-canvas-task-dot\" aria-hidden=\"true\"></i>",
          "<span class=\"crt-canvas-task-copy\">",
          "<small>" + escapeHtml((item.projectName ? "Project · " + item.projectName : "Draft") + " / " + category + " / " + stateLabel) + "</small>",
          "<strong>" + escapeHtml(item.title) + "</strong>",
          "<em>" + escapeHtml(detailParts.join(" / ") || "workspace pending") + "</em>",
          "</span>",
          "</button>",
          taskDispositionToggleHtml(item, dock),
          "</div>",
        ].join("");
      }).join("");
      const projectHtml = projects.map((project) => {
        const counts = project.counts || {};
        const currentContext = project.id === String(dock.destinationProjectId || "");
        const detail = [
          (counts.running || 0) + " running",
          (counts.needsYou || 0) + " action required",
          ((counts.current || 0) + (counts.history || 0)) + " tasks",
        ].join(" · ");
        return [
          "<div class=\"crt-canvas-task-card\">",
          "<button type=\"button\" class=\"crt-canvas-task-item crt-canvas-project-item" + (currentContext ? " active" : "") + "\" data-conversation-open=\"project\" data-conversation-project-id=\"" + escapeAttr(project.id) + "\" title=\"Open " + escapeAttr(project.name) + " in the main Chat\">",
          "<i class=\"crt-canvas-task-dot\" aria-hidden=\"true\"></i>",
          "<span class=\"crt-canvas-task-copy\">",
          "<small>" + (currentContext ? "Project / current Chat" : "Project / persistent context") + "</small>",
          "<strong>" + escapeHtml(project.name) + "</strong>",
          "<em>" + escapeHtml(project.latestTaskTitle || detail) + "</em>",
          "</span>",
          "<b>Open in Chat</b>",
          "</button>",
          "</div>",
        ].join("");
      }).join("");
      const workspaceFocusMode = dock.workspaceFocusMode === "pinned" ? "pinned" : "auto";
      const workspaceFocusLocked = workspaceFocusMode === "pinned";
      const workspaceFocusPath = String(dock.workspaceFocusPath || "");
      const selectedAction = selectedItem
        ? (selectedItem.canResume ? "resume" : selectedItem.canRetry ? "retry" : "")
        : "";
      const selectedActionTitle = selectedAction === "resume"
        ? "Resume the interrupted run from its checkpoint"
        : selectedAction === "retry"
          ? (selectedItem.retryAuthorizationRequestId
            ? "Explicitly authorize the denied operation for one new attempt and retry"
            : "Retry the failed run with the same instruction")
          : "";
      const controlButtons = [];
      if (filter !== "projects" && selectedAction && selectedItem.attemptId) {
        controlButtons.push(
          "<button type=\"button\" data-work-execution-action=\"" + selectedAction + "\" data-work-action-item-id=\"" + escapeAttr(selectedItem.id) + "\" data-work-action-attempt-id=\"" + escapeAttr(selectedItem.attemptId) + "\" data-work-authorization-request-id=\"" + escapeAttr(selectedItem.retryAuthorizationRequestId || "") + "\" title=\"" + escapeAttr(selectedActionTitle) + "\"" + (state.workExecutionSubmitting ? " disabled" : "") + ">" + escapeHtml(selectedAction === "resume" ? "Resume" : (selectedItem.retryAuthorizationRequestId ? "Authorize & Retry" : "Retry")) + "</button>"
        );
      }
      const filterCounts = {
        current: allItems.filter((item) => taskMatchesFilter(item, "current", dock)).length,
        history: allItems.filter((item) => taskMatchesFilter(item, "history", dock)).length,
        projects: projects.length,
      };
      const filterHtml = [
        ["current", ui("current")],
        ["projects", ui("projects")],
        ["history", ui("history")],
      ].map((entry) => {
        const key = entry[0];
        const label = entry[1];
        return "<button type=\"button\" data-work-filter=\"" + key + "\" class=\"" + (filter === key ? "active" : "") + "\" aria-pressed=\"" + (filter === key ? "true" : "false") + "\">" + label + " " + filterCounts[key] + "</button>";
      }).join("");
      const workspaceFocusName = workspaceDirName(workspaceFocusPath);
      const controlHtml = controlButtons.length
        ? "<div class=\"crt-canvas-task-control\">" + controlButtons.join("") + "</div>"
        : "";
      // Where the next unnamed instruction goes. Voice sets a project; the
      // one recovery action returns to Drafts if that voice switch was wrong.
      const destination = String(dock.destinationLabel || "");
      const destinationExitHtml = !workspaceFocusLocked && destination
        ? "<button type=\"button\" data-work-destination-action=\"exit-project\" title=\"Return this conversation to Drafts without changing task history\">Exit project</button>"
        : "";
      const focusHeadHtml = workspaceFocusLocked
        ? [
          "<div class=\"crt-canvas-task-focus\" role=\"group\" aria-label=\"Workspace routing lock\">",
          "<span class=\"is-locked\" title=\"" + escapeAttr("Workspace locked: " + (workspaceFocusPath || "current workspace") + " - browsing task history does not change it.") + "\">Locked &middot; " + escapeHtml(workspaceFocusName) + "</span>",
          "<button type=\"button\" data-work-focus-mode=\"auto\" title=\"Return to automatic workspace routing\">Unlock</button>",
          "</div>",
        ].join("")
        : (destinationExitHtml
          ? "<div class=\"crt-canvas-task-focus\" role=\"group\" aria-label=\"Current work destination\">" + destinationExitHtml + "</div>"
          : "");
      const projectedDestinationError = dock.destinationFeedback
        && dock.destinationFeedback.status === "rejected"
        ? String(dock.destinationFeedback.message || "")
        : "";
      const railExpanded = state.taskRailExpanded === true;
      const railEntries = filter === "projects" ? projects : items;
      const railToggleHtml = railEntries.length
        ? "<button type=\"button\" class=\"crt-canvas-task-rail-toggle\" data-action=\"toggle-task-rail\" aria-expanded=\"" + (railExpanded ? "true" : "false") + "\" aria-label=\"" + (railExpanded ? "Collapse task list" : "Expand task list") + "\" title=\"" + (railExpanded ? "Collapse task list" : "Expand task list") + "\">" + (railExpanded ? "&#9662;" : "&#9656;") + "</button>"
        : "";
      let railHtml = "";
      if (railExpanded) {
        railHtml = [
          "<div class=\"crt-canvas-task-railrow\">",
          "<div class=\"crt-canvas-task-rail\">" + (filter === "projects" ? projectHtml : itemHtml) + "</div>",
          controlHtml,
          "</div>",
        ].join("");
      } else if (selectedItem) {
        const selectedHistory = !taskBelongsToCurrentSession(selectedItem, dock);
        const stripClasses = [
          "crt-canvas-task-strip",
          taskIsRunning(selectedItem) ? "is-running" : "",
          ["stalled", "cancel_pending"].includes(selectedItem.liveness) ? "is-stalled" : "",
          taskNeedsAttention(selectedItem) ? "needs-attention" : "",
          selectedHistory || taskIsHistory(selectedItem) ? "is-complete" : "",
        ].filter(Boolean).join(" ");
        const selectedLiveness = selectedItem.liveness === "stalled"
          ? "stalled " + taskSilenceLabel(selectedItem)
          : (selectedItem.liveness === "cancel_pending" ? "stopping" : selectedItem.execution);
        const stripState = [selectedLiveness, selectedItem.completion]
          .filter((value, index, values) => value && values.indexOf(value) === index)
          .join(" + ") || "open";
        railHtml = [
          "<div class=\"crt-canvas-task-striprow\">",
          "<button type=\"button\" class=\"" + stripClasses + "\" data-action=\"toggle-task-rail\" title=\"" + escapeAttr(selectedItem.title + " - expand the task list") + "\">",
          "<i class=\"crt-canvas-task-dot\" aria-hidden=\"true\"></i>",
          "<span>" + escapeHtml(selectedItem.title) + "</span>",
          "<em>" + escapeHtml(stripState) + "</em>",
          "</button>",
          taskDispositionToggleHtml(selectedItem, dock),
          controlHtml,
          "</div>",
        ].join("");
      }
      return [
        "<section class=\"crt-canvas-task-dock\" aria-label=\"Task history and focus\">",
        "<div class=\"crt-canvas-task-dock-head\">",
        "<div class=\"crt-canvas-task-filters\" role=\"group\" aria-label=\"Task list filter\">" + filterHtml + "</div>",
        focusHeadHtml,
        railToggleHtml,
        "</div>",
        (state.workActionError || projectedDestinationError) ? "<p class=\"crt-canvas-task-action-error\" role=\"alert\">" + escapeHtml(state.workActionError || projectedDestinationError) + "</p>" : "",
        railHtml,
        taskDispositionOverlayHtml(items, dock),
        "</section>",
      ].join("");
    }

    function handleTaskFilter(button) {
      const filter = String(button && button.getAttribute("data-work-filter") || "").toLowerCase();
      if (!["current", "projects", "history"].includes(filter)) return;
      state.taskFilter = filter;
      if (state.taskRailExpanded !== true) {
        state.taskRailExpanded = true;
      }
      state.workDispositionMenu = "";
      render();
    }

    function canvasModeTabs() {
      if (!state.diff || !state.markdown) return "";
      return [
        "<nav class=\"crt-canvas-mode-tabs\" aria-label=\"Report and diff views\">",
        "<button type=\"button\" data-action=\"show-report\" class=\"" + (state.mode === "markdown" ? "active" : "") + "\">" + escapeHtml(ui("report")) + "</button>",
        "<button type=\"button\" data-action=\"show-diff\" class=\"" + (state.mode === "diff" ? "active" : "") + "\">" + escapeHtml(ui("diff")) + "</button>",
        "</nav>",
      ].join("");
    }

    function rememberCurrentView() {
      const snapshot = {
        phase: state.phase,
        title: state.title,
        lead: state.lead,
        progress: state.progress,
        preset: state.preset,
        size: { width: state.size.width, height: state.size.height },
      };
      if (state.mode === "markdown" && state.markdown) state.reportView = snapshot;
      if (state.mode === "diff" && state.diff) state.diffView = snapshot;
    }

    function switchCanvasMode(mode) {
      rememberCurrentView();
      state.mode = mode;
      const snapshot = mode === "markdown" ? state.reportView : (mode === "diff" ? state.diffView : null);
      if (snapshot) {
        state.phase = snapshot.phase || state.phase;
        state.title = snapshot.title || state.title;
        state.lead = snapshot.lead || state.lead;
        if (Number.isFinite(Number(snapshot.progress))) state.progress = Number(snapshot.progress);
        if (snapshot.preset) state.preset = snapshot.preset;
        if (snapshot.size && Number.isFinite(Number(snapshot.size.width)) && Number.isFinite(Number(snapshot.size.height))) {
          state.size = clampSize({ width: Number(snapshot.size.width), height: Number(snapshot.size.height) });
        }
      }
      render();
    }

    function firstDiffText(values) {
      for (const value of values) {
        if (typeof value === "string" && value.trim()) return value.trim();
        if (value && typeof value === "object" && !Array.isArray(value)) {
          const nested = [value.message, value.reason, value.summary, value.detail]
            .find((item) => typeof item === "string" && item.trim());
          if (nested) return nested.trim();
        }
      }
      return "";
    }

    function pendingExternalExport(diff, reasonText) {
      const directFlags = [diff.pendingExport, diff.pending_export, diff.externalExportPending, diff.external_export_pending];
      if (directFlags.some((value) => {
        if (value === true) return true;
        if (typeof value === "string") return ["pending", "required", "waiting"].includes(value.toLowerCase());
        if (Array.isArray(value)) return value.length > 0;
        if (value && typeof value === "object") {
          const status = String(value.status || value.state || "pending").toLowerCase();
          return ["pending", "required", "waiting"].includes(status);
        }
        return false;
      })) return true;
      const collections = [
        diff.pendingExports,
        diff.pending_exports,
        diff.externalArtifacts,
        diff.external_artifacts,
        diff.artifacts,
      ];
      if (collections.some((items) => Array.isArray(items) && items.some((item) => {
        if (!item || typeof item !== "object") return false;
        const status = String(item.status || item.state || "").toLowerCase();
        const location = String(item.location || item.scope || "").toLowerCase();
        return status === "pending" && ["external", "export", "outside_workspace"].includes(location);
      }))) return true;
      const normalized = String(reasonText || "").toLowerCase();
      const external = normalized.includes("external")
        || normalized.includes("outside workspace")
        || normalized.includes("outside the workspace")
        || normalized.includes("desktop");
      const pending = normalized.includes("pending")
        || normalized.includes("approval")
        || normalized.includes("approve")
        || normalized.includes("permission");
      return external && pending;
    }

    function diffEmptyDetails(diff) {
      const reason = firstDiffText([
        diff.reason,
        diff.blockedReason,
        diff.blocked_reason,
        diff.blocked,
        diff.message,
      ]);
      const reasonCode = firstDiffText([
        diff.reasonCode,
        diff.reason_code,
        diff.reason && typeof diff.reason === "object" ? diff.reason.code : "",
        typeof diff.reason === "string" ? diff.reason : "",
      ]).toLowerCase();
      const reasonState = {
        staged_export_missing: {
          title: "Staged deliverable missing",
          message: "The provider did not create the requested staged deliverable, so there is no export diff to review.",
        },
        staged_export_unverified: {
          title: "Staged deliverable unverified",
          message: "The provider attempt did not finish successfully, so its staged files cannot be offered for approval.",
        },
        export_discovery_error: {
          title: "Export inspection failed",
          message: "Amadeus could not safely inspect the staged deliverable. No export approval was created.",
        },
        external_export_failed: {
          title: "Desktop export failed",
          message: "The authorized Desktop copy failed safely without overwriting an existing target.",
        },
        external_export_recovery_required: {
          title: "Export recovery required",
          message: "The Desktop export was authorized but interrupted before every approved file was verified.",
        },
        external_export_drift: {
          title: "Desktop export changed",
          message: "The committed Desktop output is missing or no longer matches the approved artifact.",
        },
        external_export_denied: {
          title: "Desktop export denied",
          message: "The Desktop export was declined and no target file was created.",
        },
        external_export_expired: {
          title: "Desktop export expired",
          message: "The Desktop export approval expired and no target file was created.",
        },
      }[reasonCode];
      if (reasonState) {
        return {
          title: reasonState.title,
          message: reason && reason.toLowerCase() !== reasonCode ? reason : reasonState.message,
        };
      }
      if (pendingExternalExport(diff, reason)) {
        return {
          title: "External export pending",
          message: reason || "The requested output is outside the workspace and still needs approval. There is no workspace diff yet.",
        };
      }
      if (
        diff.available === false
        || diff.diffAvailable === false
        || diff.diff_available === false
        || reasonCode === "attempt_diff_unavailable"
        || reasonCode.includes("diff unavailable")
      ) {
        return {
          title: "Attempt diff unavailable",
          message: reasonCode === "attempt_diff_unavailable"
            ? "This attempt has no persistent Git baseline, so its historical diff cannot be reconstructed."
            : (reason || "This attempt has no persisted diff evidence."),
        };
      }
      if (diff.blocked === true || (diff.blocked && typeof diff.blocked === "object") || reasonCode.includes("blocked")) {
        return {
          title: "Diff blocked",
          message: reason || "The attempt is blocked before workspace changes can be reviewed.",
        };
      }
      return {
        title: "No workspace changes",
        message: reason || "This attempt did not change files inside the workspace.",
      };
    }

    function diffPane() {
      const diff = state.diff && typeof state.diff === "object" ? state.diff : {};
      const files = Array.isArray(diff.files) ? diff.files.filter((item) => item && typeof item === "object") : [];
      if (!files.length) {
        const empty = diffEmptyDetails(diff);
        const hidden = Array.isArray(diff.hiddenBaselineFiles) ? diff.hiddenBaselineFiles.filter(Boolean) : [];
        const hiddenText = hidden.length
          ? "<p>Pre-run changes hidden: " + escapeHtml(hidden.slice(0, 8).join(", ")) + "</p>"
          : "";
        return [
          "<div class=\"crt-canvas-pane diff\">",
          "<div class=\"crt-canvas-diff-empty\">",
          "<strong>" + escapeHtml(empty.title) + "</strong>",
          "<p>" + escapeHtml(empty.message) + "</p>",
          hiddenText,
          "</div>",
          "</div>",
        ].join("");
      }

      state.activeDiffFile = Math.max(0, Math.min(files.length - 1, Number(state.activeDiffFile) || 0));
      const active = files[state.activeDiffFile];
      const additions = Number(diff.additions || 0);
      const deletions = Number(diff.deletions || 0);
      const fileButtons = files.map((file, index) => {
        const path = String(file.path || file.newPath || file.oldPath || "unknown");
        const status = String(file.status || "modified");
        return [
          "<button type=\"button\" class=\"crt-canvas-diff-file " + (index === state.activeDiffFile ? "active" : "") + "\" data-diff-file-index=\"" + index + "\" title=\"" + escapeAttr(path) + "\">",
          "<span>" + escapeHtml(path) + "</span>",
          "<small>" + escapeHtml(status) + " <b>+" + Number(file.additions || 0) + "</b> <i>-" + Number(file.deletions || 0) + "</i></small>",
          "</button>",
        ].join("");
      }).join("");

      const hunks = Array.isArray(active.hunks) ? active.hunks : [];
      const hunkHtml = hunks.length
        ? hunks.map((hunk) => {
            const lines = Array.isArray(hunk.lines) ? hunk.lines : [];
            const lineHtml = lines.map((line) => {
              const kind = ["add", "remove", "context", "meta"].includes(String(line.kind)) ? String(line.kind) : "context";
              if (kind === "meta") {
                return "<div class=\"crt-canvas-diff-line meta\">" + escapeHtml(String(line.text || "")) + "</div>";
              }
              const oldNumber = Number.isFinite(Number(line.oldLine)) ? String(line.oldLine) : "";
              const newNumber = Number.isFinite(Number(line.newLine)) ? String(line.newLine) : "";
              const prefix = kind === "add" ? "+" : (kind === "remove" ? "-" : " ");
              return [
                "<div class=\"crt-canvas-diff-line " + kind + "\">",
                "<span>" + oldNumber + "</span>",
                "<span>" + newNumber + "</span>",
                "<i>" + prefix + "</i>",
                "<code>" + escapeHtml(String(line.text || "")) + "</code>",
                "</div>",
              ].join("");
            }).join("");
            return [
              "<section class=\"crt-canvas-diff-hunk\">",
              "<header>" + escapeHtml(String(hunk.header || "@@")) + "</header>",
              lineHtml,
              "</section>",
            ].join("");
          }).join("")
        : "<div class=\"crt-canvas-diff-empty\"><strong>No inline patch</strong><p>This file is changed or untracked, but Git did not return displayable hunks.</p></div>";

      return [
        "<div class=\"crt-canvas-pane diff\">",
        "<aside class=\"crt-canvas-diff-files\">",
        "<div class=\"crt-canvas-diff-summary\"><strong>" + files.length + " file" + (files.length === 1 ? "" : "s") + "</strong><span>+" + additions + " / -" + deletions + (diff.truncated ? " / clipped" : "") + "</span></div>",
        fileButtons,
        "</aside>",
        "<div class=\"crt-canvas-diff-content\">",
        "<div class=\"crt-canvas-diff-file-head\"><span>" + escapeHtml(String(active.path || active.newPath || active.oldPath || "unknown")) + "</span><span>+" + Number(active.additions || 0) + " / -" + Number(active.deletions || 0) + "</span></div>",
        hunkHtml,
        "</div>",
        "</div>",
      ].join("");
    }

    function permissionPane() {
      if (!state.permissionVisible) return "";
      const request = state.permissionRequest || {
        id: "",
        capability: "permission",
        action: "scoped_action",
        scope: [],
        reason: "A scoped operation needs your approval.",
        reversibility: "Unknown reversibility",
        options: ["deny"],
        retryRequired: false,
        diagnosticOnly: false,
      };
      const optionSet = Array.isArray(request.options) ? request.options : [];
      const diagnosticOnly = request.diagnosticOnly === true;
      const targetCount = request.scope.length;
      const scopes = targetCount
        ? (optionSet.includes("allow_once")
            ? "<p class=\"crt-canvas-permission-scope-note\">Allow once covers all " + targetCount + " listed target" + (targetCount === 1 ? "" : "s") + ".</p>"
            : "<p class=\"crt-canvas-permission-scope-note\">This request covers all " + targetCount + " listed target" + (targetCount === 1 ? "" : "s") + ".</p>")
          + "<ul aria-label=\"Exact permission targets\">" + request.scope.map((path) => "<li><code>" + escapeHtml(path) + "</code></li>").join("") + "</ul>"
        : "<p class=\"crt-canvas-permission-scope-note\">The provider did not report exact path targets. No path scope can be verified from this request.</p>";
      const binaryPreviews = request.previewComplete === true && Array.isArray(request.previews)
        ? request.previews
        : [];
      const previewRows = binaryPreviews.length
        ? "<p>Binary files are approved by immutable identity; their bytes are rechecked before publication.</p>"
          + "<ul aria-label=\"Binary export identities\">"
          + binaryPreviews.map((preview) => [
            "<li><code>", escapeHtml(preview.path), "</code><br>",
            escapeHtml(preview.mediaType || "application/octet-stream"), " · ",
            escapeHtml(String(preview.sizeBytes || 0)), " bytes · SHA-256 <code>",
            escapeHtml(preview.sha256), "</code></li>",
          ].join("")).join("")
          + "</ul>"
        : "";
      const buttons = [];
      if (optionSet.includes("allow_once")) {
        buttons.push("<button type=\"button\" data-permission-action=\"allow_once\"" + (state.permissionSubmitting ? " disabled" : "") + ">" + escapeHtml(ui("allowOnce")) + "</button>");
      }
      if (optionSet.includes("deny")) {
        buttons.push("<button type=\"button\" data-permission-action=\"deny\"" + (state.permissionSubmitting ? " disabled" : "") + ">" + escapeHtml(diagnosticOnly ? ui("dismiss") : ui("deny")) + "</button>");
      }
      if (optionSet.includes("retry_export")) {
        buttons.push("<button type=\"button\" data-permission-action=\"retry_export\"" + (state.permissionSubmitting ? " disabled" : "") + ">Retry approved export</button>");
      }
      if (optionSet.includes("abandon_export")) {
        buttons.push("<button type=\"button\" data-permission-action=\"abandon_export\"" + (state.permissionSubmitting ? " disabled" : "") + ">Abandon export</button>");
      }
      return [
        "<section class=\"crt-canvas-permission\">",
        "<span>" + escapeHtml(optionSet.includes("retry_export") || optionSet.includes("abandon_export") ? ui("recovery") : (diagnosticOnly ? ui("providerBlocked") : ui("permission"))) + "</span>",
        "<strong>" + escapeHtml(request.capability + " / " + request.action) + "</strong>",
        "<p>" + escapeHtml(request.reason) + "</p>",
        scopes,
        previewRows,
        "<p>" + escapeHtml(request.reversibility) + (diagnosticOnly ? " This run cannot be approved in place; dismiss it. If more work is needed, describe the next task to Amadeus." : (request.retryRequired ? " A new provider run is required after approval." : "")) + "</p>",
        state.permissionError ? "<p class=\"crt-canvas-permission-error\">" + escapeHtml(state.permissionError) + "</p>" : "",
        "<div>" + buttons.join("") + "</div>",
        "</section>",
      ].join("");
    }

    async function handlePermissionAction(button) {
      const action = String(button && button.getAttribute("data-permission-action") || "");
      const request = state.permissionRequest;
      if (!request || !request.id || !["allow_once", "deny", "retry_export", "abandon_export"].includes(action)) {
        state.permissionError = "Permission request is incomplete. Refresh the task card and try again.";
        render();
        return;
      }
      if (!Array.isArray(request.options) || !request.options.includes(action)) return;
      state.permissionSubmitting = true;
      state.permissionError = "";
      render();
      try {
        await postCanvasAction("permission", action, workItemActionPayload({
          permission_request_id: request.id,
          work_item_id: request.workItemId || String(state.workContext && state.workContext.workItemId || ""),
          attempt_id: request.attemptId || String(state.workContext && state.workContext.attemptId || ""),
        }));
        if (state.permissionRequest && state.permissionRequest.id === request.id) {
          state.permissionVisible = false;
          state.permissionRequest = null;
        }
        state.permissionSubmitting = false;
        render();
      } catch (err) {
        state.permissionSubmitting = false;
        const errorCode = String(err && err.code || "");
        const staleRequest = new Set([
          "stale_revision",
          "permission_request_not_current",
          "permission_request_not_pending",
          "permission_attempt_not_selected",
          "permission_work_item_not_selected",
        ]).has(errorCode);
        if (
          staleRequest
          && state.permissionRequest
          && state.permissionRequest.id === request.id
        ) {
          state.permissionVisible = false;
          state.permissionRequest = null;
          state.permissionError = "";
        } else {
          state.permissionError = String(err && err.message || err || "Permission action failed");
        }
        render();
        if (typeof window.__weBridgeLog === "function") {
          window.__weBridgeLog("canvas.permission_action_failed", {
            action,
            permission_request_id: request.id,
            error: String(err && (err.stack || err)),
          }, "warning");
        }
      }
    }

    function attentionPane() {
      const request = state.attentionRequest;
      if (!request && !state.attentionError) return "";
      const options = request ? request.options.map((option) => {
        const kind = option.entityKind === "project"
          ? "PROJECT"
          : (option.entityKind === "work_item" ? "WORKITEM" : "CHOICE");
        const detail = [option.parentLabel, option.description].filter(Boolean).join(" · ");
        return [
          "<button type=\"button\" class=\"crt-canvas-attention-option\" data-attention-option-id=\"" + escapeAttr(option.id) + "\"" + (state.attentionSubmitting ? " disabled" : "") + ">",
          "<span>" + escapeHtml(kind) + "</span>",
          "<strong>" + escapeHtml(option.label) + "</strong>",
          detail ? "<small>" + escapeHtml(detail) + "</small>" : "<small>&nbsp;</small>",
          "</button>",
        ].join("");
      }).join("") : "";
      return [
        "<section class=\"crt-canvas-attention\" role=\"dialog\" aria-labelledby=\"crt-canvas-attention-title\">",
        "<span>" + escapeHtml(ui("choice")) + "</span>",
        "<h3 id=\"crt-canvas-attention-title\">" + escapeHtml(request ? request.title : ui("selectionFailed")) + "</h3>",
        request && request.prompt ? "<p>" + escapeHtml(request.prompt) + "</p>" : "",
        state.attentionError ? "<p class=\"crt-canvas-attention-error\">" + escapeHtml(state.attentionError) + "</p>" : "",
        options ? "<div class=\"crt-canvas-attention-options\">" + options + "</div>" : "",
        "</section>",
      ].join("");
    }

    function reportAttentionPresented(request) {
      if (!request || !request.id || state.attentionPresentedId === request.id) return;
      state.attentionPresentedId = request.id;
      window.requestAnimationFrame(() => {
        const current = state.attentionRequest;
        if (!current || current.id !== request.id || !state.expanded) return;
        void postCanvasAction("attention", "presented", {
          request_id: request.id,
        }).catch((err) => {
          if (state.attentionPresentedId === request.id) state.attentionPresentedId = "";
          if (typeof window.__weBridgeLog === "function") {
            window.__weBridgeLog("canvas.attention_receipt_failed", {
              request_id: request.id,
              error: String(err && (err.stack || err)),
            }, "warning");
          }
        });
      });
    }

    function applyAttentionEnvelope(value, reportPresentation) {
      const request = attentionRequestFromEnvelope(value);
      const previousId = String(state.attentionRequest && state.attentionRequest.id || "");
      state.attentionRequest = request;
      state.attentionSubmitting = false;
      state.attentionError = "";
      if (!request) {
        state.attentionPresentedId = "";
      } else {
        state.hasContent = true;
        state.expanded = true;
        if (request.id !== previousId) state.attentionPresentedId = "";
      }
      render();
      if (request && reportPresentation !== false) reportAttentionPresented(request);
    }

    async function handleAttentionOption(button) {
      const request = state.attentionRequest;
      const optionId = String(button && button.getAttribute("data-attention-option-id") || "");
      if (state.attentionSubmitting || !request || !request.id || !optionId) return;
      if (!request.options.some((option) => option.id === optionId)) return;
      state.attentionSubmitting = true;
      state.attentionError = "";
      render();
      try {
        const response = await postCanvasAction("attention", "resolve", {
          request_id: request.id,
          option_id: optionId,
        });
        applyAttentionEnvelope(
          { requests: Array.isArray(response.requests) ? response.requests : [] },
          true,
        );
      } catch (err) {
        state.attentionSubmitting = false;
        state.attentionError = String(err && err.message || err || "Attention action failed");
        render();
        if (typeof window.__weBridgeLog === "function") {
          window.__weBridgeLog("canvas.attention_action_failed", {
            request_id: request.id,
            option_id: optionId,
            error: String(err && (err.stack || err)),
          }, "warning");
        }
      }
    }

    function resetSelectedTaskContent() {
      state.mode = "workflow";
      state.phase = "Ready";
      state.title = ui("waitingTitle");
      state.lead = ui("waitingLead");
      state.progress = 0;
      state.markdown = "";
      state.diff = null;
      state.activeDiffFile = 0;
      state.reportView = null;
      state.diffView = null;
      state.html = "";
      state.url = "";
      state.browserSessionId = "";
      state.pageTitle = "";
      state.excerpt = "";
      state.screenshot = "";
      state.links = [];
      state.actions = [];
      state.permissionVisible = false;
      state.permissionRequest = null;
      state.permissionSubmitting = false;
      state.workExecutionSubmitting = false;
      state.workPreviewSubmitting = false;
      state.workDispositionSubmitting = false;
      state.workDispositionMenu = "";
      state.workActionError = "";
      state.permissionError = "";
      state.signals = [];
      state.presentationKey = "";
    }

    function render() {
      root.classList.toggle("expanded", state.expanded);
      root.classList.toggle("has-content", state.hasContent);
      dot.setAttribute("aria-label", state.expanded ? ui("fold") : ui("expand"));
      updateStatus();
      if (!state.expanded) {
        if (renderedCardHtml) {
          card.innerHTML = "";
          renderedCardHtml = "";
        }
        return;
      }

      const body = state.mode === "markdown"
        ? markdownPane()
        : state.mode === "diff"
          ? diffPane()
        : state.mode === "browser"
          ? browserPane()
          : state.mode === "html"
            ? htmlPane()
            : state.mode === "image"
              ? imagePane()
              : state.mode === "table"
                ? tablePane()
                : state.mode === "code"
                  ? codePane()
                  : workflowPane();

      const nextCardHtml = [
        "<div class=\"crt-canvas-semantic-header\"><span>" + escapeHtml(surfaceKicker()) + "</span><strong>" + escapeHtml(surfaceTitle()) + "</strong></div>",
        "<div class=\"crt-canvas-actions crt-canvas-overlay-controls\">",
        workPreviewLaunchButton(),
        "<button type=\"button\" data-action=\"preset\" aria-label=\"Toggle canvas size\">[]</button>",
        "<button type=\"button\" data-action=\"fold\" aria-label=\"Fold canvas\">&times;</button>",
        "</div>",
        taskDockPane(),
        canvasModeTabs(),
        body,
        permissionPane(),
        attentionPane(),
      ].join("");
      const cardChanged = nextCardHtml !== renderedCardHtml;
      if (cardChanged) {
        card.innerHTML = nextCardHtml;
        renderedCardHtml = nextCardHtml;
      }
      applySize();
      positionWorkDispositionMenu();
      if (!cardChanged) return;

      card.querySelectorAll("[data-action=\"show-report\"]").forEach((button) => {
        button.addEventListener("click", () => {
          switchCanvasMode("markdown");
        });
      });
      card.querySelectorAll("[data-action=\"show-diff\"]").forEach((button) => {
        button.addEventListener("click", () => {
          switchCanvasMode("diff");
        });
      });
      card.querySelectorAll("[data-diff-file-index]").forEach((button) => {
        button.addEventListener("click", () => {
          state.activeDiffFile = Number(button.getAttribute("data-diff-file-index") || 0);
          render();
        });
      });
      card.querySelectorAll("[data-work-item-id]").forEach((button) => {
        button.addEventListener("click", () => {
          handleWorkItemSelect(button);
        });
      });
      card.querySelectorAll("[data-work-filter]").forEach((button) => {
        button.addEventListener("click", () => {
          handleTaskFilter(button);
        });
      });
      card.querySelectorAll("[data-action=\"toggle-task-rail\"]").forEach((button) => {
        button.addEventListener("click", () => {
          state.taskRailExpanded = state.taskRailExpanded !== true;
          render();
        });
      });
      card.querySelectorAll("[data-work-focus-mode]").forEach((button) => {
        button.addEventListener("click", () => {
          handleWorkItemFocus(button);
        });
      });
      card.querySelectorAll("[data-work-destination-action=\"exit-project\"]").forEach((button) => {
        button.addEventListener("click", () => {
          handleDestinationExit(button);
        });
      });
      card.querySelectorAll("[data-conversation-open]").forEach((button) => {
        button.addEventListener("click", () => {
          handleConversationOpen(button);
        });
      });
      card.querySelectorAll("[data-work-execution-action]").forEach((button) => {
        button.addEventListener("click", () => {
          handleWorkItemExecution(button);
        });
      });
      card.querySelectorAll("[data-work-preview-item-id]").forEach((button) => {
        button.addEventListener("click", () => {
          handleWorkItemPreview(button);
        });
      });
      card.querySelectorAll("[data-work-disposition-toggle]").forEach((button) => {
        button.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          const itemId = String(button.getAttribute("data-work-disposition-toggle") || "");
          state.workDispositionMenu = state.workDispositionMenu === itemId ? "" : itemId;
          render();
        });
      });
      card.querySelectorAll("[data-work-disposition-action]").forEach((button) => {
        button.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          handleWorkItemDisposition(button);
        });
      });
      card.querySelectorAll(".crt-canvas-task-rail").forEach((rail) => {
        rail.addEventListener("scroll", positionWorkDispositionMenu, { passive: true });
      });
      card.querySelectorAll("[data-action=\"preset\"]").forEach((button) => {
        button.addEventListener("click", () => {
          setPreset(state.preset === "compact" ? "wide" : state.preset === "wide" ? "full" : "compact");
        });
      });
      card.querySelectorAll("[data-action=\"fold\"]").forEach((button) => {
        button.addEventListener("click", () => {
          state.expanded = false;
          render();
        });
      });
    }

    card.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const attentionButton = target.closest("[data-attention-option-id]");
      if (attentionButton && card.contains(attentionButton)) {
        event.preventDefault();
        event.stopPropagation();
        handleAttentionOption(attentionButton);
        return;
      }
      const permissionButton = target.closest("[data-permission-action]");
      if (permissionButton && card.contains(permissionButton)) {
        event.preventDefault();
        event.stopPropagation();
        handlePermissionAction(permissionButton);
        return;
      }
      const fileButton = target.closest("[data-file-action]");
      if (fileButton && card.contains(fileButton)) {
        event.preventDefault();
        event.stopPropagation();
        closeCommandMenus();
        handleFileAction(fileButton);
        return;
      }
      const webButton = target.closest("[data-web-action]");
      if (webButton && card.contains(webButton)) {
        event.preventDefault();
        event.stopPropagation();
        handleWebAction(webButton);
        return;
      }
      const commandButton = target.closest("[data-command-action]");
      if (commandButton && card.contains(commandButton)) {
        event.preventDefault();
        event.stopPropagation();
        handleCommandAction(commandButton);
        return;
      }
      const providerButton = target.closest("[data-provider-action]");
      if (providerButton && card.contains(providerButton)) {
        event.preventDefault();
        event.stopPropagation();
        closeFileMenus();
        closeCommandMenus();
        handleProviderAction(providerButton);
      }
    });
    card.addEventListener("wheel", handleCanvasWheel, { passive: false });

    if (typeof document.addEventListener === "function") {
      document.addEventListener("click", (event) => {
        if (!state.workDispositionMenu) return;
        const target = event.target;
        if (target instanceof Element && target.closest(".crt-canvas-task-disposition")) return;
        state.workDispositionMenu = "";
        render();
      });
    }

    dot.addEventListener("click", () => {
      state.expanded = !state.expanded;
      if (state.expanded) state.hasContent = true;
      render();
    });
    status.addEventListener("click", () => {
      state.expanded = !state.expanded;
      if (state.expanded) state.hasContent = true;
      render();
    });
    window.setInterval(updateStatus, 30000);
    window.setInterval(() => {
      if (!state.demoEnabled) return;
      applyDemo(demoFrame(Date.now() - state.startedAt));
    }, 550);

    root.appendChild(dot);
    root.appendChild(status);
    root.appendChild(card);
    host.appendChild(root);
    if (typeof ResizeObserver === "function") {
      resizeObserver = new ResizeObserver(() => {
        if (!state.expanded) return;
        positionWorkDispositionMenu();
        const rect = card.getBoundingClientRect();
        if (!Number.isFinite(rect.width) || !Number.isFinite(rect.height)) return;
        if (Math.abs(rect.width - state.size.width) < 2 && Math.abs(rect.height - state.size.height) < 2) return;
        state.preset = "custom";
        state.size = clampSize({ width: rect.width, height: rect.height });
        savePreset();
        saveSize();
      });
      resizeObserver.observe(card);
    }
    render();

    return {
      layout(bounds) {
        if (!bounds) return;
        root.style.left = Math.round(bounds.x) + "px";
        root.style.top = Math.round(bounds.y) + "px";
        root.style.width = Math.round(bounds.width) + "px";
        root.style.height = Math.round(bounds.height) + "px";
        applySize();
      },
      setPresentation(profile) {
        const nextLocale = normalizePresentationLocale(
          profile && (profile.presentation_locale || profile.presentationLocale || profile.locale)
        );
        const defaultTitles = Object.values(presentationText).map((catalog) => catalog.waitingTitle);
        const defaultLeads = Object.values(presentationText).map((catalog) => catalog.waitingLead);
        const titleWasDefault = defaultTitles.includes(state.title);
        const leadWasDefault = defaultLeads.includes(state.lead);
        const changed = nextLocale !== state.presentationLocale;
        state.presentationLocale = nextLocale;
        document.documentElement.lang = nextLocale;
        if (titleWasDefault) state.title = ui("waitingTitle");
        if (leadWasDefault) state.lead = ui("waitingLead");
        dot.setAttribute("aria-label", state.expanded ? ui("fold") : ui("expand"));
        if (!changed && !titleWasDefault && !leadWasDefault) return;
        renderedCardHtml = "";
        render();
      },
      setAttention(payload) {
        applyAttentionEnvelope(payload || {}, true);
      },
      setPayload(payload) {
        const data = payload || {};
        rememberCurrentView();
        const hasWorkContext = own(data, "workContext");
        const hasTaskDock = own(data, "taskDock");
        const nextPresentationKey = presentationKey(data);
        let switchedWorkItem = false;
        let switchedAttempt = false;
        if (hasWorkContext) {
          const nextContext = normalizeWorkContext(data.workContext);
          const currentWorkItemId = String(state.workContext && state.workContext.workItemId || "");
          const nextWorkItemId = String(nextContext && nextContext.workItemId || "");
          const currentAttemptId = String(state.workContext && state.workContext.attemptId || "");
          const nextAttemptId = String(nextContext && nextContext.attemptId || "");
          switchedWorkItem = !!(currentWorkItemId !== nextWorkItemId && (currentWorkItemId || nextWorkItemId));
          switchedAttempt = !!(currentAttemptId !== nextAttemptId && (currentAttemptId || nextAttemptId));
          state.workContext = nextContext;
        }
        const samePresentation = !switchedWorkItem
          && !switchedAttempt
          && !!nextPresentationKey
          && nextPresentationKey === state.presentationKey;
        const changedPresentation = (hasTaskDock || hasWorkContext)
          && !!nextPresentationKey
          && !!state.presentationKey
          && nextPresentationKey !== state.presentationKey;
        const preservedView = samePresentation ? {
          mode: state.mode,
          activeDiffFile: state.activeDiffFile,
          phase: state.phase,
          title: state.title,
          lead: state.lead,
          progress: state.progress,
          reportView: state.reportView,
          diffView: state.diffView,
          preset: state.preset,
          size: { width: state.size.width, height: state.size.height },
        } : null;
        if (switchedWorkItem || switchedAttempt || changedPresentation) {
          resetSelectedTaskContent();
        }
        if (nextPresentationKey || switchedWorkItem || switchedAttempt) {
          state.presentationKey = nextPresentationKey;
        }
        if (hasTaskDock) {
          state.taskDock = normalizeTaskDock(data.taskDock);
          state.workActionError = "";
        }
        state.demoEnabled = false;
        if (lifecycleTimer) {
          window.clearTimeout(lifecycleTimer);
          lifecycleTimer = null;
        }
        if (data.clear === true || data.action === "clear") {
          state.hasContent = false;
          state.expanded = false;
          state.permissionVisible = false;
          state.permissionRequest = null;
          state.permissionSubmitting = false;
          state.workActionError = "";
          state.permissionError = "";
          state.mode = "workflow";
          state.phase = "Ready";
          state.title = ui("waitingTitle");
          state.lead = ui("waitingLead");
          state.progress = 0;
          state.markdown = "";
          state.diff = null;
          state.activeDiffFile = 0;
          state.reportView = null;
          state.diffView = null;
          state.html = "";
          state.url = "";
          state.browserSessionId = "";
          state.pageTitle = "";
          state.excerpt = "";
          state.screenshot = "";
          state.links = [];
          state.actions = [];
          state.workContext = null;
          state.taskDock = null;
          state.presentationKey = "";
          state.signals = [{ label: "idle", text: "No canvas artifact is active.", detail: "manual open" }];
          render();
          return;
        }
        const inferredMode = inferPayloadMode(data);
        if (inferredMode) state.mode = inferredMode;
        if (data.phase) state.phase = String(data.phase);
        if (data.title) state.title = normalizeProviderDisplayTitle(data.title);
        if (data.lead) state.lead = String(data.lead);
        if (typeof data.markdown === "string") {
          state.markdown = normalizeProviderDisplayMarkdown(data.markdown);
          if (inferredMode === "markdown") {
            state.diff = null;
            state.activeDiffFile = 0;
          }
        }
        else if (data.artifact && data.artifact.content && typeof data.artifact.content.markdown === "string") {
          state.markdown = normalizeProviderDisplayMarkdown(data.artifact.content.markdown);
        }
        if (typeof data.reportMarkdown === "string" && data.reportMarkdown) {
          state.markdown = normalizeProviderDisplayMarkdown(data.reportMarkdown);
        }
        if (data.reportView && typeof data.reportView === "object") {
          state.reportView = {
            ...(state.reportView || {}),
            phase: String(data.reportView.phase || "Result"),
            title: normalizeProviderDisplayTitle(data.reportView.title || "Provider result report"),
            lead: String(data.reportView.lead || ""),
            progress: Number.isFinite(Number(data.reportView.progress)) ? Number(data.reportView.progress) : 100,
          };
        }
        const structuredDiff = data.diff && typeof data.diff === "object"
          ? data.diff
          : (data.artifact && data.artifact.content && data.artifact.content.diff && typeof data.artifact.content.diff === "object"
            ? data.artifact.content.diff
            : null);
        if (structuredDiff) {
          const enrichedDiff = { ...structuredDiff };
          const contextSources = [
            data,
            data.metadata && typeof data.metadata === "object" ? data.metadata : null,
          ].filter(Boolean);
          const contextFields = [
            ["reason", ["reason"]],
            ["reasonCode", ["reasonCode", "reason_code"]],
            ["blocked", ["blocked"]],
            ["blockedReason", ["blockedReason", "blocked_reason"]],
            ["available", ["available", "diffAvailable", "diff_available"]],
            ["pendingExport", ["pendingExport", "pending_export", "externalExportPending", "external_export_pending"]],
            ["pendingExports", ["pendingExports", "pending_exports"]],
            ["externalArtifacts", ["externalArtifacts", "external_artifacts"]],
            ["artifacts", ["artifacts"]],
          ];
          contextFields.forEach((entry) => {
            const target = entry[0];
            if (own(enrichedDiff, target)) return;
            for (const source of contextSources) {
              const alias = entry[1].find((key) => own(source, key));
              if (alias) {
                enrichedDiff[target] = source[alias];
                break;
              }
            }
          });
          state.diff = enrichedDiff;
          state.activeDiffFile = 0;
        }
        if (typeof data.html === "string") state.html = data.html;
        if (typeof data.url === "string") state.url = data.url;
        if (typeof data.browserSessionId === "string") state.browserSessionId = data.browserSessionId;
        else if (typeof data.browser_session_id === "string") state.browserSessionId = data.browser_session_id;
        if (typeof data.pageTitle === "string") state.pageTitle = data.pageTitle;
        if (typeof data.excerpt === "string") state.excerpt = data.excerpt;
        if (typeof data.screenshot === "string") state.screenshot = data.screenshot;
        if (Array.isArray(data.links)) {
          state.links = data.links
            .filter((item) => item && typeof item.url === "string")
            .slice(0, 8)
            .map((item) => ({ url: String(item.url), title: String(item.title || item.label || "") }));
        }
        const actionRefs = Array.isArray(data.actions)
          ? data.actions
          : (data.artifact && Array.isArray(data.artifact.refs) ? data.artifact.refs : []);
        if (Array.isArray(actionRefs)) {
          state.actions = actionRefs
            .filter((item) => {
              if (!item) return false;
              const metadata = item.metadata && typeof item.metadata === "object" ? item.metadata : {};
              return typeof item.url === "string"
                || typeof item.uri === "string"
                || String(item.kind || "").toLowerCase() === "provider"
                || String(metadata.target || "").toLowerCase() === "provider";
            })
            .slice(0, 8)
            .map((item) => {
              const metadata = item.metadata && typeof item.metadata === "object" ? item.metadata : {};
              return {
                url: String(item.url || item.uri || ""),
                uri: String(item.uri || item.url || ""),
                label: String(item.label || item.title || metadata.label || ""),
                kind: String(item.kind || ""),
                target: String(item.target || metadata.target || ""),
                defaultAction: String(item.defaultAction || item.default_action || metadata.action || ""),
                action: String(item.action || metadata.action || ""),
                provider: String(item.provider || metadata.provider || ""),
                runId: String(item.run_id || item.runId || metadata.run_id || metadata.runId || item.ref || ""),
                cwd: String(item.cwd || metadata.cwd || ""),
                ref: String(item.ref || metadata.ref || ""),
                metadata,
              };
            });
        }
        if (Number.isFinite(Number(data.progress))) state.progress = Math.max(0, Math.min(100, Number(data.progress)));
        if (inferredMode === "markdown") {
          state.reportView = { phase: state.phase, title: state.title, lead: state.lead, progress: state.progress };
        } else if (inferredMode === "diff") {
          state.diffView = { phase: state.phase, title: state.title, lead: state.lead, progress: state.progress };
        }
        if (Array.isArray(data.signals)) state.signals = data.signals.slice(0, 4);
        if (["compact", "wide", "full"].includes(data.sizePreset)) {
          setPreset(data.sizePreset);
        }
        if (data.size && Number.isFinite(Number(data.size.width)) && Number.isFinite(Number(data.size.height))) {
          state.preset = "custom";
          state.size = clampSize({ width: Number(data.size.width), height: Number(data.size.height) });
          savePreset();
          saveSize();
        }
        if (typeof data.expanded === "boolean") state.expanded = data.expanded;
        else if (typeof data.open === "boolean") state.expanded = data.open;
        else if (data.visible !== false) state.expanded = true;
        if (data.visible === false || data.action === "fold") state.expanded = false;
        if (own(data, "permissionRequest")) {
          state.permissionRequest = normalizePermissionRequest(data.permissionRequest);
          state.permissionSubmitting = false;
          state.permissionError = "";
        }
        if (typeof data.permissionVisible === "boolean") {
          state.permissionVisible = data.permissionVisible;
          if (!state.permissionVisible) {
            state.permissionRequest = null;
            state.permissionSubmitting = false;
            state.permissionError = "";
          }
        }
        if (preservedView) {
          state.mode = preservedView.mode;
          state.activeDiffFile = preservedView.activeDiffFile;
          state.phase = preservedView.phase;
          state.title = preservedView.title;
          state.lead = preservedView.lead;
          state.progress = preservedView.progress;
          state.reportView = preservedView.reportView;
          state.diffView = preservedView.diffView;
          state.preset = preservedView.preset;
          state.size = clampSize(preservedView.size);
        }
        state.hasContent = data.visible === false ? state.hasContent : true;
        const ttlMs = Number(data.ttlMs || data.timeoutMs || 0);
        if (Number.isFinite(ttlMs) && ttlMs > 0) {
          lifecycleTimer = window.setTimeout(() => {
            lifecycleTimer = null;
            if (data.lifecycle === "clear") {
              state.hasContent = false;
              state.permissionVisible = false;
              state.permissionRequest = null;
              state.permissionSubmitting = false;
              state.permissionError = "";
            }
            state.expanded = false;
            render();
          }, ttlMs);
        }
        render();
      },
      toggle() {
        state.expanded = !state.expanded;
        if (state.expanded) state.hasContent = true;
        render();
      },
    };
  }

  window.createCrtCanvasSurface = createCrtCanvasSurface;
})();
