"""Config, allowlist and per-session state under MUTATION_GATE_HOME."""

import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

DEFAULTS = {"threshold": 80, "max_blocks": 3, "budget_seconds": 480, "disabled": []}


def home():
    return Path(os.environ.get("MUTATION_GATE_HOME", Path.home() / ".config" / "mutation-gate"))


def work_dir(name):
    path = home() / "state" / "work" / name
    path.mkdir(parents=True, exist_ok=True)
    # Tools run inside sandboxes here; a symlinked path breaks vitest's related-file lookup.
    return path.resolve()


class CorruptFile(Exception):
    pass


def _read(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _read_strict(path, default):
    """Like _read, but a file that exists and does not parse is an error, never silently replaced."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except ValueError as exc:
        raise CorruptFile(f"{path} 파싱 실패 ({exc}). 직접 고친 뒤 다시 실행하라.") from exc


class repo_lock:
    """Exclusive per-repo lock so two sessions never run mutation tools on one repo at once."""

    def __init__(self, repo, blocking=True):
        name = hashlib.sha1(str(repo).encode()).hexdigest()[:16]
        self.path = home() / "state" / "locks" / f"{name}.lock"
        self.blocking = blocking
        self.fd = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            return False
        return True

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_config():
    return {**DEFAULTS, **_read(home() / "config.json", {})}


def update_config(**changes):
    cfg = load_config()
    cfg.update(changes)
    _write(home() / "config.json", cfg)
    return cfg


def is_enabled(project):
    return project not in load_config()["disabled"]


def set_enabled(project, enabled):
    disabled = [p for p in load_config()["disabled"] if p != project]
    if not enabled:
        disabled.append(project)
    update_config(disabled=sorted(disabled))


def _allow_path():
    return home() / "allow.json"


def allowlist(project):
    return _read_strict(_allow_path(), {}).get(project, {})


def allowed_ids(project):
    return set(allowlist(project))


def allow(project, mutant_id, reason):
    data = _read_strict(_allow_path(), {})
    data.setdefault(project, {})[mutant_id] = {"reason": reason, "at": time.strftime("%Y-%m-%d %H:%M")}
    _write(_allow_path(), data)


def disallow(project, mutant_id):
    data = _read_strict(_allow_path(), {})
    data.get(project, {}).pop(mutant_id, None)
    _write(_allow_path(), data)


def _session_path(session_id):
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "unknown"
    return home() / "state" / "sessions" / f"{safe}.json"


def load_session(session_id):
    data = _read(_session_path(session_id), {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("repos", {})
    data.setdefault("blocks", 0)
    data.setdefault("verdicts", {})
    return data


def _save_session(session_id, data):
    _write(_session_path(session_id), data)


def set_session_value(session_id, key, value):
    data = load_session(session_id)
    data[key] = value
    _save_session(session_id, data)


def remember_repo(session_id, repo, base):
    data = load_session(session_id)
    if repo not in data["repos"]:
        data["repos"][repo] = base
        _save_session(session_id, data)


def bump_blocks(session_id):
    data = load_session(session_id)
    data["blocks"] += 1
    _save_session(session_id, data)
    return data["blocks"]


def reset_blocks(session_id):
    data = load_session(session_id)
    if data["blocks"]:
        data["blocks"] = 0
        _save_session(session_id, data)


def cached_verdict(session_id, repo, fp):
    entry = load_session(session_id)["verdicts"].get(repo)
    if entry and entry.get("fingerprint") == fp:
        return entry["verdict"]
    return None


def save_verdict(session_id, repo, fp, verdict):
    data = load_session(session_id)
    data["verdicts"][repo] = {"fingerprint": fp, "verdict": verdict}
    _save_session(session_id, data)
