import json
from pathlib import Path
from types import SimpleNamespace

from filelock import FileLock
import pytest

from bcdata.bootstrap import atomic_json
from bcdata.teachers.base import file_sha256
from bcdata.ufld_queue import completed_run, enqueue, gpu_available, worker


def frozen_run(path):
    dataset = path / "dataset"
    dataset.mkdir(parents=True)
    atomic_json(dataset / "manifest.json", [{"image": "train.png", "split": "train"}, {"image": "val.png", "split": "val"}])
    atomic_json(dataset / "hashes.json", {"manifest.json": file_sha256(dataset / "manifest.json")})
    return path


def complete(path, epoch=20, epochs=20):
    path.mkdir(parents=True, exist_ok=True)
    checkpoint = path / "best.pt"
    checkpoint.write_bytes(b"disposable fake checkpoint; never loaded as model weights")
    state = {"phase": "trained_awaiting_review", "epoch": epoch, "epochs": epochs,
             "checkpoint": str(checkpoint), "checkpoint_sha256": file_sha256(checkpoint)}
    atomic_json(path / "status.json", state)
    return state


def queue_fixture(settings, tmp_path, done=True, *, warm_start=False):
    predecessor = tmp_path / "old"
    if done:
        complete(predecessor)
    else:
        atomic_json(predecessor / "status.json", {"phase": "training", "epoch": 14, "epochs": 20})
    prepared = frozen_run(tmp_path / "next")
    state = enqueue(settings, predecessor, prepared, launch=False, warm_start=warm_start)
    return predecessor, prepared, state


def test_enqueue_is_idempotent_and_does_not_change_active_report_or_frozen_data(settings, tmp_path):
    current_report = {"phase": "training", "epoch": 14}
    atomic_json(settings.path("reports", "ufld_training.json"), current_report)
    old, prepared, state = queue_fixture(settings, tmp_path, done=False)
    request_before = Path(state["request_path"]).read_bytes()
    inventory_before = (prepared / "dataset" / "hashes.json").read_bytes()
    again = enqueue(settings, old, prepared, launch=False)
    assert again["request_id"] == state["request_id"]
    assert len(list(settings.path("reports", "ufld_queue").glob("*/request.json"))) == 1
    assert Path(state["request_path"]).read_bytes() == request_before
    assert (prepared / "dataset" / "hashes.json").read_bytes() == inventory_before
    assert json.loads(settings.path("reports", "ufld_training.json").read_text()) == current_report
    assert state["state"] == "waiting"
    assert state["train_images"] == state["val_images"] == 1
    other = frozen_run(tmp_path / "another")
    with pytest.raises(ValueError, match="already queued"):
        enqueue(settings, old, other, launch=False)


@pytest.mark.parametrize("warm_start", [False, True])
def test_waits_for_predecessor_and_gpu_then_launches_exactly_once(settings, tmp_path, warm_start):
    old, prepared, state = queue_fixture(settings, tmp_path, done=False, warm_start=warm_start)
    request_before = Path(state["request_path"]).read_bytes()
    gpu_lock = FileLock(str(settings.path("logs", "large-models.lock")))
    gpu_lock.acquire()
    sleeps, launches = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        current = json.loads(settings.path("reports", "ufld_queue.json").read_text())
        assert current["state"] == "waiting"
        if len(sleeps) == 1:
            assert "predecessor" in current["reason"]
            complete(old)
        elif len(sleeps) == 2:
            assert "GPU" in current["reason"]
            gpu_lock.release()
        else:
            pytest.fail("Worker should not need another polling sleep")

    def launch(s, request):
        assert gpu_available(s)
        assert completed_run(old)
        assert request["prepared_run"] == str(prepared)
        if warm_start:
            assert request["warm_start_checkpoint"] == str(old / "best.pt")
            assert request["warm_start_checkpoint_sha256"] == file_sha256(old / "best.pt")
            assert request["optimizer_restored"] is False
        else:
            assert "warm_start_checkpoint" not in request
        launches.append(request["request_id"])
        complete(prepared)
        return SimpleNamespace(pid=12345, poll=lambda: 0)

    result = worker(settings, Path(state["request_path"]), launch_training=launch, sleep=sleep)
    assert len(sleeps) == 2 and launches == [state["request_id"]]
    assert result["state"] == "completed"
    assert result["training"]["checkpoint"] == str(prepared / "best.pt")
    assert Path(state["request_path"]).read_bytes() == request_before
    marker = json.loads(Path(state["request_path"]).with_name("launch.json").read_text())
    assert marker["warm_start"] is warm_start
    assert marker["predecessor_checkpoint_sha256"] == file_sha256(old / "best.pt")
    if warm_start:
        assert result["warm_start_checkpoint"] == marker["warm_start_checkpoint"] == str(old / "best.pt")
        assert result["warm_start_checkpoint_sha256"] == marker["warm_start_checkpoint_sha256"] == file_sha256(old / "best.pt")
        assert result["warm_start_source_epoch"] == marker["warm_start_source_epoch"] == 20
        assert result["warm_start_source_run"] == marker["warm_start_source_run"] == str(old)
        assert result["optimizer_restored"] is False
    assert worker(settings, Path(state["request_path"]), launch_training=lambda *_: pytest.fail("Duplicate trainer"))["state"] == "completed"


def test_hash_mismatch_blocks_launch_and_reports_error(settings, tmp_path):
    old, prepared, state = queue_fixture(settings, tmp_path)
    (old / "best.pt").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash"):
        worker(settings, state["request_path"], launch_training=lambda *_: pytest.fail("Must not launch"))
    latest = json.loads(settings.path("reports", "ufld_queue.json").read_text())
    assert latest["state"] == "error" and "hash" in latest["reason"]
    assert not Path(state["request_path"]).with_name("launch.json").exists()


def test_edited_frozen_inventory_blocks_launch(settings, tmp_path):
    old, prepared, state = queue_fixture(settings, tmp_path)
    atomic_json(prepared / "dataset" / "hashes.json", {"replacement": "bad"})
    with pytest.raises(ValueError, match="inventory changed"):
        worker(settings, state["request_path"], launch_training=lambda *_: pytest.fail("Must not launch"))


def test_predecessor_must_complete_requested_epochs(settings, tmp_path):
    old, prepared, state = queue_fixture(settings, tmp_path)
    complete(old, epoch=14)
    with pytest.raises(ValueError, match="all requested epochs"):
        worker(settings, state["request_path"], launch_training=lambda *_: pytest.fail("Must not launch"))


def test_child_failure_is_not_reported_as_success_and_is_not_retried(settings, tmp_path):
    old, prepared, state = queue_fixture(settings, tmp_path)
    calls = []

    def launch(*_):
        calls.append(1)
        return SimpleNamespace(pid=12345, poll=lambda: 9)

    with pytest.raises(RuntimeError, match="exited with code 9"):
        worker(settings, state["request_path"], launch_training=launch)
    assert calls == [1]
    assert worker(settings, state["request_path"], launch_training=lambda *_: pytest.fail("Must not retry"))["state"] == "error"


def test_restarted_worker_recognizes_finished_child_without_launching_again(settings, tmp_path):
    old, prepared, state = queue_fixture(settings, tmp_path)
    atomic_json(Path(state["request_path"]).with_name("launch.json"), {"child_pid": 12345})
    complete(prepared)
    result = worker(settings, state["request_path"], launch_training=lambda *_: pytest.fail("Must not relaunch"))
    assert result["state"] == "completed"


def test_two_workers_do_not_share_one_request(settings, tmp_path):
    old, prepared, state = queue_fixture(settings, tmp_path)
    with FileLock(str(Path(state["request_path"]).with_name("worker.lock"))):
        result = worker(settings, state["request_path"], launch_training=lambda *_: pytest.fail("Must not launch"))
    assert result["state"] == "waiting"


@pytest.mark.parametrize("warm_start", [False, True])
def test_hidden_child_command_uses_frozen_run_and_waits_for_gpu(settings, tmp_path, monkeypatch, warm_start):
    from bcdata import ufld_queue
    old, prepared, state = queue_fixture(settings, tmp_path, warm_start=warm_start)
    observed = []

    def spawn(command, s, log):
        observed.append((command, s, log))
        complete(prepared)
        return SimpleNamespace(pid=12345, poll=lambda: 0)

    monkeypatch.setattr(ufld_queue, "_spawn", spawn)
    worker(settings, state["request_path"])
    command, _, log = observed[0]
    expected = ["--prepared-run", str(prepared), "--epochs", "20", "--gpu-lock-timeout", "-1"]
    if warm_start:
        expected += ["--warm-start", str(old / "best.pt")]
    assert command[3:] == expected
    assert "--resume-run" not in command
    assert ".gpu-venv" in command[0]
    assert log.name == "training.log"


def test_warm_start_mode_is_immutable_and_part_of_request_identity(settings, tmp_path):
    old, prepared, state = queue_fixture(settings, tmp_path, warm_start=True)
    request = Path(state["request_path"])
    before = request.read_bytes()
    sealed = json.loads(before)
    assert sealed["warm_start"] is True
    assert "warm_start_checkpoint" not in sealed
    assert enqueue(settings, old, prepared, launch=False, warm_start=True)["request_id"] == state["request_id"]
    with pytest.raises(ValueError, match="already queued"):
        enqueue(settings, old, prepared, launch=False)
    assert request.read_bytes() == before


def test_legacy_request_without_mode_keeps_fresh_initialization(settings, tmp_path, monkeypatch):
    from bcdata import ufld_queue
    old, prepared, state = queue_fixture(settings, tmp_path)
    request_path = Path(state["request_path"])
    for path in (request_path, request_path.with_name("status.json"), settings.path("reports", "ufld_queue.json")):
        legacy = json.loads(path.read_text())
        legacy.pop("warm_start")
        atomic_json(path, legacy)
    before = request_path.read_bytes()
    assert enqueue(settings, old, prepared, launch=False)["request_id"] == state["request_id"]
    with pytest.raises(ValueError, match="already queued"):
        enqueue(settings, old, prepared, launch=False, warm_start=True)
    commands = []

    def spawn(command, *_):
        commands.append(command)
        complete(prepared)
        return SimpleNamespace(pid=12345, poll=lambda: 0)

    monkeypatch.setattr(ufld_queue, "_spawn", spawn)
    result = worker(settings, request_path)
    assert result["state"] == "completed"
    assert "--warm-start" not in commands[0]
    assert "--resume-run" not in commands[0]
    assert "warm_start_checkpoint" not in result
    assert request_path.read_bytes() == before


def test_cached_predecessor_hash_is_rechecked_after_gpu_wait(settings, tmp_path):
    old, prepared, state = queue_fixture(settings, tmp_path, warm_start=True)
    gpu_lock = FileLock(str(settings.path("logs", "large-models.lock")))
    gpu_lock.acquire()

    def sleep(_):
        current = json.loads(settings.path("reports", "ufld_queue.json").read_text())
        assert current["state"] == "waiting" and "GPU" in current["reason"]
        (old / "best.pt").write_bytes(b"changed while the worker waited for the GPU")
        gpu_lock.release()

    try:
        with pytest.raises(ValueError, match="hash"):
            worker(settings, state["request_path"], sleep=sleep,
                   launch_training=lambda *_: pytest.fail("Unverified weights must never launch"))
    finally:
        gpu_lock.release()
    assert not Path(state["request_path"]).with_name("launch.json").exists()


def test_non_boolean_warm_start_mode_is_rejected(settings, tmp_path):
    old = tmp_path / "old"
    complete(old)
    prepared = frozen_run(tmp_path / "next")
    with pytest.raises(ValueError, match="boolean"):
        enqueue(settings, old, prepared, launch=False, warm_start="false")


def test_next_request_replaces_completed_queue_report_and_old_worker_cannot_overwrite_it(settings, tmp_path):
    from bcdata.ufld_queue import _write_status
    old, prepared, state = queue_fixture(settings, tmp_path)
    _write_status(settings, state, state="completed")
    complete(prepared)
    next_run = frozen_run(tmp_path / "third")
    next_state = enqueue(settings, prepared, next_run, launch=False)
    assert json.loads(settings.path("reports", "ufld_queue.json").read_text())["request_id"] == next_state["request_id"]
    _write_status(settings, state, state="completed", reason="Late old worker update")
    assert json.loads(settings.path("reports", "ufld_queue.json").read_text())["request_id"] == next_state["request_id"]
