import hashlib
import json

import pytest

from tools.probes.install_emotion_bundle import install


def bundle(tmp_path, rows):
    root = tmp_path / 'bundle'
    root.mkdir()
    records = []
    for name, content, enabled in rows:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        records.append(dict(path=name, size=len(content), sha256=hashlib.sha256(content).hexdigest(), install=enabled))
    (root / 'MANIFEST-resources.json').write_text(json.dumps({'files': records}), encoding='utf-8')
    return root


def test_installs_only_emotion_pairs_without_changing_default_or_env(tmp_path):
    name = 'assets/audio/reference/emotions/sad.txt'
    source = bundle(tmp_path, [(name, b'paired transcript', True), ('listening/default.wav', b'audio', False)])
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.env').write_bytes(b'existing settings')
    result = install(source, repo)
    assert result['asset_copies'] == 1
    assert (repo / name).read_bytes() == b'paired transcript'
    assert (repo / '.env').read_bytes() == b'existing settings'
    assert not (repo / 'listening').exists()
    assert install(source, repo)['asset_copies'] == 0


def test_conflict_preflight_does_not_partially_install(tmp_path):
    first = 'assets/audio/reference/emotions/angry.txt'
    second = 'assets/audio/reference/emotions/sad.txt'
    source = bundle(tmp_path, [(first, b'one', True), (second, b'two', True)])
    repo = tmp_path / 'repo'
    (repo / second).parent.mkdir(parents=True)
    (repo / second).write_bytes(b'keep mine')
    with pytest.raises(FileExistsError):
        install(source, repo)
    assert not (repo / first).exists()
    assert (repo / second).read_bytes() == b'keep mine'


def test_hash_failure_and_verify_only_never_copy(tmp_path):
    name = 'assets/audio/reference/emotions/sad.txt'
    source = bundle(tmp_path, [(name, b'one', True)])
    repo = tmp_path / 'repo'
    assert install(source, repo, verify_only=True)['asset_copies'] == 1
    assert not repo.exists()
    (source / name).write_bytes(b'bad')
    with pytest.raises(ValueError, match='integrity'):
        install(source, repo)
    assert not repo.exists()


def test_bundle_cannot_install_over_default_reference(tmp_path):
    source = bundle(tmp_path, [('assets/audio/reference/kurisu_reference.wav', b'fake', True)])
    with pytest.raises(ValueError, match='destination'):
        install(source, tmp_path / 'repo')
