from pathlib import Path

import pytest

from mutation_gate.adapters import mutmut

FIXTURE = Path(__file__).parent / "fixtures" / "py-mini"
pytestmark = pytest.mark.skipif(
    not (FIXTURE / ".venv" / "bin" / "mutmut").exists(),
    reason="run tests/setup_fixtures.sh first",
)

PYPROJECT = """[project]
name = "py-mini"
version = "0.0.1"

[tool.pytest.ini_options]
pythonpath = ["src"]
"""
AGE = """def is_adult(age):
    return age >= 18


def label(age):
    if age >= 65:
        return "senior"
    return "other"


class Box:
    def double(self, n):
        return n * 2


GREETING = "hi"


class Tool:
    @staticmethod
    def half(n):
        return n // 2
"""
WEAK_TEST = """from pkg.age import is_adult


def test_runs():
    is_adult(30)
"""
STRONG_TEST = """from pkg.age import is_adult


def test_boundary():
    assert is_adult(17) is False
    assert is_adult(18) is True
"""


@pytest.fixture
def py(repo, gate_home):
    repo.write("pyproject.toml", PYPROJECT)
    repo.write("src/pkg/__init__.py", "")
    repo.write("src/pkg/age.py", AGE)
    (repo.path / ".venv").symlink_to(FIXTURE / ".venv")
    repo.write(".gitignore", ".venv\n")
    return repo


def test_weak_test_leaves_survivors_with_details(py):
    py.write("tests/test_age.py", WEAK_TEST)

    result = mutmut.run(str(py.path), {"src/pkg/age.py": {2}}, budget=120)

    assert result.error is None and result.failure is None
    assert result.mutants and all(m.status == "undetected" for m in result.mutants)
    assert {m.line for m in result.mutants} == {2}
    ge = [m for m in result.mutants if "age > 18" in m.replacement]
    assert ge and ge[0].original == "return age >= 18"


def test_strong_test_kills_everything(py):
    py.write("tests/test_age.py", STRONG_TEST)

    result = mutmut.run(str(py.path), {"src/pkg/age.py": {2}}, budget=120)

    assert result.error is None and result.failure is None
    assert result.mutants and all(m.status == "detected" for m in result.mutants)


def test_only_mutants_on_changed_lines_count(py):
    py.write("tests/test_age.py", STRONG_TEST)

    # label() changed on line 7 only; line 6 mutants of the same function are out of scope.
    result = mutmut.run(str(py.path), {"src/pkg/age.py": {7}}, budget=120)

    assert result.error is None and result.failure is None
    assert {m.line for m in result.mutants} == {7}


def test_untested_function_mutants_are_undetected(py):
    py.write("tests/test_age.py", STRONG_TEST)

    result = mutmut.run(str(py.path), {"src/pkg/age.py": {13}}, budget=120)

    assert result.mutants and all(m.status == "undetected" for m in result.mutants)
    assert {m.line for m in result.mutants} == {13}


def test_decorated_method_lines_map_to_file_lines(py):
    py.write("tests/test_age.py", STRONG_TEST)

    result = mutmut.run(str(py.path), {"src/pkg/age.py": {22}}, budget=120)

    assert result.mutants
    assert {(m.line, m.original) for m in result.mutants} == {(22, "return n // 2")}


def test_module_level_lines_are_not_mutated(py):
    py.write("tests/test_age.py", STRONG_TEST)

    result = mutmut.run(str(py.path), {"src/pkg/age.py": {16}}, budget=120)

    assert result == mutmut.AdapterResult()


def test_failing_tests_are_a_failure(py):
    py.write("tests/test_age.py", STRONG_TEST + "\n\ndef test_broken():\n    assert False\n")

    result = mutmut.run(str(py.path), {"src/pkg/age.py": {2}}, budget=120)

    assert result.error is None
    assert "실패" in result.failure


def test_work_dir_is_removed_after_run(py):
    py.write("tests/test_age.py", WEAK_TEST)

    mutmut.run(str(py.path), {"src/pkg/age.py": {2}}, budget=120)

    assert not (py.path / "mutants").exists()


def test_foreign_mutants_dir_is_left_alone(py):
    py.write("tests/test_age.py", WEAK_TEST)
    py.write("mutants/keep.txt", "user data")

    result = mutmut.run(str(py.path), {"src/pkg/age.py": {2}}, budget=120)

    assert "mutants/" in result.error
    assert (py.path / "mutants" / "keep.txt").read_text() == "user data"


def test_missing_mutmut_is_a_tool_error_with_install_hint(repo, gate_home):
    repo.write("src/pkg/age.py", AGE)

    result = mutmut.run(str(repo.path), {"src/pkg/age.py": {2}}, budget=30)

    assert result.failure is None
    assert "mutmut" in result.error and "install" in result.error


def test_mangled_names_cover_functions_and_methods():
    names = mutmut.touched_functions(AGE, {2, 7, 13, 16})
    assert names == ["x_is_adult", "x_label", "xǁBoxǁdouble"]


def test_module_name_strips_src_prefix_and_init():
    assert mutmut.module_name("src/pkg/age.py") == "pkg.age"
    assert mutmut.module_name("pkg/age.py") == "pkg.age"
    assert mutmut.module_name("src/pkg/__init__.py") == "pkg"


def test_parse_show_keeps_removed_lines_that_start_with_dashes():
    text = "# m: survived\n--- src/a.py\n+++ src/a.py\n@@ -1,3 +1,3 @@\n def f(x):\n---x\n+--x + 1\n"
    assert mutmut._parse_show(text) == (2, "--x", "--x + 1", 1)


def test_functions_in_init_are_mutated(py):
    py.write("src/pkg/__init__.py", "def is_adult(age):\n    return age >= 18\n")
    py.write("tests/test_init.py", "from pkg import is_adult\n\n\ndef test_runs():\n    is_adult(30)\n")

    result = mutmut.run(str(py.path), {"src/pkg/__init__.py": {2}}, budget=120)

    assert result.mutants and all(m.status == "undetected" for m in result.mutants)


def test_comment_above_def_keeps_line_mapping(py):
    src = "def other():\n    return 1\n\n\n# Legal adulthood threshold.\ndef is_adult(age):\n    return age >= 18\n"
    py.write("src/pkg/age.py", src)
    py.write("tests/test_age.py", WEAK_TEST)

    result = mutmut.run(str(py.path), {"src/pkg/age.py": {7}}, budget=120)

    assert result.mutants and {m.line for m in result.mutants} == {7}


def test_decorated_functions_are_reported_unverified(py):
    py.write("src/pkg/age.py", "import functools\n\n\n@functools.cache\ndef is_adult(age):\n    return age >= 18\n")
    py.write("tests/test_age.py", WEAK_TEST)

    result = mutmut.run(str(py.path), {"src/pkg/age.py": {6}}, budget=120)

    assert result.mutants == []
    assert any("is_adult" in u for u in result.unverified)


def test_no_test_covers_any_mutant_is_a_failure(py):
    py.write("tests/test_other.py", "def test_nothing():\n    assert True\n")

    result = mutmut.run(str(py.path), {"src/pkg/age.py": {2}}, budget=120)

    assert result.failure and "테스트" in result.failure


def test_tests_that_only_fail_inside_mutmut_sandbox_are_an_error(py):
    py.write("data/limit.txt", "18\n")
    py.write("tests/test_age.py", STRONG_TEST + "\n\ndef test_data():\n    assert open('data/limit.txt').read().strip() == '18'\n")

    result = mutmut.run(str(py.path), {"src/pkg/age.py": {2}}, budget=120)

    assert result.failure is None
    assert result.error and "sandbox" in result.error


def test_tracked_mutmut_binary_is_refused(repo, gate_home):
    from conftest import git
    repo.write("src/pkg/age.py", AGE)
    repo.write(".venv/bin/mutmut", "#!/bin/sh\ntouch PWNED\n")
    (repo.path / ".venv/bin/mutmut").chmod(0o755)
    git(repo.path, "add", "-f", ".venv/bin/mutmut")

    result = mutmut.run(str(repo.path), {"src/pkg/age.py": {2}}, budget=30)

    assert result.error and result.error_kind == "missing"
    assert not (repo.path / "PWNED").exists()


def test_committed_marker_does_not_get_mutants_dir_deleted(py):
    from conftest import git
    py.write("tests/test_age.py", WEAK_TEST)
    py.write("mutants/.mutation-gate", "created by mutation-gate\n")
    py.write("mutants/notes.md", "keep me\n")
    git(py.path, "add", "-f", "mutants")

    result = mutmut.run(str(py.path), {"src/pkg/age.py": {2}}, budget=120)

    assert result.error and "mutants/" in result.error
    assert (py.path / "mutants" / "notes.md").read_text() == "keep me\n"
