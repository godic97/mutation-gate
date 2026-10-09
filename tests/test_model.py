import os
import subprocess
import time

import pytest

from mutation_gate.model import Mutant, run_tree, to_ranges


def m(**kw):
    base = dict(path="src/a.ts", line=3, mutator="EqualityOperator", original="x < lo", replacement="x <= lo", status="undetected")
    base.update(kw)
    return Mutant(**base)


def test_mutant_id_ignores_line_number_and_indentation():
    assert m(line=3).id == m(line=40, original="  x < lo ").id


def test_mutant_id_differs_by_replacement_and_path():
    assert m().id != m(replacement="x >= lo").id
    assert m().id != m(path="src/b.ts").id


def test_mutant_id_is_short_hex():
    assert len(m().id) == 8
    int(m().id, 16)


def test_to_ranges_merges_consecutive_lines():
    assert to_ranges({5, 1, 2, 3, 9, 10}) == [(1, 3), (5, 5), (9, 10)]


def test_to_ranges_empty():
    assert to_ranges(set()) == []


def test_run_tree_kills_grandchildren_on_timeout(tmp_path):
    pid_file = tmp_path / "pid"
    with pytest.raises(subprocess.TimeoutExpired):
        run_tree(["sh", "-c", f"sleep 30 & echo $! > {pid_file}; wait"], cwd=tmp_path, env=os.environ, timeout=1)
    pid = int(pid_file.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_mutant_id_differs_by_context_and_occurrence():
    base = m()
    assert Mutant(**{**base.__dict__, "context": "x_f"}).id != base.id
    assert Mutant(**{**base.__dict__, "occurrence": 1}).id != base.id


def test_number_occurrences_separates_identical_mutants():
    from mutation_gate.model import number_occurrences

    a, b = m(line=3), m(line=7)
    number_occurrences([b, a])
    assert (a.occurrence, b.occurrence) == (0, 1)
    assert a.id != b.id


def test_kill_active_stops_running_groups(tmp_path):
    import threading

    from mutation_gate.model import ACTIVE_GROUPS, kill_active

    pid_file = tmp_path / "pid"
    errors = []

    def runner():
        try:
            run_tree(["sh", "-c", f"sleep 30 & echo $! > {pid_file}; wait"], cwd=tmp_path, env=os.environ, timeout=20)
        except Exception as exc:  # killed: communicate returns, nothing raised
            errors.append(exc)

    t = threading.Thread(target=runner)
    t.start()
    for _ in range(50):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        time.sleep(0.1)
    assert ACTIVE_GROUPS
    kill_active()
    t.join(5)
    assert not t.is_alive()
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
