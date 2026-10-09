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
        return f"mutmut is not in the project's venv. Install: uv pip install --python {shlex.quote(str(python))} mutmut"
    return (f"mutmut not found (no {repo}/.venv). Install: cd {shlex.quote(str(repo))} && "
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
        return AdapterResult(error=f"not running {binary}: git tracks it, so the repo may have put it there", error_kind="missing")
    deadline = time.monotonic() + budget

    origin, unverified, sources = {}, [], {}
    for rel in sorted(changed):
        try:
            sources[rel] = (repo / rel).read_text().split("\n")
            funcs = _touched("\n".join(sources[rel]), changed[rel])
        except (OSError, SyntaxError) as exc:
            return AdapterResult(failure=f"could not parse {rel}: {exc}")
        module = module_name(rel)
        for name, lo, hi, mutable in funcs:
            if mutable and module:
                origin[f"{module}.{name}"] = (rel, lo, hi)
            else:
                unverified.append(f"changed function mutmut does not mutate (decorated): {rel}:{lo} {name.split('ǁ')[-1].removeprefix('x_')}")
    patterns = [f"{qualified}__mutmut_*" for qualified in origin]
    if not patterns:
        return AdapterResult(unverified=unverified)

    work = _WorkDir(repo)
    if not work.create():
        return AdapterResult(error=f"{work.path}/ already exists and mutation-gate did not create it. Move it and try again", error_kind="missing")
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
            return AdapterResult(failure="no test runs the changed code (mutmut)")
        if "module name starts with `src.`" in lowered:
            example = module_name(next(iter(sorted(changed))))
            return AdapterResult(
                error=f"the tests import through `src.`, which mutmut cannot trace. Import by package name, like "
                      f"`from {example} import …` (pyproject [tool.pytest.ini_options] pythonpath = [\"src\"])",
                error_kind="sandbox",
            )
        if "failed to collect stats" in lowered or "failed to run clean test" in lowered:
            left = max(1, int(deadline - time.monotonic()))
            if _plain_pytest_passes(repo, binary, min(left, 120)):
                return AdapterResult(
                    error="the tests fail only in mutmut's sandbox (mutants/); they seem to use files mutmut does not copy "
                          "(data, sibling packages). Check also_copy in pyproject [tool.mutmut]",
                    error_kind="sandbox",
                )
            failed = [l for l in output.splitlines() if l.startswith(("FAILED", "ERROR"))]
            return AdapterResult(failure="the tests are failing (mutmut clean run):\n" + tail("\n".join(failed), 800))
        if proc.returncode != 0:
            return AdapterResult(error=f"mutmut failed (exit {proc.returncode}):\n{tail(output)}", error_kind="crash")

        statuses = {}
        for line in mm("results", "--all", "true").stdout.splitlines():
            match = RESULT_LINE.match(line)
            if match and any(fnmatch.fnmatchcase(match.group(1), p) for p in patterns):
                statuses[match.group(1)] = match.group(2).strip()
        unchecked = [n for n, s in statuses.items() if s == "not checked"]
        if unchecked:
            return AdapterResult(error=f"mutmut did not check {len(unchecked)} mutant(s) (incomplete run)", error_kind="crash")

        names = [n for n, s in statuses.items() if s not in NEUTRAL]
        with ThreadPoolExecutor(max_workers=8) as pool:
            shows = list(pool.map(lambda n: mm("show", n).stdout, names))
    except subprocess.TimeoutExpired:
        return AdapterResult(error=f"mutmut did not finish within {budget}s (incomplete run)", error_kind="timeout")
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
        result.error, result.error_kind = f"could not map {unmapped} mutmut show result(s) to source lines", "crash"
    return result
