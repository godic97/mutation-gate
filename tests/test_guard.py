import os
import threading
import time

import pytest

from mutation_gate import diff, gate, guard, store


@pytest.fixture
def env(gate_home, tmp_path, monkeypatch):
    plugin = tmp_path / "cache" / "mutation-gate" / "0.1.0"
    plugin.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    return plugin


def call(tool, sid="s1", cwd="/", **tool_input):
    return guard.on_pre_tool({"session_id": sid, "cwd": cwd, "tool_name": tool, "tool_input": tool_input})


def denied(out):
    return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_ordinary_edit_is_allowed(env, repo):
    assert call("Edit", file_path=str(repo.path / "src/a.ts"), old_string="a", new_string="b") is None


def tracked(repo):
    return store.load_session("s1")["repos"]


def enabled(repo):
    repo.commit()
    store.set_enabled(str(repo.path.resolve()), True)
    return str(repo.path.resolve()), diff.snapshot(repo.path)


def test_edit_records_repo_base_before_change(env, repo):
    root, snap = enabled(repo)
    call("Write", file_path=str(repo.path / "src" / "new.py"), content="x = 1\n")
    assert tracked(repo)[root]["base"] == snap


def test_repos_that_are_not_enabled_are_not_recorded(env, repo):
    repo.commit()
    call("Write", file_path=str(repo.path / "src" / "new.py"), content="x = 1\n")
    call("Bash", cwd=str(repo.path), command="ls")
    assert tracked(repo) == {}


def test_bash_records_repo_of_cwd(env, repo):
    root, snap = enabled(repo)
    call("Bash", cwd=str(repo.path), command="ls")
    assert tracked(repo)[root]["base"] == snap


@pytest.mark.parametrize("target", [
    "{plugin}/mutation_gate/gate.py",
    "{home}/config.json",
    "{home}/allow.json",
    "/Users/max/workspace/mutation-gate/mutation_gate/gate.py",
])
def test_writes_to_gate_files_are_denied(env, gate_home, target):
    path = target.format(plugin=env, home=gate_home)
    assert denied(call("Write", file_path=path, content="x"))
    assert denied(call("Edit", file_path=path, old_string="a", new_string="b"))


def test_settings_edit_touching_the_plugin_is_denied(env, tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"enabledPlugins": {"mutation-gate@local": true}}')
    assert denied(call("Edit", file_path=str(settings), old_string='"mutation-gate@local": true', new_string='"mutation-gate@local": false'))


def test_settings_rewrite_dropping_the_plugin_is_denied(env, tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"enabledPlugins": {"mutation-gate@local": true}}')
    assert denied(call("Write", file_path=str(settings), content='{"enabledPlugins": {}}'))


def test_unrelated_settings_edit_is_allowed(env, tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"model": "x"}')
    assert call("Edit", file_path=str(settings), old_string='"x"', new_string='"y"') is None


@pytest.mark.parametrize("text", [
    "// Stryker disable next-line all",
    "/* Stryker disable all */",
    "x = 1  # pragma: no mutate",
])
def test_adding_suppression_markers_is_denied(env, text):
    assert denied(call("Edit", file_path="/p/src/a.ts", old_string="a", new_string=f"a\n{text}"))
    assert denied(call("Write", file_path="/p/src/a.ts", content=text))
    assert denied(call("MultiEdit", file_path="/p/src/a.ts", edits=[{"old_string": "a", "new_string": text}]))


def test_keeping_an_existing_suppression_marker_is_allowed(env):
    text = "// Stryker disable next-line all"
    assert call("Edit", file_path="/p/src/a.ts", old_string=f"{text}\nfoo", new_string=f"{text}\nbar") is None


def test_mutmut_config_keys_in_pyproject_are_denied(env):
    out = call("Edit", file_path="/p/pyproject.toml", old_string="[tool.mutmut]", new_string='[tool.mutmut]\ndo_not_mutate = ["a.py"]')
    assert denied(out)


@pytest.mark.parametrize("command", [
    "mutation-gate allow abcd1234 equivalent",
    "python3 ~/.claude/plugins/cache/x/mutation-gate/0.1.0/bin/mutation-gate off",
    "cp /tmp/a ~/.config/mutation-gate/allow.json",
    "claude plugin disable mutation-gate@local",
    "sed -i '' 's/x/y  # pragma: no mutate/' src/a.py",
    "python3 -c 'import json; json.dump({}, open(\"/Users/max/.claude/settings.json\",\"w\"))'",
    "jq 'del(.enabledPlugins)' ~/.claude/settings.json > /tmp/s && mv /tmp/s ~/.claude/settings.json",
])
def test_bash_tampering_is_denied(env, command):
    assert denied(call("Bash", command=command))


@pytest.mark.parametrize("command", ["pnpm test", "git status", "cat .claude/settings.json", "grep -r foo src"])
def test_ordinary_bash_is_allowed(env, command):
    assert call("Bash", command=command) is None


def test_deny_reason_names_the_rule(env, gate_home):
    out = call("Write", file_path=str(gate_home / "allow.json"), content="{}")
    assert "mutation-gate" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_bash_records_repo_entered_with_cd(env, repo, tmp_path):
    root, snap = enabled(repo)
    call("Bash", cwd=str(tmp_path), command=f"cd {repo.path.name} && sed -i '' 's/a/b/' src/a.py")
    assert tracked(repo)[root]["base"] == snap


def test_bash_records_repo_named_with_git_dash_c(env, repo, tmp_path):
    root, snap = enabled(repo)
    call("Bash", cwd=str(tmp_path), command=f"git -C {repo.path} commit -am x")
    assert tracked(repo)[root]["base"] == snap


def test_suppression_marker_in_docs_is_allowed(env):
    assert call("Write", file_path="/p/README.md", content="Do not add `// Stryker disable` comments") is None


@pytest.mark.parametrize("command", ['grep -rn "pragma: no mutate" src', "rg 'Stryker disable' ."])
def test_searching_for_markers_is_allowed(env, command):
    assert call("Bash", command=command) is None


def test_disable_all_hooks_in_settings_is_denied(env, tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"model": "x"}')
    assert denied(call("Edit", file_path=str(settings), old_string='"model": "x"', new_string='"model": "x", "disableAllHooks": true'))
    assert denied(call("Write", file_path=str(settings), content='{"disableAllHooks": true}'))


def test_suppression_denied_even_under_an_ancestor_named_tests(env, tmp_path, repo):
    path = repo.path / "src" / "a.py"
    assert "proj" in path.parts
    nested = tmp_path / "tests" / "proj" / "src" / "a.py"
    assert denied(call("Write", file_path=str(nested), content="x = 1  # pragma: no mutate"))


@pytest.mark.parametrize("command", [
    'echo "" > "$CLAUDE_PLUGIN_ROOT/mutation_gate/gate.py"',
    "sed -i '' 's/80/0/' ${CLAUDE_PLUGIN_ROOT}/mutation_gate/store.py",
    "cp /tmp/x ~/.claude/plugins/cache/m/mutation_gate/gate.py",
    "echo '{}' > $MUTATION_GATE_HOME/allow.json",
])
def test_bash_writes_into_plugin_are_denied(env, command):
    assert denied(call("Bash", command=command))


@pytest.mark.parametrize("command", [
    "git log --grep=mutation-gate",
    "cd ~/workspace/mutation-gate && .venv/bin/python -m pytest -q",
    'rg "pragma: no mutate" src 2>/dev/null',
    "cp appsettings.json.example out/",
    "ls 2>&1 | head",
])
def test_harmless_bash_is_allowed(env, command):
    assert call("Bash", command=command) is None


@pytest.mark.parametrize("command", [
    "mutation-gate allow abcd1234 x",
    "mutation-gate threshold 0",
    "python3 ~/workspace/mutation-gate/bin/mutation-gate off",
])
def test_bash_running_the_cli_is_denied(env, command):
    assert denied(call("Bash", command=command))


def test_bash_records_repo_of_absolute_path_argument(env, repo, tmp_path):
    root, snap = enabled(repo)
    call("Bash", cwd=str(tmp_path), command=f"sed -i '' 's/a/b/' {repo.path}/src/a.py")
    assert tracked(repo)[root]["base"] == snap


def test_deny_checks_run_even_when_recording_fails(env, gate_home, monkeypatch):
    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(gate, "track", broken)
    assert denied(call("Write", file_path=str(gate_home / "allow.json"), content="{}"))


def test_mcp_tool_paths_into_the_gate_are_denied(env, gate_home):
    out = guard.on_pre_tool({"session_id": "s1", "cwd": "/", "tool_name": "mcp__fs__write_file",
                             "tool_input": {"path": str(gate_home / "config.json"), "content": "{}"}})
    assert denied(out)


def test_mcp_tool_paths_record_enabled_repos(env, repo):
    root, snap = enabled(repo)
    guard.on_pre_tool({"session_id": "s1", "cwd": "/", "tool_name": "mcp__fs__write_file",
                       "tool_input": {"path": str(repo.path / "src" / "a.py"), "content": "x"}})
    assert tracked(repo)[root]["base"] == snap


def test_bash_writing_inside_a_protected_cwd_is_denied(env):
    assert denied(call("Bash", cwd=str(env), command="echo 'THRESHOLD = 0' > gate.py"))


def test_settings_env_pointing_the_gate_elsewhere_is_denied(env, tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"env": {}}')
    out = call("Edit", file_path=str(settings), old_string='"env": {}', new_string='"env": {"MUTATION_GATE_HOME": "/tmp/x"}')
    assert denied(out)


def test_write_to_a_fifo_does_not_hang(env, tmp_path):
    fifo = tmp_path / "pipe.py"
    os.mkfifo(fifo)
    result = []
    t = threading.Thread(target=lambda: result.append(call("Write", file_path=str(fifo), content="x = 1")), daemon=True)
    t.start()
    t.join(3)
    assert not t.is_alive()


def test_many_cd_targets_stay_fast(env, tmp_path):
    command = " && ".join(f"cd /nonexistent/d{i}" for i in range(300))
    start = time.monotonic()
    call("Bash", cwd=str(tmp_path), command=command)
    assert time.monotonic() - start < 3


@pytest.mark.parametrize("command", [
    "mutation-gate test src/pkg",
    "mutation-gate test 2>&1 | tail -40",
    '"/x/plugins/cache/m/mutation-gate/0.1.0/bin/mutation-gate" test src',
    "mutation-gate last",
    "mutation-gate status",
])
def test_claude_may_measure_with_the_cli(env, command):
    assert call("Bash", command=command) is None


@pytest.mark.parametrize("command", [
    '"/x/plugins/cache/m/mutation-gate/0.1.0/bin/mutation-gate" allow abcd1234 x',
    "mutation-gate test src && mutation-gate off",
])
def test_cli_changes_stay_denied_in_any_form(env, command):
    assert denied(call("Bash", command=command))
