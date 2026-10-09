"""PreToolUse: remember which enabled repos Claude touches, so the Stop hook knows what to test."""

import os
import re
from pathlib import Path

from . import diff, gate

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Directories a Bash command works in besides its cwd: `cd dir && …`, `git -C dir …`, absolute paths.
BASH_DIRS = re.compile(r"(?:^|&&|;|\|\||\()\s*cd\s+([^\s;&|)]+)|\bgit\s+-C\s+([^\s;&|)]+)")
BASH_PATHS = re.compile(r"(?<![\w$=])(~?/[^\s'\";|&<>()`]+)")
MAX_BASH_DIRS = 20


def _resolve(path):
    return Path(os.path.expanduser(path)).resolve(strict=False)


def _existing_dir(path):
    while not path.is_dir() and path != path.parent:
        path = path.parent
    return path


def _remember(session_id, start):
    """Snapshot the repo containing `start` if it is enabled. Never raises: a hook must not get in the way."""
    try:
        root = diff.repo_root(_existing_dir(_resolve(start)))
        if root:
            gate.track(session_id, root)
    except Exception:
        pass


def _bash_dirs(cwd, command):
    dirs = [a or b for a, b in BASH_DIRS.findall(command)]
    found = []
    for target in [cwd] + [d.strip("'\"") for d in dirs] + BASH_PATHS.findall(command):
        parent = _existing_dir(_resolve(os.path.join(cwd, os.path.expanduser(target))))
        if parent not in found:
            found.append(parent)
        if len(found) >= MAX_BASH_DIRS:
            break
    return found


def on_pre_tool(payload):
    sid = payload.get("session_id") or "unknown"
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool in EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if isinstance(path, str) and path:
            _remember(sid, path)
    elif tool == "Bash":
        for d in _bash_dirs(payload.get("cwd") or ".", str(tool_input.get("command") or "")):
            _remember(sid, d)
    return None
