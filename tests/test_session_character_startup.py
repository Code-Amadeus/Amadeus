"""Session-first imports and production identity resolution share the frozen boot role."""
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_session_import_first_pins_selected_character_before_runtime_composition(tmp_path):
    program = r'''
import importlib.resources as resources
import json
from pathlib import Path
import sys
from config import environment
environment.load_project_environment = lambda _: environment.EnvironmentReader({
    "AMADEUS_CHARACTER_ID": "mira", "RAG_ENABLED": "false"})
original_files = resources.files
class CharacterResources:
    def joinpath(self, name):
        return Path(sys.argv[1]) if name == "mira.toml" else original_files("characters").joinpath(name)
resources.files = lambda package: CharacterResources() if package == "characters" else original_files(package)
from core import session_manager as sessions
from llm import character_prompts as characters
from config import settings
from server.app import _source_session_role_identity
assert sessions.conversation_history.character_id == characters.active_character_id() == "mira"
sessions._SESSION_DIR = sys.argv[2]
sessions.create_session("first")
before = _source_session_role_identity("first")
settings.CHARACTER_ID = "kurisu"
characters.load.cache_clear()
characters.load = lambda *_args: (_ for _ in ()).throw(AssertionError("boot pack must already be pinned"))
sessions.create_session("second")
assert sessions.conversation_history.character_id == "mira"
assert _source_session_role_identity("second") == before
assert characters.active_character_id() == "mira"
print(json.dumps({"identity": before, "session": sessions.get_session_character_id("second")}))
'''
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", program,
        str(ROOT / "tests/fixtures/mira_character.toml"), str(tmp_path / "sessions")],
        cwd=ROOT, capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {"identity": {"character_id": "mira", "display_name": "Mira"},
                                       "session": "mira"}
