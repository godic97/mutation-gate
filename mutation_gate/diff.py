"""Snapshots of a repo's code files, the lines changed between two snapshots, and tamper scans.

A snapshot is a git tree built from a private copy of the index plus the working tree's code
files (tracked or untracked, .gitignore respected). The real index and refs are never touched.
Diffing two snapshots counts exactly what changed during the session: work that existed before
it is in the base, and commits made during it do not hide anything.
"""

import configparser
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
GIT_TIMEOUT = 60

# Directories written by mutation tools or package managers; never part of the gate's input.
IGNORED_ANYWHERE = {"node_modules", ".venv", "venv", "__pycache__", ".stryker-tmp"}
IGNORED_AT_ROOT = {"mutants"}

JS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs", ".vue", ".svelte"}
TEST_DIRS = {"__tests__", "tests", "test"}
# JS/TS that supports tests or tooling rather than shipping: never mutated.
JS_SUPPORT_TOP = {"test", "tests", "e2e", "cypress", "playwright"}
JS_SUPPORT_NAME = re.compile(r"\.(stories|story|test-utils|fixture|fixtures)\.|^setupTests\.|^\.eslintrc\.|\.d\.[mc]?ts$")
# Compiled languages: extension -> adapter language.
OTHER_LANGS = {".rs": "rust", ".go": "go", ".java": "jvm", ".kt": "jvm", ".cs": "dotnet", ".scala": "scala"}
RUST_NON_SOURCE = {"tests", "benches", "examples"}
# Maven/Gradle/sbt layout: src/<set>/<lang>/... where any set but `main` holds tests.
JVM_TEST_SETS = {"test", "it", "integrationTest", "testFixtures"}

HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# Markers that silence mutants, only where the tools read them: inside a comment. Adding any of
# them is treated as tampering; the same words inside a string are not.
INLINE_SUPPRESSIONS = [
    re.compile(r"(//|/\*)\s*stryker\s+disable", re.IGNORECASE),
    re.compile(r"#\s*pragma:\s*no\s+mutate", re.IGNORECASE),
    re.compile(r"#\[(mutants::skip|cfg_attr\(test,\s*mutants::skip\))\]"),
    # Stryker4s reads its suppressions from string literals such as "stryker4s.mutation.EqualityOperator",
    # wherever the @SuppressWarnings around them is written.
    re.compile(r"[\"']stryker4s\.mutation"),
]
CONFIG_FILES = {"pyproject.toml", "setup.cfg", "mutmut.toml", "stryker4s.conf"}
# mutmut 3 settings that change which mutants exist or how they are judged.
CONFIG_SUPPRESSIONS = re.compile(
    r"\b(do_not_mutate|do_not_mutate_patterns|only_mutate|mutate_only_covered_lines|source_paths"
    r"|paths_to_mutate|tests_dir|pytest_add_cli_args|pytest_add_cli_args_test_selection"
    r"|type_check_command|timeout_multiplier|timeout_constant|max_stack_depth)\b"
)
# Markers that switch tests off or narrow a run to a few tests.
TEST_SKIPS = re.compile(
    r"\b(it|test|describe|suite|context)\.(skip|only|todo|skipIf|runIf)\b"
    r"|\b(xit|xtest|xdescribe|fit|fdescribe)\s*\("
    r"|@pytest\.mark\.(skip|skipif|xfail)\b|\bpytest\.(skip|xfail)\s*\(|\bunittest\.skip"
    r"|#\[ignore\b|\bt\.Skip(Now|f)?\(|@Disabled\b|@Ignore\b|\[Ignore\b|\bSkip\s*=|\bignore\(\s*\""
)


def _git(repo, *args, check=True, env=None, stdin=None):
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-c", "core.quotePath=false", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=check, env=env, input=stdin, timeout=GIT_TIMEOUT,
    )


def repo_root(path):
    try:
        result = _git(path, "rev-parse", "--show-toplevel", check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return str(Path(result.stdout.strip()).resolve())


def head_sha(repo):
    result = _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else EMPTY_TREE


def stash_ref(repo):
    return _git(repo, "rev-parse", "-q", "--verify", "refs/stash", check=False).stdout.strip()


def _ignored(rel):
    parts = Path(rel).parts
    return bool(parts) and (parts[0] in IGNORED_AT_ROOT or any(p in IGNORED_ANYWHERE for p in parts[:-1]))


def is_code_path(rel):
    p = Path(rel)
    return not _ignored(rel) and (
        p.suffix in JS_EXTS or p.suffix == ".py" or p.suffix in OTHER_LANGS or p.name in CONFIG_FILES
    )


def _jvm_test(parts):
    return any(a == "src" and b in JVM_TEST_SETS for a, b in zip(parts, parts[1:]))


def is_test(rel):
    p = Path(rel)
    name, dirs = p.name, p.parts[:-1]
    if p.suffix in JS_EXTS:
        return ".test." in name or ".spec." in name or "__tests__" in dirs
    if p.suffix == ".py" and name != "conftest.py":
        return name.startswith("test_") or name.endswith("_test.py") or any(d in TEST_DIRS for d in dirs)
    if p.suffix == ".rs":
        return any(d in RUST_NON_SOURCE for d in dirs)
    if p.suffix == ".go":
        return name.endswith("_test.go")
    if p.suffix in (".java", ".kt", ".scala"):
        return _jvm_test(dirs) or any(d in TEST_DIRS for d in dirs)
    if p.suffix == ".cs":
        return (p.stem.endswith(("Tests", "Test"))
                or any(d.endswith((".Tests", ".Test", ".UnitTests")) or d in TEST_DIRS for d in dirs))
    return False


def classify(rel):
    """Return the adapter language for a mutable source file, or None."""
    p = Path(rel)
    if _ignored(rel) or is_test(rel) or p.name == "conftest.py":
        return None
    if p.suffix in JS_EXTS:
        support = p.parts[0] in JS_SUPPORT_TOP or "__mocks__" in p.parts or JS_SUPPORT_NAME.search(p.name)
        return None if support or ".config." in p.name else "js"
    if p.suffix == ".py":
        return None if any(d in TEST_DIRS for d in p.parts[:-1]) else "py"
    return OTHER_LANGS.get(p.suffix)


def snapshot(repo):
    """Tree id of the repo's code files as they are on disk right now."""
    gitdir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    with tempfile.TemporaryDirectory(prefix="mutation-gate-") as tmp:
        index = Path(tmp) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        if (gitdir / "index").exists():
            # copy2 keeps the index mtime, which git needs to spot same-size edits made in the
            # same second as the last index write (racy-git); a fresh mtime hides them.
            shutil.copy2(gitdir / "index", index)
        elif head_sha(repo) != EMPTY_TREE:
            _git(repo, "read-tree", "HEAD", env=env)
        out = _git(repo, "ls-files", "-z", "--modified", "--deleted", "--others", "--exclude-standard", env=env).stdout
        paths = sorted({p for p in out.split("\0") if p and is_code_path(p)})
        if paths:
            _git(repo, "update-index", "--add", "--remove", "-z", "--stdin", env=env, stdin="\0".join(paths) + "\0")
        return _git(repo, "write-tree", env=env).stdout.strip()


@dataclass
class Changes:
    added: dict = field(default_factory=dict)  # repo-relative path -> {line number: text}
    deleted_tests: list = field(default_factory=list)


def _lines(text):
    """Split on newlines only (str.splitlines also splits on \\f, \\v, \\u2028...)."""
    lines = text.split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def _hunk_lines(patch):
    """{new line number: text} for added lines, walking hunks by their line counts.

    A hunk that only deletes marks the two new-side lines around the deletion (text ""), so
    removing a check still puts the surrounding code under test.
    """
    added = {}
    old_left = new_left = 0
    lineno = 0
    for line in _lines(patch):
        if old_left == 0 and new_left == 0:
            match = HUNK_RE.match(line)
            if match:
                old_left = int(match.group(1) or 1)
                lineno = int(match.group(2))
                new_left = int(match.group(3) or 1)
                if new_left == 0:
                    for n in (lineno, lineno + 1):
                        if n >= 1:
                            added.setdefault(n, "")
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("-"):
            old_left -= 1
        elif line.startswith("+"):
            added[lineno] = line[1:]
            lineno += 1
            new_left -= 1
    return added


def changes(repo, base, current):
    result = Changes()
    if base == current:
        return result
    out = _git(repo, "diff-tree", "-r", "-z", "--name-status", "--no-renames", base, current).stdout
    tokens = [t for t in out.split("\0") if t]
    for status, rel in zip(tokens[::2], tokens[1::2]):
        if not is_code_path(rel):
            continue
        if status == "D":
            if is_test(rel):
                result.deleted_tests.append(rel)
            continue
        patch = _git(
            repo, "diff-tree", "-p", "-U0", "--no-color", "--no-textconv", "--no-ext-diff", "--no-renames",
            base, current, "--", f":(literal){rel}",
        ).stdout
        lines = _hunk_lines(patch)
        if lines:
            result.added[rel] = lines
    result.deleted_tests.sort()
    return result


def find_suppressions(added):
    """Added lines that silence mutants: markers in mutable source, scope keys in mutation config."""
    found = []
    for rel in sorted(added):
        if classify(rel):
            rx_list = INLINE_SUPPRESSIONS
        elif Path(rel).name in CONFIG_FILES:
            rx_list = [CONFIG_SUPPRESSIONS]
        else:
            continue
        for lineno in sorted(added[rel]):
            text = added[rel][lineno]
            if any(rx.search(text) for rx in rx_list):
                found.append((rel, lineno, text))
    return found


def find_test_skips(added):
    return [
        (rel, n, added[rel][n])
        for rel in sorted(added) if is_test(rel)
        for n in sorted(added[rel]) if TEST_SKIPS.search(added[rel][n])
    ]


def _mutation_section(name, text):
    if text is None:
        return None
    try:
        if name == "setup.cfg":
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(text)
            return dict(parser["mutmut"]) if parser.has_section("mutmut") else None
        if name == "stryker4s.conf":  # HOCON: compare the text
            return hashlib.sha1(text.encode()).hexdigest()
        data = tomllib.loads(text)
        return data.get("tool", {}).get("mutmut") if name == "pyproject.toml" else data
    except (ValueError, configparser.Error):
        return "unparseable:" + hashlib.sha1(text.encode()).hexdigest()


def mutation_config_changes(repo, base):
    """Root config files whose mutmut section differs from the base snapshot."""
    changed = []
    for name in sorted(CONFIG_FILES):
        old = _git(repo, "show", f"{base}:{name}", check=False)
        before = _mutation_section(name, old.stdout if old.returncode == 0 else None)
        path = Path(repo) / name
        after = _mutation_section(name, path.read_text(errors="replace") if path.is_file() else None)
        if before != after:
            changed.append(name)
    return changed
