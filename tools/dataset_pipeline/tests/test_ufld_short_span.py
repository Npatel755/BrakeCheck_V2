"""Regression checks for separately reported short-span UFLDv2 evaluation."""
from contextlib import nullcontext
import copy
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from bcdata import ufld_baseline as baseline


def predictions(batch_size=1):
    pred = {}
    for axis, grids, anchors in (("row", 200, 72), ("col", 100, 81)):
        pred["loc_" + axis] = torch.full((batch_size, grids, anchors, 4), -10.)
        pred["loc_" + axis][:, grids // 2] = 10.
        pred["exist_" + axis] = torch.zeros((batch_size, 2, anchors, 4))
        pred["exist_" + axis][:, 0] = 8.
    return pred


def activate(pred, axis, slot, anchors, probability=.9, image=0):
    pred["exist_" + axis][image, 0, anchors, slot] = 0.
    pred["exist_" + axis][image, 1, anchors, slot] = math.log(probability / (1 - probability))


@pytest.mark.parametrize(("axis", "slot", "anchors"), [
    ("row", 1, list(range(35, 39))),
    ("row", 2, list(range(35, 39))),
    ("col", 0, list(range(10, 14))),
    ("col", 3, list(range(10, 14))),
])
def test_confident_short_trace_is_secondary_only(axis, slot, anchors):
    pred = predictions()
    activate(pred, axis, slot, anchors)
    assert baseline.decode(pred, 0) == baseline.decode(pred, 0, "stock") == []
    lines = baseline.decode(pred, 0, "short_span_v1")
    assert len(lines) == 1 and len(lines[0]) == 4
    anchor_axis = 1 if axis == "row" else 0
    source = baseline.ROW_ANCHORS if axis == "row" else baseline.COL_ANCHORS
    extent = baseline.HEIGHT if axis == "row" else baseline.WIDTH
    np.testing.assert_allclose(np.asarray(lines[0])[:, anchor_axis], source[anchors] * extent)


@pytest.mark.parametrize("probability", [.51, .74])
def test_positive_argmax_alone_does_not_pass_short_span_confidence(probability):
    pred = predictions()
    activate(pred, "row", 1, list(range(20, 60)), probability)
    assert len(baseline.decode(pred, 0)) == 1  # historical default stays unchanged
    assert baseline.decode(pred, 0, "short_span_v1") == []


def test_mean_confidence_uses_only_positive_anchors_and_minimum_four():
    pred = predictions()
    activate(pred, "row", 1, [30, 31, 32], .99)
    assert baseline.decode(pred, 0, "short_span_v1") == []
    activate(pred, "row", 1, [33], .76)
    assert len(baseline.decode(pred, 0, "short_span_v1")) == 1


@pytest.mark.parametrize(("axis", "slot", "minimum"), [("row", 1, 37), ("col", 0, 21)])
def test_historical_gate_boundaries_and_local_geometry_remain_unchanged(axis, slot, minimum):
    pred = predictions()
    activate(pred, axis, slot, list(range(5, 5 + minimum - 1)))
    assert baseline.decode(pred, 0) == []
    activate(pred, axis, slot, [5 + minimum - 1])
    stock = baseline.decode(pred, 0)
    assert len(stock) == 1
    assert stock == baseline.decode(pred, 0, "short_span_v1")


def test_empty_heads_and_unsupported_policy():
    pred = predictions()
    assert baseline.decode(pred, 0) == baseline.decode(pred, 0, "short_span_v1") == []
    with pytest.raises(ValueError, match="Unknown.*decoder"):
        baseline.decode(pred, 0, "typo")


def test_validation_keeps_stock_primary_and_counts_secondary_negative_false_positives(tmp_path, monkeypatch):
    pred = predictions(2)
    for image in (0, 1):
        activate(pred, "row", 1, [35, 36, 37, 38], image=image)
    line = baseline.decode(pred, 0, "short_span_v1")[0]
    (tmp_path / "positive.lines.txt").write_text(" ".join(str(x) for point in line for x in point))
    (tmp_path / "negative.lines.txt").write_text("")
    dataset = SimpleNamespace(root=tmp_path, records=[{"image": "positive.jpg"}, {"image": "negative.jpg"}])

    class Loader:
        def __init__(self):
            self.dataset = dataset

        def __iter__(self):
            yield {"images": torch.zeros(2, 3, 1, 1), "index": torch.tensor([0, 1])}

    class Model:
        calls = 0

        def eval(self):
            pass

        def __call__(self, images):
            self.calls += 1
            return pred

    monkeypatch.setattr(torch, "autocast", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(baseline, "criterion", lambda *a: (torch.tensor(2.), {}))
    model = Model()
    metrics = baseline.validate(model, Loader(), "cpu")
    assert model.calls == 1  # both policies consume the same forward pass
    assert (metrics["tp"], metrics["fp"], metrics["fn"]) == (0, 0, 1)
    assert metrics["lane_f1"] == 0 and metrics["decoder_policy"] == "stock"
    assert metrics["negative_images"] == metrics["correct_negative_images"] == 1
    secondary = metrics["decoder_comparisons"]["short_span_v1"]
    assert (secondary["tp"], secondary["fp"], secondary["fn"]) == (1, 1, 0)
    assert secondary["negative_images"] == 1 and secondary["correct_negative_images"] == 0
    assert secondary["images"] == metrics["images"] == 2
    assert secondary["loss"] == metrics["loss"] == 2
    assert "not an independent test score" in secondary["interpretation"]


def test_new_fine_tuning_options_and_recorded_resume_schedule():
    args = baseline.parse_arguments(["--warm-start", "best.pt", "--evaluate-initial",
                                     "--learning-rate", ".000625", "--warmup-steps", "40"])
    assert args.evaluate_initial and baseline.training_schedule(args) == (.000625, 40)
    assert baseline.training_schedule(args, {"learning_rate": .002, "warmup_steps": 70}) == (.002, 70)
    assert baseline.training_schedule(args, {"learning_rate": .00625}) == (.00625, 110)
    assert baseline.training_schedule(args, {}) == (.00625, 110)
    assert baseline.schedule_multiplier(0, 100, 40) == pytest.approx(1 / 40)
    assert baseline.schedule_multiplier(0, 100, 0) == 1
    assert baseline.schedule_multiplier(100, 100, 0) == 0


@pytest.mark.parametrize("arguments", [
    ["--learning-rate", "0"], ["--learning-rate", "nan"], ["--learning-rate", "inf"],
    ["--learning-rate", "-1"], ["--warmup-steps", "-1"],
    ["--evaluate-initial"], ["--resume-run", "old", "--evaluate-initial"],
    ["--gpu-lock-timeout", "nan"],
])
def test_invalid_training_options_fail_before_cuda_or_dataset_access(arguments):
    with pytest.raises(SystemExit):
        baseline.parse_arguments(arguments)


@pytest.mark.parametrize("config", [
    {"learning_rate": float("nan")}, {"learning_rate": 0}, {"learning_rate": True},
    {"warmup_steps": -1}, {"warmup_steps": .5}, {"warmup_steps": True},
])
def test_invalid_saved_schedule_is_not_resumed(config):
    with pytest.raises(ValueError):
        baseline.training_schedule(baseline.parse_arguments([]), config)


def test_resume_rejects_changed_decoder_rules_but_accepts_legacy_checkpoints():
    baseline.validate_resume_decoder_policies({})
    baseline.validate_resume_decoder_policies({"decoder_policies": copy.deepcopy(baseline.DECODER_POLICIES)})
    changed = copy.deepcopy(baseline.DECODER_POLICIES)
    changed["short_span_v1"]["row_minimum"] = 12
    with pytest.raises(ValueError, match="decoder policies changed"):
        baseline.validate_resume_decoder_policies({"decoder_policies": changed})


def test_initial_checkpoint_survives_regression_and_each_decoder_selects_its_own_best(tmp_path):
    model = torch.nn.Linear(1, 1, bias=False)
    optimizer = torch.optim.SGD(model.parameters(), lr=.000625, momentum=.9)
    config = {"learning_rate": .000625, "warmup_steps": 40, "manifest_sha256": "corrected-label-snapshot"}

    def metrics(stock, short):
        return {"lane_f1": stock, "decoder_comparisons": {"short_span_v1": {"lane_f1": short}}}

    def read(name):
        return torch.load(tmp_path / name, map_location="cpu", weights_only=False)

    with torch.no_grad():
        model.weight.fill_(1.)
    best, short_best = baseline.save_training_checkpoint(
        tmp_path, model, optimizer, epoch=-1, step=0, history=[], config=config,
        metrics=metrics(.72, .79), best=-1., best_short_span=-1., initial_baseline=True)
    initial_bytes = {name: (tmp_path / name).read_bytes() for name in ("best.pt", "best_short_span.pt")}
    initial = read("last.pt")
    assert initial["epoch"] + 1 == 0 and initial["step"] == 0  # valid resume starting point
    assert initial["initial_baseline"] is True and initial["history"] == []
    assert initial["best"] == best == .72 and initial["best_short_span"] == short_best == .79
    assert initial["config"]["manifest_sha256"] == "corrected-label-snapshot"

    with torch.no_grad():
        model.weight.fill_(2.)
    best, short_best = baseline.save_training_checkpoint(
        tmp_path, model, optimizer, epoch=0, step=10, history=[{"epoch": 1}], config=config,
        metrics=metrics(.70, .78), best=best, best_short_span=short_best)
    assert all((tmp_path / name).read_bytes() == data for name, data in initial_bytes.items())
    last = read("last.pt")
    assert last["epoch"] == 0 and last["initial_baseline"] is False
    assert last["model"]["weight"].item() == 2.
    assert read("best.pt")["model"]["weight"].item() == 1.

    with torch.no_grad():
        model.weight.fill_(3.)
    best, short_best = baseline.save_training_checkpoint(
        tmp_path, model, optimizer, epoch=1, step=20, history=[{"epoch": 1}, {"epoch": 2}], config=config,
        metrics=metrics(.73, .77), best=best, best_short_span=short_best)
    assert read("best.pt")["model"]["weight"].item() == 3.
    assert (tmp_path / "best_short_span.pt").read_bytes() == initial_bytes["best_short_span.pt"]
    assert best == .73 and short_best == .79

    with torch.no_grad():
        model.weight.fill_(4.)
    best, short_best = baseline.save_training_checkpoint(
        tmp_path, model, optimizer, epoch=2, step=30, history=[{"epoch": 3}], config=config,
        metrics=metrics(.71, .80), best=best, best_short_span=short_best)
    assert read("best.pt")["model"]["weight"].item() == 3.
    candidate = read("best_short_span.pt")
    assert candidate["model"]["weight"].item() == 4.
    assert candidate["validation"]["decoder_comparisons"]["short_span_v1"]["lane_f1"] == .80
    assert best == .73 and short_best == .80
