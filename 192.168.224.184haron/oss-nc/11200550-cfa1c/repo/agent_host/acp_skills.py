"""Explicit ACP implementation bindings for native Skill discovery.

ACP v1 has no Skill-install RPC. DSH's repeatable profile overlays are a
separate, opt-in launch contract (verified with 0.2.0-rc.2). No provider id or
command-name heuristic selects it. Installed resource paths remain stable;
only the disposable launch overlay is generated per process.
"""
from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_host.acp_configuration import AcpAgentSpec
from agent_host.skills import provider_skills


@contextmanager
def skill_launch_arguments(spec: AcpAgentSpec):
    if spec.skill_discovery == "catalog":
        yield spec.args
        return
    if spec.skill_discovery != "dsh":
        raise ValueError("Unsupported ACP Skill discovery integration")
    # DSH discovers flat Markdown at each root as well as directory bundles.
    # Pass the exact contribution directory, not its parent containing other
    # Skills. Resources continue to resolve beside the installed SKILL.md.
    roots = [str(skill.path.parent) for skill in provider_skills(spec.provider_id)]
    with TemporaryDirectory(prefix="amadeus-acp-skills-") as directory:
        patch = Path(directory) / "skills.json"
        patch.write_text(json.dumps([{"id": "skill-filesystem", "config": {
            "includeDefaultRoots": False, "customSkillDirs": roots,
        }}]), encoding="utf-8")
        # JSON is valid YAML; the final overlay wins over earlier argv patches.
        yield (*spec.args, "--patch", str(patch))
