import json

import pytest

from bcdata.bootstrap import atomic_json
from bcdata.quality import phash, read_rgb
from bcdata.teachers.base import file_sha256
from bcdata.ufld_baseline import NativeDataset, load_prepared_export
from bcdata.zod_training import build, load
from .test_ufld_experiment import experiment_fixture
from .test_zod_clearance import confirm
from .test_zod_mirror import fixture


def setup(f, *, count=1):
    root, store = fixture(f.temp, count=count, warnings=True)
    (root / 'upstream').mkdir()
    (root / 'upstream/README.md').write_text('Fixture source notice; CC-BY-SA-4.0')
    for row in f.inventory:
        if row['split'] in ('val', 'test'):
            row['phash'] = f'{phash(read_rgb(f.dataset / f.records[-1]["image"])):016x}'
    atomic_json(f.dataset / 'split_inventory.json', f.inventory)
    return store


def test_zod_training_keeps_base_and_gates_explicit_share_alike_additions(experiment_fixture):
    f = experiment_fixture
    store = setup(f)
    confirm(store, 'zod-000000', 'approve-zod-training')
    before = {p: p.read_bytes() for p in f.dataset.rglob('*') if p.is_file()}
    report = build(f.temp, f.base, f.output)
    assert report['accepted_zod'] == 1 and report['train_images'] == 3 and report['val_images'] == 1
    target, records, marker = load(f.output)
    assert records[:3] == f.records
    assert records[-1]['pool'] == 'share_alike'
    assert records[-1]['split'] == 'train'
    assert records[-1]['session_id'] == 'zod-all-recordings-train-only'
    assert len(NativeDataset(target, 'train')) == 3
    assert load_prepared_export(f.output, experimental=True)[1] == records
    with pytest.raises(ValueError, match='ineligible'):
        load_prepared_export(f.output)
    assert all(p.read_bytes() == raw for p, raw in before.items())
    with pytest.raises(ValueError, match='new isolated'):
        build(f.temp, f.base, f.output)


def test_pending_or_skipped_labels_cannot_enter_zod_training(experiment_fixture):
    f = experiment_fixture
    store = setup(f, count=5)
    confirm(store, 'zod-000000', 'approve-zod-training')
    from .test_zod_mirror import decision
    store.decide(decision(store.detail('zod-000002'), action='skip'), 'Human')
    report = build(f.temp, f.base, f.output)
    assert report['label_candidates'] == 1 and report['accepted_zod'] == 1


def test_real_correction_is_trainable_without_releasing_source_peers(experiment_fixture):
    f = experiment_fixture
    store = setup(f, count=5)
    confirm(store, 'zod-000000', 'correct-zod-training', lanes=[{'slot': 2, 'points': [[400, 500], [350, 650]]}])
    assert store.status()['counts']['groups_blocked'] == 1
    report = build(f.temp, f.base, f.output)
    assert report['accepted_zod'] == report['label_candidates'] == 1


def test_near_duplicate_of_reserved_evaluation_is_quarantined(experiment_fixture):
    f = experiment_fixture
    store = setup(f)
    confirm(store, 'zod-000000', 'approve-zod-training')
    item = store.detail('zod-000000')
    protected_hash = f'{phash(read_rgb(store.root / "images/1280x720/000000.jpg")):016x}'
    f.inventory[-1]['phash'] = protected_hash
    atomic_json(f.dataset / 'split_inventory.json', f.inventory)
    with pytest.raises(ValueError, match='No cleared, trainable'):
        build(f.temp, f.base, f.output)


def test_no_zod_evaluation_can_be_mixed_with_unknown_recordings(experiment_fixture):
    f = experiment_fixture
    setup(f)
    f.inventory[-1]['source'] = 'zod_mirror'
    atomic_json(f.dataset / 'split_inventory.json', f.inventory)
    with pytest.raises(ValueError, match='recording identities'):
        build(f.temp, f.base, f.output)


def test_stale_frozen_human_approval_is_rejected_even_with_updated_file_hash(experiment_fixture):
    f = experiment_fixture
    store = setup(f)
    confirm(store, 'zod-000000', 'approve-zod-training')
    build(f.temp, f.base, f.output)
    target = f.output / 'dataset'
    name = 'zod-evidence/zod-000000.json'
    evidence = json.loads((target / name).read_text())
    evidence['row']['decision_fingerprint'] = 'stale'
    atomic_json(target / name, evidence)
    hashes = json.loads((target / 'hashes.json').read_text())
    hashes[name] = file_sha256(target / name)
    atomic_json(target / 'hashes.json', hashes)
    with pytest.raises(ValueError, match='approval is not current'):
        load(f.output)


def test_training_freezes_real_model_adoption_receipt_and_exact_human_event(experiment_fixture):
    from bcdata.simulation_review import SimulationReview
    from bcdata.zod_mirror_review import ZodCorrection
    from bcdata.zod_mirror import encoded, sha
    f = experiment_fixture
    store = setup(f)
    item = store.detail('zod-000000')
    db = store._db()
    row = db.execute('SELECT * FROM frames').fetchone()
    binding = sha(encoded([row['native_sha'], row['image_sha']]).encode())
    candidate = {**item['geometry'], 'model_label': 'Fixture teacher',
                 'model_receipt': {'license': 'MIT', 'checkpoint_sha256': 'a' * 64}}
    raw = encoded(candidate)
    version = 'fixture-model:inference-1:continuous-v5'
    db.execute('INSERT INTO zod_model_proposals VALUES(?,?,?,?,?,?)',
               (item['image_id'], 'fixture-model', version, raw, sha(raw.encode()), binding))
    db.commit(); db.close()
    lanes = SimulationReview._validate_lanes(item['geometry']['lanes'], 1280, 720, source_metadata=True)
    store.correct(ZodCorrection(image_id=item['image_id'], revision=item['revision'], fingerprint=item['fingerprint'],
        event_id='adopt-model-for-training', lanes=lanes, resolved_qc_reasons=item['source_qc_reasons'],
        approve=True, complete_annotation=True, candidate='fixture-model', candidate_version=version), 'Human')
    build(f.temp, f.base, f.output)
    target, _, _ = load(f.output)
    adoptions = json.loads((target / 'zod-model-adoptions.json').read_text())
    assert len(adoptions) == 1
    assert adoptions[0]['adopted_version'] == version
    assert adoptions[0]['model_receipt']['checkpoint_sha256'] == 'a' * 64
    assert adoptions[0]['event_id'] == 'adopt-model-for-training'
