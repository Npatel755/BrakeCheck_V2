"""Audi repair gates preserve real sample approval and distinguish blocked work."""
import json
import uuid

from bcdata.source_validation import A2D2_REPAIR_POLICY, refresh_groups, run
from tests.test_a2d2_pipeline import audi_fixture, approve


def prepared(tmp_path):
    settings, store, ids = audi_fixture(tmp_path)
    report = run(settings, apply=True, source_name="a2d2", image_ids=ids, include_staged=True)
    with store.registry() as reg:
        for image_id in ids:
            reg.update_image(image_id, status="curated")
        reg.conn.execute("UPDATE source_validation_groups SET batch_id=?", (A2D2_REPAIR_POLICY+":fixture",))
        reg.conn.commit()
    return settings, store, report["sample_ids"]


def test_audi_repair_pass_evidence_keeps_its_policy_and_actual_samples(tmp_path):
    settings, store, samples = prepared(tmp_path)
    for image_id in samples:
        approve(store, image_id)
    refresh_groups(settings)
    with store.registry() as reg:
        group = reg.conn.execute("SELECT * FROM source_validation_groups").fetchone()
        evidence = json.loads(group["evidence"])
        assert group["state"] == "passed"
        assert evidence["policy"] == A2D2_REPAIR_POLICY
        assert set(evidence["human_approved_sample_ids"]) == set(samples)
    assert store.stats()["held_source_repairs"] == 0


def test_blocked_images_are_counted_as_repairs_without_becoming_manual_samples(tmp_path):
    settings, store, samples = prepared(tmp_path)
    before = store.stats()
    assert before["awaiting_source_audit"] == 3 and before["held_source_repairs"] == 0
    store.decide(samples[0], "corrected", "Disposable test reviewer", 0, str(uuid.uuid4()), lanes=[])
    for image_id in samples[1:]:
        approve(store, image_id)
    refresh_groups(settings)
    stats = store.stats()
    assert stats["source_audit_samples"] == 0
    assert stats["awaiting_source_audit"] == stats["held_source_repairs"] == 3
    assert stats["source_validated"] == 0
