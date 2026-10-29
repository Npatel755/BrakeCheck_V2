"""Offline checks for complete, non-destructive candidate resource staging."""
import hashlib
import json

import pytest

from stage_candidate import stage


def candidate_source(tmp_path):
    source = tmp_path / 'source'
    for name in ('CommaLane', 'IntelRoad'):
        package = source / f'{name}.mlpackage'
        weights = package / 'Data/com.apple.CoreML/weights/weight.bin'
        weights.parent.mkdir(parents=True)
        weights.write_bytes(b'fixture weights')
        (package / 'Data/com.apple.CoreML/model.mlmodel').write_bytes(b'fixture model')
        (package / 'Manifest.json').write_text(json.dumps({'itemInfoEntries': {
            'model': {'path': 'com.apple.CoreML/model.mlmodel'},
            'weights': {'path': 'com.apple.CoreML/weights'},
        }}), encoding='utf-8')
    return source


def test_staged_candidates_have_exact_hashes_and_leave_source_unchanged(tmp_path):
    source = candidate_source(tmp_path)
    original = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    output = stage(tmp_path / 'trial', source)
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    assert {m['name'] for m in manifest['models']} == {'CommaLane', 'IntelRoad'}
    assert len(manifest['files']) == 6
    for name, digest in manifest['files'].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    assert original == {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}


def test_existing_output_is_preserved(tmp_path):
    source = candidate_source(tmp_path)
    output = tmp_path / 'existing'
    output.mkdir()
    sentinel = output / 'keep.txt'
    sentinel.write_text('keep', encoding='utf-8')
    with pytest.raises(FileExistsError):
        stage(output, source)
    assert sentinel.read_text(encoding='utf-8') == 'keep'


def test_missing_weights_fail_before_creating_output(tmp_path):
    source = candidate_source(tmp_path)
    (source / 'IntelRoad.mlpackage/Data/com.apple.CoreML/weights/weight.bin').unlink()
    output = tmp_path / 'trial'
    with pytest.raises(FileNotFoundError):
        stage(output, source)
    assert not output.exists()
