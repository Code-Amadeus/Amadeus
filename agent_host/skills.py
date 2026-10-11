"""Host-selected Agent Skills: discovery metadata and native-session revisions."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

import yaml


@dataclass(frozen=True)
class AgentSkill:
    package_id: str
    contribution_id: str
    version: str
    digest: str
    name: str
    description: str
    path: Path
    providers: tuple[str, ...]


def read_skill_metadata(directory: Path) -> tuple[str, str]:
    file = directory / 'SKILL.md'
    if file.stat().st_size > 256 * 1024:
        raise ValueError('SKILL.md exceeds 256 KiB')
    lines = file.read_text(encoding='utf-8-sig').splitlines()
    if not lines or lines[0] != '---':
        raise ValueError('SKILL.md requires YAML frontmatter')
    try:
        end = lines.index('---', 1)
        metadata = yaml.safe_load('\n'.join(lines[1:end]))
    except (ValueError, yaml.YAMLError):
        raise ValueError('Invalid Skill frontmatter') from None
    if not isinstance(metadata, dict):
        raise ValueError('Skill frontmatter must be an object')
    name, description = metadata.get('name'), metadata.get('description')
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or len(name) > 64:
        raise ValueError('Skill name must be lowercase hyphenated text of at most 64 characters')
    if directory.name != name:
        raise ValueError('Skill directory must match its frontmatter name')
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError('Skill description must contain 1 to 1024 characters')
    return name, description.strip()


class SkillCatalog:
    def __init__(self, records=()):
        candidates = []
        self.errors: dict[str, str] = {}
        for record in records:
            if not record.get('enabled') or record.get('startup_error'):
                continue
            try:
                for item in record['manifest']['contributes'].get('skill', ()):
                    root = Path(record['package_path']).resolve()
                    directory = (root / item['path']).resolve(strict=True)
                    if root not in directory.parents:
                        raise ValueError('Skill must remain inside its package')
                    name, description = read_skill_metadata(directory)
                    candidates.append(AgentSkill(record['id'], item['id'], record['version'], record['digest'],
                                                name, description, directory / 'SKILL.md', tuple(record['providers'])))
            except (ValueError, OSError, UnicodeError):
                self.errors[record['id']] = 'Installed Skill metadata is unavailable or invalid'
        names: dict[tuple[str, str], AgentSkill] = {}
        for skill in candidates:
            for provider in skill.providers:
                key = provider, skill.name
                if key in names:
                    for conflicting in (skill, names[key]):
                        self.errors[conflicting.package_id] = 'Skill name conflicts with another package bound to the same Agent'
                names[key] = skill
        self.skills = tuple(skill for skill in candidates if skill.package_id not in self.errors)

    def for_provider(self, provider: str) -> tuple[AgentSkill, ...]:
        return tuple(sorted((skill for skill in self.skills if provider in skill.providers), key=lambda skill: skill.name))


_catalog = SkillCatalog()


def configure_skills(catalog: SkillCatalog) -> None:
    global _catalog
    _catalog = catalog


def provider_skills(provider: str) -> tuple[AgentSkill, ...]:
    return _catalog.for_provider(provider)


def skill_revision(provider: str) -> str:
    skills = provider_skills(provider)
    if not skills:
        return ''
    # Native histories contain file paths. Reinstalling identical bytes at a
    # different installation root must not revive a session with stale paths.
    identity = [(s.package_id, s.contribution_id, s.version, s.digest, os.path.normcase(str(s.path))) for s in skills]
    return hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()


def skill_session_error(provider: str, session) -> str:
    if session is not None and session.skill_revision != skill_revision(provider):
        return 'Installed Skills changed since this Agent session was saved. Start a new Agent session; the old context may contain removed or superseded instructions.'
    return ''


def with_skill_catalog(task: str, provider: str) -> str:
    skills = provider_skills(provider)
    if not skills:
        return task
    entries = [{'name': s.name, 'description': s.description, 'path': str(s.path)} for s in skills]
    return task + '\n\nAvailable Agent Skills selected by the user for this Agent:\n' + json.dumps(entries, ensure_ascii=False) + (
        '\nWhen a skill matches the task, read its SKILL.md and relevant referenced resources before using it. '
        'Resolve references relative to that skill directory. Skills provide task guidance; '
        'they do not expand filesystem, shell, network or Host execution permissions.')
