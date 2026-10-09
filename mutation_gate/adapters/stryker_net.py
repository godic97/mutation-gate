"""Stryker.NET adapter (C#, VSTest: xUnit, NUnit, MSTest)."""

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
from ..model import AdapterResult, number_occurrences, run_tree, tail
from .stryker import _parse, _tracked

DOTNET_DIRS = ["~/.dotnet"]
TOOL_DIRS = ["~/.dotnet/tools"]
# Stryker.NET 5 is built for .NET 10 only; 4.16.0 is the last release that installs on older SDKs.
LEGACY_STRYKER = "4.16.0"
DEFAULT_CHANNEL = 8
TEST_PROJECT = re.compile(r"Microsoft\.NET\.Test\.Sdk|MSTest\.Sdk|<IsTestProject>\s*true\s*<", re.IGNORECASE)
PROJECT_REF = re.compile(r"<ProjectReference\b[^>]*?\bInclude\s*=\s*\"([^\"]+)\"", re.IGNORECASE)
TARGET_FRAMEWORKS = re.compile(r"<TargetFrameworks?>([^<]+)<", re.IGNORECASE)
NET_MAJOR = re.compile(r"\bnet(\d+)\.\d")
SDK_LINE = re.compile(r"^(\d+)\.", re.MULTILINE)
BUILD_ERROR = re.compile(r": error [A-Z]+\d+:")
CSHARP_ERROR = re.compile(r": error CS\d+:")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Stryker.NET reads `mutate` entries as globs: any glob character in a path matches itself via `?`.
GLOB_CHARS = re.compile(r"[\[\]*?{}]")
# The final error panel is wrapped at 80 columns when the output is piped.
PANEL = "Stryker.NET failed to mutate your project"
PANEL_WIDTH = 80
FILTERED = "Removed by mutate filter"


def _read(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def _key(path):
    # Projects written on Windows reference each other in any case; macOS paths ignore it too.
    return os.path.normpath(str(path)).lower()


def _csprojs(repo):
    out = diff._git(repo, "ls-files", "-z", "-co", "--exclude-standard", "--", "*.csproj", check=False).stdout
    return [repo / p for p in sorted(out.split("\0")) if p and not diff._ignored(p)]


def _references(csproj):
    refs = set()
    for include in PROJECT_REF.findall(_read(csproj)):
        for part in include.split(";"):
            part = part.strip().replace("\\", "/")
            if part and "$(" not in part:
                refs.add(_key(csproj.parent / part))
    return refs


def _owner(repo, rel):
    """The project a source file belongs to: the nearest directory above it with a .csproj."""
    path = (repo / rel).parent
    while True:
        found = sorted(path.glob("*.csproj"))
        if found:
            return found[0]
        if path == repo or repo not in path.parents:
            return None
        path = path.parent


def _channel(projects):
    """The .NET major version the projects target (net8.0 -> 8), for install hints."""
    majors = [int(m) for p in projects for tf in TARGET_FRAMEWORKS.findall(_read(p)) for m in NET_MAJOR.findall(tf)]
    return max(majors, default=DEFAULT_CHANNEL)


def _sdk_major(dotnet, env):
    try:
        out = subprocess.run([dotnet, "--list-sdks"], capture_output=True, text=True, env=env, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return max((int(m) for m in SDK_LINE.findall(out)), default=None)


def _stryker_install(dotnet_cmd, major):
    cmd = f"{dotnet_cmd} tool install -g dotnet-stryker"
    if major is not None and major < 10:
        return f"{cmd} --version {LEGACY_STRYKER} (Stryker.NET 5 needs the .NET 10 SDK; {LEGACY_STRYKER} is the last release for .NET {major})"
    return cmd


def _missing_sdk(channel):
    stryker = _stryker_install("DOTNET_ROOT=~/.dotnet ~/.dotnet/dotnet", channel)
    return ("the .NET SDK is not installed (no dotnet on PATH or in ~/.dotnet). Install it in user scope, without "
            "touching shell profiles: curl -sSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh && "
            f"bash /tmp/dotnet-install.sh --channel {channel}.0 --install-dir ~/.dotnet ; then: {stryker}")


def _missing_stryker(dotnet, env):
    root = env["DOTNET_ROOT"]
    on_path = shutil.which("dotnet") == dotnet
    dotnet_cmd = "dotnet" if on_path else f"DOTNET_ROOT={shlex.quote(root)} {shlex.quote(dotnet)}"
    return f"dotnet-stryker is not installed. Install: {_stryker_install(dotnet_cmd, _sdk_major(dotnet, env))}"


def _env(dotnet):
    root = os.path.dirname(os.path.realpath(dotnet))
    return tools.env_with(
        [os.path.dirname(dotnet), root], DOTNET_ROOT=root, DOTNET_CLI_TELEMETRY_OPTOUT="1",
        DOTNET_NOLOGO="1", DOTNET_CLI_UI_LANGUAGE="en",
    )


def _refused(repo, binary):
    for path in {binary, os.path.realpath(binary)}:
        if _tracked(repo, path):
            return AdapterResult(error=f"not running {binary}: git tracks it, so the repo may have put it there", error_kind="missing")
    return None


def _pattern(path):
    return GLOB_CHARS.sub("?", str(path))


def _panel(output):
    """Lines of the final error panel, with the 80-column wrapping undone."""
    if PANEL not in output:
        return []
    lines, buf = [], ""
    for line in output[output.index(PANEL):].split("\n"):
        buf += line
        if line.endswith(" ") or len(line) >= PANEL_WIDTH:
            continue
        lines.append(buf.strip())
        buf = ""
    if buf.strip():
        lines.append(buf.strip())
    return lines[1:]


def _short(text, repo):
    return text.replace(f"{repo}{os.sep}", "")


def _failing_tests(output, repo):
    lines = _panel(output)
    marker = next((i for i, l in enumerate(lines) if "failing tests" in l), -1)
    return _short("\n".join(l for l in lines[marker + 1 :] if l), repo)


def _build_errors(output, repo):
    # The log above the panel has the compiler lines unwrapped; the panel repeats them wrapped.
    log = output.split(PANEL)[0].splitlines()
    seen = []
    for line in log if any(BUILD_ERROR.search(l) for l in log) else _panel(output):
        if BUILD_ERROR.search(line):
            line = re.sub(r"\s*\[[^\]]+\.\w+proj\]\s*$", "", line.strip())
            if line not in seen:
                seen.append(line)
    return _short("\n".join(seen), repo)


def _rekey(report, repo, project_dir):
    """The report's files under repo-relative paths, without mutants outside the mutate patterns."""
    files = {}
    for key, info in report.get("files", {}).items():
        path = Path(key) if os.path.isabs(key) else project_dir / key
        rel = os.path.relpath(os.path.realpath(path), repo)
        mutants = [m for m in info.get("mutants", []) if m.get("statusReason") != FILTERED]
        files[rel] = {**info, "mutants": mutants}
    return {"files": files}


def _run_project(repo, stryker, env, csproj, tests, changed, timeout):
    work = store.work_dir(hashlib.sha1(str(csproj).encode()).hexdigest()[:12] + "-stryker-net")
    # An empty config of our own, so a stryker-config.json in the project cannot narrow the run.
    config = work / "stryker-config.json"
    config.write_text(json.dumps({"stryker-config": {}}))
    report = work / "reports" / "mutation-report.json"
    if report.exists():
        report.unlink()
    # Whole files, not changed lines: _parse keeps the mutants that overlap the changed lines.
    cmd = [stryker, "--config-file", str(config), "--reporter", "json", "--output", str(work),
           "--skip-version-check", "--break-on-initial-test-failure"]
    for test in tests:
        cmd += ["--test-project", str(test)]
    for rel in sorted(changed):
        cmd += ["--mutate", _pattern(repo / rel)]
    proc = run_tree(cmd, cwd=csproj.parent, env=env, timeout=timeout)
    output = ANSI.sub("", proc.stdout + proc.stderr)
    flat = " ".join(output.split())

    if "Initial testrun has failing tests" in flat:
        return AdapterResult(failure="the tests are failing (Stryker.NET initial test run):\n" + tail(_failing_tests(output, repo), 800))
    if "No test result reported" in flat:
        names = ", ".join(os.path.relpath(t, repo) for t in tests)
        return AdapterResult(failure=f"no test runs in {names} (Stryker.NET: No test result reported)")
    if "Initial build of targeted project failed" in flat or "Initial build failed" in flat:
        errors = _build_errors(output, repo)
        if CSHARP_ERROR.search(errors):
            return AdapterResult(failure="the code does not compile (Stryker.NET initial build):\n" + tail(errors, 800))
        return AdapterResult(error=f"the build failed (Stryker.NET initial build):\n{tail(errors or output)}", error_kind="crash")
    if proc.returncode != 0 or not report.exists():
        return AdapterResult(error=f"Stryker.NET failed (exit {proc.returncode}):\n{tail(_short(output, repo))}", error_kind="crash")

    data = _rekey(json.loads(report.read_text()), repo, csproj.parent)
    report.unlink()
    mutants, ignored = _parse(data, changed, "")
    unverified = [f"changed file is not compiled by {os.path.relpath(csproj, repo)}: {rel}"
                  for rel in sorted(set(changed) - set(data["files"]))]
    return AdapterResult(mutants=mutants, ignored=ignored, unverified=unverified)


def run(repo, changed, budget):
    repo = Path(repo).resolve()
    deadline = time.monotonic() + budget
    merged = AdapterResult()

    csprojs = _csprojs(repo)
    test_projects = [p for p in csprojs if TEST_PROJECT.search(_read(p))]
    test_keys = {_key(p) for p in test_projects}
    groups = {}
    for rel, lines in sorted(changed.items()):
        csproj = _owner(repo, rel)
        if csproj is None:
            merged.unverified.append(f"changed file is not in any .csproj: {rel}")
        elif _key(csproj) in test_keys:
            merged.unverified.append(f"changed file belongs to test project {os.path.relpath(csproj, repo)}, not mutated: {rel}")
        else:
            groups.setdefault(csproj, {})[rel] = lines
    if not groups:
        return merged
    covering = {csproj: [t for t in test_projects if _key(csproj) in _references(t)] for csproj in groups}

    dotnet = tools.find("dotnet", DOTNET_DIRS)
    if dotnet is None:
        involved = [*groups, *(t for tests in covering.values() for t in tests)]
        merged.error, merged.error_kind = _missing_sdk(_channel(involved)), "missing"
        return merged
    refused = _refused(repo, dotnet)
    if refused:
        return refused
    env = _env(dotnet)
    stryker = tools.find("dotnet-stryker", TOOL_DIRS)
    if stryker is None:
        merged.error, merged.error_kind = _missing_stryker(dotnet, env), "missing"
        return merged
    refused = _refused(repo, stryker)
    if refused:
        return refused

    for csproj in sorted(groups):
        tests = covering[csproj]
        if not tests:
            result = AdapterResult(failure=f"no test project references {os.path.relpath(csproj, repo)} "
                                           "(a .csproj with Microsoft.NET.Test.Sdk and a <ProjectReference> to it)")
        else:
            try:
                remaining = max(1, int(deadline - time.monotonic()))
                result = _run_project(repo, stryker, env, csproj, tests, groups[csproj], remaining)
            except subprocess.TimeoutExpired:
                result = AdapterResult(error=f"Stryker.NET did not finish within {budget}s (incomplete run)", error_kind="timeout")
        merged.mutants += result.mutants
        merged.ignored += result.ignored
        merged.unverified += result.unverified
        merged.failure = merged.failure or result.failure
        if result.error and not merged.error:
            merged.error, merged.error_kind = result.error, result.error_kind
    number_occurrences(merged.mutants)
    merged.mutants.sort(key=lambda m: (m.path, m.line, m.mutator, m.replacement))
    return merged
