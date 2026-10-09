import time

import pytest

from mutation_gate import diff, gate, store, tracker


def call(tool, sid="s1", cwd="/", **tool_input):
    return tracker.on_pre_tool({"session_id": sid, "cwd": cwd, "tool_name": tool, "tool_input": tool_input})


def tracked():
    return store.load_session("s1")["repos"]


def enabled(repo):
    repo.commit()
    store.set_enabled(str(repo.path.resolve()), True)
    return str(repo.path.resolve()), diff.snapshot(repo.path)


def test_edit_records_repo_base_before_change(gate_home, repo):
    root, snap = enabled(repo)
    call("Write", file_path=str(repo.path / "src" / "new.py"), content="x = 1\n")
    assert tracked()[root]["base"] == snap


def test_repos_that_are_not_enabled_are_not_recorded(gate_home, repo):
    repo.commit()
    call("Write", file_path=str(repo.path / "src" / "new.py"), content="x = 1\n")
    call("Bash", cwd=str(repo.path), command="ls")
    assert tracked() == {}


def test_bash_records_repo_of_cwd(gate_home, repo):
    root, snap = enabled(repo)
    call("Bash", cwd=str(repo.path), command="ls")
    assert tracked()[root]["base"] == snap


def test_bash_records_repo_entered_with_cd(gate_home, repo, tmp_path):
    root, snap = enabled(repo)
    call("Bash", cwd=str(tmp_path), command=f"cd {repo.path.name} && sed -i '' 's/a/b/' src/a.py")
    assert tracked()[root]["base"] == snap


def test_bash_records_repo_named_with_git_dash_c(gate_home, repo, tmp_path):
    root, snap = enabled(repo)
    call("Bash", cwd=str(tmp_path), command=f"git -C {repo.path} commit -am x")
    assert tracked()[root]["base"] == snap


def test_bash_records_repo_of_absolute_path_argument(gate_home, repo, tmp_path):
    root, snap = enabled(repo)
    call("Bash", cwd=str(tmp_path), command=f"sed -i '' 's/a/b/' {repo.path}/src/a.py")
    assert tracked()[root]["base"] == snap


@pytest.mark.parametrize("command", [
    "mutation-gate allow abcd1234 x", "echo '// Stryker disable all' >> src/a.ts", "claude plugin disable mutation-gate",
])
def test_never_denies_anything(gate_home, command):
    assert call("Bash", command=command) is None
    assert call("Write", file_path="/p/src/a.ts", content="// Stryker disable all") is None


def test_recording_errors_do_not_escape(gate_home, monkeypatch, repo):
    enabled(repo)
    monkeypatch.setattr(gate, "track", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    assert call("Write", file_path=str(repo.path / "a.py"), content="x") is None


def test_many_cd_targets_stay_fast(gate_home, tmp_path):
    command = " && ".join(f"cd /nonexistent/d{i}" for i in range(300))
    start = time.monotonic()
    call("Bash", cwd=str(tmp_path), command=command)
    assert time.monotonic() - start < 3
