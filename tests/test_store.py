import json

import pytest

from mutation_gate import store


def test_config_defaults_when_file_missing(gate_home):
    cfg = store.load_config()
    assert cfg["threshold"] == 80
    assert cfg["max_blocks"] == 3
    assert cfg["budget_seconds"] == 480
    assert cfg["disabled"] == []


def test_set_threshold_persists(gate_home):
    store.update_config(threshold=85)
    assert store.load_config()["threshold"] == 85
    assert json.loads((gate_home / "config.json").read_text())["threshold"] == 85


def test_disable_and_enable_project(gate_home):
    store.set_enabled("/p/one", False)
    assert store.is_enabled("/p/one") is False
    assert store.is_enabled("/p/two") is True
    store.set_enabled("/p/one", True)
    assert store.is_enabled("/p/one") is True


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


def test_session_block_counter_resets(gate_home):
    assert store.bump_blocks("s1") == 1
    assert store.bump_blocks("s1") == 2
    store.reset_blocks("s1")
    assert store.load_session("s1")["blocks"] == 0


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
        assert store.repo_lock("/repo", blocking=False).acquire() is False
    lock = store.repo_lock("/repo", blocking=False)
    assert lock.acquire() is True
    lock.release()
