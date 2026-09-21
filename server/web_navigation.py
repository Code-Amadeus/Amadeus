"""Shared system-browser navigation for accepted Host actions and agent tools."""
from __future__ import annotations

import urllib.parse
import webbrowser


def sanitize_source_url(raw_url: str) -> str:
    text = str(raw_url or "").strip()
    if text.lower().startswith("www."):
        text = "https://" + text
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid_url")
    return urllib.parse.urlunparse(parsed)


def open_web_url(raw_url: str) -> dict:
    """Request a visible tab; the OS acknowledgement is not page-load evidence."""
    url = sanitize_source_url(raw_url)
    if not webbrowser.open(url, new=2, autoraise=True):
        raise RuntimeError("system_browser_did_not_accept_url")
    return {"url": url, "status": "launch_requested", "page_load_verified": False}
