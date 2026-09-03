"""API key parsing tolerates accidental quotes/whitespace from .env edits.

Covers the single auth-credential contract in config.environment:
settings, browser providers and provider command auth must observe the
same normalized value for the same key.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Documented normalization rules (config.environment.secret_value):
# whitespace stripped; at most one leading/trailing quote char removed
# (paired or not — the .env typo case); inner quotes never touched.
_RULE_CASES = [
    ('  "sk-test123"  ', "sk-test123"),  # paired double quotes + whitespace
    ("  'sk-test123'  ", "sk-test123"),  # paired single quotes + whitespace
    ('sk-test"', "sk-test"),  # unpaired trailing quote (the reviewed typo)
    ('"sk-test', "sk-test"),  # unpaired leading quote
    ('" sk "\t', "sk"),  # quotes + inner whitespace
    ('my"pass"word', 'my"pass"word'),  # inner quotes: legal token untouched
    ("it's-a-token", "it's-a-token"),  # inner apostrophe untouched
    ("   ", ""),  # whitespace-only -> empty (key counts as unconfigured)
    ("sk-test123", "sk-test123"),  # clean value passes through
]


def test_secret_value_normalization_rules() -> None:
    probe = f"""
from config.environment import secret_value
cases = {_RULE_CASES!r}
for raw, expected in cases:
    got = secret_value(raw)
    assert got == expected, (raw, got, expected)
print("SECRET_RULES_OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-1000:]
    assert "SECRET_RULES_OK" in result.stdout


def test_real_malformed_dotenv_three_consumers_agree(tmp_path) -> None:
    """A real malformed .env yields one identical value for all three consumers."""
    env_file = tmp_path / ".env"
    env_file.write_text('DEEPSEEK_API_KEY=sk-from-dotenv"\n', encoding="utf-8")
    probe = f"""
import os
from pathlib import Path
tmp = Path({str(tmp_path)!r})
assert "DEEPSEEK_API_KEY" not in os.environ

# Real .env chain: parse the file into the process environment first, exactly
# as a project .env reaches every consumer, then let the three consumers read.
from dotenv import load_dotenv
load_dotenv(tmp / ".env")

from config import settings
import server.browser_branch_planner as planner
from tools.codex_provider_auth import provider_token
consumed = (
    settings.DEEPSEEK_API_KEY,
    planner._secret("DEEPSEEK_API_KEY"),
    provider_token(env_key="DEEPSEEK_API_KEY", env_file=tmp / ".env"),
)
assert consumed == ("sk-from-dotenv",) * 3, consumed
print("SECRET_DOTENV_OK")
"""
    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    result = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "SECRET_DOTENV_OK" in result.stdout


def test_process_env_overrides_dotenv_three_consumers_agree(tmp_path) -> None:
    """A normalized process value keeps authority over .env for all consumers."""
    env_file = tmp_path / ".env"
    env_file.write_text('DEEPSEEK_API_KEY=sk-from-dotenv"\n', encoding="utf-8")
    probe = f"""
import os
from pathlib import Path
tmp = Path({str(tmp_path)!r})
from dotenv import load_dotenv
load_dotenv(tmp / ".env")

os.environ["DEEPSEEK_API_KEY"] = '  "sk-from-process"  '
from config import settings
import server.browser_branch_planner as planner
from tools.codex_provider_auth import provider_token
consumed = (
    settings.DEEPSEEK_API_KEY,
    planner._secret("DEEPSEEK_API_KEY"),
    provider_token(env_key="DEEPSEEK_API_KEY", env_file=tmp / ".env"),
)
assert consumed == ("sk-from-process",) * 3, consumed
print("SECRET_PRIORITY_OK")
"""
    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    result = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "SECRET_PRIORITY_OK" in result.stdout


def test_provider_token_still_prefers_process_environment(tmp_path) -> None:
    """Existing authority contract for the command-auth tool is preserved."""
    env_file = tmp_path / ".env"
    env_file.write_text('DEEPSEEK_API_KEY=  "sk-dotenv"  \n', encoding="utf-8")
    previous = os.environ.get("DEEPSEEK_API_KEY")
    try:
        os.environ["DEEPSEEK_API_KEY"] = ' "sk-process" '
        from tools.codex_provider_auth import provider_token

        assert provider_token(env_key="DEEPSEEK_API_KEY", env_file=env_file) == "sk-process"
        # Whitespace-only process value normalizes to empty -> fall through.
        os.environ["DEEPSEEK_API_KEY"] = "   "
        assert provider_token(env_key="DEEPSEEK_API_KEY", env_file=env_file) == "sk-dotenv"
        os.environ.pop("DEEPSEEK_API_KEY")
        assert provider_token(env_key="DEEPSEEK_API_KEY", env_file=env_file) == "sk-dotenv"
    finally:
        if previous is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = previous
