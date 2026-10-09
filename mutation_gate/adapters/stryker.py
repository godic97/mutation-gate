"""StrykerJS adapter (vitest runner)."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from .. import store
from ..model import DETECTED, UNDETECTED, AdapterResult, Mutant, run_tree, tail, to_ranges

ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Stryker reads `mutate` entries as globs; Next.js-style paths ([id], (group)) must match literally.
GLOB_CHARS = set("[]()*?{}!+@")


def _glob_escape(rel):
    return "".join(f"[{c}]" if c in GLOB_CHARS else c for c in rel)
STATUS = {"Killed": DETECTED, "Timeout": DETECTED, "Survived": UNDETECTED, "NoCoverage": UNDETECTED}


def _install_hint(repo):
    pkgs = "@stryker-mutator/core @stryker-mutator/vitest-runner"
    if (repo / "pnpm-lock.yaml").exists():
        cmd = f"pnpm add -D {pkgs}"
    elif (repo / "yarn.lock").exists():
        cmd = f"yarn add -D {pkgs}"
    else:
        cmd = f"npm install -D {pkgs}"
    return f"Stryker가 설치되어 있지 않음. 설치: cd {repo} && {cmd}"


def _write_config(repo, work, mutate):
    report = work / "mutation.json"
    config = {
        "mutate": mutate,
        "testRunner": "vitest",
        "plugins": ["@stryker-mutator/vitest-runner"],
        "reporters": ["json"],
        "jsonReporter": {"fileName": str(report)},
        "coverageAnalysis": "perTest",
        "tempDirName": str(work / "stryker-tmp"),
        "cleanTempDir": "always",
        "thresholds": {"high": 80, "low": 60, "break": None},
    }
    path = work / "stryker.config.json"
    path.write_text(json.dumps(config, indent=2))
    return path, report


def _snippet(source_lines, loc):
    start, end = loc["start"], loc["end"]
    line = source_lines[start["line"] - 1] if start["line"] - 1 < len(source_lines) else ""
    if start["line"] == end["line"]:
        return line[start["column"] - 1 : end["column"] - 1]
    return line[start["column"] - 1 :].rstrip() + " …"


def _parse(report, changed):
    mutants = []
    for rel, info in report.get("files", {}).items():
        lines = changed.get(rel)
        if not lines:
            continue
        source_lines = info.get("source", "").splitlines()
        for raw in info.get("mutants", []):
            status = STATUS.get(raw.get("status"))
            if status is None:
                continue
            loc = raw["location"]
            touched = sorted(n for n in lines if loc["start"]["line"] <= n <= loc["end"]["line"])
            if not touched:
                continue
            mutants.append(
                Mutant(
                    path=rel,
                    line=touched[0],
                    mutator=raw.get("mutatorName", "?"),
                    original=_snippet(source_lines, loc),
                    replacement=raw.get("replacement", ""),
                    status=status,
                )
            )
    return sorted(mutants, key=lambda m: (m.path, m.line, m.mutator, m.replacement))


def run(repo, changed, budget):
    # vitest's related-file lookup compares resolved paths; a symlinked cwd finds no tests.
    repo = Path(repo).resolve()
    binary = repo / "node_modules" / ".bin" / "stryker"
    if not binary.exists():
        return AdapterResult(error=_install_hint(repo), cacheable=False)

    work = store.work_dir(hashlib.sha1(str(repo).encode()).hexdigest()[:12] + "-stryker")
    # In the config file, not on the command line: --mutate splits its value on commas.
    mutate = [f"{_glob_escape(rel)}:{a}-{b}" for rel in sorted(changed) for a, b in to_ranges(changed[rel])]
    config, report = _write_config(repo, work, mutate)
    if report.exists():
        report.unlink()
    env = {**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0"}
    try:
        proc = run_tree([str(binary), "run", str(config)], cwd=repo, env=env, timeout=budget)
    except subprocess.TimeoutExpired:
        return AdapterResult(error=f"Stryker가 {budget}초 안에 끝나지 않음 (검사 미완료)")

    output = ANSI.sub("", proc.stdout + proc.stderr)
    if "There were failed tests in the initial test run" in output:
        errors = [l for l in output.splitlines() if "ERROR" in l or "✗" in l or "×" in l]
        return AdapterResult(failure="테스트가 현재 실패함 (Stryker initial test run):\n" + tail("\n".join(errors), 800))
    if "No tests were executed" in output:
        return AdapterResult(failure="변경된 코드를 import하는 테스트가 하나도 없음 (Stryker: No tests were executed)")
    if proc.returncode != 0 or not report.exists():
        return AdapterResult(error=f"Stryker 실행 실패 (exit {proc.returncode}):\n{tail(output)}")
    return AdapterResult(mutants=_parse(json.loads(report.read_text()), changed))
