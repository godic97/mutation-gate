import json

import pytest

from mutation_gate import gate, store
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
    base = repo.commit()
    repo.write("src/a.ts", "a\nB\n")
    repo.write("src/a.test.ts", "test\n")
    return repo, base


def evaluate(repo, base, result, threshold=80, allowed=()):
    fake = FakeAdapter(result)
    verdict = gate.evaluate(str(repo.path), base, threshold, set(allowed), 60, {"js": fake, "py": fake})
    return verdict, fake


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
    verdict, _ = evaluate(repo, base, AdapterResult(error="Stryker가 설치되어 있지 않음"))
    assert verdict["status"] == "error"
    assert verdict["errors"] == ["Stryker가 설치되어 있지 않음"]


def test_suppression_comment_fails_even_with_perfect_score(repo):
    repo.write("src/a.ts", "a\n")
    base = repo.commit()
    repo.write("src/a.ts", "a\n// Stryker disable next-line all\nb\n")
    verdict, _ = evaluate(repo, base, AdapterResult(mutants=[mutant(DETECTED)]))
    assert verdict["status"] == "fail"
    assert verdict["suppressions"] == [["src/a.ts", 2, "// Stryker disable next-line all"]]


def test_test_only_changes_are_clean(repo):
    base = repo.commit()
    repo.write("src/a.test.ts", "test\n")
    verdict, fake = evaluate(repo, base, AdapterResult())
    assert verdict["status"] == "clean"
    assert fake.calls == []


def test_no_mutants_on_changed_lines_passes(ts_change):
    repo, base = ts_change
    verdict, _ = evaluate(repo, base, AdapterResult())
    assert verdict["status"] == "pass" and verdict["total"] == 0


def test_block_reason_tells_claude_what_to_fix():
    verdict = {
        "status": "fail", "score": 50.0, "detected": 1, "total": 2, "allowed": 0,
        "survivors": [{"id": "abcd1234", "path": "src/a.ts", "line": 2, "mutator": "EqualityOperator",
                       "original": "x < lo", "replacement": "x <= lo"}],
        "failures": [], "errors": [], "suppressions": [],
    }
    reason = gate.block_reason({"/r/proj": verdict}, threshold=80)
    assert "src/a.ts:2" in reason and "x < lo → x <= lo" in reason and "abcd1234" in reason
    assert "50.0%" in reason and "80%" in reason
    assert "억제" in reason  # forbids suppression comments


# --- Stop hook flow -------------------------------------------------------------------------


@pytest.fixture
def stop_env(ts_change, gate_home, monkeypatch):
    repo, base = ts_change
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo.path))
    store.remember_repo("s1", str(repo.path.resolve()), base)
    return repo


def stop(result, monkeypatch, payload=None):
    fake = FakeAdapter(result)
    monkeypatch.setattr(gate, "ADAPTERS", {"js": fake, "py": fake})
    out = gate.on_stop({"session_id": "s1", "cwd": "/", **(payload or {})})
    return out, fake


def test_stop_blocks_and_counts(stop_env, monkeypatch):
    out, _ = stop(AdapterResult(mutants=[mutant(UNDETECTED)]), monkeypatch)
    assert out["decision"] == "block"
    assert "1/3" in out["systemMessage"]
    assert store.load_session("s1")["blocks"] == 1


def test_stop_gives_up_after_max_blocks_with_banner(stop_env, monkeypatch):
    for _ in range(3):
        out, _ = stop(AdapterResult(mutants=[mutant(UNDETECTED)]), monkeypatch)
        assert out["decision"] == "block"
        stop_env.write("src/a.ts", stop_env.path.joinpath("src/a.ts").read_text() + "x\n")
    out, _ = stop(AdapterResult(mutants=[mutant(UNDETECTED)]), monkeypatch)
    assert "decision" not in out
    assert "게이트 실패" in out["systemMessage"]


def test_stop_pass_reports_score_to_user(stop_env, monkeypatch):
    out, _ = stop(AdapterResult(mutants=[mutant(DETECTED)]), monkeypatch)
    assert "decision" not in out
    assert "100.0%" in out["systemMessage"]


def test_stop_reuses_cached_verdict_when_nothing_changed(stop_env, monkeypatch):
    stop(AdapterResult(mutants=[mutant(DETECTED)]), monkeypatch)
    _, fake = stop(AdapterResult(mutants=[mutant(DETECTED)]), monkeypatch)
    assert fake.calls == []


def test_stop_does_not_cache_missing_tool_errors(stop_env, monkeypatch):
    stop(AdapterResult(error="not installed", cacheable=False), monkeypatch)
    _, fake = stop(AdapterResult(mutants=[mutant(DETECTED)]), monkeypatch)
    assert len(fake.calls) == 1


def test_stop_error_warns_without_blocking(stop_env, monkeypatch):
    out, _ = stop(AdapterResult(error="Stryker가 설치되어 있지 않음"), monkeypatch)
    assert "decision" not in out
    assert "Stryker가 설치되어 있지 않음" in out["systemMessage"]


def test_stop_skips_disabled_project(stop_env, monkeypatch):
    store.set_enabled(str(stop_env.path.resolve()), False)
    out, fake = stop(AdapterResult(mutants=[mutant(UNDETECTED)]), monkeypatch)
    assert out is None and fake.calls == []


def test_stop_silent_when_nothing_changed(repo, gate_home, monkeypatch):
    base = repo.commit()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo.path))
    store.remember_repo("s1", str(repo.path.resolve()), base)
    out, fake = stop(AdapterResult(), monkeypatch)
    assert out is None


def test_stop_uses_project_dir_head_when_session_has_no_base(repo, gate_home, monkeypatch):
    repo.write("src/a.ts", "a\n")
    repo.commit()
    repo.write("src/a.ts", "A\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo.path))
    out, fake = stop(AdapterResult(mutants=[mutant(UNDETECTED)]), monkeypatch)
    assert out["decision"] == "block"
    assert fake.calls == [{"src/a.ts": {1}}]


def test_stop_fails_when_plugin_files_changed_during_session(stop_env, monkeypatch, tmp_path):
    plugin = tmp_path / "plugin"
    (plugin / "mutation_gate").mkdir(parents=True)
    (plugin / "mutation_gate" / "gate.py").write_text("original")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    gate.on_session_start({"session_id": "s1", "cwd": str(stop_env.path)})
    (plugin / "mutation_gate" / "gate.py").write_text("THRESHOLD = 0")

    out, _ = stop(AdapterResult(mutants=[mutant(DETECTED)]), monkeypatch)

    assert out["decision"] == "block"
    assert "플러그인" in out["reason"]


def test_prompt_submit_resets_block_counter(gate_home):
    store.bump_blocks("s1")
    gate.on_prompt({"session_id": "s1"})
    assert store.load_session("s1")["blocks"] == 0


def test_session_start_records_project_base(repo, gate_home, monkeypatch):
    head = repo.commit()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo.path))
    gate.on_session_start({"session_id": "s1", "cwd": str(repo.path)})
    assert store.load_session("s1")["repos"] == {str(repo.path.resolve()): head}


def test_verdict_is_json_serializable(ts_change):
    repo, base = ts_change
    verdict, _ = evaluate(repo, base, AdapterResult(mutants=[mutant(UNDETECTED)]))
    json.dumps(verdict)


def test_stop_caches_timeouts_so_unchanged_code_is_not_rerun(stop_env, monkeypatch):
    stop(AdapterResult(error="시간 초과"), monkeypatch)
    _, fake = stop(AdapterResult(mutants=[mutant(DETECTED)]), monkeypatch)
    assert fake.calls == []


def test_stop_keeps_checking_other_repos_when_one_crashes(stop_env, monkeypatch, tmp_path):
    from conftest import Repo, git
    broken = tmp_path / "broken"
    broken.mkdir()
    git(broken, "init", "-q")
    store.remember_repo("s1", str(broken.resolve()), "not-a-sha")

    out, fake = stop(AdapterResult(mutants=[mutant(UNDETECTED)]), monkeypatch)

    assert out["decision"] == "block"
    assert "broken" in out["systemMessage"] and "⚠" in out["systemMessage"]


def test_stop_says_when_changed_lines_have_nothing_to_mutate(stop_env, monkeypatch):
    out, _ = stop(AdapterResult(), monkeypatch)
    assert "변이 대상 없음" in out["systemMessage"]
