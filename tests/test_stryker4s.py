import shutil
from pathlib import Path

import pytest

from conftest import git
from mutation_gate import tools
from mutation_gate.adapters import stryker4s

# Each sbt-backed test starts sbt once: about 5-10 s with warm caches. The very first run on a
# machine downloads sbt, Scala, Stryker4s and munit (about 300 MB) and takes a minute or two.
FIXTURE = Path(__file__).parent / "fixtures" / "scala-mini"
PRICE = "src/main/scala/shop/Price.scala"  # line 4: isAdult (age >= 18), line 6: discount
SUITE = "src/test/scala/shop/PriceSuite.scala"
needs_sbt = pytest.mark.skipif(
    tools.find("sbt", stryker4s.SBT_DIRS) is None or stryker4s._java_home() is None,
    reason="needs sbt and a JDK: see the Scala lines in tests/setup_fixtures.sh",
)


def suite(*asserts):
    body = "\n".join(f"    {a}" for a in asserts)
    return f"package shop\n\nclass PriceSuite extends munit.FunSuite {{\n  test(\"price\") {{\n{body}\n  }}\n}}\n"


WEAK = suite("assert(Price.isAdult(30))")
# 19 as well: with only 17 and 18, `age == 18` would survive.
BOUNDARY = suite("assert(!Price.isAdult(17))", "assert(Price.isAdult(18))", "assert(Price.isAdult(19))")


@pytest.fixture
def scala(repo, gate_home):
    shutil.copytree(FIXTURE, repo.path, dirs_exist_ok=True, ignore=shutil.ignore_patterns("target"))
    return repo


def leftovers(repo):
    """Paths git reports in the repo besides sbt's own build output."""
    out = git(repo.path, "status", "--porcelain", "--ignored", "--untracked-files=all")
    allowed = ("target/", "project/target/")
    return [line for line in out.splitlines() if not line[3:].startswith(allowed)]


def stryker_dirs(repo):
    return sorted(str(p.relative_to(repo.path)) for p in repo.path.rglob("stryker4s-*"))


@needs_sbt
def test_weak_test_leaves_survivors_on_the_changed_line(scala, gate_home):
    scala.write(SUITE, WEAK)
    scala.commit()

    result = stryker4s.run(str(scala.path), {PRICE: {4}}, budget=300)

    assert result.error is None and result.failure is None, result
    assert result.mutants and {m.line for m in result.mutants} == {4}
    assert all(m.path == PRICE for m in result.mutants)
    survivors = [m for m in result.mutants if m.status == "undetected"]
    assert [(m.mutator, m.original, m.replacement) for m in survivors] == [("EqualityOperator", ">=", ">")]
    # Nothing is left behind: no report, no sandbox, no file outside sbt's target directories.
    assert leftovers(scala) == [] and stryker_dirs(scala) == []
    assert not list((gate_home / "state" / "work").rglob("report.json"))


@needs_sbt
def test_strong_boundary_test_detects_every_mutant(scala):
    scala.write(SUITE, BOUNDARY)

    result = stryker4s.run(str(scala.path), {PRICE: {4}}, budget=300)

    assert result.error is None and result.failure is None, result
    assert len(result.mutants) == 3
    assert all(m.status == "detected" for m in result.mutants)


@needs_sbt
def test_only_mutants_on_changed_lines_count(scala):
    # Nothing tests discount(), so line 6 holds only survivors; they must not leak into line 4.
    scala.write(SUITE, BOUNDARY)

    line4 = stryker4s.run(str(scala.path), {PRICE: {4}}, budget=300)
    line6 = stryker4s.run(str(scala.path), {PRICE: {6}}, budget=300)

    assert {m.line for m in line4.mutants} == {4} and all(m.status == "detected" for m in line4.mutants)
    assert {m.line for m in line6.mutants} == {6} and all(m.status == "undetected" for m in line6.mutants)
    assert {m.mutator for m in line6.mutants} == {"EqualityOperator", "ConditionalExpression"}


PLUGIN = 'addSbtPlugin("io.stryker-mutator" % "sbt-stryker4s" % "1.1.1")\n'
HOSTILE_CONF = """stryker4s {
  mutate: ["nothing/**"]
  files: ["nothing/**"]
  test-filter: ["nothing.*"]
  reporters: ["html"]
  timeout-factor: 0.001
  thresholds { high: 100, low: 99, break: 98 }
}
"""
HOSTILE_SBT = """
root / strykerExcludedMutations := Seq("EqualityOperator")
root / strykerTestFilter := Seq("nothing.*")
root / strykerMutate := Seq("nothing/**")
root / strykerFiles := Seq("nothing/**")
root / strykerReporters := Seq("html")
root / strykerThresholdsBreak := 99
root / strykerLegacyTestRunner := true
"""


@needs_sbt
@pytest.mark.parametrize("where", ["stryker4s.conf", "build.sbt"])
def test_project_stryker4s_config_does_not_change_the_mutants(scala, where):
    scala.write(SUITE, WEAK)
    if where == "stryker4s.conf":
        scala.write("stryker4s.conf", HOSTILE_CONF)
    else:
        scala.write("project/plugins.sbt", PLUGIN)
        scala.write("build.sbt", (FIXTURE / "build.sbt").read_text() + HOSTILE_SBT)

    result = stryker4s.run(str(scala.path), {PRICE: {4}}, budget=300)

    assert result.error is None and result.failure is None, result
    assert result.unverified == [] and result.ignored == 0
    assert len(result.mutants) == 3
    assert [m.replacement for m in result.mutants if m.status == "undetected"] == [">"]


@needs_sbt
def test_project_settings_the_gate_cannot_override_are_noted(scala):
    scala.write(SUITE, WEAK)
    scala.write("stryker4s.conf", 'stryker4s { excluded-mutations: ["EqualityOperator"] }\n')
    scala.write("project/plugins.sbt", PLUGIN)
    scala.write("build.sbt", (FIXTURE / "build.sbt").read_text()
                + "\nroot / strykerScalaDialect := scala.meta.dialects.Scala213\n")

    result = stryker4s.run(str(scala.path), {PRICE: {4}}, budget=300)

    assert result.error is None and result.failure is None, result
    assert result.mutants == [] and result.ignored == 3  # every mutant on line 4 is an EqualityOperator
    assert sorted(result.unverified) == [
        "project stryker4s settings apply: excluded-mutations is set in stryker4s.conf (sbt project root)",
        "project stryker4s settings apply: strykerScalaDialect is set in build.sbt (sbt project root)",
    ]


@needs_sbt
def test_failing_tests_are_a_failure(scala):
    scala.write(SUITE, suite("assertEquals(Price.isAdult(17), true)"))

    result = stryker4s.run(str(scala.path), {PRICE: {4}}, budget=300)

    assert result.error is None
    assert result.failure.startswith("the tests are failing")
    assert "shop.PriceSuite.price" in result.failure
    assert stryker_dirs(scala) == []  # Stryker4s keeps its sandbox after an error; the adapter does not


@needs_sbt
def test_tests_failing_only_in_the_forked_runner_are_an_error(scala):
    # Passes in sbt's own JVM, fails in Stryker4s's forked test runner.
    scala.write(SUITE, suite("assert(Price.isAdult(30))", 'assert(!sys.props("java.class.path").contains("stryker4s"))'))

    result = stryker4s.run(str(scala.path), {PRICE: {4}}, budget=300)

    assert result.failure is None
    assert result.error_kind == "crash" and "forked" in result.error


@needs_sbt
def test_project_without_tests_is_a_failure(scala):
    result = stryker4s.run(str(scala.path), {PRICE: {4}}, budget=300)

    assert result.error is None
    assert "no test runs" in result.failure


@needs_sbt
def test_subproject_files_keep_repo_relative_paths(scala):
    scala.write("build.sbt", 'ThisBuild / scalaVersion := "2.13.18"\n'
                'lazy val core = (project in file("core"))\n'
                '  .settings(libraryDependencies += "org.scalameta" %% "munit" % "1.3.6" % Test)\n'
                'lazy val root = (project in file(".")).aggregate(core)\n')
    (scala.path / "core").mkdir()
    shutil.move(scala.path / "src", scala.path / "core" / "src")
    scala.write(f"core/{SUITE}", WEAK)

    result = stryker4s.run(str(scala.path), {f"core/{PRICE}": {4}}, budget=300)

    assert result.error is None and result.failure is None, result
    assert result.mutants and all(m.path == f"core/{PRICE}" and m.line == 4 for m in result.mutants)
    assert [m.replacement for m in result.mutants if m.status == "undetected"] == [">"]


@needs_sbt
def test_path_with_a_space_is_mutated(scala):
    shutil.move(scala.path / "src/main/scala/shop", scala.path / "src/main/scala/my shop")
    scala.write(SUITE, WEAK)

    result = stryker4s.run(str(scala.path), {"src/main/scala/my shop/Price.scala": {4}}, budget=300)

    assert result.error is None and result.failure is None, result
    assert result.mutants and all(m.path == "src/main/scala/my shop/Price.scala" for m in result.mutants)


@needs_sbt
def test_scala_file_outside_any_sbt_source_dir_is_unverified(scala):
    scala.write(SUITE, WEAK)
    scala.write("scripts/Tool.scala", "object Tool {\n  def ok(n: Int): Boolean = n > 0\n}\n")

    result = stryker4s.run(str(scala.path), {"scripts/Tool.scala": {2}}, budget=300)

    assert result.error is None and result.failure is None and result.mutants == []
    assert any("scripts/Tool.scala" in w for w in result.unverified)


@needs_sbt
def test_whole_run_timeout_is_reported_and_cleaned_up(scala):
    scala.write(SUITE, suite("Thread.sleep(120000)", "assert(Price.isAdult(30))"))

    result = stryker4s.run(str(scala.path), {PRICE: {4}}, budget=stryker4s.MIN_START_SECONDS + 15)

    assert result.error_kind == "timeout" and result.failure is None
    assert stryker_dirs(scala) == []


@needs_sbt
def test_too_little_budget_does_not_start_sbt(scala):
    result = stryker4s.run(str(scala.path), {PRICE: {4}}, budget=5)

    assert result.error_kind == "timeout"
    assert not (scala.path / "target").exists()


@needs_sbt
def test_sbt_older_than_stryker4s_supports_is_a_missing_tool(scala):
    scala.write("project/build.properties", "sbt.version=1.10.11\n")

    result = stryker4s.run(str(scala.path), {PRICE: {4}}, budget=60)

    assert result.error_kind == "missing" and "1.11.2" in result.error
    assert not result.cacheable


def test_missing_sbt_is_a_tool_error_with_install_hint(repo, gate_home, monkeypatch):
    monkeypatch.setattr(stryker4s, "_java_home", lambda: "/jdk")
    monkeypatch.setattr(stryker4s.tools, "find", lambda name, extra_dirs=(): None)
    repo.write(PRICE, "object Price\n")

    result = stryker4s.run(str(repo.path), {PRICE: {1}}, budget=30)

    assert result.error_kind == "missing" and result.failure is None
    assert "github.com/sbt/sbt/releases/download/" in result.error and "~/.local/opt" in result.error
    assert not result.cacheable


def test_missing_jdk_is_a_tool_error_with_install_hint(repo, gate_home, monkeypatch):
    monkeypatch.setattr(stryker4s, "_java_home", lambda: None)
    repo.write(PRICE, "object Price\n")

    result = stryker4s.run(str(repo.path), {PRICE: {1}}, budget=30)

    assert result.error_kind == "missing" and "adoptium" in result.error


def test_tracked_sbt_binary_is_refused(repo, gate_home, monkeypatch):
    fake = repo.write("bin/sbt", "#!/bin/sh\ntouch PWNED\n")
    fake.chmod(0o755)
    repo.write(PRICE, "object Price\n")
    repo.commit()
    monkeypatch.setattr(stryker4s, "_java_home", lambda: "/jdk")
    monkeypatch.setattr(stryker4s.tools, "find", lambda name, extra_dirs=(): str(fake))

    result = stryker4s.run(str(repo.path), {PRICE: {1}}, budget=30)

    assert result.error_kind == "missing" and "git tracks it" in result.error
    assert not (repo.path / "PWNED").exists()


def test_build_root_prefers_the_directory_with_project_build_properties(tmp_path):
    repo = tmp_path.resolve()
    (repo / "project").mkdir()
    (repo / "project" / "build.properties").write_text("sbt.version=1.12.15\n")
    (repo / "build.sbt").write_text("")
    (repo / "core").mkdir()
    (repo / "core" / "build.sbt").write_text("")
    assert stryker4s._build_root(repo, "core/src/main/scala/A.scala") == repo
    assert stryker4s._build_root(repo / "core", "src/main/scala/A.scala") == repo / "core"


def test_manifest_round_trip(tmp_path):
    manifest = tmp_path / "m.tsv"
    manifest.write_text("unmapped\t/r/x.scala\n"
                        "project\tcore\t/r/core\tstryker4s-report\t1700000000000\n"
                        "file\tcore\tsrc/main/scala/A.scala\n"
                        "failed\tcore\tstryker4s.exception.InitialTestRunFailedException: boom\n"
                        "plaintest\tcore\tfailed\n"
                        "projectsetting\tcore\texcluded-mutations\tstryker4s.conf\n")
    runs, unmapped = stryker4s._read_manifest(manifest)
    assert unmapped == ["/r/x.scala"]
    core = runs["core"]
    assert core["base"] == Path("/r/core") and core["status"] == "failed" and core["plain"] == "failed"
    assert core["before"] == {"stryker4s-report"} and core["before_reports"] == {"1700000000000"}
    assert core["settings"] == [("excluded-mutations", "stryker4s.conf")]
