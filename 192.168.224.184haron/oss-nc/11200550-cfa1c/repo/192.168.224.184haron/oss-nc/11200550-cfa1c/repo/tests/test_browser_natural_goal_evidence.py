"""Natural-language URL extraction boundaries and known parsing limitations."""
import pytest

from agent_host.browser_request_contract import normalize_web_address, web_addresses


@pytest.mark.parametrize("host", ["127.0.0.1:8080", "localhost:8080", "example.test"])
@pytest.mark.parametrize("suffix", ["", " 我想看一下这个网页。", "，我想看一下这个网页。"])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_source_url_followed_by_natural_description(host, suffix, scheme, request):
    url = f"{scheme}://{host}/comparison.html"
    if suffix.startswith("，") and (host != "example.test" or scheme == "http"):
        request.node.add_marker(pytest.mark.xfail(strict=True,
            reason="Known URL extraction bug: source prose swallowed; bare-domain fallback also changes HTTP to HTTPS"))
    assert normalize_web_address(url) in web_addresses(f"打开 {url}{suffix}", allow_bare_domain=True)


def test_unsupplied_site_url_does_not_become_browser_open_authority():
    assert "https://www.bilibili.com" not in web_addresses("帮我打开一下bilibili。", allow_bare_domain=True)
