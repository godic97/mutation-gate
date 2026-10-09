"""mutmut adapter (pytest)."""

import ast
import fnmatch
import hashlib
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import diff, store
from ..model import DETECTED, UNDETECTED, AdapterResult, Mutant, number_occurrences, run_tree, tail

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
DEF_LINE = re.compile(r"^\s*(@|def |async def )")
# mutmut 3 mutates a decorated function only when its one decorator is one of these.
ALLOWED_DECORATORS = {"staticmethod", "classmethod"}


def module_name(rel):
    dotted = rel[: -len(".py")].replace("/", ".")
    if dotted.startswith("src."):
        dotted = dotted[len("src.") :]
    # mutmut names a package's own functions after the package: pkg/__init__.py -> pkg.
    return dotted[: -len(".__init__")] if dotted.endswith(".__init__") else dotted


def _span(node):
    starts = [d.lineno for d in node.decorator_list] + [node.lineno]
    return min(starts), node.end_lineno


def _mutable(node):
    decorators = [ast.unparse(d) for d in node.decorator_list]
    return not decorators or (len(decorators) == 1 and decorators[0] in ALLOWED_DECORATORS)


def _functions(source):
    """(mangled name, first line, last line, mutable) for top-level functions and their methods."""
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.parse(source).body:
        if isinstance(node, funcs):
            yield (f"x_{node.name}", *_span(node), _mutable(node))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, funcs):
                    yield (f"xǁ{node.name}ǁ{item.name}", *_span(item), _mutable(item))


def _touched(source, lines):
    return [(name, lo, hi, ok) for name, lo, hi, ok in _functions(source) if any(lo <= n <= hi for n in lines)]


def touched_functions(source, lines):
    """mutmut's mangled names for mutable top-level functions and methods that contain `lines`."""
    return [name for name, _, _, ok in _touched(source, lines) if ok]


def _binary(repo):
    for venv in (".venv", "venv"):
        binary = repo / venv / "bin" / "mutmut"
        if binary.exists():
            return binary
    return None


def _install_hint(repo):
    python = next((repo / v / "bin" / "python" for v in (".venv", "venv") if (repo / v / "bin" / "python").exists()), None)
    if python:
        return f"mutmut가 프로젝트 venv에 없음. 설치: uv pip install --python {shlex.quote(str(python))} mutmut"
    return (f"mutmut를 찾을 수 없음 ({repo}/.venv 없음). 설치: cd {shlex.quote(str(repo))} && "
            "uv venv && uv pip install pytest mutmut")


def _tracked(repo, rel):
    return diff._git(repo, "ls-files", "--", f":(literal){rel}", check=False).stdout.strip() != ""


def _parse_show(text):
    """(line number within the shown function, original text, replacement text, anchor line).

    The shown function can start with comments that libcst keeps attached to it, so the anchor is
    the first decorator or def line: the one that maps to the function's start in the file.
    """
    orig_no = anchor = None
    original = replacement = None
    line_no = 0
    for line in text.split("\n"):
        match = HUNK.match(line)
        if match:
            line_no = int(match.group(1))
            continue
        if line_no == 0:  # header lines before the first hunk
            continue
        body = line[1:]
        if anchor is None and not line.startswith("+") and DEF_LINE.match(body):
            anchor = line_no
        if line.startswith("-"):
            if original is None:
                orig_no, original = line_no, body
            line_no += 1
        elif line.startswith("+"):
            if replacement is None:
                replacement = body
        else:
            line_no += 1
    return orig_no, original, replacement, anchor or 1


def _locate(source_lines, lo, hi, offset, anchor, original):
    """File line of a mutated line; checks the text and falls back to a unique match in the function."""
    guess = lo + offset - anchor
    if 1 <= guess <= len(source_lines) and source_lines[guess - 1].strip() == original.strip():
        return guess
    hits = [n for n in range(lo, hi + 1) if source_lines[n - 1].strip() == original.strip()]
    return hits[0] if len(hits) == 1 else None


class _WorkDir:
    """repo/mutants, created for one run and removed only if this gate created it."""

    def __init__(self, repo):
        self.repo = repo
        self.path = repo / "mutants"
        self.token_file = store.work_dir(hashlib.sha1(str(repo).encode()).hexdigest()[:12] + "-mutmut") / "marker"

    def _ours(self):
        if self.path.is_symlink() or not self.path.is_dir() or _tracked(self.repo, "mutants"):
            return False
        try:
            return (self.path / MARKER).read_text() == self.token_file.read_text()
        except OSError:
            return False

    def create(self):
        if self.path.exists() or self.path.is_symlink():
            if not self._ours():
                return False
            shutil.rmtree(self.path)
        token = secrets.token_hex(16)
        self.token_file.write_text(token)
        self.path.mkdir()
        (self.path / MARKER).write_text(token)
        return True

    def remove(self):
        if self._ours():
            shutil.rmtree(self.path)


def _plain_pytest_passes(repo, binary, timeout):
    python = binary.parent / "python"
    try:
        proc = run_tree([str(python), "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "--ignore=mutants"],
                        cwd=repo, env={**os.environ, "NO_COLOR": "1"}, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def run(repo, changed, budget):
    repo = Path(repo).resolve()
    binary = _binary(repo)
    if binary is None:
        return AdapterResult(error=_install_hint(repo), error_kind="missing")
    if _tracked(repo, os.path.relpath(binary, repo)):
        return AdapterResult(error=f"git이 추적하는 {binary}는 실행하지 않음 (repo가 넣어둔 실행 파일일 수 있음)", error_kind="missing")
    deadline = time.monotonic() + budget

    origin, unverified, sources = {}, [], {}
    for rel in sorted(changed):
        try:
            sources[rel] = (repo / rel).read_text().split("\n")
            funcs = _touched("\n".join(sources[rel]), changed[rel])
        except (OSError, SyntaxError) as exc:
            return AdapterResult(failure=f"{rel}를 파싱할 수 없음: {exc}")
        module = module_name(rel)
        for name, lo, hi, mutable in funcs:
            if mutable and module:
                origin[f"{module}.{name}"] = (rel, lo, hi)
            else:
                unverified.append(f"mutmut이 변이하지 않는 함수 변경 (데코레이터): {rel}:{lo} {name.split('ǁ')[-1].removeprefix('x_')}")
    patterns = [f"{qualified}__mutmut_*" for qualified in origin]
    if not patterns:
        return AdapterResult(unverified=unverified)

    work = _WorkDir(repo)
    if not work.create():
        return AdapterResult(error=f"{work.path}/ 가 이미 있음 (mutation-gate가 만든 것이 아님). 옮긴 뒤 다시 시도", error_kind="missing")
    env = {**os.environ, "NO_COLOR": "1"}

    def mm(*args, timeout=None):
        left = max(1, int(deadline - time.monotonic()))
        return run_tree([str(binary), *args], cwd=repo, env=env, timeout=min(timeout or left, left))

    try:
        proc = mm("run", "--", *patterns, timeout=max(1, budget - min(REPORT_RESERVE, budget // 4)))
        output = proc.stdout + proc.stderr
        lowered = output.lower()
        if "filtered for specific mutants, but nothing matches" in lowered:
            return AdapterResult(unverified=unverified)
        if "could not find any test case for any mutant" in lowered:
            return AdapterResult(failure="바뀐 코드를 실행하는 테스트가 하나도 없음 (mutmut)")
        if "module name starts with `src.`" in lowered:
            example = module_name(next(iter(sorted(changed))))
            return AdapterResult(
                error=f"테스트가 `src.` 경로로 import함 — mutmut는 이를 추적하지 못함. `from {example} import …`처럼 "
                      "패키지 이름으로 import하라 (pyproject [tool.pytest.ini_options] pythonpath = [\"src\"])",
                error_kind="sandbox",
            )
        if "failed to collect stats" in lowered or "failed to run clean test" in lowered:
            left = max(1, int(deadline - time.monotonic()))
            if _plain_pytest_passes(repo, binary, min(left, 120)):
                return AdapterResult(
                    error="mutmut sandbox(mutants/)에서만 테스트가 실패함 — 테스트가 복사되지 않은 파일(데이터·형제 패키지)을 "
                          "쓰는 듯. pyproject [tool.mutmut] also_copy 설정 확인",
                    error_kind="sandbox",
                )
            failed = [l for l in output.splitlines() if l.startswith(("FAILED", "ERROR"))]
            return AdapterResult(failure="테스트가 현재 실패함 (mutmut clean run):\n" + tail("\n".join(failed), 800))
        if proc.returncode != 0:
            return AdapterResult(error=f"mutmut 실행 실패 (exit {proc.returncode}):\n{tail(output)}", error_kind="crash")

        statuses = {}
        for line in mm("results", "--all", "true").stdout.splitlines():
            match = RESULT_LINE.match(line)
            if match and any(fnmatch.fnmatchcase(match.group(1), p) for p in patterns):
                statuses[match.group(1)] = match.group(2).strip()
        unchecked = [n for n, s in statuses.items() if s == "not checked"]
        if unchecked:
            return AdapterResult(error=f"mutmut가 mutant {len(unchecked)}개를 검사하지 못함 (검사 미완료)", error_kind="crash")

        names = [n for n, s in statuses.items() if s not in NEUTRAL]
        with ThreadPoolExecutor(max_workers=8) as pool:
            shows = list(pool.map(lambda n: mm("show", n).stdout, names))
    except subprocess.TimeoutExpired:
        return AdapterResult(error=f"mutmut가 {budget}초 안에 끝나지 않음 (검사 미완료)", error_kind="timeout")
    finally:
        work.remove()

    mutants, unmapped = [], 0
    for name, text in zip(names, shows):
        qualified = name.rsplit("__mutmut_", 1)[0]
        rel, lo, hi = origin[qualified]
        offset, original, replacement, anchor = _parse_show(text)
        line_no = None if offset is None else _locate(sources[rel], lo, hi, offset, anchor, original)
        if line_no is None:
            unmapped += 1
            continue
        if line_no not in changed[rel]:
            continue
        mutants.append(Mutant(
            path=rel, line=line_no, mutator="mutmut", original=original.strip(),
            replacement=(replacement or "").strip(), status=STATUS.get(statuses[name], UNDETECTED),
            context=qualified,
        ))
    number_occurrences(mutants)
    result = AdapterResult(mutants=sorted(mutants, key=lambda m: (m.path, m.line, m.replacement)), unverified=unverified)
    if unmapped:
        result.error, result.error_kind = f"mutmut show 결과 {unmapped}개를 소스 줄에 매핑하지 못함", "crash"
    return result
