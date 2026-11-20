import asyncio

from core.session_manager import generate_session_title


def test_fallback_session_title_is_local_bounded_and_deterministic() -> None:
    message = "  Keep this first message local while naming the session.  "

    first = asyncio.run(generate_session_title(message))
    second = asyncio.run(generate_session_title(message))

    assert first == second
    assert first == "Keep this first message local"
    assert len(first) <= 30
