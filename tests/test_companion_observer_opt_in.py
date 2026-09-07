"""An explicitly disabled source never opens local Codex records."""
import asyncio
from unittest.mock import patch

from server.handlers.codex_observer_handler import CodexObserverHandler


def test_empty_scope_never_constructs_or_starts_an_observer(tmp_path):
    async def run():
        with patch("server.handlers.codex_observer_handler.CodexDesktopObserver") as observer:
            handler = CodexObserverHandler([], tmp_path / "unread")
            handler.start()
            assert await handler.handle("companion.tasks", {}) == {"tasks": []}
            assert handler.job is None
            observer.assert_not_called()
            await handler.close()
    asyncio.run(run())
