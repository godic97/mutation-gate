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


def test_code_no_test_imports_is_listed_as_uncovered_mutants(js):
    js.write("src/other.test.ts", 'import { it, expect } from "vitest";\nit("x", () => { expect(1).toBe(1); });\n')

    result = stryker.run(str(js.path), {"src/calc.ts": {2}}, budget=120)

    assert result.error is None and result.failure is None
    assert result.mutants and all(m.status == "undetected" for m in result.mutants)


def test_project_without_any_tests_is_a_failure(js):
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


def test_change_inside_a_multiline_expression_is_mutated(js):
    js.write("src/perm.ts", "export function can(u: { admin: boolean; owner: boolean }): boolean {\n  return (\n    u.admin ||\n    u.owner\n  );\n}\n")
    js.write("src/perm.test.ts", 'import { it, expect } from "vitest";\nimport { can } from "./perm";\nit("x", () => { expect(can({ admin: true, owner: true })).toBe(true); });\n')

    result = stryker.run(str(js.path), {"src/perm.ts": {3}}, budget=120)

    assert result.error is None and result.failure is None
    assert any(m.status == "undetected" and m.mutator == "LogicalOperator" for m in result.mutants)


def test_types_only_change_has_nothing_to_mutate(js):
    js.write("src/types.ts", "export interface User {\n  name: string;\n  email?: string;\n}\n")

    result = stryker.run(str(js.path), {"src/types.ts": {3}}, budget=120)

    assert result == stryker.AdapterResult()


def test_identical_blocks_get_distinct_ids(js):
    body = "  const out: string[] = [];\n  out.push(s);\n  return out;\n"
    js.write("src/dup.ts", f"export function a(s: string) {{\n{body}}}\nexport function b(s: string) {{\n{body}}}\n")
    js.write("src/dup.test.ts", 'import { it } from "vitest";\nimport { a, b } from "./dup";\nit("x", () => { a("1"); b("2"); });\n')

    result = stryker.run(str(js.path), {"src/dup.ts": set(range(1, 11))}, budget=120)

    ids = [m.id for m in result.mutants]
    assert ids and len(ids) == len(set(ids))


def test_existing_disable_comment_is_counted_as_ignored(js):
    js.write("src/calc.ts", "// Stryker disable all\n" + CALC)
    js.write("src/calc.test.ts", WEAK_TEST)

    result = stryker.run(str(js.path), {"src/calc.ts": {3}}, budget=120)

    assert result.ignored > 0


def test_tracked_stryker_binary_is_refused(repo, gate_home):
    repo.write("src/calc.ts", CALC)
    repo.write("node_modules/.bin/stryker", "#!/bin/sh\ntouch PWNED\n")
    (repo.path / "node_modules/.bin/stryker").chmod(0o755)
    from conftest import git
    git(repo.path, "add", "-f", "node_modules/.bin/stryker")

    result = stryker.run(str(repo.path), {"src/calc.ts": {2}}, budget=30)

    assert result.error and result.error_kind == "missing"
    assert not (repo.path / "PWNED").exists()


def test_report_is_removed_after_parsing(js, gate_home):
    js.write("src/calc.test.ts", WEAK_TEST)
    stryker.run(str(js.path), {"src/calc.ts": {2}}, budget=120)
    assert not list((gate_home / "state" / "work").glob("*/mutation.json"))


def test_monorepo_package_runs_with_its_own_vitest_config(js):
    js.write("packages/app/vitest.config.ts", 'import { defineConfig } from "vitest/config";\nimport path from "node:path";\nexport default defineConfig({ resolve: { alias: { "@lib": path.resolve(__dirname, "lib") } } });\n')
    js.write("packages/app/lib/age.ts", AGE)
    js.write("packages/app/lib/age.test.ts", AGE_TEST.replace('"./age"', '"@lib/age"'))

    result = stryker.run(str(js.path), {"packages/app/lib/age.ts": {2}}, budget=120)

    assert result.error is None and result.failure is None, result
    assert result.mutants and all(m.path == "packages/app/lib/age.ts" for m in result.mutants)
    assert all(m.status == "detected" for m in result.mutants)


def test_root_vitest_config_with_alias_tests_the_mutated_code(js):
    js.write("vitest.config.ts", 'import { defineConfig } from "vitest/config";\nimport path from "node:path";\nexport default defineConfig({ resolve: { alias: { "@src": path.resolve(__dirname, "src") } } });\n')
    js.write("src/age.ts", AGE)
    js.write("src/age.test.ts", AGE_TEST.replace('"./age"', '"@src/age"'))

    result = stryker.run(str(js.path), {"src/age.ts": {2}}, budget=120)

    assert result.mutants and all(m.status == "detected" for m in result.mutants), result
