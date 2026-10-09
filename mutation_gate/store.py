"""Config, allowlist and per-session state under MUTATION_GATE_HOME."""

import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

DEFAULTS = {"threshold": 80, "max_blocks": 3, "budget_seconds": 480, "enabled": []}
# The Stop hook times out at 600 s; keep the budget well inside it.
BUDGET_RANGE = (60, 540)


def home():
    return Path(os.environ.get("MUTATION_GATE_HOME", Path.home() / ".config" / "mutation-gate"))


def _private_dir(path):
    """mkdir -p with 0700 on every level under home(): state holds copies of source code."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    base = home()
    for p in [path, *path.parents]:
        if p != base and base not in p.parents:
            break
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass
    return path


def work_dir(name):
    path = _private_dir(home() / "state" / "work" / name)
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


class _flock:
    def __init__(self, path):
        self.path = path
        self.fd = None

    def acquire(self, timeout=None):
        """Block until locked, or give up after `timeout` seconds and return False."""
        _private_dir(self.path.parent)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | (0 if deadline is None else fcntl.LOCK_NB))
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    return False
                time.sleep(0.1)

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


def repo_lock(repo):
    """Exclusive per-repo lock so two sessions never run mutation tools on one repo at once."""
    name = hashlib.sha1(str(repo).encode()).hexdigest()[:16]
    return _flock(home() / "state" / "locks" / f"{name}.lock")


def _write(path, data):
    _private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _int_in(value, lo, hi, default):
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if lo <= value <= hi else default


def load_config():
    raw = _read(home() / "config.json", {})
    cfg = {**DEFAULTS, **(raw if isinstance(raw, dict) else {})}
    cfg["threshold"] = _int_in(cfg["threshold"], 0, 100, DEFAULTS["threshold"])
    cfg["max_blocks"] = _int_in(cfg["max_blocks"], 1, 100, DEFAULTS["max_blocks"])
    budget = cfg["budget_seconds"]
    if isinstance(budget, bool) or not isinstance(budget, int):
        budget = DEFAULTS["budget_seconds"]
    cfg["budget_seconds"] = min(max(budget, BUDGET_RANGE[0]), BUDGET_RANGE[1])
    if not isinstance(cfg["enabled"], list):
        cfg["enabled"] = []
    return cfg


def update_config(**changes):
    cfg = load_config()
    cfg.update(changes)
    _write(home() / "config.json", cfg)
    return cfg


def is_enabled(project):
    return project in load_config()["enabled"]


def set_enabled(project, enabled):
    projects = [p for p in load_config()["enabled"] if p != project]
    if enabled:
        projects.append(project)
    update_config(enabled=sorted(projects))


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


def _mutate_session(session_id, fn):
    """Load, change and save one session file under a lock: hooks of one session run concurrently."""
    path = _session_path(session_id)
    with _flock(path.with_suffix(".lock")):
        data = load_session(session_id)
        result = fn(data)
        _write(path, data)
    return result


def set_session_value(session_id, key, value):
    _mutate_session(session_id, lambda d: d.__setitem__(key, value))


def remember_repo(session_id, repo, base):
    _mutate_session(session_id, lambda d: d["repos"].setdefault(repo, base))


def bump_blocks(session_id):
    def bump(d):
        d["blocks"] += 1
        return d["blocks"]
    return _mutate_session(session_id, bump)


def reset_blocks(session_id):
    if load_session(session_id)["blocks"]:
        _mutate_session(session_id, lambda d: d.__setitem__("blocks", 0))


def cached_verdict(session_id, repo, fp):
    entry = load_session(session_id)["verdicts"].get(repo)
    if entry and entry.get("fingerprint") == fp:
        return entry["verdict"]
    return None


def save_verdict(session_id, repo, fp, verdict):
    _mutate_session(session_id, lambda d: d["verdicts"].__setitem__(repo, {"fingerprint": fp, "verdict": verdict}))


def prune_sessions(days=14):
    cutoff = time.time() - days * 86400
    for path in (home() / "state" / "sessions").glob("*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def update_repo(session_id, repo, **fields):
    def apply(d):
        entry = d["repos"].get(repo)
        if isinstance(entry, dict):
            entry.update(fields)
    _mutate_session(session_id, apply)
