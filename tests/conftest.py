import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class Repo:
    def __init__(self, path: Path):
        self.path = path

    def write(self, rel, text):
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def commit(self, msg="c"):
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "--allow-empty", "-m", msg)
        return git(self.path, "rev-parse", "HEAD")

    def head(self):
        return git(self.path, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "proj"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    git(path, "config", "commit.gpgsign", "false")
    return Repo(path)


@pytest.fixture
def gate_home(tmp_path, monkeypatch):
    home = tmp_path / "gate-home"
    monkeypatch.setenv("MUTATION_GATE_HOME", str(home))
    return home
