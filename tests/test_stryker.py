import os
import shutil
from pathlib import Path

import pytest

from mutation_gate.adapters import stryker

FIXTURE = Path(__file__).parent / "fixtures" / "js-mini"
pytestmark = pytest.mark.skipif(
    not (FIXTURE / "node_modules" / ".bin" / "stryker").exists(),
    reason="run tests/setup_fixtures.sh first",
)

CALC = """export function clamp(x: number, lo: number, hi: number): number {
  if (x < lo) return lo;
  if (x > hi) return hi;
  return x;
}
"""
WEAK_TEST = """import { it } from "vitest";
import { clamp } from "./calc";
it("runs", () => { clamp(5, 0, 10); });
"""
STRONG_TEST = """import { it, expect } from "vitest";
import { clamp } from "./calc";
it("clamps", () => {
  expect(clamp(-1, 0, 10)).toBe(0);
  expect(clamp(0, 0, 10)).toBe(0);
  expect(clamp(10, 0, 10)).toBe(10);
  expect(clamp(11, 0, 10)).toBe(10);
  expect(clamp(5, 0, 10)).toBe(5);
});
"""
AGE = """export function isAdult(age: number): boolean {
  return age >= 18;
}
"""
AGE_TEST = """import { it, expect } from "vitest";
import { isAdult } from "./age";
it("boundary", () => {
  expect(isAdult(17)).toBe(false);
  expect(isAdult(18)).toBe(true);
});
"""


@pytest.fixture
def js(repo, gate_home):
    shutil.copy(FIXTURE / "package.json", repo.path / "package.json")
    os.symlink(FIXTURE / "node_modules", repo.path / "node_modules")
    repo.write(".gitignore", "node_modules\n")
    repo.write("src/calc.ts", CALC)
    return repo


def test_weak_test_leaves_survivors_with_details(js):
    js.write("src/calc.test.ts", WEAK_TEST)

    result = stryker.run(str(js.path), {"src/calc.ts": {2}}, budget=120)

    assert result.error is None and result.failure is None
    assert result.mutants, "expected mutants on line 2"
    assert {m.line for m in result.mutants} == {2}
    assert all(m.status == "undetected" for m in result.mutants)
    lt = [m for m in result.mutants if m.replacement == "x <= lo"]
    assert lt and lt[0].original == "x < lo" and lt[0].mutator == "EqualityOperator"


def test_strong_test_kills_everything(js):
    js.write("src/age.ts", AGE)
    js.write("src/age.test.ts", AGE_TEST)

    result = stryker.run(str(js.path), {"src/age.ts": {1, 2, 3}}, budget=120)

    assert result.error is None and result.failure is None
    assert result.mutants
    assert all(m.status == "detected" for m in result.mutants)


def test_equivalent_mutant_survives_even_a_thorough_test(js):
    # x < lo -> x <= lo returns lo either way when x == lo: no test can kill it.
    js.write("src/calc.test.ts", STRONG_TEST)

    result = stryker.run(str(js.path), {"src/calc.ts": {2}}, budget=120)

    survivors = [m.replacement for m in result.mutants if m.status == "undetected"]
    assert survivors == ["x <= lo"]


def test_no_related_tests_is_a_failure(js):
    result = stryker.run(str(js.path), {"src/calc.ts": {2}}, budget=120)

    assert result.error is None
    assert "테스트" in result.failure


def test_failing_tests_are_a_failure(js):
    js.write("src/calc.test.ts", STRONG_TEST.replace("toBe(5)", "toBe(6)"))

    result = stryker.run(str(js.path), {"src/calc.ts": {2}}, budget=120)

    assert result.error is None
    assert "실패" in result.failure


def test_missing_stryker_is_a_tool_error_with_install_hint(repo, gate_home):
    repo.write("src/calc.ts", CALC)
    repo.write("pnpm-lock.yaml", "")

    result = stryker.run(str(repo.path), {"src/calc.ts": {2}}, budget=30)

    assert result.failure is None
    assert "pnpm add -D @stryker-mutator/core @stryker-mutator/vitest-runner" in result.error


def test_project_stryker_config_is_ignored(js):
    js.write("src/calc.test.ts", WEAK_TEST)
    js.write("stryker.config.json", '{"mutate": ["nothing/**"], "testRunner": "command"}')

    result = stryker.run(str(js.path), {"src/calc.ts": {2}}, budget=120)

    assert result.error is None
    assert result.mutants


def test_symlinked_state_dir_still_finds_related_tests(js, tmp_path, monkeypatch):
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "link-home"
    link.symlink_to(real)
    monkeypatch.setenv("MUTATION_GATE_HOME", str(link))
    js.write("src/calc.test.ts", WEAK_TEST)

    result = stryker.run(str(js.path), {"src/calc.ts": {2}}, budget=120)

    assert result.failure is None and result.error is None
    assert result.mutants


def test_paths_with_glob_characters_are_mutated(js):
    js.write("src/app/[id]/(group)/page.ts", AGE)
    js.write("src/app/[id]/(group)/page.test.ts", AGE_TEST.replace('"./age"', '"./page"'))

    result = stryker.run(str(js.path), {"src/app/[id]/(group)/page.ts": {2}}, budget=120)

    assert result.error is None and result.failure is None
    assert result.mutants and all(m.path == "src/app/[id]/(group)/page.ts" for m in result.mutants)


def test_missing_stryker_error_is_not_cached(repo, gate_home):
    repo.write("src/calc.ts", CALC)
    assert stryker.run(str(repo.path), {"src/calc.ts": {2}}, budget=30).cacheable is False
