"""gremlins adapter (Go modules, go test)."""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from .. import diff, store, tools
from ..model import DETECTED, UNDETECTED, AdapterResult, Mutant, number_occurrences, run_tree, tail

GREMLINS_PKG = "github.com/go-gremlins/gremlins/cmd/gremlins"
GREMLINS_VERSION = "v0.6.0"
GO_DIRS = ["~/.local/go/bin"]
TOOL_DIRS = ["~/go/bin"]
# Each gremlins worker copies the whole module into the work dir; cap the copies.
WORKERS = 4

ANSI = re.compile(r"\x1b\[[0-9;]*m")
STATUS = {"KILLED": DETECTED, "TIMED OUT": DETECTED, "LIVED": UNDETECTED, "NOT COVERED": UNDETECTED}
# The mutant does not compile, or was never run: says nothing about the tests.
DROPPED = {"NOT VIABLE", "SKIPPED"}
# gremlins' own token table (internal/engine/mappings.go): mutation type -> original -> replacement.
_SELF_ASSIGN = ("+=", "&=", "&^=", "*=", "|=", "/=", "%=", "<<=", ">>=", "-=", "^=")
REPLACEMENTS = {
    "ARITHMETIC_BASE": {"+": "-", "*": "/", "/": "*", "%": "*", "-": "+"},
    "CONDITIONALS_BOUNDARY": {">=": ">", ">": ">=", "<=": "<", "<": "<="},
    "CONDITIONALS_NEGATION": {"==": "!=", "!=": "==", ">=": "<", ">": "<=", "<=": ">", "<": ">="},
    "INCREMENT_DECREMENT": {"++": "--", "--": "++"},
    "INVERT_ASSIGNMENTS": {"+=": "-=", "-=": "+=", "*=": "/=", "/=": "*=", "%=": "%="},
    "INVERT_BITWISE": {"&": "|", "|": "&", "^": "&", "&^": "&", "<<": ">>", ">>": "<<"},
    "INVERT_BWASSIGN": {"&=": "|=", "|=": "&=", "^=": "&=", "&^=": "&=", "<<=": ">>=", ">>=": "<<="},
    "INVERT_LOGICAL": {"&&": "||", "||": "&&"},
    "INVERT_LOOPCTRL": {"break": "continue", "continue": "break"},
    "INVERT_NEGATIVES": {"-": "+"},
    "REMOVE_SELF_ASSIGNMENTS": {op: "=" for op in _SELF_ASSIGN},
}
# gremlins' defaults plus && <-> ||, break <-> continue and compound assignments. Bitwise stays off:
# it mostly turns &x into |x, which does not compile.
MUTATORS = {
    "arithmetic-base": True, "conditionals-boundary": True, "conditionals-negation": True,
    "increment-decrement": True, "invert-negatives": True, "invert-logical": True,
    "invert-loopctrl": True, "invert-assignments": True, "remove-self-assignments": True,
    "invert-bitwise": False, "invert-bwassign": False,
}
# Directories the go tool never builds as packages.
NOT_PACKAGES = {"testdata", "vendor"}
PACKAGE = re.compile(r"^\s*package\s+(\w+)", re.M)
MODULE = re.compile(r'^\s*module\s+"?([^\s"]+)"?', re.M)
FUNC = re.compile(r"^func\s*(?:\(([^)]*)\)\s*)?([A-Za-z_]\w*)")
RE2_SPECIAL = re.compile(r"([\\.+*?()|\[\]{}^$])")
GENERIC = re.compile(r"\[.*")


def _tool_dirs():
    dirs = [os.environ["GOBIN"]] if os.environ.get("GOBIN") else []
    dirs += [os.path.join(p, "bin") for p in os.environ.get("GOPATH", "").split(os.pathsep) if p]
    return dirs + TOOL_DIRS


def _find_go():
    return tools.find("go", GO_DIRS)


def _find_gremlins():
    return tools.find("gremlins", _tool_dirs())


def _install_hint(go):
    install = f"install {GREMLINS_PKG}@{GREMLINS_VERSION}"
    if go is None:
        return ("Go and gremlins are not installed. Install Go from https://go.dev/dl/ (no sudo: extract the "
                f"tarball so ~/.local/go/bin/go exists), then: ~/.local/go/bin/go {install}")
    go_cmd = "go" if shutil.which("go") == go else shlex.quote(go)
    return f"gremlins is not installed. Install: {go_cmd} {install}"


def _tracked(repo, path):
    for candidate in {path, os.path.realpath(path)}:
        rel = os.path.relpath(candidate, repo)
        if rel.startswith(".."):
            continue
        if diff._git(repo, "ls-files", "--error-unmatch", "--", f":(literal){rel}", check=False).returncode == 0:
            return True
    return False


def _module_root(repo, directory):
    """Nearest directory with a go.mod, inside the repo."""
    path = directory
    while True:
        if (path / "go.mod").is_file():
            return path
        if path == repo or repo not in path.parents:
            return None
        path = path.parent


def _module_problem(module):
    # gremlins takes the module path from the first line of go.mod, verbatim. Anything else there
    # makes it test the wrong package (false kills) or lose coverage (false survivors).
    text = (module / "go.mod").read_text(errors="replace")
    match = MODULE.search(text)
    first = text.split("\n", 1)[0].rstrip("\r")
    if not match or first != f"module {match.group(1)}":
        return (f"gremlins reads the module path from line 1 of {module / 'go.mod'} only; "
                "put `module <path>` alone on the first line")
    return None


def _clear(*paths):
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists() or path.is_symlink():
            path.unlink()


def _function_names(lines):
    """{line number: enclosing top-level function} for gofmt-style code (methods as Type.Name)."""
    names, current = {}, None
    for no, text in enumerate(lines, 1):
        match = FUNC.match(text)
        if match:
            receiver, name = match.group(1), match.group(2)
            if receiver and receiver.split():
                receiver_type = GENERIC.sub("", receiver.split()[-1].lstrip("*"))
                name = f"{receiver_type}.{name}"
            names[no] = current = name
            if "{" in text and text.count("{") == text.count("}"):
                current = None  # one-line function
            continue
        if current:
            names[no] = current
            if text.startswith("}"):
                current = None
    return names


def _display(text, column, mutator):
    """(original line, mutated line) for one-line display; None when the mutation changes nothing."""
    raw = text.encode()
    start = column - 1  # gremlins reports 1-based byte columns
    table = REPLACEMENTS.get(mutator, {})
    for token in sorted(table, key=len, reverse=True):
        if 0 <= start and raw[start : start + len(token)] == token.encode():
            if table[token] == token:  # INVERT_ASSIGNMENTS maps %= to itself: an equivalent mutant
                return None
            mutated = raw[:start] + table[token].encode() + raw[start + len(token) :]
            return text.strip(), mutated.decode(errors="replace").strip()
    return text.strip(), f"<{mutator} at column {column}>"


def _parse(data, repo, pkg_dir, files, sources):
    mutants = []
    for entry in data.get("files") or []:
        rel = Path(os.path.relpath(pkg_dir / entry.get("file_name", ""), repo)).as_posix()
        lines = files.get(rel)
        if not lines:
            continue
        source = sources[rel]
        names = _function_names(source)
        for raw in entry.get("mutations") or []:
            status, line = raw.get("status"), raw.get("line", 0)
            if status in DROPPED or line not in lines or not 1 <= line <= len(source):
                continue
            mutator = raw.get("type", "?")
            shown = _display(source[line - 1], raw.get("column", 0), mutator)
            if shown is None:
                continue
            mutants.append(Mutant(
                path=rel, line=line, mutator=mutator, original=shown[0], replacement=shown[1],
                status=STATUS.get(status, UNDETECTED), context=names.get(line) or shown[0],
            ))
    return mutants


def _failure(output):
    """The go test output of gremlins' coverage run, without gremlins' own lines."""
    segment = output.split("Gathering coverage...", 1)[-1].split("ERROR:", 1)[0]
    lines = [l for l in segment.splitlines() if l.strip() and not l.startswith("go: ")]
    return tail("\n".join(lines), 800)


def _run_package(gremlins, go, repo, module, pkg_dir, files, deadline):
    sources = {}
    for rel in files:
        try:
            sources[rel] = [l.rstrip("\r") for l in (repo / rel).read_text(errors="replace").split("\n")]
        except OSError as exc:
            return AdapterResult(error=f"could not read {rel}: {exc}", error_kind="crash")
    clauses = (PACKAGE.search("\n".join(lines)) for lines in sources.values())
    packages = {m.group(1) for m in clauses if m}

    # gremlins tests a mutant by walking up the directory until a path ends with the package name;
    # when none does (package main in cmd/tool) it would test the wrong package. Integration mode
    # runs every test of the module per mutant instead: slower, but right.
    rel_pkg = Path(os.path.relpath(pkg_dir, module)).as_posix()
    integration = rel_pkg != "." and not all(rel_pkg.endswith(p) for p in packages)

    # gremlins walks the directory recursively: skip subdirectories and the files not changed.
    changed_names = {Path(rel).name for rel in files}
    others = sorted(p.name for p in pkg_dir.iterdir()
                    if p.suffix == ".go" and not p.name.endswith("_test.go") and p.name not in changed_names)
    exclude = ["/"] + (["^(?:" + "|".join(RE2_SPECIAL.sub(r"\\\1", n) for n in others) + ")$"] if others else [])

    work = store.work_dir(hashlib.sha1(str(module).encode()).hexdigest()[:12] + "-gremlins")
    if module == work or module in work.parents:
        return AdapterResult(error=f"the mutation-gate state dir {work} is inside the Go module {module}", error_kind="crash")
    tmp, report, config = work / "tmp", work / "gremlins.json", work / "gremlins.yaml"
    _clear(tmp, report)
    tmp.mkdir(mode=0o700)
    # Our own config file, so a .gremlins.yaml in the repo cannot turn mutators off or exclude files.
    config.write_text(json.dumps({
        "unleash": {"exclude-files": exclude, "workers": WORKERS, "integration": integration},
        "mutants": {name: {"enabled": on} for name, on in MUTATORS.items()},
    }, indent=2))
    env = {k: v for k, v in tools.env_with([os.path.dirname(go)], TMPDIR=str(tmp)).items()
           if not k.startswith("GREMLINS_")}
    try:
        proc = run_tree([gremlins, "unleash", "--config", str(config), "-o", str(report), str(pkg_dir)],
                        cwd=module, env=env, timeout=max(1, int(deadline - time.monotonic())))
        output = ANSI.sub("", proc.stdout + proc.stderr)
        if "failed to gather coverage" in output:
            if re.search(r"^(--- FAIL|FAIL\s|panic:)", output, re.M):
                return AdapterResult(failure="the tests are failing (go test on the unmutated code):\n" + _failure(output))
            return AdapterResult(error=f"gremlins could not run the tests (exit {proc.returncode}):\n{tail(output)}", error_kind="crash")
        if proc.returncode == 0 and not report.exists() and "No results to report" in output:
            return AdapterResult()  # no mutable operators in this package's changed files
        if proc.returncode != 0 or not report.exists():
            return AdapterResult(error=f"gremlins failed (exit {proc.returncode}):\n{tail(output)}", error_kind="crash")
        try:
            data = json.loads(report.read_text())
        except ValueError as exc:
            return AdapterResult(error=f"could not parse the gremlins report: {exc}", error_kind="crash")
        return AdapterResult(mutants=_parse(data, repo, pkg_dir, files, sources))
    finally:
        _clear(tmp, report)


def run(repo, changed, budget):
    repo = Path(repo).resolve()
    deadline = time.monotonic() + budget
    gremlins, go = _find_gremlins(), _find_go()
    if gremlins is None:
        return AdapterResult(error=_install_hint(go), error_kind="missing")
    if _tracked(repo, gremlins):
        return AdapterResult(error=f"not running {gremlins}: git tracks it, so the repo may have put it there", error_kind="missing")
    if go is None:
        return AdapterResult(error=_install_hint(None), error_kind="missing")
    if _tracked(repo, go):
        return AdapterResult(error=f"not running {go}: git tracks it, so the repo may have put it there", error_kind="missing")

    groups, unverified, problems = {}, [], {}
    for rel in sorted(changed):
        dirs = Path(rel).parts[:-1]
        if any(d in NOT_PACKAGES or d.startswith(("_", ".")) for d in dirs):
            unverified.append(f"changed Go file the go tool does not build as a package: {rel}")
            continue
        pkg_dir = (repo / rel).parent
        module = _module_root(repo, pkg_dir)
        if module is None:
            return AdapterResult(error=f"no go.mod above {rel}; only Go modules are supported", error_kind="missing")
        if module not in problems:
            problems[module] = _module_problem(module)
        if problems[module]:
            return AdapterResult(error=problems[module], error_kind="missing")
        groups.setdefault((module, pkg_dir), {})[rel] = changed[rel]

    merged = AdapterResult(unverified=unverified)
    for module, pkg_dir in sorted(groups):
        try:
            result = _run_package(gremlins, go, repo, module, pkg_dir, groups[(module, pkg_dir)], deadline)
        except subprocess.TimeoutExpired:
            result = AdapterResult(error=f"gremlins did not finish within {budget}s (incomplete run)", error_kind="timeout")
        merged.mutants += result.mutants
        merged.failure = merged.failure or result.failure
        if result.error and not merged.error:
            merged.error, merged.error_kind = result.error, result.error_kind
        if result.error_kind == "timeout":
            break
    number_occurrences(merged.mutants)
    merged.mutants.sort(key=lambda m: (m.path, m.line, m.mutator, m.replacement))
    return merged
