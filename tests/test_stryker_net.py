import os
import shutil
from pathlib import Path

import pytest

from conftest import git
from mutation_gate import tools
from mutation_gate.adapters import stryker_net

FIXTURE = Path(__file__).parent / "fixtures" / "dotnet-mini"
needs_dotnet = pytest.mark.skipif(
    not (tools.find("dotnet", stryker_net.DOTNET_DIRS) and tools.find("dotnet-stryker", stryker_net.TOOL_DIRS)),
    reason="needs the .NET SDK and dotnet-stryker; run tests/setup_fixtures.sh first",
)

AGE = """namespace Shop;

public static class Age
{
    public static bool IsAdult(int age) => age >= 18;

    public static bool IsSenior(int age) => age >= 65;
}
"""


def _tests_file(*body):
    lines = "\n".join(f"        {line}" for line in body)
    return f"using Shop;\n\nnamespace Shop.Tests;\n\npublic class AgeTests\n{{\n    [Fact]\n    public void Check()\n    {{\n{lines}\n    }}\n}}\n"


WEAK = _tests_file("Age.IsAdult(30);")
WEAK_BOTH = _tests_file("Age.IsAdult(30);", "Age.IsSenior(30);")


@pytest.fixture
def shop(repo, gate_home):
    shutil.copytree(FIXTURE, repo.path, dirs_exist_ok=True, ignore=shutil.ignore_patterns("bin", "obj"))
    return repo


def files_outside_build_output(root):
    return {
        p.relative_to(root) for p in root.rglob("*")
        if p.is_file() and ".git" not in p.parts and not {"bin", "obj"} & set(p.parts)
    }


def fake_tool(directory, name):
    """An executable that leaves a PWNED marker in its working directory if anything runs it."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\ntouch PWNED\n")
    path.chmod(0o755)
    return path


def without_tools_on_path(monkeypatch, *names):
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    keep = [d for d in dirs if not any(os.path.exists(os.path.join(d, n)) for n in names)]
    monkeypatch.setenv("PATH", os.pathsep.join(keep))


@needs_dotnet
def test_weak_test_leaves_survivors_with_details(shop, gate_home):
    shop.write("Shop.Tests/AgeTests.cs", WEAK)
    before = files_outside_build_output(shop.path)

    result = stryker_net.run(str(shop.path), {"Shop/Age.cs": {5}}, budget=300)

    assert result.error is None and result.failure is None, result
    assert result.mutants and {m.line for m in result.mutants} == {5}
    assert all(m.status == "undetected" and m.path == "Shop/Age.cs" for m in result.mutants)
    gt = [m for m in result.mutants if m.replacement == "age > 18"]
    assert gt and gt[0].original == "age >= 18" and gt[0].mutator == "Equality mutation"
    # Only bin/ and obj/ may appear in the repo; the report is deleted once read.
    assert files_outside_build_output(shop.path) == before
    assert not list(gate_home.rglob("mutation-report.json"))


@needs_dotnet
def test_strong_boundary_test_detects_everything(shop):
    result = stryker_net.run(str(shop.path), {"Shop/Age.cs": set(range(1, 7))}, budget=300)

    assert result.error is None and result.failure is None, result
    assert result.mutants
    assert all(m.status == "detected" for m in result.mutants)


@needs_dotnet
def test_only_changed_lines_count(shop):
    shop.write("Shop/Age.cs", AGE)
    shop.write("Shop.Tests/AgeTests.cs", WEAK_BOTH)

    result = stryker_net.run(str(shop.path), {"Shop/Age.cs": {7}}, budget=300)

    assert result.error is None and result.failure is None, result
    assert result.mutants and {m.line for m in result.mutants} == {7}
    assert {m.original for m in result.mutants} == {"age >= 65"}


@needs_dotnet
def test_failing_tests_are_a_failure(shop):
    shop.write("Shop.Tests/AgeTests.cs", _tests_file("Assert.True(Age.IsAdult(17));"))

    result = stryker_net.run(str(shop.path), {"Shop/Age.cs": {5}}, budget=300)

    assert result.error is None and not result.mutants
    assert result.failure.startswith("the tests are failing")
    assert "Shop.Tests.AgeTests.Check" in result.failure


@needs_dotnet
def test_project_stryker_config_is_ignored(shop):
    shop.write("Shop.Tests/AgeTests.cs", WEAK)
    shop.write("Shop/stryker-config.json", '{"stryker-config": {"mutate": ["!**/*.cs"], "mutation-level": "Basic"}}')

    result = stryker_net.run(str(shop.path), {"Shop/Age.cs": {5}}, budget=300)

    assert result.error is None and result.failure is None, result
    assert result.mutants


@needs_dotnet
def test_existing_disable_comment_is_counted_as_ignored(shop):
    shop.write("Shop/Age.cs", AGE.replace("    public static bool IsAdult", "    // Stryker disable once all\n    public static bool IsAdult"))
    shop.write("Shop.Tests/AgeTests.cs", WEAK_BOTH)

    result = stryker_net.run(str(shop.path), {"Shop/Age.cs": {6, 8}}, budget=300)

    assert result.ignored > 0
    assert result.mutants and {m.line for m in result.mutants} == {8}


def test_missing_dotnet_stryker_is_a_tool_error_with_install_hint(shop, monkeypatch, tmp_path):
    without_tools_on_path(monkeypatch, "dotnet-stryker")
    monkeypatch.setattr(stryker_net, "TOOL_DIRS", [str(tmp_path / "no-tools")])

    result = stryker_net.run(str(shop.path), {"Shop/Age.cs": {5}}, budget=60)

    assert result.failure is None and not result.mutants
    assert result.error_kind == "missing" and result.cacheable is False
    assert "tool install -g dotnet-stryker" in result.error


def test_missing_sdk_hint_names_the_install_script_and_channel(shop, monkeypatch, tmp_path):
    without_tools_on_path(monkeypatch, "dotnet", "dotnet-stryker")
    monkeypatch.setattr(stryker_net, "DOTNET_DIRS", [str(tmp_path / "no-dotnet")])

    result = stryker_net.run(str(shop.path), {"Shop/Age.cs": {5}}, budget=60)

    assert result.error_kind == "missing"
    assert "dotnet-install.sh --channel 8.0 --install-dir ~/.dotnet" in result.error
    assert f"dotnet tool install -g dotnet-stryker --version {stryker_net.LEGACY_STRYKER}" in result.error

    for csproj in ("Shop/Shop.csproj", "Shop.Tests/Shop.Tests.csproj"):
        shop.write(csproj, (shop.path / csproj).read_text().replace("net8.0", "net10.0"))
    result = stryker_net.run(str(shop.path), {"Shop/Age.cs": {5}}, budget=60)

    assert "--channel 10.0" in result.error and "--version" not in result.error


@pytest.mark.parametrize("tracked", ["dotnet", "dotnet-stryker"])
def test_tracked_binary_is_refused(shop, monkeypatch, tmp_path, tracked):
    outside = tmp_path / "outside-bin"
    for name in ("dotnet", "dotnet-stryker"):
        fake_tool(outside, name)
    fake_tool(shop.path / "tools", tracked)
    git(shop.path, "add", "-f", f"tools/{tracked}")
    monkeypatch.setenv("PATH", os.pathsep.join([str(shop.path / "tools"), str(outside), os.environ["PATH"]]))

    result = stryker_net.run(str(shop.path), {"Shop/Age.cs": {5}}, budget=60)

    assert result.error_kind == "missing" and "git tracks it" in result.error
    assert not list(shop.path.rglob("PWNED")) and not list(tmp_path.rglob("PWNED"))


def test_source_project_without_a_test_project_is_a_failure(repo, gate_home, monkeypatch, tmp_path):
    shutil.copytree(FIXTURE / "Shop", repo.path / "Shop")
    outside = tmp_path / "outside-bin"
    for name in ("dotnet", "dotnet-stryker"):
        fake_tool(outside, name)
    monkeypatch.setenv("PATH", os.pathsep.join([str(outside), os.environ["PATH"]]))

    result = stryker_net.run(str(repo.path), {"Shop/Age.cs": {5}}, budget=60)

    assert result.error is None and "no test project references Shop/Shop.csproj" in result.failure
    assert not list(tmp_path.rglob("PWNED"))


def test_file_outside_any_project_is_reported_unverified(repo, gate_home):
    repo.write("scripts/Tool.cs", "class Tool {}\n")

    result = stryker_net.run(str(repo.path), {"scripts/Tool.cs": {1}}, budget=60)

    assert result.unverified == ["changed file is not in any .csproj: scripts/Tool.cs"]
    assert result.error is None and result.failure is None


@needs_dotnet
def test_run_over_budget_is_a_timeout(shop):
    result = stryker_net.run(str(shop.path), {"Shop/Age.cs": {5}}, budget=1)

    assert result.error_kind == "timeout" and result.failure is None and not result.mutants


@needs_dotnet
def test_gate_check_tests_whole_csharp_files(shop):
    from mutation_gate import gate

    shop.write("Shop/Age.cs", AGE)
    shop.write("Shop.Tests/AgeTests.cs", WEAK_BOTH)

    verdict = gate.check(str(shop.path), ["Shop/Age.cs", "Shop.Tests/AgeTests.cs"], 80, set(), 300,
                         adapters={"dotnet": stryker_net})

    assert verdict["status"] == "fail" and verdict["total"] == 4
    assert {(s["path"], s["line"]) for s in verdict["survivors"]} == {("Shop/Age.cs", 5), ("Shop/Age.cs", 7)}
