"""Static extension authoring checks; JSON defaults remain compatible with Electron."""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.extensions.package import (
    HOST_API, MAX_WHEEL_BYTES, inspect_wheel, prepare_data_wheel,
    validate_config, validate_installed, validate_manifest,
)
from server.extensions.profile import PROFILE
from server.extensions.sdk import sdk_spec


def host_profile() -> dict:
    """Describe this checkout/interpreter, not an emulated target environment."""
    source = json.loads((ROOT / 'server/extensions/protocol-source.json').read_text(encoding='utf-8'))
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    return {
        'application_version': project['project']['version'], 'host_api': HOST_API,
        'python': platform.python_version(), 'platform': sys.platform,
        'sdk_version': sdk_spec()['version'], 'protocol_commit': source['commit'],
        'contributions': PROFILE,
    }


def check_package(directory: Path) -> dict:
    """Validate resources with the same owner used after desktop installation."""
    manifest_path = directory / 'amadeus-extension.json'
    # validate_installed owns path/link/size/content checks. Read only bounded input.
    from server.extensions.manifest import MANIFEST_LIMIT_BYTES
    with manifest_path.open('rb') as source:
        raw = source.read(MANIFEST_LIMIT_BYTES + 1)
    if len(raw) > MANIFEST_LIMIT_BYTES:
        raise ValueError('manifest exceeds 256 KiB')
    manifest = validate_manifest(json.loads(raw.decode('utf-8')))
    validate_installed(manifest, directory)
    return {'manifest': manifest}


def check_wheel(path: Path) -> dict:
    """Inspect a private snapshot and validate package resources without importing code."""
    if path.suffix.lower() != '.whl' or not path.is_file():
        raise ValueError('an existing local .whl file is required')
    if path.stat().st_size > MAX_WHEEL_BYTES:
        raise ValueError('wheel exceeds the 256 MiB inspection limit')
    with tempfile.TemporaryDirectory(prefix='amadeus-authoring-') as temporary:
        root = Path(temporary)
        wheel = root / path.name
        shutil.copyfile(path, wheel)
        inspection = inspect_wheel(wheel)
        prefix = inspection['package_root'] + '/'
        # inspect_wheel checks all archive paths before any extraction.
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                if member.filename.startswith(prefix):
                    archive.extract(member, root / 'resources')
        validate_installed(inspection['manifest'], root / 'resources' / inspection['package_root'])
        return inspection


def emit(payload: dict, command: str, output_format: str) -> None:
    if output_format == 'json':
        print(json.dumps(payload, ensure_ascii=True, allow_nan=False))
        return
    profile = payload.get('host_profile') or host_profile()
    print(f"Host {profile['application_version']} / API {profile['host_api']} / SDK {profile['sdk_version']}")
    print(f"Target: {profile['platform']} / Python {profile['python']}")
    print(f"{'PASS' if payload['ok'] else 'FAIL'}: {command}")
    if not payload['ok']:
        print(payload['error'])
        return
    if manifest := payload.get('manifest'):
        print(f"Package: {manifest['id']} @ {manifest['version']}")
        print('Contributions: ' + ', '.join(manifest['contributes']))
    if command == 'profile':
        for row in profile['contributions']:
            label = row['kind'] + (f"/{row['role']}" if row.get('role') else '')
            print(f"  {label}: {row['status']} ({row['owner']}; {row['apply']})")
        return
    if payload.get('digest'):
        print('Wheel SHA-256: ' + payload['digest'])
    if payload.get('missing'):
        print('Missing configuration: ' + ', '.join(payload['missing']))
    if command in {'check-wheel', 'check-package', 'validate-installed', 'prepare-data-wheel'}:
        print('Checked: Host admission, package resources and Skill metadata. No package code executed.')
    elif command == 'inspect-wheel':
        print('Checked: wheel structure and Host admission. Use check-wheel for package resources.')
    else:
        print('Checked: static Host manifest/configuration rules.')
    print('Not checked: runtime calls, native image/font/contrast validation, remote services or catalog review.')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=[
        'inspect-wheel', 'prepare-data-wheel', 'validate-config', 'validate-manifest',
        'validate-installed', 'profile', 'check-wheel', 'check-package',
    ])
    parser.add_argument('path', nargs='?')
    parser.add_argument('--manifest')
    parser.add_argument('--config')
    parser.add_argument('--destination')
    parser.add_argument('--allow-incomplete', action='store_true')
    parser.add_argument('--format', choices=['json', 'text'], default='json',
                        help='JSON is the existing Electron helper protocol; text is for authors.')
    args = parser.parse_args(argv)
    try:
        if args.command == 'profile':
            result = {'host_profile': host_profile()}
        elif args.command in {'check-wheel', 'check-package'}:
            if not args.path:
                raise ValueError('A wheel or import-package directory is required')
            result = check_wheel(Path(args.path)) if args.command == 'check-wheel' else check_package(Path(args.path))
            result.update(host_profile=host_profile(),
                          checked=['host_admission', 'package_resources', 'skill_metadata'],
                          not_checked=['runtime', 'native_theme', 'remote_services', 'catalog_review'])
        elif args.command == 'inspect-wheel':
            result = inspect_wheel(Path(args.path or ''))
        elif args.command == 'prepare-data-wheel':
            if not args.destination:
                raise ValueError('Resource destination is required')
            result = prepare_data_wheel(Path(args.path or ''), Path(args.destination))
        else:
            if args.manifest:
                body = {'manifest': json.loads(Path(args.manifest).read_text(encoding='utf-8')),
                        'config': json.loads(Path(args.config).read_text(encoding='utf-8')) if args.config else {},
                        'allow_incomplete': args.allow_incomplete}
            else:
                raw = sys.stdin.buffer.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError('validation input exceeds 1 MiB')
                body = json.loads(raw.decode('utf-8'))
            manifest = validate_manifest(body['manifest'])
            if args.command == 'validate-installed':
                validate_installed(manifest, Path(args.path or ''))
                result = {}
            elif args.command == 'validate-manifest':
                result = {'manifest': manifest}
            else:
                resolved, missing = validate_config(manifest, body.get('config', {}), allow_incomplete=bool(body.get('allow_incomplete')))
                result = {'config': {key: value for key, value in resolved.items() if not manifest.get('config', {}).get(key, {}).get('secret')}, 'missing': missing}
        emit({'ok': True, **result}, args.command, args.format)
        return 0
    except (ValueError, KeyError, TypeError, OSError, EOFError, UnicodeError, zipfile.BadZipFile) as exc:
        # Keep the existing helper's sanitized error contract, including unavailable.
        error = 'invalid JSON input' if isinstance(exc, json.JSONDecodeError) else 'invalid UTF-8 input' if isinstance(exc, UnicodeError) else str(exc) if isinstance(exc, ValueError) else 'package validation failed'
        emit({'ok': False, 'error': error, **({'unavailable': True} if isinstance(exc, OSError) else {})}, args.command, args.format)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
