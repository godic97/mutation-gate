"""LLM mode: mutants written by Claude as find/replace edits, run against the project's own tests.

For languages without a mutation tool adapter. Each mutation is applied to the file in place, the
project's test command runs, and the file is restored byte for byte. The original bytes are first
written to a journal outside the repo, so a run killed half-way is undone by `restore_all()`
(called before every run, at session start and at stop, and by `mutation-gate restore`).

The test command comes from the project layout or from the command line, never from the
manifest, so the command that runs is always visible where the user approves it.
"""

import base64
import hashlib
import json
import subprocess
import time
from pathlib import Path

from . import gate, store, tools
from .model import DETECTED, UNDETECTED, Mutant, number_occurrences, run_tree, tail


def detect_test_command(root):
    """The project's usual test command, from files at its root; None when nothing is recognised."""
    r = Path(root)

    def has(name):
        return (r / name).exists()

    if has("Cargo.toml"):
        return "cargo test"
    if has("go.mod"):
        return "go test ./..."
    if has("pom.xml"):
        return "./mvnw -q test" if has("mvnw") else "mvn -q test"
    if has("build.gradle") or has("build.gradle.kts"):
        return "./gradlew test" if has("gradlew") else "gradle test"
    if has("build.sbt"):
        return "sbt test"
    if any(r.glob("*.sln")) or any(r.glob("*.csproj")):
        return "dotnet test"
    if has("composer.json") and has("vendor/bin/phpunit"):
        return "vendor/bin/phpunit"
    if has("Gemfile"):
        return "bundle exec rspec" if has("spec") else "bundle exec rake test"
    try:
        if json.loads((r / "package.json").read_text()).get("scripts", {}).get("test"):
            return "npm test"
    except (OSError, ValueError):
        pass
    if any(has(n) for n in ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini")) or has("tests"):
        venv = next((v for v in (".venv", "venv") if has(f"{v}/bin/python")), None)
        return f"{venv}/bin/python -m pytest -q" if venv else "python3 -m pytest -q"
    if has("CMakeLists.txt"):
        return "ctest --test-dir build --output-on-failure"
    return None


# --- journal ---------------------------------------------------------------------------------


def _journal_dir():
    return store.home() / "state" / "journal"


def _journal_path(path):
    return _journal_dir() / (hashlib.sha1(str(path).encode()).hexdigest()[:16] + ".json")


def _journal_write(root, path, content):
    store._write(_journal_path(path), {
        "root": str(root), "path": str(path),
        "content": base64.b64encode(content).decode(), "sha256": hashlib.sha256(content).hexdigest(),
    })


def _journal_clear(path):
    try:
        _journal_path(path).unlink()
    except FileNotFoundError:
        pass


def restore_all():
    """Put back every file a killed run left mutated. Returns the restored paths."""
    restored = []
    for entry in sorted(_journal_dir().glob("*.json")):
        try:
            data = json.loads(entry.read_text())
            content = base64.b64decode(data["content"])
            if hashlib.sha256(content).hexdigest() != data["sha256"]:
                continue  # a damaged journal must never overwrite the file
            Path(data["path"]).write_bytes(content)
            entry.unlink()
            restored.append(data["path"])
        except (OSError, ValueError, KeyError):
            continue
    return restored


# --- running -----------------------------------------------------------------------------------


def _run_tests(root, command, timeout):
    """'passed', 'failed' or 'timeout', and the output tail."""
    try:
        proc = run_tree(["/bin/sh", "-c", command], cwd=root, env=tools.toolchain_env(), timeout=max(1, timeout))
    except subprocess.TimeoutExpired:
        return "timeout", ""
    return ("passed" if proc.returncode == 0 else "failed"), tail(proc.stdout + proc.stderr, 1200)


def _check(root, mutation):
    """(absolute path, text, line) for a valid mutation, or a reason it cannot run."""
    for key in ("file", "find", "replace"):
        if not isinstance(mutation.get(key), str):
            return None, f"`{key}` is missing"
    root = Path(root).resolve()
    path = (root / mutation["file"]).resolve()
    if root not in path.parents:
        return None, f"{mutation['file']} is outside the repo"
    if not path.is_file():
        return None, f"{mutation['file']} not found"
    text = path.read_text(errors="replace")
    count = text.count(mutation["find"])
    if count == 0:
        return None, "`find` text not found"
    if count > 1:
        return None, f"`find` text occurs {count} times; it must be unique"
    if mutation["find"] == mutation["replace"]:
        return None, "`replace` equals `find`"
    line = text[: text.index(mutation["find"])].count("\n") + 1
    return (path, text, line), None


def _with_mutation(root, path, text, mutation, command, timeout):
    original = path.read_bytes()
    _journal_write(root, path, original)
    try:
        path.write_text(text.replace(mutation["find"], mutation["replace"], 1))
        return _run_tests(root, command, timeout)
    finally:
        path.write_bytes(original)
        _journal_clear(path)


def _first_line(text):
    lines = text.strip().split("\n")
    return lines[0].strip() + (" …" if len(lines) > 1 else "")


def run(root, data, command, budget):
    """Verdict for a manifest of mutations, in the same shape as gate.check()."""
    restore_all()
    cfg = store.load_config()
    deadline = time.monotonic() + budget
    planned, skipped = [], []
    for mutation in data.get("mutations", []):
        checked, reason = _check(root, mutation)
        if checked:
            planned.append((mutation, *checked))
        else:
            skipped.append({"file": str(mutation.get("file")), "find": str(mutation.get("find")), "reason": reason})

    run_info = {"mutants": [], "failures": [], "errors": [], "warnings": [], "ignored": 0, "cacheable": False}
    extra = {}
    command = command or detect_test_command(root)
    if not command:
        run_info["errors"].append("no test command found for this project: pass --test-cmd")
    else:
        started = time.monotonic()
        baseline, output = _run_tests(root, command, deadline - time.monotonic())
        if baseline != "passed":
            run_info["failures"].append(f"the tests are failing before any mutation (`{command}`):\n{output}")
        else:
            per_mutant = max(30.0, 3 * (time.monotonic() - started) + 15)
            for mutation, path, text, line in planned:
                if deadline - time.monotonic() < per_mutant:
                    skipped.append({"file": mutation["file"], "find": mutation["find"], "reason": "time budget used up"})
                    continue
                outcome, _ = _with_mutation(root, path, text, mutation, command, per_mutant)
                m = Mutant(
                    path=str(path.relative_to(Path(root).resolve())), line=line, mutator=mutation.get("symbol") or "llm",
                    original=_first_line(mutation["find"]), replacement=_first_line(mutation["replace"]),
                    status=UNDETECTED if outcome == "passed" else DETECTED,
                    context=f"{mutation['find']}\0{mutation['replace']}",
                )
                run_info["mutants"].append(m)
                extra[id(m)] = {k: mutation[k] for k in ("consequence", "breaksOn") if isinstance(mutation.get(k), str)}
            number_occurrences(run_info["mutants"])
    if skipped:
        run_info["warnings"].append(f"{len(skipped)} mutation(s) skipped — they verified nothing")

    verdict = gate._verdict_dict(run_info, cfg["threshold"], store.allowed_ids(str(root)), executable=bool(planned))
    by_id = {m.id: extra.get(id(m), {}) for m in run_info["mutants"]}
    for s in verdict["survivors"]:
        s.update(by_id.get(s["id"], {}))
    verdict["skipped"] = skipped
    return verdict


def verify(root, spec, command, budget):
    """Triple gate for a new test: passes on clean code, fails on the mutant, kills a sibling."""
    restore_all()
    deadline = time.monotonic() + budget
    command = command or detect_test_command(root)
    if not command:
        return {"verdict": "bad-spec", "reason": "no test command found: pass --test-cmd"}
    checked, reason = _check(root, spec.get("mutation") or {})
    if not checked:
        return {"verdict": "bad-spec", "reason": f"mutation: {reason}"}
    siblings, skipped = [], []
    for sibling in spec.get("siblings") or []:
        ok, why = _check(root, sibling)
        (siblings.append((sibling, *ok)) if ok else skipped.append({"find": str(sibling.get("find")), "reason": why}))
    if not siblings:
        return {"verdict": "bad-spec", "reason": "no usable sibling mutation of the same function", "skipped": skipped}

    result = {"skipped": skipped, "siblings_total": len(siblings), "siblings_killed": 0}
    clean, output = _run_tests(root, command, deadline - time.monotonic())
    result["clean"] = clean
    if clean != "passed":
        return {**result, "verdict": "rejected", "reason": "the test fails on the clean code", "output": output}
    path, text, _ = checked
    outcome, _ = _with_mutation(root, path, text, spec["mutation"], command, deadline - time.monotonic())
    result["mutant"] = "survived" if outcome == "passed" else "killed"
    if outcome == "passed":
        return {**result, "verdict": "rejected", "reason": "the test passes with the mutation in place"}
    for sibling, s_path, s_text, _ in siblings:
        outcome, _ = _with_mutation(root, s_path, s_text, sibling, command, deadline - time.monotonic())
        result["siblings_killed"] += outcome != "passed"
    if not result["siblings_killed"]:
        return {**result, "verdict": "rejected",
                "reason": "the test kills only the mutation it was written for, no sibling: it fits the mutant, not the behaviour"}
    return {**result, "verdict": "accepted"}
