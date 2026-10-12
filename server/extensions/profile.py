"""Static Host support; manifests cannot select or import an execution owner."""
import json
from pathlib import Path

PROFILE = json.loads(Path(__file__).with_name('profile.json').read_text(encoding='utf-8'))


def contribution_profile(kind: str, role: str | None = None) -> dict | None:
    return next((row for row in PROFILE if row['kind'] == kind and row.get('role') == role), None)


def validate_supported_contributions(contributes: dict) -> None:
    for kind, items in contributes.items():
        for item in items:
            profile = contribution_profile(kind, item.get('role'))
            if profile is None or profile['status'] != 'open':
                reason = profile['reason'] if profile else 'Unknown contribution type or role.'
                raise ValueError(f'Unsupported contribution {kind}: {reason}')
