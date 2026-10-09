import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import git
from mutation_gate import tools
from mutation_gate.adapters import cargo_mutants

FIXTURE = Path(__file__).parent / "fixtures" / "rust-mini"
HAVE_TOOL = bool(tools.find("cargo", ["~/.cargo/bin"]) and tools.find("cargo-mutants", ["~/.cargo/bin"]))
needs_tool = pytest.mark.skipif(not HAVE_TOOL, reason="install Rust and cargo-mutants: cargo install --locked cargo-mutants")

LIB = (FIXTURE / "src" / "lib.rs").read_text()
WEAK_TEST = """#[test]
fn runs() {
    rust_mini::is_adult(30);
    rust_mini::clamp(5, 0, 10);
}
"""
STRONG_TEST = """use rust_mini::is_adult;

#[test]
fn boundary() {
    assert!(!is_adult(17));
    assert!(is_adult(18));
}
"""
FAILING_TEST = """#[test]
fn wrong() {
    assert!(!rust_mini::is_adult(30), "thirty counts as adult");
}
"""
SLOW_TEST = """#[test]
fn slow() {
    std::thread::sleep(std::time::Duration::from_secs(60));
}
"""


@pytest.fixture
def rust(repo, gate_home):
    shutil.copytree(FIXTURE, repo.path, dirs_exist_ok=True, ignore=shutil.ignore_patterns("target", "Cargo.lock"))
    return repo


def _files(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*") if ".git" not in p.relative_to(path).parts)


@needs_tool
def test_weak_test_leaves_survivors_with_details(rust, gate_home):
    rust.write("tests/smoke.rs", WEAK_TEST)
    before = _files(rust.path)

    result = cargo_mutants.run(str(rust.path), {"src/lib.rs": {2}}, budget=180)

    assert result.error is None and result.failure is None, result
    assert {m.line for m in result.mutants} == {2}
    assert all(m.path == "src/lib.rs" and m.status == "undetected" for m in result.mutants)
    op = [m for m in result.mutants if m.mutator == "BinaryOperator"]
    assert [(m.original, m.replacement) for m in op] == [(">=", "<")]
    assert op[0].context == "is_adult"
    body = sorted((m.original, m.replacement) for m in result.mutants if m.mutator == "FnValue")
    assert body == [("age >= 18", "false"), ("age >= 18", "true")]
    # Nothing left behind: no target/, Cargo.lock or mutants.out in the repo, no report in the state dir.
    assert _files(rust.path) == before
    assert not list(gate_home.rglob("outcomes.json")) and not list(gate_home.rglob("*.diff"))


@needs_tool
def test_strong_boundary_test_detects_everything(rust):
    rust.write("tests/age.rs", STRONG_TEST)

    result = cargo_mutants.run(str(rust.path), {"src/lib.rs": {1, 2, 3}}, budget=180)

    assert result.error is None and result.failure is None, result
    assert len(result.mutants) == 3
    assert all(m.status == "detected" for m in result.mutants)


@needs_tool
def test_only_mutants_on_changed_lines_count(rust):
    rust.write("tests/smoke.rs", WEAK_TEST)

    result = cargo_mutants.run(str(rust.path), {"src/lib.rs": {9}}, budget=180)

    assert result.error is None and result.failure is None, result
    # `x > hi` on line 9, and clamp's whole body, whose span starts on line 6 but contains line 9.
    assert {m.line for m in result.mutants} == {9}
    assert sorted(m.replacement for m in result.mutants if m.mutator == "BinaryOperator") == ["<", "==", ">="]
    assert all(m.original == ">" for m in result.mutants if m.mutator == "BinaryOperator")
    body = [m for m in result.mutants if m.mutator == "FnValue"]
    assert len(body) == 3 and all(m.original == "if x < lo { …" for m in body)
    assert not any(m.original in (">=", "<") for m in result.mutants)


@needs_tool
def test_whole_file_gives_every_mutant_a_distinct_id(rust):
    rust.write("tests/smoke.rs", WEAK_TEST)
    count = len(LIB.split("\n"))

    result = cargo_mutants.run(str(rust.path), {"src/lib.rs": set(range(1, count + 1))}, budget=180)

    assert result.error is None and result.failure is None, result
    assert len(result.mutants) == 12
    assert {m.line for m in result.mutants} == {2, 6, 9}
    ids = [m.id for m in result.mutants]
    assert len(ids) == len(set(ids))


@needs_tool
def test_unviable_mutants_are_dropped(rust):
    rust.write("src/lib.rs", LIB + "\npub struct Age(pub u32);\n\npub fn make(n: u32) -> Age {\n    Age(n + 1)\n}\n")
    rust.write("tests/make.rs", "#[test]\nfn makes() {\n    assert_eq!(rust_mini::make(1).0, 2);\n}\n")

    result = cargo_mutants.run(str(rust.path), {"src/lib.rs": {18}}, budget=180)

    assert result.error is None and result.failure is None, result
    # `Default::default()` for a type without Default does not compile: not counted either way.
    assert result.mutants and not any("Default" in m.replacement for m in result.mutants)
    assert all(m.status == "detected" for m in result.mutants)


@needs_tool
def test_failing_tests_are_a_failure(rust):
    rust.write("tests/wrong.rs", FAILING_TEST)

    result = cargo_mutants.run(str(rust.path), {"src/lib.rs": {2}}, budget=180)

    assert result.error is None and result.mutants == []
    assert result.failure.startswith("the tests are failing (cargo-mutants baseline):")
    assert "wrong" in result.failure and "thirty counts as adult" in result.failure


@needs_tool
def test_package_without_tests_is_a_failure(rust):
    result = cargo_mutants.run(str(rust.path), {"src/lib.rs": {2}}, budget=180)

    assert result.error is None
    assert "no test" in result.failure


@needs_tool
def test_comment_only_change_has_nothing_to_mutate(rust):
    rust.write("src/lib.rs", "// ages\n" + LIB)
    rust.write("tests/smoke.rs", WEAK_TEST)

    result = cargo_mutants.run(str(rust.path), {"src/lib.rs": {1}}, budget=180)

    assert result == cargo_mutants.AdapterResult()


@needs_tool
def test_workspace_member_in_a_subdirectory(rust):
    rust.write("Cargo.toml", '[workspace]\nmembers = ["crates/mini"]\nresolver = "2"\n')
    shutil.copytree(FIXTURE, rust.path / "crates" / "mini", ignore=shutil.ignore_patterns("target", "Cargo.lock"))
    shutil.rmtree(rust.path / "src")
    rust.write("crates/mini/src/lib.rs", LIB + "\npub mod extra;\n")
    rust.write("crates/mini/src/extra.rs", "pub fn double(n: i32) -> i32 {\n    n * 2\n}\n")
    rust.write("crates/mini/tests/smoke.rs", "#[test]\nfn runs() {\n    rust_mini::extra::double(3);\n}\n")

    result = cargo_mutants.run(str(rust.path), {"crates/mini/src/extra.rs": {2}}, budget=180)

    assert result.error is None and result.failure is None, result
    assert result.mutants and all(m.path == "crates/mini/src/extra.rs" and m.line == 2 for m in result.mutants)
    assert any(m.original == "*" and m.status == "undetected" for m in result.mutants)
    assert not (rust.path / "target").exists() and not (rust.path / "Cargo.lock").exists()


@needs_tool
def test_project_config_cannot_hide_mutants(rust):
    rust.write(".cargo/mutants.toml", 'exclude_globs = ["src/lib.rs"]\nexclude_re = ["."]\nskip_calls = ["is_adult"]\n'
               'additional_cargo_test_args = ["--", "--skip", "runs"]\ncap_lints = true\n')
    rust.write("tests/smoke.rs", WEAK_TEST)

    result = cargo_mutants.run(str(rust.path), {"src/lib.rs": {2}}, budget=180)

    assert result.error is None and result.failure is None, result
    assert len(result.mutants) == 3 and all(m.status == "undetected" for m in result.mutants)


def test_only_build_settings_of_the_project_config_are_passed(tmp_path):
    (tmp_path / ".cargo").mkdir()
    config = tmp_path / ".cargo" / "mutants.toml"
    config.write_text(
        'features = ["serde", "net/tls"]\nno_default_features = true\nprofile = "mutants"\ncopy_vcs = true\n'
        'exclude_re = ["."]\nskip_calls = ["f"]\ntimeout_multiplier = 0.1\ntest_tool = "nextest"\n'
        'additional_cargo_test_args = ["--", "--skip", "x"]\nall_features = "yes"\ncap_lints = 1\n'
    )
    assert cargo_mutants._config_flags(tmp_path) == [
        "--features=serde,net/tls", "--no-default-features", "--profile=mutants", "--copy-vcs=true",
    ]
    config.write_text('features = ["ok", "--evil=1"]\nprofile = "-x"\n')
    assert cargo_mutants._config_flags(tmp_path) == []
    config.write_text("not toml [")
    assert cargo_mutants._config_flags(tmp_path) == []


@needs_tool
def test_file_outside_any_package_is_unverified(repo, gate_home):
    repo.write("scripts/tool.rs", "fn main() {}\n")

    result = cargo_mutants.run(str(repo.path), {"scripts/tool.rs": {1}}, budget=60)

    assert result.mutants == [] and result.error is None
    assert result.unverified == ["not in a Cargo package (no Cargo.toml above it): scripts/tool.rs"]


@needs_tool
def test_run_over_budget_is_a_timeout_and_stops_the_tests(rust, gate_home):
    rust.write("tests/slow.rs", SLOW_TEST)

    result = cargo_mutants.run(str(rust.path), {"src/lib.rs": {2}}, budget=12)

    assert result.error_kind == "timeout" and result.mutants == []
    assert not list(gate_home.glob("state/work/*/tmp"))
    ps = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
    assert str(gate_home) not in ps


def test_missing_cargo_is_a_tool_error_with_rustup_hint(repo, gate_home, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CARGO_HOME", raising=False)
    repo.write("Cargo.toml", (FIXTURE / "Cargo.toml").read_text())
    repo.write("src/lib.rs", LIB)

    result = cargo_mutants.run(str(repo.path), {"src/lib.rs": {2}}, budget=30)

    assert result.error_kind == "missing" and result.failure is None
    assert "cargo install --locked cargo-mutants" in result.error
    assert "https://sh.rustup.rs | sh -s -- -y --no-modify-path" in result.error
    assert result.cacheable is False


def test_missing_cargo_mutants_is_a_tool_error_with_install_hint(repo, gate_home, tmp_path, monkeypatch):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "cargo").write_text("#!/bin/sh\nexit 1\n")
    (fake_bin / "cargo").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CARGO_HOME", raising=False)
    repo.write("src/lib.rs", LIB)

    result = cargo_mutants.run(str(repo.path), {"src/lib.rs": {2}}, budget=30)

    assert result.error_kind == "missing"
    assert result.error.startswith("cargo-mutants is not installed. Install: cargo install --locked cargo-mutants")
    assert "rustup" not in result.error


@pytest.mark.parametrize("name", ["cargo", "cargo-mutants"])
def test_tracked_binary_is_refused(repo, gate_home, tmp_path, monkeypatch, name):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for tool in ("cargo", "cargo-mutants"):
        (fake_bin / tool).write_text("#!/bin/sh\nexit 1\n")
        (fake_bin / tool).chmod(0o755)
    planted = repo.write(f"bin/{name}", "#!/bin/sh\ntouch PWNED\n")
    planted.chmod(0o755)
    git(repo.path, "add", "-f", f"bin/{name}")
    monkeypatch.setenv("PATH", f"{repo.path / 'bin'}:{fake_bin}:{os.environ['PATH']}")
    repo.write("src/lib.rs", LIB)

    result = cargo_mutants.run(str(repo.path), {"src/lib.rs": {2}}, budget=30)

    assert result.error_kind == "missing" and "git tracks it" in result.error
    assert f"bin/{name}" in result.error
    assert not (repo.path / "PWNED").exists()


def test_diff_adds_exactly_the_changed_lines():
    rows = cargo_mutants._source_lines("a\r\nb\nc\nd\n")
    assert rows == ["a", "b", "c", "d"]
    text = cargo_mutants._diff([("src/x.rs", rows, {1, 3, 4, 9})])
    assert text == "--- a/src/x.rs\n+++ b/src/x.rs\n@@ -0,0 +1,1 @@\n+a\n@@ -1,0 +3,2 @@\n+c\n+d\n"
