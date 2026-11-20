__all__ = [
    "BrowserAdapter",
    "BrowserBranchAdapter",
    "CodexAppServerAdapter",
    "DirectCodexAdapter",
    "McpProviderAdapter",
    "McpToolBinding",
    "OpenClawAdapter",
]


def __getattr__(name: str):
    if name == "BrowserAdapter":
        from agent_host.adapters.browser import BrowserAdapter

        return BrowserAdapter
    if name == "BrowserBranchAdapter":
        from agent_host.adapters.browser_branch import BrowserBranchAdapter

        return BrowserBranchAdapter
    if name == "CodexAppServerAdapter":
        from agent_host.adapters.codex_app_server import CodexAppServerAdapter

        return CodexAppServerAdapter
    if name == "DirectCodexAdapter":
        from agent_host.adapters.direct_codex import DirectCodexAdapter

        return DirectCodexAdapter
    if name in {"McpProviderAdapter", "McpToolBinding"}:
        from agent_host.adapters.mcp_provider import (
            McpProviderAdapter,
            McpToolBinding,
        )

        return {
            "McpProviderAdapter": McpProviderAdapter,
            "McpToolBinding": McpToolBinding,
        }[name]
    if name == "OpenClawAdapter":
        from agent_host.adapters.openclaw import OpenClawAdapter

        return OpenClawAdapter
    raise AttributeError(name)
