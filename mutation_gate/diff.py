"""Changed-line extraction and suppression scanning."""

import hashlib
import re
import subprocess
from pathlib import Path

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Directories written by mutation tools or package managers; never part of the gate's input.
IGNORED_DIRS = {"mutants", ".stryker-tmp", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}

JS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs"}
TEST_DIRS = {"__tests__", "tests", "test"}

# Untracked files above this size are data, not source; reading them every turn is waste.
MAX_UNTRACKED_BYTES = 1_000_000

HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# Markers that silence mutants. Adding any of them is treated as tampering.
INLINE_SUPPRESSIONS = [
    re.compile(r"stryker\s+disable", re.IGNORECASE),
    re.compile(r"pragma:\s*no\s+mutate", re.IGNORECASE),
]
CONFIG_FILES = {"pyproject.toml", "setup.cfg", "mutmut.toml"}
CONFIG_SUPPRESSIONS = re.compile(
    r"\b(do_not_mutate|do_not_mutate_patterns|only_mutate|mutate_only_covered_lines|source_paths)\b"
)


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=check,
    )


def repo_root(path):
    result = _git(path, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        return None
    return str(Path(result.stdout.strip()).resolve())


def head_sha(repo):
    result = _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else EMPTY_TREE


def _ignored(rel):
    return any(part in IGNORED_DIRS for part in Path(rel).parts[:-1])


def _diff_text(repo, base):
    return _git(
        repo, "diff", "--no-color", "--no-ext-diff", "--no-renames", "--unified=0", base, "--"
    ).stdout


def _untracked(repo):
    out = _git(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout
    return sorted(p for p in out.split("\0") if p and not _ignored(p))


def _lines(text):
    """Split like a diff does: on newlines only (str.splitlines also splits on \f, \v, \u2028...)."""
    lines = text.split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def _changed_files(repo, base):
    # -z gives raw names: no C-quoting of non-ASCII, no tab suffix for names with spaces.
    out = _git(repo, "diff", "--name-only", "-z", "--no-renames", "--diff-filter=d", base, "--").stdout
    return [p for p in out.split("\0") if p and not _ignored(p)]


def _hunk_lines(patch):
    """{new line number: text} for added lines, walking hunks by their line counts."""
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


def added_lines(repo, base):
    """Map each changed file (repo-relative) to {new line number: text} since `base`."""
    added = {}
    for rel in _changed_files(repo, base):
        patch = _git(repo, "diff", "--no-color", "--no-ext-diff", "--no-renames", "--unified=0", base, "--", f":(literal){rel}").stdout
        lines = _hunk_lines(patch)
        if lines:
            added[rel] = lines
    for rel in _untracked(repo):
        try:
            if (Path(repo) / rel).stat().st_size > MAX_UNTRACKED_BYTES:
                continue
            text = (Path(repo) / rel).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        lines = _lines(text)
        if lines:
            added[rel] = {i: t for i, t in enumerate(lines, start=1)}
    return added


def fingerprint(repo, base):
    digest = hashlib.sha256(_diff_text(repo, base).encode())
    for rel in _untracked(repo):
        digest.update(rel.encode() + b"\0")
        try:
            path = Path(repo) / rel
            st = path.stat()
            if st.st_size > MAX_UNTRACKED_BYTES:
                digest.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
            else:
                digest.update(path.read_bytes())
        except OSError:
            pass
    return digest.hexdigest()


def classify(rel):
    """Return the adapter language for a mutable source file, or None."""
    p = Path(rel)
    parts = p.parts
    if any(part in IGNORED_DIRS for part in parts[:-1]):
        return None
    name = p.name
    if p.suffix in JS_EXTS:
        if name.endswith(".d.ts") or ".test." in name or ".spec." in name or ".config." in name:
            return None
        if any(part in TEST_DIRS for part in parts[:-1]):
            return None
        return "js"
    if p.suffix == ".py":
        if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
            return None
        if any(part in TEST_DIRS for part in parts[:-1]):
            return None
        return "py"
    return None


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
