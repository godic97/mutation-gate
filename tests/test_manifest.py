import json
from pathlib import Path

import pytest

from mutation_gate import manifest, store

FIXTURE = Path(__file__).parent / "fixtures" / "py-mini"
pytestmark = pytest.mark.skipif(not (FIXTURE / ".venv" / "bin" / "python").exists(), reason="run tests/setup_fixtures.sh first")

AGE = "def is_adult(age):\n    return age >= 18\n"
WEAK = "from pkg.age import is_adult\n\n\ndef test_runs():\n    is_adult(30)\n"
STRONG = "from pkg.age import is_adult\n\n\ndef test_edge():\n    assert is_adult(17) is False\n    assert is_adult(18) is True\n"
CMD = ".venv/bin/python -m pytest -q -p no:cacheprovider"

BOUNDARY = {"file": "src/pkg/age.py", "find": "age >= 18", "replace": "age > 18",
            "symbol": "is_adult", "consequence": "18-year-olds are not adults", "breaksOn": "is_adult(18) -> False"}
ALWAYS = {"file": "src/pkg/age.py", "find": "return age >= 18", "replace": "return True", "symbol": "is_adult"}


@pytest.fixture
def py(repo, gate_home):
    repo.write("pyproject.toml", '[project]\nname = "p"\nversion = "0"\n\n[tool.pytest.ini_options]\npythonpath = ["src"]\n')
    repo.write("src/pkg/__init__.py", "")
    repo.write("src/pkg/age.py", AGE)
    (repo.path / ".venv").symlink_to(FIXTURE / ".venv")
    repo.write(".gitignore", ".venv\n")
    return repo


def run(py, mutations, cmd=CMD, budget=120):
    return manifest.run(str(py.path.resolve()), {"mutations": mutations}, cmd, budget)


def test_weak_tests_let_mutations_survive(py):
    py.write("tests/test_age.py", WEAK)

    v = run(py, [BOUNDARY, ALWAYS])

    assert v["status"] == "fail" and v["total"] == 2 and v["detected"] == 0
    s = v["survivors"][0]
    assert s["path"] == "src/pkg/age.py" and s["line"] == 2
    assert s["original"] == "age >= 18" and s["replacement"] == "age > 18"
    assert s["consequence"] == "18-year-olds are not adults" and s["breaksOn"] == "is_adult(18) -> False"


def test_strong_tests_kill_them(py):
    py.write("tests/test_age.py", STRONG)

    v = run(py, [BOUNDARY, ALWAYS])

    assert v["status"] == "pass" and v["detected"] == 2


def test_source_is_restored_byte_for_byte(py):
    py.write("tests/test_age.py", WEAK)
    before = (py.path / "src/pkg/age.py").read_bytes()

    run(py, [BOUNDARY, ALWAYS])

    assert (py.path / "src/pkg/age.py").read_bytes() == before
    assert not list((store.home() / "state" / "journal").glob("*.json"))


def test_ambiguous_or_missing_find_is_skipped(py):
    py.write("tests/test_age.py", STRONG)
    py.write("src/pkg/age.py", AGE + "\n\ndef twice(age):\n    return age >= 18\n")

    v = run(py, [ALWAYS, {"file": "src/pkg/age.py", "find": "no such text", "replace": "x"}])

    assert v["total"] == 0
    reasons = " ".join(s["reason"] for s in v["skipped"])
    assert "2 times" in reasons and "not found" in reasons


def test_file_outside_the_repo_is_skipped(py, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n")
    py.write("tests/test_age.py", STRONG)

    v = run(py, [{"file": str(outside), "find": "x = 1", "replace": "x = 2"}])

    assert v["skipped"] and "outside" in v["skipped"][0]["reason"]
    assert outside.read_text() == "x = 1\n"


def test_failing_baseline_stops_before_mutating(py):
    py.write("tests/test_age.py", STRONG + "\n\ndef test_broken():\n    assert False\n")

    v = run(py, [BOUNDARY])

    assert v["status"] == "fail" and "failing" in v["failures"][0]
    assert v["total"] == 0


def test_leftover_journal_is_restored(py):
    target = py.path / "src/pkg/age.py"
    manifest._journal_write(str(py.path.resolve()), str(target), target.read_bytes())
    target.write_text("def is_adult(age):\n    return True\n")

    restored = manifest.restore_all()

    assert restored == [str(target)]
    assert target.read_text() == AGE


@pytest.mark.parametrize("files, expected", [
    ({"Cargo.toml": ""}, "cargo test"),
    ({"go.mod": "module x\n"}, "go test ./..."),
    ({"pom.xml": "<project/>"}, "mvn -q test"),
    ({"build.sbt": ""}, "sbt test"),
    ({"App.csproj": "<Project/>"}, "dotnet test"),
    ({"package.json": '{"scripts": {"test": "vitest run"}}'}, "npm test"),
    ({"composer.json": "{}", "vendor/bin/phpunit": ""}, "vendor/bin/phpunit"),
    ({"Gemfile": "", "spec/a_spec.rb": ""}, "bundle exec rspec"),
])
def test_detects_test_command(repo, files, expected):
    for name, text in files.items():
        repo.write(name, text)
    assert manifest.detect_test_command(str(repo.path)) == expected


def test_detects_pytest_through_the_repo_venv(py):
    assert manifest.detect_test_command(str(py.path)) == ".venv/bin/python -m pytest -q"


def test_no_known_project_means_no_command(repo):
    repo.write("README.md", "x")
    assert manifest.detect_test_command(str(repo.path)) is None


# --- verify: the triple gate -----------------------------------------------------------------

SIBLINGS = [
    {"file": "src/pkg/age.py", "find": "age >= 18", "replace": "age >= 19"},
    {"file": "src/pkg/age.py", "find": "return age >= 18", "replace": "return False"},
]


def verify(py, test_cmd):
    spec = {"mutation": BOUNDARY, "siblings": SIBLINGS}
    return manifest.verify(str(py.path.resolve()), spec, test_cmd, 120)


def test_verify_accepts_a_test_that_pins_the_behaviour(py):
    py.write("tests/test_new.py", STRONG)
    v = verify(py, ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_new.py")
    assert v["verdict"] == "accepted", v
    assert v["clean"] == "passed" and v["mutant"] == "killed" and v["siblings_killed"] >= 1


def test_verify_rejects_a_test_that_only_fits_the_one_mutant(py):
    overfit = "from pkg.age import is_adult\n\n\ndef test_fit():\n    assert is_adult(18) is True\n    assert is_adult(19) is True\n"
    py.write("tests/test_new.py", overfit)
    spec = {"mutation": BOUNDARY, "siblings": [{"file": "src/pkg/age.py", "find": "return age >= 18", "replace": "return True"}]}
    v = manifest.verify(str(py.path.resolve()), spec, ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_new.py", 120)
    assert v["verdict"] == "rejected" and v["mutant"] == "killed" and v["siblings_killed"] == 0


def test_verify_rejects_a_test_that_fails_on_clean_code(py):
    py.write("tests/test_new.py", "def test_bad():\n    assert False\n")
    v = verify(py, ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_new.py")
    assert v["verdict"] == "rejected" and v["clean"] == "failed"
