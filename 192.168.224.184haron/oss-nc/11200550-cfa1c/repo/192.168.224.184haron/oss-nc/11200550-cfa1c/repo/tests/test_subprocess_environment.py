from config.subprocess_environment import isolated_child_environment
import pytest
from types import SimpleNamespace


def test_platform_environment_does_not_forward_host_credentials_or_import_overrides():
    source = {
        "SystemRoot": "C:/Windows", "Path": "C:/tools", "TEMP": "C:/temp",
        "https_proxy": "https://proxy.invalid", "LANG": "en_NZ.UTF-8",
        "OPENAI_API_KEY": "host-key", "AMADEUS_MCP_CONNECTIONS": "other-keys",
        "AMADEUS_EXTENSIONS": "package-keys", "PYTHONPATH": "untrusted-import",
        "PYTHONHOME": "wrong-interpreter", "CODEX_TOKEN": "provider-key",
        "AMADEUS_DESKTOP_AUTH_TOKEN": "desktop-token",
    }
    assert isolated_child_environment(source=source) == {
        "SystemRoot": "C:/Windows", "Path": "C:/tools", "TEMP": "C:/temp",
        "https_proxy": "https://proxy.invalid", "LANG": "en_NZ.UTF-8",
    }
    assert source["OPENAI_API_KEY"] == "host-key"


def test_only_explicit_child_bindings_are_added():
    left = isolated_child_environment(source={"OTHER_PACKAGE_KEY": "other"},
                                      extra={"SERVICE_KEY": "left"})
    right = isolated_child_environment(source={"OTHER_PACKAGE_KEY": "other"},
                                       extra={"SERVICE_KEY": "right"})
    assert left == {"SERVICE_KEY": "left"}
    assert right == {"SERVICE_KEY": "right"}


@pytest.mark.parametrize('platform', ['nt', 'posix'])
def test_explicit_environment_case_matches_platform_semantics(monkeypatch, platform):
    import config.subprocess_environment as child_environment
    monkeypatch.setattr(child_environment, 'os', SimpleNamespace(name=platform))
    source = {'Path': 'inherited-path', 'https_proxy': 'inherited-proxy'}
    extra = {'PATH': 'explicit-path', 'HTTPS_PROXY': 'explicit-proxy'}
    result = isolated_child_environment(source=source, extra=extra)
    assert result['PATH'] == 'explicit-path' and result['HTTPS_PROXY'] == 'explicit-proxy'
    if platform == 'nt':
        assert result == extra
    else:
        assert result == {**source, **extra}
    assert source == {'Path': 'inherited-path', 'https_proxy': 'inherited-proxy'}
