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
    assert store.is_enabled(root) is False
    cli("on", cwd=repo.path)
    assert store.is_enabled(root) is True
    cli("off", cwd=repo.path)
    assert store.is_enabled(root) is False
    cli("threshold", "90", cwd=repo.path)
    assert store.load_config()["threshold"] == 90
    assert cli("threshold", "150", cwd=repo.path).returncode != 0


def test_status_shows_settings(repo, gate_home):
    cli("on", cwd=repo.path)
    cli("allow", "abcd1234", "reason", cwd=repo.path)
    out = cli("status", cwd=repo.path).stdout
    assert "threshold 80%" in out and "abcd1234" in out and ": on" in out



def test_pre_tool_hook_prints_nothing_when_allowed(repo, gate_home):
    payload = {"session_id": "s1", "cwd": str(repo.path), "tool_name": "Bash", "tool_input": {"command": "ls"}}
    out = cli("hook", "pre-tool", cwd=repo.path, stdin=json.dumps(payload))
    assert out.returncode == 0 and out.stdout == ""


def test_hook_crash_is_reported_to_user_not_swallowed(repo, gate_home):
    out = cli("hook", "stop", cwd=repo.path, stdin="{not json")
    assert out.returncode == 0
    assert "mutation-gate internal error" in json.loads(out.stdout)["systemMessage"]


def test_stop_hook_end_to_end_reports_weak_python_test(repo, gate_home):
    fixture = Path(__file__).parent / "fixtures" / "py-mini" / ".venv"
    repo.write("pyproject.toml", '[project]\nname = "p"\nversion = "0"\n\n[tool.pytest.ini_options]\npythonpath = ["src"]\n')
    repo.write("src/pkg/__init__.py", "")
    repo.write("src/pkg/age.py", "def is_adult(age):\n    return age > 0\n")
    repo.write(".gitignore", ".venv\n")
    (repo.path / ".venv").symlink_to(fixture)
    repo.commit()
    cli("on", cwd=repo.path)
    env = {"CLAUDE_PROJECT_DIR": str(repo.path)}
    cli("hook", "session-start", cwd=repo.path, env=env, stdin=json.dumps({"session_id": "e2e", "cwd": str(repo.path)}))
    repo.write("src/pkg/age.py", "def is_adult(age):\n    return age >= 18\n")
    repo.write("tests/test_age.py", "from pkg.age import is_adult\n\ndef test_runs():\n    is_adult(30)\n")

    out = cli("hook", "stop", cwd=repo.path, env=env, stdin=json.dumps({"session_id": "e2e", "cwd": str(repo.path)}))

    result = json.loads(out.stdout)
    assert "decision" not in result, out.stdout + out.stderr
    assert "src/pkg/age.py:2" in result["systemMessage"]
    assert str(repo.path.resolve()) in store.load_session("e2e")["repos"]

    last = cli("last", cwd=repo.path).stdout
    assert "src/pkg/age.py:2" in last


def test_on_reports_whether_the_mutation_tool_is_installed(repo, gate_home):
    out = cli("on", cwd=repo.path).stdout
    assert ": on" in out and "installed" in out


def test_killed_stop_hook_takes_the_tool_processes_with_it(repo, gate_home, tmp_path):
    import signal
    import time

    pid_file = tmp_path / "child.pid"
    repo.write(".gitignore", "node_modules\n")
    repo.write("node_modules/vitest/package.json", "{}")
    repo.write("node_modules/@stryker-mutator/vitest-runner/package.json", "{}")
    repo.write("node_modules/.bin/stryker", f"#!/bin/sh\nsleep 60 &\necho $! > {pid_file}\nwait\n")
    (repo.path / "node_modules/.bin/stryker").chmod(0o755)
    repo.write("src/a.ts", "export const a = 1;\n")
    repo.commit()
    cli("on", cwd=repo.path)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo.path)}
    cli("hook", "session-start", cwd=repo.path, env=env, stdin=json.dumps({"session_id": "k", "cwd": str(repo.path)}))
    repo.write("src/a.ts", "export const a = 2;\n")

    hook = subprocess.Popen([sys.executable, str(CLI), "hook", "stop"], cwd=repo.path, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    hook.stdin.write(json.dumps({"session_id": "k", "cwd": str(repo.path)}))
    hook.stdin.close()
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        time.sleep(0.1)
    hook.send_signal(signal.SIGTERM)
    hook.wait(10)
    time.sleep(0.3)
    child = int(pid_file.read_text())
    try:
        os.kill(child, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive


def _py_project(repo, test_body):
    fixture = Path(__file__).parent / "fixtures" / "py-mini" / ".venv"
    repo.write("pyproject.toml", '[project]\nname = "p"\nversion = "0"\n\n[tool.pytest.ini_options]\npythonpath = ["src"]\n')
    repo.write("src/pkg/__init__.py", "")
    repo.write("src/pkg/age.py", "def is_adult(age):\n    return age >= 18\n")
    repo.write("tests/test_age.py", test_body)
    repo.write(".gitignore", ".venv\n")
    (repo.path / ".venv").symlink_to(fixture)
    repo.commit()


WEAK = "from pkg.age import is_adult\n\ndef test_runs():\n    is_adult(30)\n"
STRONG = "from pkg.age import is_adult\n\ndef test_edge():\n    assert is_adult(17) is False\n    assert is_adult(18) is True\n"


def test_test_command_reports_survivors_and_fails(repo, gate_home):
    _py_project(repo, WEAK)

    out = cli("test", "src/pkg/age.py", cwd=repo.path)

    assert out.returncode == 1, out.stdout + out.stderr
    assert "FAIL" in out.stdout and "src/pkg/age.py:2" in out.stdout


def test_test_command_passes_strong_tests(repo, gate_home):
    _py_project(repo, STRONG)

    out = cli("test", "src", cwd=repo.path)

    assert out.returncode == 0, out.stdout + out.stderr
    assert "PASS" in out.stdout and "100.0%" in out.stdout


def test_test_command_without_paths_uses_uncommitted_source_changes(repo, gate_home):
    _py_project(repo, WEAK)
    repo.write("src/pkg/age.py", "def is_adult(age):\n    return age > 17\n")

    out = cli("test", cwd=repo.path)

    assert out.returncode == 1 and "src/pkg/age.py" in out.stdout


def test_test_command_with_nothing_to_test_says_so(repo, gate_home):
    repo.write("README.md", "x\n")
    repo.commit()

    out = cli("test", cwd=repo.path)

    assert out.returncode == 2 and "No source files" in out.stdout


def test_test_command_result_shows_in_last(repo, gate_home):
    _py_project(repo, WEAK)
    cli("test", "src/pkg/age.py", cwd=repo.path)
    assert "src/pkg/age.py:2" in cli("last", cwd=repo.path).stdout


def test_post_tool_hook_reminds_through_stdin(repo, gate_home):
    payload = {"session_id": "n1", "cwd": str(repo.path), "tool_name": "Bash", "tool_input": {"command": "pytest -q"}}
    out = cli("hook", "post-tool", cwd=repo.path, stdin=json.dumps(payload))
    assert "mutation-gate test" in json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
    cli("hook", "prompt", cwd=repo.path, stdin=json.dumps({"session_id": "n1"}))
    out = cli("hook", "post-tool", cwd=repo.path, stdin=json.dumps(payload))
    assert out.stdout.strip()


def test_hooks_json_wires_every_event():
    hooks = json.loads((CLI.parent.parent / "hooks" / "hooks.json").read_text())["hooks"]
    commands = {event: [h["command"] for g in groups for h in g["hooks"]] for event, groups in hooks.items()}
    assert set(commands) == {"SessionStart", "PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"}
    for event, (command,) in commands.items():
        assert command.startswith('python3 -I "${CLAUDE_PLUGIN_ROOT}/bin/mutation-gate" hook ')


def test_mutate_command_reports_llm_mutants(repo, gate_home, tmp_path):
    _py_project(repo, WEAK)
    spec = tmp_path / "m.json"
    spec.write_text(json.dumps({"mutations": [{"file": "src/pkg/age.py", "find": "age >= 18", "replace": "age > 18",
                                               "consequence": "18 is not adult", "breaksOn": "is_adult(18) -> False"}]}))

    out = cli("mutate", str(spec), cwd=repo.path)

    assert out.returncode == 1, out.stdout + out.stderr
    assert "mutation-gate mutate" in out.stdout and "FAIL" in out.stdout
    assert "src/pkg/age.py:2" in out.stdout and "is_adult(18) -> False" in out.stdout


def test_verify_command_exit_codes(repo, gate_home, tmp_path):
    _py_project(repo, STRONG)
    spec = tmp_path / "v.json"
    spec.write_text(json.dumps({
        "mutation": {"file": "src/pkg/age.py", "find": "age >= 18", "replace": "age > 18"},
        "siblings": [{"file": "src/pkg/age.py", "find": "age >= 18", "replace": "age >= 19"}],
    }))
    cmd = ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_age.py"

    out = cli("verify", str(spec), "--test-cmd", cmd, cwd=repo.path)

    assert out.returncode == 0 and "accepted" in out.stdout, out.stdout + out.stderr


def test_session_start_restores_files_a_killed_run_left_mutated(repo, gate_home):
    from mutation_gate import manifest
    repo.write("a.py", "x = 1\n")
    repo.commit()
    manifest._journal_write(str(repo.path), str(repo.path / "a.py"), b"x = 1\n")
    repo.write("a.py", "x = 2\n")

    out = cli("hook", "session-start", cwd=repo.path, stdin=json.dumps({"session_id": "r1", "cwd": str(repo.path)}))

    assert (repo.path / "a.py").read_text() == "x = 1\n"
    assert "restored" in json.loads(out.stdout)["systemMessage"]


def test_on_lists_user_scope_tools(repo, gate_home, tmp_path):
    fake_home = tmp_path / "home"
    tool = fake_home / ".cargo" / "bin" / "cargo-mutants"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)

    out = cli("on", cwd=repo.path, env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin"}).stdout

    assert "cargo-mutants" in out
