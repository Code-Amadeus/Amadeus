"""Build the trusted SDK offline with the project's installed build backend.

Only bundled first-party sources are built. Third-party extensions still use
wheel-only installation; neither this command nor the installer fetches an SDK.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def build_sdk(source: Path, output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    # Build away from sources; setuptools must not leave build/egg-info in a
    # source archive or consume a stale local build of the package.
    with tempfile.TemporaryDirectory(prefix='amadeus-sdk-') as temporary:
        work = Path(temporary)
        package = work / 'source'
        package.mkdir()
        shutil.copy2(source / 'pyproject.toml', package / 'pyproject.toml')
        shutil.copytree(source / 'src', package / 'src',
            ignore=shutil.ignore_patterns('__pycache__', '*.egg-info'))
        wheels = work / 'wheels'
        result = subprocess.run([sys.executable, '-I', '-c',
            'import sys; from setuptools.build_meta import build_wheel; build_wheel(sys.argv[1])',
            str(wheels)], cwd=package, capture_output=True, text=True, encoding='utf-8')
        if result.returncode:
            raise RuntimeError('SDK build failed. Use the documented Python development environment '
                '(setuptools and wheel must be installed).\n' + result.stderr)
        built = list(wheels.glob('*.whl'))
        if len(built) != 1:
            raise RuntimeError('SDK build did not produce exactly one wheel')
        target = output / built[0].name
        # The installer may be reading the previous build. Replace only a
        # complete wheel, without removing the existing one on build failure.
        with tempfile.NamedTemporaryFile(dir=output, prefix='.sdk-', suffix='.tmp', delete=False) as staging:
            staging_path = Path(staging.name)
            with built[0].open('rb') as stream:
                shutil.copyfileobj(stream, staging)
        try:
            os.replace(staging_path, target)
        finally:
            staging_path.unlink(missing_ok=True)
        return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'build/extension-sdk')
    arguments = parser.parse_args()
    print(build_sdk(ROOT / 'sdk/extension-python', arguments.output_dir.resolve()))
