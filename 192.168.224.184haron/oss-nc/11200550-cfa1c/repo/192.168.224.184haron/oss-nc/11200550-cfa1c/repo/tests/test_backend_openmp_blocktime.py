"""The backend bounds OpenMP spin-waiting before anything can load torch."""

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
module = ast.parse((ROOT / "server" / "app.py").read_text(encoding="utf-8"))


def _imported_roots(node):
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module.split(".")[0]]
    return []


def _sets_blocktime(node):
    call = node.value if isinstance(node, ast.Expr) else None
    return (
        isinstance(call, ast.Call)
        and ast.unparse(call.func) == "os.environ.setdefault"
        and [ast.literal_eval(arg) for arg in call.args] == ["KMP_BLOCKTIME", "1"]
    )


def test_blocktime_default_precedes_every_non_stdlib_import():
    body = module.body
    setting = next(index for index, node in enumerate(body) if _sets_blocktime(node))
    first_external = next(
        index for index, node in enumerate(body)
        if any(root not in sys.stdlib_module_names and root != "__future__"
               for root in _imported_roots(node))
    )
    # setdefault keeps an explicit value from the environment or Electron.
    assert setting < first_external
