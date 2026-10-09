import os

from mutation_gate import tools


def test_find_prefers_path_then_extra_dirs(tmp_path, monkeypatch):
    on_path = tmp_path / "on-path"
    extra = tmp_path / "extra"
    on_path.mkdir()
    extra.mkdir()
    for d in (on_path, extra):
        exe = d / "mytool"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(on_path))
    assert tools.find("mytool", [str(extra)]) == str(on_path / "mytool")
    monkeypatch.setenv("PATH", "/nonexistent")
    assert tools.find("mytool", [str(extra)]) == str(extra / "mytool")
    assert tools.find("nope", [str(extra)]) is None


def test_find_expands_home_and_globs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/nonexistent")
    exe = tmp_path / ".local" / "opt" / "thing-1.2" / "bin" / "thing"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert tools.find("thing", ["~/.local/opt/thing-*/bin"]) == str(exe)


def test_env_with_puts_tool_dirs_first_on_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    env = tools.env_with([str(tmp_path / "bin")], JAVA_HOME="/jdk")
    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / "bin")
    assert env["JAVA_HOME"] == "/jdk" and env["NO_COLOR"] == "1"


def test_java_home_found_under_local_opt(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JAVA_HOME", raising=False)
    home = tmp_path / ".local" / "opt" / "jdk-21.0.1+1" / "Contents" / "Home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "java").write_text("")
    assert tools.java_home() == str(home)
