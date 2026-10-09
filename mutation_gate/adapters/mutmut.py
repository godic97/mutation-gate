"""mutmut adapter (pytest)."""

import ast
import fnmatch
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..model import DETECTED, UNDETECTED, AdapterResult, Mutant, run_tree, tail

MARKER = ".mutation-gate"
STATUS = {
    "killed": DETECTED,
    "timeout": DETECTED,
    "caught by type check": DETECTED,
    "segfault": DETECTED,
    "survived": UNDETECTED,
    "no tests": UNDETECTED,
    "suspicious": UNDETECTED,
}
# Statuses that say nothing about test quality; anything else unknown counts as undetected.
NEUTRAL = {"skipped"}
# Seconds kept back from the budget for `results` and `show` after the run.
REPORT_RESERVE = 60
RESULT_LINE = re.compile(r"^\s+(\S+): (.+)$")
HUNK = re.compile(r"^@@ -(\d+)")


def module_name(rel):
    dotted = rel[: -len(".py")].replace("/", ".")
    return dotted[len("src.") :] if dotted.startswith("src.") else dotted


def _span(node):
    starts = [d.lineno for d in node.decorator_list] + [node.lineno]
    return min(starts), node.end_lineno


def _functions(source):
    """(mangled name, first line, last line) for each function mutmut mutates."""
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.parse(source).body:
        if isinstance(node, funcs):
            yield (f"x_{node.name}", *_span(node))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, funcs):
                    yield (f"xǁ{node.name}ǁ{item.name}", *_span(item))


def _touched(source, lines):
    return [(name, lo) for name, lo, hi in _functions(source) if any(lo <= n <= hi for n in lines)]


def touched_functions(source, lines):
    """mutmut's mangled names for top-level functions and methods that contain `lines`."""
    return [name for name, _ in _touched(source, lines)]


def _tools(repo):
    for venv in (".venv", "venv"):
        binary = repo / venv / "bin" / "mutmut"
        if binary.exists():
            return binary
    return None


def _install_hint(repo):
    python = next((repo / v / "bin" / "python" for v in (".venv", "venv") if (repo / v / "bin" / "python").exists()), None)
    if python:
        return f"mutmut가 프로젝트 venv에 없음. 설치: uv pip install --python {python} mutmut"
    return f"mutmut를 찾을 수 없음 ({repo}/.venv 없음). 프로젝트 venv를 만들고 install: uv venv && uv pip install pytest mutmut"


def _parse_show(text):
    """(line number within the function, original text, replacement text) from `mutmut show` output."""
    orig_no = None
    original = replacement = None
    line_no = 0
    for line in text.split("\n"):
        match = HUNK.match(line)
        if match:
            line_no = int(match.group(1))
            continue
        if line_no == 0:  # header lines before the first hunk
            continue
        if line.startswith("-"):
            if original is None:
                orig_no, original = line_no, line[1:]
            line_no += 1
        elif line.startswith("+"):
            if replacement is None:
                replacement = line[1:]
        else:
            line_no += 1
    return orig_no, original, replacement


def _clear(work):
    if work.exists():
        shutil.rmtree(work)


def run(repo, changed, budget):
    repo = Path(repo).resolve()
    binary = _tools(repo)
    if binary is None:
        return AdapterResult(error=_install_hint(repo), cacheable=False)
    deadline = time.monotonic() + budget

    # `mutmut show` numbers diff lines from the function's first line, so keep each start line.
    origin = {}
    for rel in sorted(changed):
        try:
            funcs = _touched((repo / rel).read_text(), changed[rel])
        except (OSError, SyntaxError) as exc:
            return AdapterResult(failure=f"{rel}를 파싱할 수 없음: {exc}")
        for name, lo in funcs:
            origin[f"{module_name(rel)}.{name}"] = (rel, lo)
    patterns = [f"{qualified}__mutmut_*" for qualified in origin]
    if not patterns:
        return AdapterResult()

    work = repo / "mutants"
    if work.exists() and not (work / MARKER).exists():
        return AdapterResult(error=f"{work}/ 가 이미 있음 (mutation-gate가 만든 것이 아님). mutants/ 를 옮긴 뒤 다시 시도")
    _clear(work)
    work.mkdir()
    (work / MARKER).write_text("created by mutation-gate; removed after each run\n")
    env = {**os.environ, "NO_COLOR": "1"}

    def mm(*args, timeout=None):
        left = max(1, int(deadline - time.monotonic()))
        return run_tree([str(binary), *args], cwd=repo, env=env, timeout=min(timeout or left, left))

    try:
        proc = mm("run", *patterns, timeout=max(1, budget - min(REPORT_RESERVE, budget // 4)))
        output = proc.stdout + proc.stderr
        if "Filtered for specific mutants, but nothing matches" in output:
            return AdapterResult()
        if "failed to collect stats" in output or "failed to run clean test" in output:
            failed = [l for l in output.splitlines() if l.startswith(("FAILED", "ERROR"))]
            return AdapterResult(failure="테스트가 현재 실패함 (mutmut clean run):\n" + tail("\n".join(failed), 800))
        if proc.returncode != 0:
            return AdapterResult(error=f"mutmut 실행 실패 (exit {proc.returncode}):\n{tail(output)}")

        statuses = {}
        for line in mm("results", "--all", "true").stdout.splitlines():
            match = RESULT_LINE.match(line)
            if match and any(fnmatch.fnmatchcase(match.group(1), p) for p in patterns):
                statuses[match.group(1)] = match.group(2).strip()
        unchecked = [n for n, s in statuses.items() if s == "not checked"]
        if unchecked:
            return AdapterResult(error=f"mutmut가 mutant {len(unchecked)}개를 검사하지 못함 (검사 미완료)")

        names = [n for n, s in statuses.items() if s not in NEUTRAL]
        with ThreadPoolExecutor(max_workers=8) as pool:
            shows = list(pool.map(lambda n: mm("show", n).stdout, names))
    except subprocess.TimeoutExpired:
        return AdapterResult(error=f"mutmut가 {budget}초 안에 끝나지 않음 (검사 미완료)")
    finally:
        _clear(work)

    mutants = []
    for name, text in zip(names, shows):
        offset, original, replacement = _parse_show(text)
        if offset is None:
            continue
        rel, start = origin[name.rsplit("__mutmut_", 1)[0]]
        line_no = start + offset - 1
        if line_no not in changed[rel]:
            continue
        mutants.append(
            Mutant(path=rel, line=line_no, mutator="mutmut", original=original.strip(),
                   replacement=(replacement or "").strip(), status=STATUS.get(statuses[name], UNDETECTED))
        )
    return AdapterResult(mutants=sorted(mutants, key=lambda m: (m.path, m.line, m.replacement)))
