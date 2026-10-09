import pytest

from mutation_gate import nudge


def post(tool, sid="s1", cwd="/p", **tool_input):
    return nudge.on_post_tool({"session_id": sid, "cwd": cwd, "tool_name": tool, "tool_input": tool_input})


def context(out):
    return out["hookSpecificOutput"]["additionalContext"] if out else None


@pytest.mark.parametrize("path", ["/p/tests/test_price.py", "/p/src/price.test.ts", "/p/pkg/price_test.py", "/p/src/__tests__/a.tsx"])
def test_writing_a_test_file_reminds_claude_to_mutation_test(gate_home, path):
    out = post("Write", file_path=path, content="x")
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "mutation-gate test" in context(out)


@pytest.mark.parametrize("command", [
    "pytest -q", ".venv/bin/python -m pytest tests/", "python3 -m pytest", "npx vitest run",
    "pnpm test", "npm run test -- --run", "yarn test", "uv run pytest",
])
def test_running_tests_reminds_claude(gate_home, command):
    assert "mutation-gate test" in context(post("Bash", command=command))


@pytest.mark.parametrize("call", [
    ("Write", {"file_path": "/p/src/price.py", "content": "x"}),
    ("Edit", {"file_path": "/p/README.md", "old_string": "a", "new_string": "b"}),
    ("Bash", {"command": "ls -la"}),
    ("Bash", {"command": "git commit -m 'add tests'"}),
])
def test_other_work_gets_no_reminder(gate_home, call):
    tool, tool_input = call
    assert post(tool, **tool_input) is None


def test_reminds_once_per_prompt(gate_home):
    assert post("Bash", command="pytest") is not None
    assert post("Write", file_path="/p/tests/test_b.py", content="x") is None
    nudge.on_prompt({"session_id": "s1"})
    assert post("Bash", command="pytest") is not None


def test_running_mutation_gate_test_ends_the_reminders(gate_home):
    assert post("Bash", command="mutation-gate test src/price.py") is None
    assert post("Bash", command="pytest") is None


def test_sessions_are_independent(gate_home):
    assert post("Bash", command="pytest", sid="a") is not None
    assert post("Bash", command="pytest", sid="b") is not None


@pytest.mark.parametrize("command", ["cargo test", "go test ./...", "mvn test", "./gradlew test", "dotnet test", "sbt test"])
def test_running_tests_in_other_languages_reminds_claude(gate_home, command):
    assert "mutation-gate test" in context(post("Bash", command=command))
