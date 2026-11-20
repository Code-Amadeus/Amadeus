"""Browser-specific expected-state extraction at the execution boundary."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote_plus, urljoin


BROWSER_PAGE_STATE_FACET = "browser.page_state"


def structured_action_target(
    action: dict[str, Any],
    page_state: dict[str, Any],
) -> dict[str, str]:
    """Resolve a predicted page target without interpreting provider prose."""

    action_name = str(action.get("action") or "").strip().lower()
    if action_name == "open":
        url = str(action.get("url") or "").strip()
        return {"url": url} if url.startswith(("http://", "https://")) else {}
    if action_name != "click_ref":
        return {}
    ref = str(
        action.get("ref") or action.get("action_ref") or action.get("target_ref") or ""
    ).strip()
    if not ref:
        return {}
    refs = (
        page_state.get("interaction_refs")
        if isinstance(page_state.get("interaction_refs"), list)
        else []
    )
    target = next(
        (
            item
            for item in refs
            if isinstance(item, dict) and str(item.get("ref") or "").strip() == ref
        ),
        None,
    )
    if not isinstance(target, dict):
        return {}
    href = str(target.get("href") or "").strip()
    if not href:
        return {}
    resolved = urljoin(str(page_state.get("url") or ""), href)
    return {"url": resolved} if resolved.startswith(("http://", "https://")) else {}


def observed_submit_expected_state(
    action: dict[str, Any],
    *,
    previous_state: dict[str, Any],
    current_state: dict[str, Any],
) -> dict[str, str]:
    """Certify a submitted value only from a matching observed transition."""

    if str(action.get("action") or "").strip().lower() != "fill_ref":
        return {}
    if not bool(action.get("submit")):
        return {}
    value = " ".join(
        str(action.get("value") or action.get("input") or "").casefold().split()
    )
    if not value:
        return {}
    previous_url = str(previous_state.get("url") or "").strip()
    previous_title = " ".join(str(previous_state.get("title") or "").casefold().split())
    current_url = str(current_state.get("url") or "").strip()
    current_title = " ".join(str(current_state.get("title") or "").casefold().split())
    if current_url == previous_url and current_title == previous_title:
        return {}
    observed_label = " ".join(
        unquote_plus(f"{current_url} {current_title}").casefold().split()
    )
    if value not in observed_label:
        return {}
    if current_url.startswith(("http://", "https://")):
        return {"url": current_url}
    return {"title": str(current_state.get("title") or "").strip()} if current_title else {}
