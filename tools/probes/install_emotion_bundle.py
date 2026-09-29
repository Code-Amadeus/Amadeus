"""Verify an extracted local emotion bundle and install only its asset members."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def contained(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f'Invalid bundle member: {name}')
    return path


def install(bundle, repo, *, verify_only=False, overwrite=False):
    bundle, repo = Path(bundle).resolve(), Path(repo).resolve()
    manifests = sorted(bundle.glob('MANIFEST-*.json'))
    if not manifests:
        raise ValueError('No MANIFEST-*.json found; extract the ZIPs into one directory first.')
    copies = []
    verified = 0
    for manifest in manifests:
        for row in json.loads(manifest.read_text(encoding='utf-8'))['files']:
            source = contained(bundle, row['path'])
            if source.stat().st_size != row['size'] or digest(source) != row['sha256']:
                raise ValueError(f'Bundle integrity mismatch: {row["path"]}')
            verified += 1
            if not row.get('install', False):
                continue
            target = contained(repo, row['path'])
            if not row['path'].startswith('assets/audio/reference/emotions/'):
                raise ValueError('Unexpected install destination')
            if target.exists():
                if digest(target) == row['sha256']:
                    continue
                if not overwrite:
                    raise FileExistsError(f'Different existing asset: {row["path"]}; back it up before using --overwrite.')
            copies.append((source, target))
    # Validate every source and conflict before the first write. Never edits .env.
    if not verify_only:
        for source, target in copies:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return {'verified_files': verified, 'asset_copies': len(copies), 'verify_only': verify_only}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle-dir', type=Path, required=True)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    print(json.dumps(install(args.bundle_dir, args.repo, verify_only=args.verify_only, overwrite=args.overwrite)))
