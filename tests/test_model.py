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
