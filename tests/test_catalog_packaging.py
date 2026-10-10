"""Exercise the real wheel rules with a new, previously unknown catalog folder."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile


def test_wheel_contains_and_loads_catalog_in_new_nested_folder(tmp_path):
    root = Path(__file__).parents[1]
    project = tmp_path / 'source'
    project.mkdir()
    shutil.copy2(root / 'pyproject.toml', project / 'pyproject.toml')
    metadata = tomllib.loads((project / 'pyproject.toml').read_text(encoding='utf-8'))
    # Keep the actual package selection and data rules, without copying models,
    # local credentials, or unrelated runtime code into this build fixture.
    for name in metadata['tool']['setuptools']['packages']:
        package = project.joinpath(*name.split('.'))
        package.mkdir(parents=True, exist_ok=True)
        (package / '__init__.py').write_text('', encoding='utf-8')
    shutil.copytree(root / 'config/catalog', project / 'config/catalog', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(root / 'config/environment.py', project / 'config/environment.py')
    relative = Path('config/catalog/new_capability/nested/fixture.json')
    (project / relative).parent.mkdir(parents=True)
    (project / relative).write_text(json.dumps({
        'id': 'packaging_fixture', 'config': {'PACKAGE_FIXTURE': {'type': 'string', 'default': 'loaded'}},
    }), encoding='utf-8')
    output = tmp_path / 'wheel'
    subprocess.run([sys.executable, '-c',
                    'from setuptools.build_meta import build_wheel; import sys; build_wheel(sys.argv[1])',
                    str(output)], cwd=project, check=True, capture_output=True, text=True)
    installed = tmp_path / 'installed'
    with zipfile.ZipFile(next(output.glob('*.whl'))) as wheel:
        assert relative.as_posix() in wheel.namelist()
        for file in (root / 'config/catalog').rglob('*.json'):
            assert file.relative_to(root).as_posix() in wheel.namelist()
        wheel.extractall(installed)
    subprocess.run([sys.executable, '-I', '-c', '''
import sys
sys.path.insert(0, sys.argv[1])
from config.catalog import read_catalog_environment
from config.environment import EnvironmentReader
assert read_catalog_environment(EnvironmentReader({}))['PACKAGE_FIXTURE'] == 'loaded'
''', str(installed)], cwd=tmp_path, check=True, capture_output=True, text=True)
