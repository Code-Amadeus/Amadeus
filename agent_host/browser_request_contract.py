"""Canonicalize model-authored Browser requests before provider intake.

The main model describes user intent, while the Browser provider owns the
atomic action contract.  A common model output is ``action=open`` for a
compound request such as "open Wikipedia and find X".  Without an address,
that is not an atomic open; treating it as one makes the strict Browser guard
reject work that should have gone through the normal research path.

This module contains only Browser contract semantics.  It does not know about
chat personas, the Work ledger, or a particular browser engine, so both the
host request assembler and Browser adapters can rely on the same vocabulary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse


_BARE_DOMAIN_RE = re.compile(
    r"(?<![A-Za-z0-9@._-])"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{2,59})"
    r"(?::\d{1,5})?(?:/[^\s<>'\"()\[\]{}，。；：！？、（）]*)?)",
    flags=re.I,
)

_SEARCH_INTENT_RE = re.compile(
    r"(?:\b(?:search(?:\s+for)?|find|look\s+up|locate)\b|"
    r"搜索|搜一下|搜一搜|查找|找到|寻找|检索|調べ|検索|探(?:す|して|せ))",
    flags=re.I,
)


@dataclass(slots=True, frozen=True)
class BrowserDelegateNormalization:
    """Canonical Browser action plus host-derived structured parameters."""

    action: str
    parameters: dict[str, Any] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict)


def normalize_delegate_browser_request(
    task: str,
    action: str,
    parameters: Mapping[str, Any] | None = None,
) -> BrowserDelegateNormalization:
    """Lower malformed compound ``open`` requests without guessing a target.

    A valid atomic open keeps its action and receives a structured ``url``
    when the address was embedded in task prose.  An address-less open that
    explicitly asks to search/find is not an open at all; clearing the atomic
    action sends it through Browser's established research path.  A vague
    address-less open remains unchanged so the adapter can reject it rather
    than silently searching for arbitrary prose.
    """

    canonical_action = str(action or "").strip().lower()
    source = parameters if isinstance(parameters, Mapping) else {}
    declared_url = str(source.get("url") or "").strip()
    urls = web_addresses(declared_url, allow_bare_domain=True)
    if not urls:
        urls = web_addresses(task, allow_bare_domain=True)

    if canonical_action != "open":
        return BrowserDelegateNormalization(action=canonical_action)

    if urls:
        return BrowserDelegateNormalization(
            action="open",
            parameters={"url": urls[0]},
            audit={
                "status": "canonical",
                "action": "open",
                "target_source": "attribute" if declared_url else "task",
            },
        )

    if _SEARCH_INTENT_RE.search(str(task or "")):
        return BrowserDelegateNormalization(
            action="",
            audit={
                "status": "lowered",
                "from_action": "open",
                "to_mode": "research",
                "reason": "addressless_open_with_search_intent",
            },
        )

    return BrowserDelegateNormalization(action="open")


def web_addresses(text: str, *, allow_bare_domain: bool = False) -> list[str]:
    """Extract normalized HTTP(S) addresses in source order."""

    source = str(text or "")
    candidates = re.findall(r"(?:https?://|www\.)[^\s<>'\")\]）]+", source, flags=re.I)
    if allow_bare_domain:
        candidates.extend(match.group(1) for match in _BARE_DOMAIN_RE.finditer(source))
    normalized: list[str] = []
    for candidate in candidates:
        fixed = normalize_web_address(candidate, allow_bare_domain=allow_bare_domain)
        if fixed and fixed not in normalized:
            normalized.append(fixed)
    return normalized


def normalize_web_address(address: str, *, allow_bare_domain: bool = False) -> str:
    text = str(address or "").strip().strip("<>\"'")
    text = re.sub(r"[.,;:!?，。；：！？、)）\]}]+$", "", text)
    if text.lower().startswith("www."):
        text = "https://" + text
    elif allow_bare_domain and "://" not in text:
        if _BARE_DOMAIN_RE.fullmatch(text) is not None:
            text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return text


def browser_research_query(task: str) -> str:
    """Remove command scaffolding from an unmistakable web-find request."""

    text = re.sub(r"\s+", " ", str(task or "")).strip()
    if not text:
        return ""
    site_match = re.search(
        r"\bopen\s+(?:the\s+)?(?P<site>[A-Za-z0-9][A-Za-z0-9._-]{1,40})"
        r"(?:\s+(?:website|site))?\s+(?:and|then)\b",
        text,
        flags=re.I,
    )
    query_match = re.search(
        r"\b(?:search(?:\s+for)?|find|look\s+up|locate)\s+"
        r"(?P<query>[^.!?；;。！？\n]+)",
        text,
        flags=re.I,
    )
    if query_match is None:
        return text[:220]
    query = query_match.group("query")
    query = re.split(
        r"\s+(?:and\s+)?(?:report|show|tell|summari[sz]e|describe)\b",
        query,
        maxsplit=1,
        flags=re.I,
    )[0]
    query = re.sub(r"^(?:for\s+)?(?:the\s+)?", "", query, flags=re.I)
    query = re.sub(r"\s+(?:page|website|site)$", "", query, flags=re.I)
    query = query.strip(" \t\r\n'\"`“”‘’<>[]{}：:。.!?！？；;,，、")
    query = re.sub(r"['\"`“”‘’]", "", query)
    site = str(site_match.group("site") if site_match else "").strip()
    if site and site.lower() not in query.lower():
        query = f"{query} {site}".strip()
    return re.sub(r"\s+", " ", query).strip()[:220] or text[:220]
