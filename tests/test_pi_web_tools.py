"""Pi and Canvas share only the system URL opener, not a Browser provider."""
from unittest.mock import Mock
from contextlib import asynccontextmanager
from types import SimpleNamespace
import json

import pytest

from server.canvas_action_router import CanvasActionRouter
from server.web_navigation import open_web_url
from agent_host import pi_web_tools


async def test_search_keeps_sources_and_never_attaches_account_credentials(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "must-not-be-used")
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, arguments))
        return SimpleNamespace(is_error=False, content=[SimpleNamespace(type="text",
            text="Title: 中文文章\nURL: https://example.test/article\nPublished: 2026-09-21")])

    @asynccontextmanager
    async def connection(spec):
        assert not spec.bearer_token_env_var and not spec.environment
        assert spec.url == "https://mcp.exa.ai/mcp?tools=web_search_exa"
        yield SimpleNamespace(call_tool=call_tool)

    monkeypatch.setattr(pi_web_tools, "open_mcp_connection", connection)
    result = await pi_web_tools.search_web({"query": "中文文章", "objective": "Find the original source"})
    assert "https://example.test/article" in result["text"]
    assert "2026-09-21" in result["text"]
    assert not result["is_error"] and not result["visible_browser_opened"]
    assert calls == [("web_search_exa", {"query": "中文文章", "objective": "Find the original source", "numResults": 5})]


def test_search_connection_failure_returns_tool_error_without_retry(monkeypatch, capsys):
    attempts = []

    async def unavailable(params):
        attempts.append(params)
        raise TimeoutError()

    monkeypatch.setattr(pi_web_tools, "search_web", unavailable)
    monkeypatch.setattr("sys.argv", ["helper", "search", '{"query":"article","objective":"source"}'])
    pi_web_tools.main()
    result = json.loads(capsys.readouterr().out)
    assert result["is_error"] and "No results were obtained" in result["text"]
    assert len(attempts) == 1


def test_pi_and_canvas_share_system_browser_opening(monkeypatch):
    opened = Mock(return_value=True)
    monkeypatch.setattr("server.web_navigation.webbrowser.open", opened)
    result = open_web_url("https://example.test/article")
    assert result == {"url": "https://example.test/article", "status": "launch_requested",
        "page_load_verified": False}
    assert CanvasActionRouter()._url_action("open", {"url": "https://example.test/article"})["ok"]
    assert opened.call_count == 2
    opened.assert_called_with("https://example.test/article", new=2, autoraise=True)


@pytest.mark.parametrize("url", ["file:///C:/private.txt", "javascript:alert(1)", "not a URL"])
def test_web_open_cannot_execute_a_non_web_target(monkeypatch, url):
    opened = Mock()
    monkeypatch.setattr("server.web_navigation.webbrowser.open", opened)
    with pytest.raises(ValueError, match="invalid_url"):
        open_web_url(url)
    assert not CanvasActionRouter()._url_action("open", {"url": url})["ok"]
    opened.assert_not_called()


def test_system_browser_rejection_is_not_reported_as_opened(monkeypatch):
    monkeypatch.setattr("server.web_navigation.webbrowser.open", lambda *_a, **_kw: False)
    with pytest.raises(RuntimeError, match="did_not_accept"):
        open_web_url("https://example.test/article")
    assert not CanvasActionRouter()._url_action("open", {"url": "https://example.test/article"})["ok"]
