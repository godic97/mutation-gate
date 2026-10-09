"""PreToolUse guard: record touched repos and deny edits that would weaken the gate."""

import os
import re
from pathlib import Path

from . import diff, store

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
NAME = "mutation-gate"
SETTINGS_NAME = re.compile(r"^settings(\.local)?\.json$")
# Settings keys that switch off every hook, the gate's included.
HOOK_KILLERS = re.compile(r"disableAllHooks|allowManagedHooksOnly")

BASH_SETTINGS = re.compile(r"(?<![\w.-])settings(\.local)?\.json")
BASH_CONFIG = re.compile(r"pyproject\.toml|setup\.cfg|mutmut\.toml")
# Redirections that write nowhere: 2>/dev/null, >&2, 2>&1.
HARMLESS_REDIRECTS = re.compile(r"\d*>&\d+|\d*>>?\s*/dev/null")
BASH_WRITES = re.compile(
    r">|<<|\b(tee|sed|mv|cp|rm|trash|ln|dd|install|rsync|truncate|touch|chmod)\b"
    r"|\b(python[\d.]*|node|perl|ruby)\s+(-\w*[ce]\b|-\s)"
)
# Ways a shell command can point at the gate's own files.
BASH_GATE_PATHS = re.compile(r"CLAUDE_PLUGIN_ROOT|CLAUDE_PLUGIN_DATA|MUTATION_GATE_HOME|mutation[-_]gate")
BASH_CLI = re.compile(r"bin/mutation-gate|(?:^|[\s;&|(])mutation-gate\s+(allow|disallow|on|off|threshold|hook)\b")
BASH_PLUGIN_CMD = re.compile(r"\bclaude\s+plugins?\s+(disable|uninstall|remove|marketplace\s+remove)\b")
# Directories a Bash command works in besides its cwd: `cd dir && …`, `git -C dir …`, absolute paths.
BASH_DIRS = re.compile(r"(?:^|&&|;|\|\||\()\s*cd\s+([^\s;&|)]+)|\bgit\s+-C\s+([^\s;&|)]+)")
BASH_PATHS = re.compile(r"(?<![\w$=])(~?/[^\s'\";|&<>()`]+)")
MAX_BASH_PATHS = 20


def _deny(reason):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"mutation-gate: {reason}",
        }
    }


def _resolve(path):
    return Path(os.path.expanduser(path)).resolve(strict=False)


def _protected(path):
    p = _resolve(path)
    if NAME in p.parts:
        return True
    roots = [os.environ.get("CLAUDE_PLUGIN_ROOT"), os.environ.get("CLAUDE_PLUGIN_DATA"), str(store.home())]
    return any(r and (p == _resolve(r) or _resolve(r) in p.parents) for r in roots)


def _existing_dir(path):
    while not path.is_dir() and path != path.parent:
        path = path.parent
    return path


def _remember(session_id, start):
    root = diff.repo_root(_existing_dir(_resolve(start)))
    if root:
        store.remember_repo(session_id, root, diff.head_sha(root))


def _repo_relative(p):
    """Path relative to its repo, so classify() never sees ancestors such as ~/tests or ~/build."""
    root = diff.repo_root(_existing_dir(p.parent))
    if root and Path(root) in p.parents:
        return str(p.relative_to(root))
    return p.name


def _pairs(tool, tool_input, path):
    """(old text, new text) pairs an edit tool call would apply."""
    if tool == "Edit":
        return [(tool_input.get("old_string", ""), tool_input.get("new_string", ""))]
    if tool == "MultiEdit":
        return [(e.get("old_string", ""), e.get("new_string", "")) for e in tool_input.get("edits", [])]
    if tool == "Write":
        try:
            old = _resolve(path).read_text()
        except (OSError, UnicodeDecodeError):
            old = ""
        return [(old, tool_input.get("content", ""))]
    return [("", tool_input.get("new_source", ""))]


def _adds(rx, old, new):
    return len(rx.findall(new)) > len(rx.findall(old))


def _check_edit(tool, tool_input):
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not path:
        return None
    if _protected(path):
        return _deny("게이트 자신의 파일·설정·예외 목록은 수정할 수 없음. 바꿔야 하면 Max에게 요청하라.")
    pairs = _pairs(tool, tool_input, path)
    p = _resolve(path)
    if SETTINGS_NAME.match(p.name) and ".claude" in p.parts:
        if any(NAME in old or NAME in new or _adds(HOOK_KILLERS, old, new) for old, new in pairs):
            return _deny("settings에서 mutation-gate 플러그인이나 hook 전체를 끄는 설정은 바꿀 수 없음. Max만 바꿀 수 있다.")
    rel = _repo_relative(p)
    for old, new in pairs:
        if diff.classify(rel) and any(_adds(rx, old, new) for rx in diff.INLINE_SUPPRESSIONS):
            return _deny("mutant 억제 주석(Stryker disable, pragma: no mutate)은 금지. 테스트를 보강하라.")
        if p.name in diff.CONFIG_FILES and _adds(diff.CONFIG_SUPPRESSIONS, old, new):
            return _deny("mutation 대상 범위를 줄이는 config 변경은 금지. 테스트를 보강하라.")
    return None


def _check_bash(command):
    if BASH_CLI.search(command):
        return _deny("mutation-gate CLI(allow·off·threshold 등)는 Max만 실행한다. status·last는 허용.")
    if BASH_PLUGIN_CMD.search(command):
        return _deny("플러그인 비활성화·삭제는 Max만 한다.")
    writes = BASH_WRITES.search(HARMLESS_REDIRECTS.sub("", command))
    if writes and BASH_GATE_PATHS.search(command):
        return _deny("게이트 자신의 파일·설정·예외 목록은 Bash로 수정할 수 없음. 바꿔야 하면 Max에게 요청하라.")
    if writes and any(rx.search(command) for rx in diff.INLINE_SUPPRESSIONS):
        return _deny("mutant 억제 주석(Stryker disable, pragma: no mutate)은 금지. 테스트를 보강하라.")
    if writes and BASH_SETTINGS.search(command):
        return _deny("Claude Code settings 파일은 Bash로 수정할 수 없음. Edit 도구를 쓰라.")
    if BASH_CONFIG.search(command) and diff.CONFIG_SUPPRESSIONS.search(command):
        return _deny("mutation 대상 범위를 줄이는 config 변경은 금지. 테스트를 보강하라.")
    return None


def on_pre_tool(payload):
    sid = payload.get("session_id", "unknown")
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if tool in EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if path:
            _remember(sid, path)
        return _check_edit(tool, tool_input)
    if tool == "Bash":
        command = tool_input.get("command", "")
        cwd = payload.get("cwd") or "."
        dirs = [a or b for a, b in BASH_DIRS.findall(command)]
        seen = set()
        for target in [cwd] + [d.strip("'\"") for d in dirs] + BASH_PATHS.findall(command)[:MAX_BASH_PATHS]:
            parent = _existing_dir(_resolve(os.path.join(cwd, os.path.expanduser(target))))
            if parent not in seen:
                seen.add(parent)
                _remember(sid, parent)
        return _check_bash(command)
    return None
