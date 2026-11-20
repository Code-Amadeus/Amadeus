from unittest.mock import patch

from config import settings
from config.log_privacy import protected_text


def test_user_text_is_redacted_by_default() -> None:
    secret = "a private spoken sentence"
    with patch.object(settings, "LOG_USER_CONTENT", False):
        rendered = protected_text(secret)

    assert secret not in rendered
    assert rendered == f"<redacted chars={len(secret)}>"


def test_user_text_preview_requires_explicit_opt_in_and_is_bounded() -> None:
    with patch.object(settings, "LOG_USER_CONTENT", True):
        rendered = protected_text("line one\nline two", limit=8)

    assert rendered == "'line one'..."
