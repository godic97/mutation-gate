import json

import pytest

from conftest import git
from mutation_gate import diff, gate, store
from mutation_gate.model import DETECTED, UNDETECTED, AdapterResult, Mutant


def mutant(status, line=2, replacement="x <= lo", path="src/a.ts"):
    return Mutant(path=path, line=line, mutator="EqualityOperator", original="x < lo", replacement=replacement, status=status)


class FakeAdapter:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, repo, changed, budget):
        self.calls.append(changed)
        return self.result


@pytest.fixture
def ts_change(repo):
    repo.write("src/a.ts", "a\nb\n")
    repo.commit()
    base = diff.snapshot(repo.path)
    repo.write("src/a.ts", "a\nB\n")
    repo.write("src/a.test.ts", "test\n")
    return repo, base


def evaluate(repo, base, result, threshold=80, allowed=()):
    fake = FakeAdapter(result)
    current = diff.snapshot(repo.path)
    verdict = gate.evaluate(str(repo.path), base, current, threshold, set(allowed), 60, {"js": fake, "py": fake})
    return verdict, fake


# --- evaluate -------------------------------------------------------------------------------


def test_only_changed_source_lines_go_to_adapter(ts_change):
    repo, base = ts_change
    _, fake = evaluate(repo, base, AdapterResult(mutants=[mutant(DETECTED)]))
    assert fake.calls == [{"src/a.ts": {2}}]


def test_passes_when_score_meets_threshold(ts_change):
    repo, base = ts_change
    mutants = [mutant(DETECTED, replacement=str(i)) for i in range(4)] + [mutant(UNDETECTED)]
    verdict, _ = evaluate(repo, base, AdapterResult(mutants=mutants))
    assert verdict["status"] == "pass"
    assert (verdict["detected"], verdict["total"], verdict["score"]) == (4, 5, 80.0)


def test_fails_below_threshold_and_lists_survivors(ts_change):
    repo, base = ts_change
    verdict, _ = evaluate(repo, base, AdapterResult(mutants=[mutant(DETECTED, replacement="y"), mutant(UNDETECTED)]))
    assert verdict["status"] == "fail"
    assert verdict["score"] == 50.0
    [s] = verdict["survivors"]
    assert s["path"] == "src/a.ts" and s["line"] == 2 and s["replacement"] == "x <= lo"
    assert s["id"] == mutant(UNDETECTED).id


def test_allowed_mutants_leave_the_denominator(ts_change):
    repo, base = ts_change
    survivor = mutant(UNDETECTED)
    verdict, _ = evaluate(repo, base, AdapterResult(mutants=[mutant(DETECTED, replacement="y"), survivor]), allowed={survivor.id})
    assert verdict["status"] == "pass"
    assert (verdict["detected"], verdict["total"], verdict["allowed"]) == (1, 1, 1)


def test_adapter_failure_fails_the_gate(ts_change):
    repo, base = ts_change
    verdict, _ = evaluate(repo, base, AdapterResult(failure="테스트가 현재 실패함"))
    assert verdict["status"] == "fail"
    assert verdict["failures"] == ["테스트가 현재 실패함"]


def test_adapter_error_is_reported_not_failed(ts_change):
    repo, base = ts_change
    verdict, _ = evaluate(repo, base, AdapterResult(error="Stryker가 설치되어 있지 않음", error_kind="missing"))
    assert verdict["status"] == "error"
    assert verdict["errors"] == ["Stryker가 설치되어 있지 않음"]




def test_added_suppression_comment_is_reported(repo):
    repo.write("src/a.ts", "a\n")
    repo.commit()
    base = diff.snapshot(repo.path)
    repo.write("src/a.ts", "a\n// Stryker disable next-line all\nb\n")
    verdict, _ = evaluate(repo, base, AdapterResult(mutants=[mutant(DETECTED)]))
    assert verdict["status"] == "pass"
    assert any("src/a.ts:2" in w for w in verdict["warnings"])


def test_skipping_a_test_is_reported(repo):
    repo.write("src/a.test.ts", "it('x', () => {})\n")
    repo.commit()
    base = diff.snapshot(repo.path)
    repo.write("src/a.test.ts", "it.skip('x', () => {})\n")
    verdict, fake = evaluate(repo, base, AdapterResult())
    assert any("it.skip" in w for w in verdict["warnings"])


def test_mutmut_config_change_is_reported(repo):
    repo.write("pyproject.toml", '[tool.mutmut]\nsource_paths = ["src"]\n')
    repo.commit()
    base = diff.snapshot(repo.path)
    repo.write("pyproject.toml", '[tool.mutmut]\nsource_paths = ["lib"]\n')
    verdict, _ = evaluate(repo, base, AdapterResult())
    assert any("pyproject.toml" in w for w in verdict["warnings"])


def test_deleted_test_file_is_a_warning(repo):
    repo.write("tests/test_a.py", "def test_a(): pass\n")
    repo.commit()
    base = diff.snapshot(repo.path)
    (repo.path / "tests" / "test_a.py").unlink()
    verdict, _ = evaluate(repo, base, AdapterResult())
    assert verdict["status"] == "pass"
    assert "tests/test_a.py" in verdict["warnings"][0]


def test_test_only_changes_are_clean(repo):
    repo.commit()
    base = diff.snapshot(repo.path)
    repo.write("src/a.test.ts", "test\n")
    verdict, fake = evaluate(repo, base, AdapterResult())
    assert verdict["status"] == "clean"
    assert fake.calls == []


def test_changed_code_with_no_mutants_is_unverified(ts_change):
    repo, base = ts_change
    verdict, _ = evaluate(repo, base, AdapterResult())
    assert verdict["status"] == "unverified"
    assert verdict["total"] == 0


def test_comment_only_change_with_no_mutants_passes(repo):
    repo.write("src/a.py", "x = 1\n")
    repo.commit()
    base = diff.snapshot(repo.path)
    repo.write("src/a.py", "# why x is 1\nx = 1\n")
    verdict, _ = evaluate(repo, base, AdapterResult())
    assert verdict["status"] == "pass"


def test_ignored_mutants_on_changed_lines_are_a_warning(ts_change):
    repo, base = ts_change
    verdict, _ = evaluate(repo, base, AdapterResult(mutants=[mutant(DETECTED)], ignored=2))
    assert verdict["status"] == "pass"
    assert "2" in verdict["warnings"][0]



def test_verdict_is_json_serializable(ts_change):
    repo, base = ts_change
    verdict, _ = evaluate(repo, base, AdapterResult(mutants=[mutant(UNDETECTED)]))
    json.dumps(verdict)


# --- Stop hook flow -------------------------------------------------------------------------


@pytest.fixture
def stop_env(repo, gate_home, monkeypatch):
    repo.write("src/a.ts", "a\nb\n")
    repo.commit()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo.path))
    store.set_enabled(str(repo.path.resolve()), True)
    gate.on_session_start({"session_id": "s1", "cwd": str(repo.path)})
    repo.write("src/a.ts", "a\nB\n")
    return repo


def stop(result, monkeypatch, payload=None):
    fake = FakeAdapter(result)
    monkeypatch.setattr(gate, "ADAPTERS", {"js": fake, "py": fake})
    out = gate.on_stop({"session_id": "s1", "cwd": "/", **(payload or {})})
    return out, fake


def test_stop_reports_survivors_without_blocking(stop_env, monkeypatch):
    out, _ = stop(AdapterResult(mutants=[mutant(UNDETECTED)]), monkeypatch)
    assert "decision" not in out
    assert "✗" in out["systemMessage"] and "src/a.ts:2" in out["systemMessage"] and "x <= lo" in out["systemMessage"]


def test_stop_pass_reports_score_to_user(stop_env, monkeypatch):
    out, _ = stop(AdapterResult(mutants=[mutant(DETECTED)]), monkeypatch)
    assert "decision" not in out
    assert "100.0%" in out["systemMessage"]


def test_stop_reuses_cached_verdict_when_nothing_changed(stop_env, monkeypatch):
    stop(AdapterResult(mutants=[mutant(DETECTED)]), monkeypatch)
    _, fake = stop(AdapterResult(mutants=[mutant(DETECTED)]), monkeypatch)
    assert fake.calls == []


def test_stop_does_not_cache_missing_tool_errors(stop_env, monkeypatch):
    stop(AdapterResult(error="not installed", error_kind="missing"), monkeypatch)
    _, fake = stop(AdapterResult(mutants=[mutant(DETECTED)]), monkeypatch)
    assert len(fake.calls) == 1


def test_stop_caches_timeouts_so_unchanged_code_is_not_rerun(stop_env, monkeypatch):
    stop(AdapterResult(error="시간 초과", error_kind="timeout"), monkeypatch)
    _, fake = stop(AdapterResult(mutants=[mutant(DETECTED)]), monkeypatch)
    assert fake.calls == []



def test_stop_error_warns_without_blocking(stop_env, monkeypatch):
    out, _ = stop(AdapterResult(error="Stryker가 설치되어 있지 않음", error_kind="missing"), monkeypatch)
    assert "decision" not in out
    assert "Stryker가 설치되어 있지 않음" in out["systemMessage"]


def test_stop_skips_projects_that_are_not_enabled(repo, gate_home, monkeypatch):
    repo.write("src/a.ts", "a\n")
    repo.commit()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo.path))
    gate.on_session_start({"session_id": "s1", "cwd": str(repo.path)})
    repo.write("src/a.ts", "A\n")
    out, fake = stop(AdapterResult(mutants=[mutant(UNDETECTED)]), monkeypatch)
    assert fake.calls == []
    assert out is None or "decision" not in out


def test_stop_hints_once_when_tools_are_installed_but_gate_is_off(repo, gate_home, monkeypatch):
    repo.write("node_modules/.bin/stryker", "")
    repo.write(".gitignore", "node_modules\n")
    repo.commit()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo.path))
    out, _ = stop(AdapterResult(), monkeypatch)
    assert "mutation-gate on" in out["systemMessage"]
    out, _ = stop(AdapterResult(), monkeypatch)
    assert out is None


def test_stop_silent_when_nothing_changed(repo, gate_home, monkeypatch):
    repo.commit()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo.path))
    store.set_enabled(str(repo.path.resolve()), True)
    gate.on_session_start({"session_id": "s1", "cwd": str(repo.path)})
    out, _ = stop(AdapterResult(), monkeypatch)
    assert out is None


def test_stop_keeps_checking_other_repos_when_one_crashes(stop_env, monkeypatch, tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    git(broken, "init", "-q")
    store.set_enabled(str(broken.resolve()), True)
    store.remember_repo("s1", str(broken.resolve()), {"base": "not-a-tree", "stash": ""})

    out, _ = stop(AdapterResult(mutants=[mutant(UNDETECTED)]), monkeypatch)

    assert "✗" in out["systemMessage"]
    assert "broken" in out["systemMessage"] and "⚠" in out["systemMessage"]


def test_stop_warns_when_work_was_stashed(stop_env, monkeypatch):
    git(stop_env.path, "stash", "-q")
    out, _ = stop(AdapterResult(), monkeypatch)
    assert "stash" in out["systemMessage"]




def test_stop_reports_busy_repo_without_caching(stop_env, monkeypatch):
    monkeypatch.setattr(gate, "LOCK_WAIT", 0.2)
    with store.repo_lock(str(stop_env.path.resolve())):
        out, fake = stop(AdapterResult(mutants=[mutant(DETECTED)]), monkeypatch)
    assert fake.calls == [] and "검사 중" in out["systemMessage"]
    _, fake = stop(AdapterResult(mutants=[mutant(DETECTED)]), monkeypatch)
    assert len(fake.calls) == 1



def test_session_start_snapshots_enabled_project(repo, gate_home, monkeypatch):
    repo.write("src/a.py", "x = 1\n")
    repo.commit()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo.path))
    store.set_enabled(str(repo.path.resolve()), True)
    gate.on_session_start({"session_id": "s1", "cwd": str(repo.path)})
    entry = store.load_session("s1")["repos"][str(repo.path.resolve())]
    assert entry["base"] == diff.snapshot(repo.path)


def test_session_start_ignores_projects_that_are_not_enabled(repo, gate_home, monkeypatch):
    repo.commit()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo.path))
    gate.on_session_start({"session_id": "s1", "cwd": str(repo.path)})
    assert store.load_session("s1")["repos"] == {}



def test_stop_never_blocks_even_when_tests_fail(stop_env, monkeypatch):
    out, _ = stop(AdapterResult(failure="테스트가 현재 실패함"), monkeypatch)
    assert "decision" not in out
    assert "테스트가 현재 실패함" in out["systemMessage"]


def test_stop_lists_only_the_first_survivors(stop_env, monkeypatch):
    mutants = [mutant(UNDETECTED, replacement=f"r{i}") for i in range(9)]
    out, _ = stop(AdapterResult(mutants=mutants), monkeypatch)
    assert out["systemMessage"].count("src/a.ts:2") == gate.BANNER_SURVIVORS
    assert "mutation-gate last" in out["systemMessage"]
