"""Shared result types for adapters, and process handling for the tools they run."""

import hashlib
import os
import signal
import subprocess
from dataclasses import dataclass, field

DETECTED = "detected"
UNDETECTED = "undetected"

# Process groups of running tools, so a hook killed by Claude Code can take them down too.
ACTIVE_GROUPS = set()


@dataclass
class Mutant:
    path: str
    line: int
    mutator: str
    original: str
    replacement: str
    status: str  # DETECTED or UNDETECTED
    # Disambiguates mutants whose text is the same: the full original span, or the function.
    context: str = ""
    occurrence: int = 0

    @property
    def id(self):
        # Line numbers shift between runs, so the id hashes what changed, not where.
        key = "\0".join([
            self.path, self.mutator, self.original.strip(), self.replacement.strip(),
            self.context, str(self.occurrence),
        ])
        return hashlib.sha1(key.encode()).hexdigest()[:8]


def number_occurrences(mutants):
    """Give identical mutants (same file, text and context) distinct ids, in line order."""
    seen = {}
    for m in sorted(mutants, key=lambda m: (m.path, m.line)):
        key = (m.path, m.mutator, m.original.strip(), m.replacement.strip(), m.context)
        m.occurrence = seen.get(key, 0)
        seen[key] = m.occurrence + 1
    return mutants


@dataclass
class AdapterResult:
    mutants: list = field(default_factory=list)
    # failure: the tests themselves are inadequate (failing, or none cover the change) -> block.
    failure: str = None
    # error: the tool could not give a verdict -> warn the user. error_kind is one of
    # missing (not installed), timeout, crash (unrecognised exit), busy (another run holds the repo).
    error: str = None
    error_kind: str = None
    # Mutants on changed lines that the tool reported but did not run (existing disable comments).
    ignored: int = 0
    # Changed functions the tool cannot mutate at all, e.g. mutmut and decorated functions.
    unverified: list = field(default_factory=list)

    @property
    def cacheable(self):
        # Rerunning on the same code could succeed once the tool is installed or the repo is free.
        return self.error_kind not in ("missing", "busy")


def to_ranges(lines):
    ranges = []
    for n in sorted(lines):
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], n)
        else:
            ranges.append((n, n))
    return ranges


def tail(text, limit=1500):
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def kill_active():
    for pgid in list(ACTIVE_GROUPS):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        ACTIVE_GROUPS.discard(pgid)


def run_tree(cmd, cwd, env, timeout):
    """subprocess.run that kills the whole process group on timeout (vitest/pytest workers too)."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", start_new_session=True,
    )
    ACTIVE_GROUPS.add(proc.pid)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    finally:
        ACTIVE_GROUPS.discard(proc.pid)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
