import sqlite3

import pytest

from db import JobStore


def create(store, tmp_path, name="job"):
    return store.create(
        job_name=name, task_id="task", backend_id=name, payload="track:1",
        mount_root=tmp_path, workspace_path=tmp_path / name,
    )


def test_restart_preserves_ownership_and_completed_evidence(tmp_path):
    path = tmp_path / "state" / "jobs.sqlite3"
    store = JobStore(path)
    store.initialize()
    create(store, tmp_path)
    create(store, tmp_path, "running")
    file = tmp_path / "job" / "track.flac"
    store.update("job", state="completed", file_paths=(file,))
    reopened = JobStore(path)
    reopened.initialize()
    assert reopened.recover_interrupted() == 1
    assert reopened.get("running").state == "interrupted"
    assert reopened.get("job").file_paths == (file,)
    assert reopened.get("job").state == "completed"
    assert reopened.get("unknown") is None


def test_cleanup_tombstones_and_pruning(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    create(store, tmp_path)
    create(store, tmp_path, "keep")
    with pytest.raises(ValueError):
        store.mark_cleaned("job")
    store.update("job", state="failed", error="failed")
    cleaned = store.mark_cleaned("job")
    assert store.mark_cleaned("job") == cleaned
    with pytest.raises(ValueError):
        store.update("job", state="completed")
    assert store.prune_cleaned(before=cleaned.cleaned_at) == 0
    assert store.prune_cleaned(before=cleaned.cleaned_at + 1) == 1
    assert store.get("keep") is not None
    store.maintain()
    assert store.get("keep") is not None


def test_conflicts_and_invalid_evidence_do_not_overwrite_records(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    original = create(store, tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        create(store, tmp_path)
    with pytest.raises(ValueError):
        store.update("job", state="completed", file_paths=(tmp_path / "outside.flac",))
    assert store.get("job") == original
    with pytest.raises(KeyError):
        store.mark_cleaned("unknown")


def test_future_schema_is_not_modified(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="version: 99"):
        JobStore(path).initialize()
