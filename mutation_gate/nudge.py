"""PostToolUse: remind Claude to mutation-test after it writes or runs tests.

The mutation-test skill depends on Claude choosing it, and a request like "write tests for X" often
does not trigger it. This hook does not depend on wording: when Claude edits a test file or runs a
test command, it adds a one-time note to Claude's context for the current prompt.
"""

import os
import re

from . import diff, store

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
TEST_COMMAND = re.compile(
    r"(^|[\s;&|(/])(pytest|py\.test|vitest|jest)\b"
    r"|\b(npm|pnpm|yarn|bun)\s+(run\s+)?test\b"
    r"|\bpython[\d.]*\s+-m\s+pytest\b"
)
MUTATION_TEST = re.compile(r"\bmutation-gate[\"']?\s+test\b")
REMINDER = (
    "mutation-gate: tests were just written or run. A passing suite does not show that the tests catch bugs. "
    "Before you finish, mutation-test the code under test (the source files, not the test files): run "
    "`mutation-gate test <source files>` as a command of its own (no pipes or `&&`) with a Bash timeout of 600000, "
    "then add assertions until it prints PASS, "
    "as the mutation-test skill describes. Quote its final summary line. Skip this only if no JS/TS or Python "
    "source is under test."
)


def _relative(path, cwd):
    absolute = os.path.join(cwd, os.path.expanduser(path))
    root = diff.repo_root(os.path.dirname(absolute)) if os.path.isdir(os.path.dirname(absolute)) else None
    return os.path.relpath(absolute, root) if root else absolute


def _touches_tests(tool, tool_input, cwd):
    if tool in EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        return isinstance(path, str) and diff.is_test(_relative(path, cwd))
    if tool == "Bash":
        return bool(TEST_COMMAND.search(str(tool_input.get("command") or "")))
    return False


def on_post_tool(payload):
    sid = payload.get("session_id") or "unknown"
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Bash" and MUTATION_TEST.search(str(tool_input.get("command") or "")):
        store.set_session_value(sid, "mutation_tested", True)
        return None
    session = store.load_session(sid)
    if session.get("nudged") or session.get("mutation_tested"):
        return None
    if not _touches_tests(tool, tool_input, payload.get("cwd") or "."):
        return None
    store.set_session_value(sid, "nudged", True)
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": REMINDER}}


def on_prompt(payload):
    sid = payload.get("session_id") or "unknown"
    session = store.load_session(sid)
    if session.get("nudged") or session.get("mutation_tested"):
        store.set_session_value(sid, "nudged", False)
        store.set_session_value(sid, "mutation_tested", False)
