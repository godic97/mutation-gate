"""Shared result types for adapters."""

import hashlib
import os
import signal
import subprocess
from dataclasses import dataclass, field

DETECTED = "detected"
UNDETECTED = "undetected"


@dataclass
class Mutant:
    path: str
    line: int
    mutator: str
    original: str
    replacement: str
    status: str  # DETECTED or UNDETECTED

    @property
    def id(self):
        # Line numbers shift between runs, so the id hashes what changed, not where.
        key = "\0".join([self.path, self.mutator, self.original.strip(), self.replacement.strip()])
        return hashlib.sha1(key.encode()).hexdigest()[:8]


@dataclass
class AdapterResult:
    mutants: list = field(default_factory=list)
    # failure: the tests themselves are inadequate (failing, or none cover the change) -> block.
    failure: str = None
    # error: the tool could not run (missing install, crash, timeout) -> warn Max, do not block.
    error: str = None
    # False when rerunning on the same code could succeed (e.g. Max installs the missing tool).
    cacheable: bool = True


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


def run_tree(cmd, cwd, env, timeout):
    """subprocess.run that kills the whole process group on timeout (vitest/pytest workers too)."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
