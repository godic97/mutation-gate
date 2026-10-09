import shutil
from pathlib import Path

import pytest

from conftest import git
from mutation_gate.adapters import go_mutation

FIXTURE = Path(__file__).parent / "fixtures" / "go-mini"
needs_go = pytest.mark.skipif(
    not (go_mutation._find_go() and go_mutation._find_gremlins()),
    reason="needs Go and gremlins: run tests/setup_fixtures.sh",
)

LINE = "func IsAdult(age int) bool { return age >= 18 }"
WEAK_TEST = """package gomini

import "testing"

func TestAdult(t *testing.T) {
	if !IsAdult(30) {
		t.Fatal("30 is adult")
	}
}
"""
STRONG_TEST = """package gomini

import "testing"

func TestBoundary(t *testing.T) {
	if IsAdult(17) {
		t.Fatal("17 is not adult")
	}
	if !IsAdult(18) {
		t.Fatal("18 is adult")
	}
}
"""
DISCOUNT = "\nfunc Discount(p int) int { return p - 1 }\n"  # line 5 of price.go once appended


@pytest.fixture
def gomod(repo, gate_home):
    shutil.copytree(FIXTURE, repo.path, dirs_exist_ok=True)
    return repo


def status(repo):
    return git(repo.path, "status", "--porcelain", "--untracked-files=all", "--ignored")


@needs_go
def test_weak_test_leaves_the_boundary_mutant_with_details(gomod, gate_home):
    gomod.write("price_test.go", WEAK_TEST)
    before = status(gomod)

    result = go_mutation.run(str(gomod.path), {"price.go": {1, 2, 3}}, budget=120)

    assert result.error is None and result.failure is None, result
    assert result.mutants and {m.line for m in result.mutants} == {3}
    survivors = [m for m in result.mutants if m.status == "undetected"]
    assert len(survivors) == 1
    s = survivors[0]
    assert (s.path, s.mutator) == ("price.go", "CONDITIONALS_BOUNDARY")
    assert s.original == LINE
    assert s.replacement == LINE.replace(">=", ">")
    assert s.context == "IsAdult"
    negation = [m for m in result.mutants if m.mutator == "CONDITIONALS_NEGATION"]
    assert negation and negation[0].replacement == LINE.replace(">=", "<") and negation[0].status == "detected"
    # Nothing left behind: not in the repo, not in the work dir.
    assert status(gomod) == before
    work = list((gate_home / "state" / "work").glob("*-gremlins"))
    assert work and not list(work[0].glob("gremlins.json")) and not (work[0] / "tmp").exists()


@needs_go
def test_strong_boundary_test_detects_everything(gomod):
    gomod.write("price_test.go", STRONG_TEST)

    result = go_mutation.run(str(gomod.path), {"price.go": {1, 2, 3}}, budget=120)

    assert result.error is None and result.failure is None, result
    assert result.mutants
    assert all(m.status == "detected" for m in result.mutants)


@needs_go
def test_only_changed_lines_count(gomod):
    gomod.write("price.go", (FIXTURE / "price.go").read_text() + DISCOUNT)
    gomod.write("price_test.go", STRONG_TEST)

    result = go_mutation.run(str(gomod.path), {"price.go": {5}}, budget=120)

    assert result.error is None and result.failure is None, result
    assert result.mutants and {m.line for m in result.mutants} == {5}
    # No test calls Discount: its mutants are not covered, so undetected.
    assert all(m.status == "undetected" and m.context == "Discount" for m in result.mutants)
    assert {m.replacement for m in result.mutants} == {"func Discount(p int) int { return p + 1 }"}


@needs_go
def test_code_without_tests_comes_back_undetected(gomod):
    result = go_mutation.run(str(gomod.path), {"price.go": {3}}, budget=120)

    assert result.error is None and result.failure is None, result
    assert result.mutants and all(m.status == "undetected" for m in result.mutants)


@needs_go
def test_failing_tests_are_a_failure(gomod):
    gomod.write("price_test.go", WEAK_TEST.replace("!IsAdult(30)", "IsAdult(30)"))

    result = go_mutation.run(str(gomod.path), {"price.go": {3}}, budget=120)

    assert result.error is None and not result.mutants
    assert result.failure.startswith("the tests are failing")
    assert "TestAdult" in result.failure


@needs_go
def test_package_main_in_a_subdirectory_runs_its_own_tests(gomod):
    # gremlins alone would run the root package's tests for package main in cmd/tool.
    gomod.write("cmd/tool/main.go", "package main\n\nfunc Half(n int) int { return n / 2 }\n\nfunc main() { _ = Half(4) }\n")
    gomod.write("cmd/tool/main_test.go", 'package main\n\nimport "testing"\n\nfunc TestHalf(t *testing.T) {\n\tif Half(8) != 4 {\n\t\tt.Fatal("bad")\n\t}\n}\n')

    result = go_mutation.run(str(gomod.path), {"cmd/tool/main.go": {3}}, budget=120)

    assert result.error is None and result.failure is None, result
    assert [(m.path, m.replacement, m.status) for m in result.mutants] == [
        ("cmd/tool/main.go", "func Half(n int) int { return n * 2 }", "detected"),
    ]


@needs_go
def test_whole_run_timeout_is_reported(gomod, gate_home):
    gomod.write("price_test.go", 'package gomini\n\nimport (\n\t"testing"\n\t"time"\n)\n\nfunc TestSlow(t *testing.T) { time.Sleep(30 * time.Second) }\n')

    result = go_mutation.run(str(gomod.path), {"price.go": {3}}, budget=3)

    assert result.error_kind == "timeout" and result.failure is None
    assert not list((gate_home / "state" / "work").glob("*-gremlins/tmp"))


def test_missing_gremlins_is_a_tool_error_with_install_hint(gomod, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("GOBIN", raising=False)
    monkeypatch.delenv("GOPATH", raising=False)
    monkeypatch.setattr(go_mutation, "TOOL_DIRS", [str(tmp_path / "no-bin")])

    result = go_mutation.run(str(gomod.path), {"price.go": {3}}, budget=30)

    assert result.failure is None and result.error_kind == "missing"
    assert "go install github.com/go-gremlins/gremlins/cmd/gremlins@v0.6.0" in result.error
    assert result.cacheable is False


def test_tracked_gremlins_binary_is_refused(gomod, monkeypatch):
    gomod.write("bin/gremlins", "#!/bin/sh\ntouch PWNED\n")
    (gomod.path / "bin/gremlins").chmod(0o755)
    git(gomod.path, "add", "-f", "bin/gremlins")
    monkeypatch.setenv("PATH", f"{gomod.path / 'bin'}:/usr/bin:/bin")

    result = go_mutation.run(str(gomod.path), {"price.go": {3}}, budget=30)

    assert result.error_kind == "missing" and "git tracks it" in result.error
    assert not (gomod.path / "PWNED").exists()


def test_display_text_and_function_context():
    assert go_mutation._display("\tif a < b && ok {", 7, "CONDITIONALS_BOUNDARY") == ("if a < b && ok {", "if a <= b && ok {")
    assert go_mutation._display("\tif a < b && ok {", 11, "INVERT_LOGICAL") == ("if a < b && ok {", "if a < b || ok {")
    assert go_mutation._display("\tx <<= 2", 4, "REMOVE_SELF_ASSIGNMENTS") == ("x <<= 2", "x = 2")
    assert go_mutation._display("\tn %= 3", 4, "INVERT_ASSIGNMENTS") is None  # gremlins maps %= to itself
    assert go_mutation._display('\ts := "é" + t', 12, "ARITHMETIC_BASE") == ('s := "é" + t', 's := "é" - t')
    src = ["package p", "", "func (s *Set[T]) Add(v T) {", "\ts.n++", "}", "", "var x = 1 + 2", "func F() int { return 1 }"]
    names = go_mutation._function_names(src)
    assert (names[4], names[5], names.get(7), names[8]) == ("Set.Add", "Set.Add", None, "F")
