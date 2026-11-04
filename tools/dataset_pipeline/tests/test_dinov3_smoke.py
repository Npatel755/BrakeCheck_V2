"""Training-loop smoke test for the DINOv3 decoder with a tiny stand-in backbone (no gated weights, CPU)."""
import pytest

torch = pytest.importorskip("torch")

from bcdata.teachers.dinov3_seg import Dinov3SegTeacher, train  # noqa: E402
from tests.test_pipeline import build  # noqa: E402


def test_train_save_load_predict_and_refuse(settings, registry, licenses, tmp_path):
    build(settings, registry, licenses, n=6)
    from bcdata.splits import assign
    with registry.tx() as c:
        c.execute("UPDATE images SET status='curated'")
    assign(settings, registry)
    with registry.tx() as c:                                   # guarantee train and val images
        ids = [r["image_id"] for r in registry.images()]
        for k, i in enumerate(ids):
            c.execute("UPDATE images SET split=? WHERE image_id=?", ("val" if k % 3 == 0 else "train", i))
    settings.cfg["teachers"]["dinov3_seg"].update(input_width=256, input_height=128)
    ckpt = tmp_path / "tiny.pt"
    res = train(settings, registry, ckpt, backbone_id="test:tiny", max_steps=6, batch_size=2, device="cpu")
    assert ckpt.exists() and res["steps"] == 6 and res["train_images"] == 4
    state = torch.load(ckpt, map_location="cpu")
    assert state["trained_steps"] > 0 and "lane_iou" in state["val_metrics"]
    assert state["backbone_frozen"] is True and state["test_images_used"] == 0
    assert state["trainable_parameters"] > 0

    t = Dinov3SegTeacher(settings, registry)
    t.ckpt = ckpt
    t.load()
    rgb = __import__("tests.synth", fromlist=["scene"]).scene()[0]
    out = t.predict(rgb, None)
    assert out.lane.shape == rgb.shape[:2] and 0.0 <= out.lane.min() and out.lane.max() <= 1.0
    t.unload()

    legacy_step = int(state["trained_steps"])
    resumed = train(settings, registry, tmp_path / "resumed" / "best.pt", backbone_id="test:tiny",
                    max_steps=legacy_step + 2, batch_size=2, device="cpu", resume_from=ckpt)
    assert resumed["steps"] == legacy_step + 2 and resumed["optimizer_restored"] is False
    last = tmp_path / "resumed" / "last.pt"
    assert "optimizer" in torch.load(last, weights_only=True)
    restored = train(settings, registry, tmp_path / "restored" / "best.pt", backbone_id="test:tiny",
                     max_steps=legacy_step + 4, batch_size=2, device="cpu", resume_from=last)
    assert restored["steps"] == legacy_step + 4 and restored["optimizer_restored"] is True

    state["trained_steps"] = 0                                 # an untrained decoder must be refused
    torch.save(state, tmp_path / "untrained.pt")
    t.ckpt = tmp_path / "untrained.pt"
    with pytest.raises(RuntimeError, match="refusing"):
        t.load()


def test_backbone_unchanged_by_decoder_training():
    from bcdata.teachers.dinov3_seg import build_model
    model = build_model("test:tiny")
    before = {name: parameter.detach().clone() for name, parameter in model.backbone.named_parameters()}
    decoder_before = model.decoder.head[-1].weight.detach().clone()
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=1e-3)
    loss = model(torch.randn(1, 3, 64, 128)).square().mean()
    loss.backward()
    optimizer.step()
    assert all(not parameter.requires_grad and parameter.grad is None and torch.equal(before[name], parameter)
               for name, parameter in model.backbone.named_parameters())
    assert not torch.equal(decoder_before, model.decoder.head[-1].weight)


def test_decoder_data_excludes_dropped_rejected_and_test(settings, registry, licenses):
    from bcdata.teachers.dinov3_seg import _load_pairs
    build(settings, registry, licenses, n=6)
    ids = [r["image_id"] for r in registry.images()]
    with registry.tx() as connection:
        connection.execute("UPDATE images SET status='curated',split='train'")
        connection.execute("UPDATE images SET status='dropped' WHERE image_id=?", (ids[0],))
        connection.execute("UPDATE images SET review_status='rejected' WHERE image_id=?", (ids[1],))
        connection.execute("UPDATE images SET split='test' WHERE image_id=?", (ids[2],))
        connection.execute("UPDATE images SET split='val' WHERE image_id=?", (ids[3],))
    pairs = _load_pairs(registry, ("train",), ("commercial",))
    assert {path for path, _ in pairs} == {registry.image(i)["path"] for i in ids[4:]}
    assert len(_load_pairs(registry, ("val",), ("commercial",))) == 1
    with pytest.raises(ValueError, match="train/val only"):
        _load_pairs(registry, ("test",), ("commercial",))
