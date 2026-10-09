import json
import os
import subprocess
import sys
from pathlib import Path

from mutation_gate import store

CLI = Path(__file__).resolve().parent.parent / "bin" / "mutation-gate"


def cli(*args, cwd, stdin=None, env=None):
    return subprocess.run(
        [sys.executable, str(CLI), *args], cwd=cwd, input=stdin, capture_output=True, text=True,
        env={**os.environ, **(env or {})},
    )


def test_allow_and_disallow_for_current_project(repo, gate_home):
    out = cli("allow", "abcd1234", "x < lo vs x <= lo: same result", cwd=repo.path)
    assert out.returncode == 0, out.stderr
    assert store.allowed_ids(str(repo.path.resolve())) == {"abcd1234"}
    cli("disallow", "abcd1234", cwd=repo.path)
    assert store.allowed_ids(str(repo.path.resolve())) == set()


def test_allow_rejects_malformed_id(repo, gate_home):
    out = cli("allow", "not-an-id", cwd=repo.path)
    assert out.returncode != 0


def test_off_on_and_threshold(repo, gate_home):
    root = str(repo.path.resolve())
    cli("off", cwd=repo.path)
    assert store.is_enabled(root) is False
    cli("on", cwd=repo.path)
    assert store.is_enabled(root) is True
    cli("threshold", "90", cwd=repo.path)
    assert store.load_config()["threshold"] == 90
    assert cli("threshold", "150", cwd=repo.path).returncode != 0


def test_status_shows_settings(repo, gate_home):
    cli("allow", "abcd1234", "reason", cwd=repo.path)
    out = cli("status", cwd=repo.path).stdout
    assert "threshold 80%" in out and "abcd1234" in out and "켜짐" in out


def test_pre_tool_hook_denies_through_stdin(repo, gate_home):
    payload = {"session_id": "s1", "cwd": str(repo.path), "tool_name": "Bash",
               "tool_input": {"command": "mutation-gate allow abcd1234 x"}}
    out = cli("hook", "pre-tool", cwd=repo.path, stdin=json.dumps(payload))
    assert out.returncode == 0
    assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pre_tool_hook_prints_nothing_when_allowed(repo, gate_home):
    payload = {"session_id": "s1", "cwd": str(repo.path), "tool_name": "Bash", "tool_input": {"command": "ls"}}
    out = cli("hook", "pre-tool", cwd=repo.path, stdin=json.dumps(payload))
    assert out.returncode == 0 and out.stdout == ""


def test_hook_crash_is_reported_to_user_not_swallowed(repo, gate_home):
    out = cli("hook", "stop", cwd=repo.path, stdin="{not json")
    assert out.returncode == 0
    assert "mutation-gate 내부 오류" in json.loads(out.stdout)["systemMessage"]


def test_stop_hook_end_to_end_blocks_weak_python_test(repo, gate_home):
    fixture = Path(__file__).parent / "fixtures" / "py-mini" / ".venv"
    repo.write("pyproject.toml", '[project]\nname = "p"\nversion = "0"\n\n[tool.pytest.ini_options]\npythonpath = ["src"]\n')
    repo.write("src/pkg/__init__.py", "")
    repo.write("src/pkg/age.py", "def is_adult(age):\n    return age > 0\n")
    repo.write(".gitignore", ".venv\n")
    (repo.path / ".venv").symlink_to(fixture)
    base = repo.commit()
    env = {"CLAUDE_PROJECT_DIR": str(repo.path)}
    cli("hook", "session-start", cwd=repo.path, env=env, stdin=json.dumps({"session_id": "e2e", "cwd": str(repo.path)}))
    repo.write("src/pkg/age.py", "def is_adult(age):\n    return age >= 18\n")
    repo.write("tests/test_age.py", "from pkg.age import is_adult\n\ndef test_runs():\n    is_adult(30)\n")

    out = cli("hook", "stop", cwd=repo.path, env=env, stdin=json.dumps({"session_id": "e2e", "cwd": str(repo.path)}))

    result = json.loads(out.stdout)
    assert result["decision"] == "block", out.stdout + out.stderr
    assert "src/pkg/age.py:2" in result["reason"]
    assert store.load_session("e2e")["repos"] == {str(repo.path.resolve()): base}

    last = cli("last", cwd=repo.path).stdout
    assert "src/pkg/age.py:2" in last
