import json
import multiprocessing
import os
import stat
import time

import pytest

from mutation_gate import store


def test_config_defaults_when_file_missing(gate_home):
    cfg = store.load_config()
    assert cfg["threshold"] == 80
    assert cfg["budget_seconds"] == 480
    assert cfg["enabled"] == []


def test_set_threshold_persists(gate_home):
    store.update_config(threshold=85)
    assert store.load_config()["threshold"] == 85
    assert json.loads((gate_home / "config.json").read_text())["threshold"] == 85


def test_projects_are_off_until_enabled(gate_home):
    assert store.is_enabled("/p/one") is False
    store.set_enabled("/p/one", True)
    assert store.is_enabled("/p/one") is True
    assert store.is_enabled("/p/two") is False
    store.set_enabled("/p/one", False)
    assert store.is_enabled("/p/one") is False


@pytest.mark.parametrize("raw, key, expected", [
    ({"threshold": 150}, "threshold", 80),
    ({"threshold": "x"}, "threshold", 80),
    ({"budget_seconds": 5}, "budget_seconds", 60),
    ({"budget_seconds": 9999}, "budget_seconds", 540),
])
def test_config_values_are_validated(gate_home, raw, key, expected):
    gate_home.mkdir(parents=True, exist_ok=True)
    (gate_home / "config.json").write_text(json.dumps(raw))
    assert store.load_config()[key] == expected


def test_allowlist_is_per_project(gate_home):
    store.allow("/p/one", "abc12345", "equivalent: loop bound")
    assert store.allowed_ids("/p/one") == {"abc12345"}
    assert store.allowed_ids("/p/two") == set()
    store.disallow("/p/one", "abc12345")
    assert store.allowed_ids("/p/one") == set()


def test_session_records_first_base_per_repo_only(gate_home):
    store.remember_repo("s1", "/repo", "base1")
    store.remember_repo("s1", "/repo", "base2")
    store.remember_repo("s1", "/other", "base3")

    assert store.load_session("s1")["repos"] == {"/repo": "base1", "/other": "base3"}



def test_cached_verdict_round_trip(gate_home):
    store.save_verdict("s1", "/repo", "fp1", {"passed": True})
    assert store.cached_verdict("s1", "/repo", "fp1") == {"passed": True}
    assert store.cached_verdict("s1", "/repo", "fp2") is None


def test_corrupt_session_file_starts_fresh(gate_home):
    store.remember_repo("s1", "/repo", "base1")
    (gate_home / "state" / "sessions" / "s1.json").write_text("{not json")
    assert store.load_session("s1")["repos"] == {}


def test_corrupt_allowlist_is_not_overwritten(gate_home):
    store.allow("/p", "abcd1234", "r")
    (gate_home / "allow.json").write_text("{broken")
    with pytest.raises(store.CorruptFile):
        store.allow("/p", "ffff0000", "r")
    assert (gate_home / "allow.json").read_text() == "{broken"


def test_repo_lock_is_exclusive(gate_home):
    with store.repo_lock("/repo"):
        assert store.repo_lock("/repo").acquire(timeout=0.2) is False
    lock = store.repo_lock("/repo")
    assert lock.acquire(timeout=0.2) is True
    lock.release()


def _remember_many(home, start):
    os.environ["MUTATION_GATE_HOME"] = home
    for i in range(start, start + 25):
        store.remember_repo("s1", f"/repo{i}", "base")


def test_concurrent_session_updates_are_not_lost(gate_home):
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_remember_many, args=(str(gate_home), n * 25)) for n in range(4)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()
    assert len(store.load_session("s1")["repos"]) == 100


def test_state_dirs_are_private(gate_home):
    store.remember_repo("s1", "/repo", "base")
    mode = stat.S_IMODE((gate_home / "state").stat().st_mode)
    assert mode == 0o700


def test_prune_removes_old_sessions_only(gate_home):
    store.remember_repo("old", "/repo", "base")
    store.remember_repo("new", "/repo", "base")
    old = gate_home / "state" / "sessions" / "old.json"
    past = time.time() - 30 * 86400
    os.utime(old, (past, past))
    store.prune_sessions(days=14)
    assert not old.exists()
    assert (gate_home / "state" / "sessions" / "new.json").exists()


def test_private_dir_never_touches_paths_outside_home(gate_home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    store._private_dir(outside / "child")
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
