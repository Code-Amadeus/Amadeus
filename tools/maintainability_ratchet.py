"""Inspect source boundaries without importing application code or requiring Git.

Run --update to tighten existing import/environment baselines, never to admit
new entries. Silent broad exceptions are an observation report, not a gate.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYERS = {name: level for level, names in enumerate((
    ("config",), ("llm", "asr", "tts", "render", "wallpaper", "vts", "vn_player"),
    ("core", "agent_host"), ("server",),
)) for name in names}
SHARED = {"server.protocol", "server.event_bus", "server.local_auth",
          "core.pyaudio_lifecycle", "core.turn_coordinator"}
PLACEHOLDER = "runtime log event at"

# Scope exemptions to the existing owner and operation, not an entire file.
WRITE_EXCEPTIONS = {
    ("config/durable_io.py", "_replace", "replace"): "The shared commit point.",
    ("render/texture_cache.py", "TextureDiskCache._publish", "replace"): "Rebuildable cache with contention and quota rules.",
    ("server/work_export_service.py", "WorkExportService._publish_atomic_replace", "replace"): "Approved export replacement with target identity and hash verification.",
    ("core/character_rag.py", "build_index", "json_text"): "Offline index generation; rebuildable metadata.",
    ("server/interaction_branch.py", "InteractionBranchCoordinator._persist", "json_text"): "Diagnostic snapshots, no runtime rehydration; per-event writes.",
    ("server/provider_branch.py", "ProviderBranch._persist", "json_text"): "Diagnostic trace, no runtime rehydration.",
    ("vn_player/context_store.py", "VNContextStore.write_json", "json_text"): "Deferred by the user on 2026-10-11; VN recovery must be designed first.",
    ("vn_player/context_store.py", "VNContextStore.write_json", "replace"): "Same explicitly deferred VN write path.",
}


def python_sources(root: Path, *, include_bundled: bool = False):
    directories = sorted(LAYERS) + (["GPT_SoVITS"] if include_bundled else [])
    if include_bundled:
        yield from sorted(root.glob("*.py"))
    for directory in directories:
        for parent, dirs, files in os.walk(root / directory):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in {"node_modules", "__pycache__"})
            for name in sorted(files):
                if name.endswith(".py"):
                    yield Path(parent) / name


def inspect_source(source: str, filename: str) -> dict:
    tree = ast.parse(source, filename=filename)
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update((a.asname or a.name.split(".")[0], a.name if a.asname else a.name.split(".")[0]) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            aliases.update((a.asname or a.name, f"{node.module}.{a.name}") for a in node.names)

    def name(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{name(node.value)}.{node.attr}"
        return ""

    imports, environment, writes, silent = set(), set(), [], []
    layer = LAYERS.get(filename.split("/")[0], 0)
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = filename.removesuffix(".py").split("/")[:-1]
                base = package[:len(package) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            targets = [module] if module not in LAYERS else [f"{module}.{a.name}" for a in node.names]
        imports.update(t for t in targets if LAYERS.get(t.split(".")[0], -1) > layer and t not in SHARED)
        key = None
        if isinstance(node, ast.Call) and name(node.func) in {"os.getenv", "os.environ.get"} and node.args:
            key = node.args[0]
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and name(node.value) == "os.environ":
            key = node.slice
        elif isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)) and name(node.comparators[0]) == "os.environ":
            key = node.left
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            environment.add(key.value)
        if isinstance(node, ast.ExceptHandler) and (node.type is None or isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}):
            children = [child for statement in node.body for child in ast.walk(statement)]
            handled = any(isinstance(child, ast.Raise)
                          or isinstance(child, ast.Name) and child.id == node.name
                          or isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                          and child.func.attr in {"exception", "error", "warning", "info", "debug", "critical", "log"}
                          for child in children)
            if not handled:
                lines = source.splitlines()
                explained = any("# best-effort:" in lines[n - 1] for n in (node.lineno, node.body[0].lineno))
                silent.append({"line": node.lineno, "explained": explained})

    class Writes(ast.NodeVisitor):
        def __init__(self):
            self.scope = []
            self.write_handles = []

        def visit_ClassDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_With(self, node):
            handles = set()
            for item in node.items:
                call = item.context_expr
                if not isinstance(call, ast.Call) or not isinstance(item.optional_vars, ast.Name):
                    continue
                function = name(call.func)
                index = 1 if function == "open" else 0
                if function != "open" and not function.endswith(".open"):
                    continue
                mode = next((k.value for k in call.keywords if k.arg == "mode"), call.args[index] if len(call.args) > index else None)
                if isinstance(mode, ast.Constant) and mode.value in {"w", "wb", "w+", "wb+"}:
                    handles.add(item.optional_vars.id)
            self.write_handles.append(handles)
            self.generic_visit(node)
            self.write_handles.pop()

        def visit_Call(self, node):
            function, kind = name(node.func), None
            if function == "os.replace" or (isinstance(node.func, ast.Attribute) and node.func.attr == "replace"
                    and (len(node.args) == 1 or any(k.arg == "target" for k in node.keywords))):
                kind = "replace"
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "write_text" and node.args and any(
                    isinstance(n, ast.Call) and name(n.func) == "json.dumps" for n in ast.walk(node.args[0])):
                kind = "json_text"
            elif function == "json.dump" and len(node.args) > 1 and isinstance(node.args[1], ast.Name) and any(node.args[1].id in h for h in self.write_handles):
                kind = "json_dump"
            if kind:
                writes.append({"owner": ".".join(self.scope), "kind": kind, "line": node.lineno})
            self.generic_visit(node)

    Writes().visit(tree)
    return {"imports": sorted(imports), "environment": sorted(environment), "writes": writes, "silent": silent}


def inventory(root: Path = ROOT) -> dict:
    return {p.relative_to(root).as_posix(): inspect_source(p.read_text(encoding="utf-8-sig"), p.relative_to(root).as_posix())
            for p in python_sources(root)}


def additions(actual: dict, allowed: dict) -> dict:
    return {file: sorted(set(values) - set(allowed.get(file, []))) for file, values in actual.items()
            if set(values) - set(allowed.get(file, []))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="tighten existing baselines; additions require explicit review")
    args = parser.parse_args()
    observed = inventory()
    imports = {f: v["imports"] for f, v in observed.items() if v["imports"]}
    environment = {f: v["environment"] for f, v in observed.items() if not f.startswith("config/") and v["environment"]}
    baseline_path = ROOT / "tests/fixtures/maintainability_baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else {"imports": imports}
    config_path = ROOT / "config/catalog_legacy.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    new_imports = additions(imports, baseline["imports"])
    new_environment = additions(environment, config.get("environment_reads", environment))
    print(json.dumps({"new_imports": new_imports, "new_environment_reads": new_environment,
                      "removed_imports_run_update": additions(baseline["imports"], imports),
                      "removed_environment_reads_run_update": additions(config.get("environment_reads", {}), environment),
                      "silent_exceptions_observation_only": {f: v["silent"] for f, v in observed.items() if v["silent"]}}, indent=2))
    if new_imports or new_environment:
        return 1
    if args.update:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps({"imports": imports}, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        config["environment_reads"] = dict(sorted(environment.items()))
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
