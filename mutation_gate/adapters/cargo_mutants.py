"""cargo-mutants adapter (cargo test)."""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import tomllib
from pathlib import Path

from .. import diff, model, store, tools
from ..model import DETECTED, UNDETECTED, AdapterResult, Mutant, number_occurrences, run_tree, tail, to_ranges

INSTALL = "cargo install --locked cargo-mutants"
RUSTUP = "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path"
STATUS = {"CaughtMutant": DETECTED, "Timeout": DETECTED, "MissedMutant": UNDETECTED}
# Does not compile: says nothing about the tests, like Stryker's CompileError.
UNVIABLE = "Unviable"
# cargo-mutants exit codes with a full report: all caught, some missed, some timed out.
REPORTED = {0, 2, 3}
BASELINE_FAILED = 4
# The project's .cargo/mutants.toml is never read by cargo-mutants (--no-config): many of its keys
# change which mutants exist or how they are judged (examine/exclude globs and regexes, skip_calls,
# error_values, timeouts, test args, test tool or packages). Only these build settings are carried
# over, as command-line flags: key -> (expected type, flag builder).
CONFIG_FLAGS = {
    "features": (list, lambda v: [f"--features={','.join(v)}"] if v else []),
    "all_features": (bool, lambda v: ["--all-features"] if v else []),
    "no_default_features": (bool, lambda v: ["--no-default-features"] if v else []),
    "profile": (str, lambda v: [f"--profile={v}"]),
    "cap_lints": (bool, lambda v: [f"--cap-lints={str(v).lower()}"]),
    "copy_vcs": (bool, lambda v: [f"--copy-vcs={str(v).lower()}"]),
    "copy_target": (bool, lambda v: [f"--copy-target={str(v).lower()}"]),
    "gitignore": (bool, lambda v: [f"--gitignore={str(v).lower()}"]),
}
CONFIG_WORD = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./+-]*$")  # feature and profile names
GLOB_CHARS = set("*?[]{}\\")
TEST_COUNT = re.compile(r"^running (\d+) tests?$", re.MULTILINE)
FAILED_LINE = re.compile(r"^(test .+ \.\.\. FAILED|error(\[E\d+\])?: .+|\s+--> .+|test result: FAILED.*)$")
NO_TOOLCHAIN = ("rustup could not choose a version", "no default is configured", "is not installed")
# Seconds for quick commands (--version, locate-project).
QUICK = 30


def _cargo_dirs():
    home = os.environ.get("CARGO_HOME")
    return ([os.path.join(home, "bin")] if home else []) + ["~/.cargo/bin"]


def _tracked(repo, path):
    """Whether git tracks the binary at `path` (or the file a symlink there points to) in the repo."""
    path = os.path.abspath(path)
    candidates = {Path(os.path.realpath(os.path.dirname(path))) / os.path.basename(path), Path(os.path.realpath(path))}
    for candidate in candidates:
        try:
            rel = candidate.relative_to(repo)
        except ValueError:
            continue
        if diff._git(repo, "ls-files", "--error-unmatch", "--", f":(literal){rel}", check=False).returncode == 0:
            return True
    return False


def _toolchain(repo, deadline):
    """(cargo, cargo-mutants, env) or an AdapterResult saying what is missing."""
    cargo = tools.find("cargo", _cargo_dirs())
    if cargo is None:
        return AdapterResult(
            error=f"cargo-mutants is not installed. Install: {INSTALL} "
                  f"(cargo was not found on PATH or in ~/.cargo/bin either; install Rust first: {RUSTUP})",
            error_kind="missing",
        )
    # Run the binary itself, not `cargo mutants`: a repo's .cargo/config.toml alias could take that name.
    binary = tools.find("cargo-mutants", [os.path.dirname(cargo), *_cargo_dirs()])
    on_path = shutil.which("cargo") is not None
    hint = f"Install: {INSTALL if on_path else cargo + ' ' + INSTALL.removeprefix('cargo ')}"
    if binary is None:
        return AdapterResult(error=f"cargo-mutants is not installed. {hint}", error_kind="missing")
    for path in (cargo, binary):
        if _tracked(repo, path):
            return AdapterResult(error=f"not running {path}: git tracks it, so the repo may have put it there", error_kind="missing")
    env = tools.env_with([os.path.dirname(cargo)], CARGO=cargo, CARGO_TERM_COLOR="never")
    try:
        proc = run_tree([binary, "mutants", "--version"], cwd=repo, env=env, timeout=_left(deadline, QUICK))
    except subprocess.TimeoutExpired:
        proc = None
    if proc is None or proc.returncode != 0 or "cargo-mutants" not in proc.stdout:
        detail = tail(proc.stdout + proc.stderr, 300) if proc else "timed out"
        return AdapterResult(error=f"cargo-mutants is not installed or does not run (`cargo mutants --version`: {detail}). {hint}",
                             error_kind="missing")
    return cargo, binary, env


def _left(deadline, cap=None):
    left = max(1, int(deadline - time.monotonic()))
    return min(left, cap) if cap else left


def _package_dir(repo, rel):
    """Nearest directory with a Cargo.toml above the file, inside the repo."""
    path = (repo / rel).parent
    while True:
        if (path / "Cargo.toml").is_file():
            return path
        if path == repo or repo not in path.parents:
            return None
        path = path.parent


def _workspace_root(cargo, package, env, deadline):
    """(workspace root, None) or (None, AdapterResult) when cargo cannot tell."""
    proc = run_tree([cargo, "locate-project", "--workspace", "--message-format", "plain"],
                    cwd=package, env=env, timeout=_left(deadline, QUICK))
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip()).parent.resolve(), None
    if any(s in proc.stderr for s in NO_TOOLCHAIN):
        return None, AdapterResult(error=f"cargo has no usable Rust toolchain. Install one: rustup default stable\n{tail(proc.stderr, 500)}",
                                   error_kind="missing")
    return None, AdapterResult(error=f"cargo could not find the workspace of {package}:\n{tail(proc.stderr)}", error_kind="crash")


def _source_lines(text):
    """The file's lines the way Rust's str::lines() splits them, which cargo-mutants compares with."""
    rows = text.split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    return [r.removesuffix("\r") for r in rows]


def _diff(files):
    """A unified diff that adds exactly the changed lines, for --in-diff.

    cargo-mutants keeps a mutant when any line of its span is an added line, and checks the added
    text against the source, so the hunks carry the current text of those lines.
    """
    out = []
    for ws_rel, rows, lines in files:
        out += [f"--- a/{ws_rel}", f"+++ b/{ws_rel}"]
        shift = 0
        for lo, hi in to_ranges(n for n in lines if 1 <= n <= len(rows)):
            count = hi - lo + 1
            out.append(f"@@ -{lo - 1 - shift},0 +{lo},{count} @@")
            out += ["+" + rows[n - 1] for n in range(lo, hi + 1)]
            shift += count
    return "\n".join(out) + "\n"


def _glob_escape(path):
    return "".join("\\" + c if c in GLOB_CHARS else c for c in path)


def _config_flags(ws_root):
    """Command-line flags for the build settings in the project's .cargo/mutants.toml.

    Fails closed: an unreadable file, an unknown key or a value of the wrong type adds nothing.
    """
    try:
        data = tomllib.loads((ws_root / ".cargo" / "mutants.toml").read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    flags = []
    for key, (kind, build) in CONFIG_FLAGS.items():
        value = data.get(key)
        if not isinstance(value, kind):
            continue
        words = value if kind is list else [value] if kind is str else []
        if all(isinstance(w, str) and CONFIG_WORD.match(w) for w in words):
            flags += build(value)
    return flags


def _span_text(rows, span):
    start, end = span["start"], span["end"]
    picked = rows[start["line"] - 1 : end["line"]]
    if not picked:
        return ""
    if len(picked) == 1:
        return picked[0][start["column"] - 1 : end["column"] - 1]
    return "\n".join([picked[0][start["column"] - 1 :], *picked[1:-1], picked[-1][: end["column"] - 1]])


def _parse(data, files):
    """Mutants whose span overlaps changed lines. `files` maps workspace-relative paths to (rel, rows, lines)."""
    mutants = []
    for outcome in data.get("outcomes", []):
        scenario = outcome.get("scenario")
        if not isinstance(scenario, dict) or "Mutant" not in scenario:
            continue  # the baseline
        raw = scenario["Mutant"]
        if raw.get("file") not in files or outcome.get("summary") == UNVIABLE:
            continue
        rel, rows, lines = files[raw["file"]]
        span = raw["span"]
        touched = sorted(n for n in lines if span["start"]["line"] <= n <= span["end"]["line"])
        if not touched:
            continue
        full = _span_text(rows, span)
        multiline = "\n" in full
        function = (raw.get("function") or {}).get("function_name", "")
        mutants.append(Mutant(
            path=rel, line=touched[0], mutator=raw.get("genre", "?"),
            original=full.split("\n")[0].rstrip() + (" …" if multiline else ""),
            replacement=raw.get("replacement", "").split("\n")[0],
            # Unknown outcomes (an unattributed Failure) count against the tests, never for them.
            status=STATUS.get(outcome.get("summary"), UNDETECTED),
            context=f"{function}\n{full}" if multiline else function,
        ))
    return mutants


def _baseline(data, out):
    """(outcome, log text) of the unmutated run."""
    for outcome in data.get("outcomes", []):
        if outcome.get("scenario") == "Baseline":
            try:
                return outcome, (out / outcome.get("log_path", "")).read_text(errors="replace")
            except OSError:
                return outcome, ""
    return None, ""


def _failure_lines(log):
    rows = log.splitlines()
    keep = []
    for i, row in enumerate(rows):
        if FAILED_LINE.match(row):
            keep.append(row)
        elif "panicked at" in row:
            keep += rows[i : i + 2]
    return "\n".join(keep) or log


def _run_interruptible(cmd, cwd, env, timeout):
    """run_tree, but SIGINT cargo-mutants shortly before the deadline.

    cargo-mutants starts each cargo build and test in a process group of its own, which run_tree's
    kill on timeout does not reach. On SIGINT it stops them and removes its build directories.
    Returns (CompletedProcess, whether the SIGINT was sent).
    """
    before = set(model.ACTIVE_GROUPS)
    sent = threading.Event()

    def interrupt():
        sent.set()
        for pgid in set(model.ACTIVE_GROUPS) - before:
            try:
                os.killpg(pgid, signal.SIGINT)
            except ProcessLookupError:
                pass

    grace = min(20, max(2, timeout // 10))
    timer = threading.Timer(max(1, timeout - grace), interrupt)
    timer.daemon = True
    timer.start()
    try:
        proc = run_tree(cmd, cwd=cwd, env=env, timeout=timeout)
    finally:
        timer.cancel()
    return proc, sent.is_set()


def _kill_leftovers(tmp):
    """Kill cargo builds and tests still running in the build directories after a hard kill."""
    try:
        ps = run_tree(["ps", "-axo", "pgid=,command="], cwd="/", env=os.environ, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return
    for row in ps.stdout.splitlines():
        pgid, _, command = row.strip().partition(" ")
        if str(tmp) in command and pgid.isdigit() and int(pgid) != os.getpgrp():
            try:
                os.killpg(int(pgid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def _run_workspace(binary, env, ws_root, changed, deadline, budget):
    repo_files = {}  # workspace-relative path -> (repo-relative path, source rows, changed lines)
    for rel, (abs_path, rows, lines) in changed.items():
        repo_files[Path(os.path.relpath(abs_path, ws_root)).as_posix()] = (rel, rows, lines)

    work = store.work_dir(hashlib.sha1(str(ws_root).encode()).hexdigest()[:12] + "-cargo-mutants")
    out, tmp, diff_file = work / "out", work / "tmp", work / "changes.diff"
    for leftover in (out, tmp):
        shutil.rmtree(leftover, ignore_errors=True)
    tmp.mkdir()
    try:
        diff_file.write_text(_diff((ws_rel, rows, lines) for ws_rel, (_, rows, lines) in sorted(repo_files.items())))
        cmd = [binary, "mutants", "--dir", str(ws_root), "--workspace", "--no-config", *_config_flags(ws_root),
               "--in-diff", str(diff_file), "--output", str(out), "--colors", "never", "--annotations", "none"]
        for ws_rel in sorted(repo_files):
            cmd += ["--file", _glob_escape(ws_rel)]
        # Build directories go to a private temp dir, removed below even if cargo-mutants is killed.
        try:
            proc, interrupted = _run_interruptible(cmd, ws_root, {**env, "TMPDIR": str(tmp)}, _left(deadline))
        except subprocess.TimeoutExpired:
            _kill_leftovers(tmp)
            return AdapterResult(error=f"cargo-mutants did not finish within {budget}s (incomplete run)", error_kind="timeout")
        output = proc.stdout + proc.stderr
        if interrupted and proc.returncode not in REPORTED:
            return AdapterResult(error=f"cargo-mutants did not finish within {budget}s (incomplete run)", error_kind="timeout")

        report = out / "mutants.out"
        try:
            data = json.loads((report / "outcomes.json").read_text())
        except (OSError, ValueError):
            data = None
        if proc.returncode == BASELINE_FAILED:
            _, log = _baseline(data or {}, report)
            return AdapterResult(failure="the tests are failing (cargo-mutants baseline):\n" + tail(_failure_lines(log or output), 800))
        if proc.returncode not in REPORTED:
            return AdapterResult(error=f"cargo-mutants failed (exit {proc.returncode}):\n{tail(output)}", error_kind="crash")
        if data is None:
            if proc.returncode == 0:  # "No mutants to filter": nothing mutable on the changed lines
                return AdapterResult()
            return AdapterResult(error=f"cargo-mutants wrote no outcomes.json (exit {proc.returncode}):\n{tail(output)}", error_kind="crash")
        _, log = _baseline(data, report)
        counts = [int(n) for n in TEST_COUNT.findall(log)]
        if counts and not any(counts):
            return AdapterResult(failure="no test runs in this package (cargo test ran 0 tests in the cargo-mutants baseline)")
        return AdapterResult(mutants=_parse(data, repo_files))
    finally:
        shutil.rmtree(out, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
        diff_file.unlink(missing_ok=True)


def run(repo, changed, budget):
    repo = Path(repo).resolve()
    deadline = time.monotonic() + budget
    found = _toolchain(repo, deadline)
    if isinstance(found, AdapterResult):
        return found
    cargo, binary, env = found

    merged = AdapterResult()
    groups, roots = {}, {}
    try:
        for rel in sorted(changed):
            package = _package_dir(repo, rel)
            if package is None:
                merged.unverified.append(f"not in a Cargo package (no Cargo.toml above it): {rel}")
                continue
            if package not in roots:
                roots[package] = _workspace_root(cargo, package, env, deadline)
            ws_root, error = roots[package]
            if error:
                return error
            try:
                text = (repo / rel).read_text()
            except (OSError, UnicodeDecodeError) as exc:
                return AdapterResult(failure=f"could not read {rel}: {exc}")
            groups.setdefault(ws_root, {})[rel] = (repo / rel, _source_lines(text), set(changed[rel]))
    except subprocess.TimeoutExpired:
        return AdapterResult(error=f"cargo did not answer within {budget}s", error_kind="timeout")

    for ws_root in sorted(groups):
        result = _run_workspace(binary, env, ws_root, groups[ws_root], deadline, budget)
        merged.mutants += result.mutants
        merged.failure = merged.failure or result.failure
        if result.error and not merged.error:
            merged.error, merged.error_kind = result.error, result.error_kind
    number_occurrences(merged.mutants)
    merged.mutants.sort(key=lambda m: (m.path, m.line, m.mutator, m.replacement))
    return merged
