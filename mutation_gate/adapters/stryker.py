"""StrykerJS adapter (vitest runner)."""

import hashlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from .. import diff, store
from ..model import DETECTED, UNDETECTED, AdapterResult, Mutant, number_occurrences, run_tree, tail

ANSI = re.compile(r"\x1b\[[0-9;]*m")
STATUS = {"Killed": DETECTED, "Timeout": DETECTED, "Survived": UNDETECTED, "NoCoverage": UNDETECTED}
INSTRUMENTED = re.compile(r"Instrumented \d+ source file\(s\) with (\d+) mutant")
VITEST_CONFIGS = [f"{kind}.config.{ext}" for kind in ("vitest", "vite") for ext in ("ts", "mts", "cts", "js", "mjs", "cjs")]
# Stryker reads `mutate` entries as globs; Next.js-style paths ([id], (group)) must match literally.
GLOB_CHARS = set("[]()*?{}!+@")


def _glob_escape(rel):
    return "".join(f"[{c}]" if c in GLOB_CHARS else c for c in rel)


def _install_hint(project):
    pkgs = "@stryker-mutator/core @stryker-mutator/vitest-runner"
    if (project / "pnpm-lock.yaml").exists():
        cmd = f"pnpm add -D {pkgs}"
    elif (project / "yarn.lock").exists():
        cmd = f"yarn add -D {pkgs}"
    else:
        cmd = f"npm install -D {pkgs}"
    return f"Stryker is not installed. Install: cd {shlex.quote(str(project))} && {cmd}"


def _project_dir(repo, rel):
    """Nearest directory (inside the repo) that holds the vitest config the file's tests use."""
    path = (repo / rel).parent
    while True:
        if any((path / name).exists() for name in VITEST_CONFIGS):
            return path
        if path == repo or repo not in path.parents:
            return repo
        path = path.parent


def _vitest_config(project):
    return next((project / n for n in VITEST_CONFIGS if (project / n).exists()), None)


def _binary(repo, project):
    for base in (project, repo):
        binary = base / "node_modules" / ".bin" / "stryker"
        if binary.exists():
            return binary
    return None


def _tracked(repo, path):
    rel = os.path.relpath(path, repo)
    return diff._git(repo, "ls-files", "--error-unmatch", "--", f":(literal){rel}", check=False).returncode == 0


def _write_config(work, mutate, project, related):
    report = work / "mutation.json"
    vitest = {"related": related}
    config_file = _vitest_config(project)
    if config_file:
        # Relative, so Stryker loads the sandbox copy: an absolute path would make aliases built
        # from __dirname point back at the unmutated sources.
        vitest["configFile"] = config_file.name
    config = {
        "mutate": mutate,
        "testRunner": "vitest",
        "plugins": ["@stryker-mutator/vitest-runner"],
        "vitest": vitest,
        "reporters": ["json"],
        "jsonReporter": {"fileName": str(report)},
        "coverageAnalysis": "perTest",
        # Stryker's default place: inside the project, so Node finds node_modules above the sandbox
        # (hoisted pnpm/npm workspaces). Stryker deletes it after every run.
        "tempDirName": ".stryker-tmp",
        "cleanTempDir": "always",
        "thresholds": {"high": 80, "low": 60, "break": None},
    }
    path = work / "stryker.config.json"
    path.write_text(json.dumps(config, indent=2))
    return path, report


def _span_text(source_lines, loc):
    start, end = loc["start"], loc["end"]
    rows = source_lines[start["line"] - 1 : end["line"]]
    if not rows:
        return ""
    if len(rows) == 1:
        return rows[0][start["column"] - 1 : end["column"] - 1]
    return "\n".join([rows[0][start["column"] - 1 :], *rows[1:-1], rows[-1][: end["column"] - 1]])


def _parse(report, changed, prefix):
    """Mutants that overlap changed lines, plus the count of Ignored ones there."""
    mutants, ignored = [], 0
    for rel_in_project, info in report.get("files", {}).items():
        rel = str(Path(prefix) / rel_in_project) if prefix else rel_in_project
        lines = changed.get(rel)
        if not lines:
            continue
        source_lines = info.get("source", "").split("\n")
        for raw in info.get("mutants", []):
            loc = raw["location"]
            touched = sorted(n for n in lines if loc["start"]["line"] <= n <= loc["end"]["line"])
            if not touched:
                continue
            if raw.get("status") == "Ignored":
                ignored += 1
                continue
            status = STATUS.get(raw.get("status"))
            if status is None:  # CompileError / RuntimeError: invalid mutants, as Stryker scores them
                continue
            full = _span_text(source_lines, loc)
            multiline = "\n" in full
            mutants.append(Mutant(
                path=rel, line=touched[0], mutator=raw.get("mutatorName", "?"),
                original=full.split("\n")[0].rstrip() + (" …" if multiline else ""),
                replacement=raw.get("replacement", "").split("\n")[0], status=status,
                context=full if multiline else "",
            ))
    return mutants, ignored


def _run_once(binary, project, work, mutate, related, budget):
    config, report = _write_config(work, mutate, project, related)
    if report.exists():
        report.unlink()
    env = {**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0"}
    proc = run_tree([str(binary), "run", str(config)], cwd=project, env=env, timeout=budget)
    return ANSI.sub("", proc.stdout + proc.stderr), proc.returncode, report


def _run_project(repo, project, changed, budget):
    binary = _binary(repo, project)
    if binary is None:
        return AdapterResult(error=_install_hint(project), error_kind="missing")
    if _tracked(repo, binary):
        return AdapterResult(error=f"not running {binary}: git tracks it, so the repo may have put it there", error_kind="missing")
    if not (project / "node_modules" / "vitest").exists() and not (repo / "node_modules" / "vitest").exists():
        return AdapterResult(error=f"no vitest in {project}; only vitest projects are supported", error_kind="missing")

    prefix = os.path.relpath(project, repo) if project != repo else ""
    # Whole files, not changed lines: Stryker only mutates nodes that lie entirely inside a range,
    # so a change inside a multi-line expression would get no mutants. _parse keeps the overlap.
    mutate = [_glob_escape(os.path.relpath(repo / rel, project)) for rel in sorted(changed)]
    work = store.work_dir(hashlib.sha1(str(project).encode()).hexdigest()[:12] + "-stryker")
    output, code, report = _run_once(binary, project, work, mutate, True, budget)

    match = INSTRUMENTED.search(output)
    if match and int(match.group(1)) == 0:
        return AdapterResult()
    if "There were failed tests in the initial test run" in output:
        errors = [l for l in output.splitlines() if "ERROR" in l or "✗" in l or "×" in l]
        return AdapterResult(failure="the tests are failing (Stryker initial test run):\n" + tail("\n".join(errors), 800))
    if "No tests were executed" in output and "failed to find test files related" in output:
        # No test imports the changed files: run every test, so their mutants show up as NoCoverage.
        output, code, report = _run_once(binary, project, work, mutate, False, budget)
    if "No tests were executed" in output:
        return AdapterResult(failure="no test runs in this project (Stryker: No tests were executed)")
    if code != 0 or not report.exists():
        return AdapterResult(error=f"Stryker failed (exit {code}):\n{tail(output)}", error_kind="crash")
    data = json.loads(report.read_text())
    report.unlink()
    files = {str(Path(prefix) / f) if prefix else f for f in data.get("files", {})}
    missing = sorted(set(changed) - files)
    if missing and match and int(match.group(1)) > 0:
        return AdapterResult(error=f"Stryker could not find the changed files: {', '.join(missing)}", error_kind="crash")
    mutants, ignored = _parse(data, changed, prefix)
    return AdapterResult(mutants=mutants, ignored=ignored)


def run(repo, changed, budget):
    # vitest's related-file lookup compares resolved paths; a symlinked cwd finds no tests.
    repo = Path(repo).resolve()
    groups = {}
    for rel, lines in changed.items():
        groups.setdefault(_project_dir(repo, rel), {})[rel] = lines
    merged = AdapterResult()
    for project in sorted(groups):
        try:
            result = _run_project(repo, project, groups[project], budget)
        except subprocess.TimeoutExpired:
            result = AdapterResult(error=f"Stryker did not finish within {budget}s (incomplete run)", error_kind="timeout")
        merged.mutants += result.mutants
        merged.ignored += result.ignored
        merged.failure = merged.failure or result.failure
        if result.error and not merged.error:
            merged.error, merged.error_kind = result.error, result.error_kind
    number_occurrences(merged.mutants)
    merged.mutants.sort(key=lambda m: (m.path, m.line, m.mutator, m.replacement))
    return merged
