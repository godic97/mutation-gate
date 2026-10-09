"""PIT (pitest) adapter for Java and Kotlin, run through Maven without touching the project's pom."""

import hashlib
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import diff, store, tools
from ..model import DETECTED, UNDETECTED, AdapterResult, Mutant, number_occurrences, run_tree, tail

PIT_VERSION = "1.30.0"
JUNIT5_PLUGIN = "org.pitest:pitest-junit5-plugin:1.2.3"
DEPENDENCY_PLUGIN = "org.apache.maven.plugins:maven-dependency-plugin:3.8.1"
MAVEN_DIRS = ["~/.local/opt/apache-maven-*/bin", "~/.local/opt/maven*/bin"]
GRADLE_FILES = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")

STATUS = {
    "KILLED": DETECTED, "TIMED_OUT": DETECTED, "MEMORY_ERROR": DETECTED, "RUN_ERROR": DETECTED,
    "SURVIVED": UNDETECTED, "NO_COVERAGE": UNDETECTED,
}
DROPPED = {"NON_VIABLE"}  # mutants the JVM rejects: say nothing about the tests

PACKAGE = re.compile(r"^\s*package\s+([\w.`]+)", re.MULTILINE)
TYPE_DECL = re.compile(r"\b(?:class|interface|enum|record|object)\s+([A-Za-z_$][\w$]*)")
JVM_NAME = re.compile(r"@file\s*:\s*JvmName\(\s*\"([\w$]+)\"\s*\)")
# Signs that the tests run on the JUnit Platform, which PIT needs pitest-junit5-plugin for.
PLATFORM_SOURCE = re.compile(r"\b(org\.junit\.jupiter|org\.junit\.platform|io\.kotest)\b")
PLATFORM_POM = re.compile(r"junit-jupiter|junit-bom|junit-platform|kotlin-test-junit5|spring-boot-starter-test")
SOURCE_SCAN_LIMIT = 400

NO_PLUGIN = "correctly installed the pitest plugin"
MINION_CRASH = "minion exited abnormally"
TESTS_RED = "did not pass without mutation"
FAILING_TEST = re.compile(r"Description \[testClass=([^,\]]+), name=(.*)\]\s*$")
FAILED_GOAL = re.compile(r"Failed to execute goal (\S+?):(compile|testCompile|test-compile) ")
SECTION = re.compile(r"^\[INFO\] --- (\S+) .*@ (\S+) ---\s*$", re.MULTILINE)
SKIP_REASON = re.compile(r"^\[INFO\]\s+- (.+)$", re.MULTILINE)
ERROR_NOISE = re.compile(
    r"^\[ERROR\]\s*$|To see the full stack trace|Re-run Maven|For more information about the errors"
    r"|\[Help \d\]|Please copy and paste the information|^\[ERROR\] (VM|Vendor|Version|Uptime|Input|BootClassPath)"
    r"|^\[ERROR\]\s+\d+ : "
)


def _install_hint():
    return (
        "Maven not found. Install it without sudo: download apache-maven-3.9.x-bin.tar.gz from "
        "https://maven.apache.org/download.cgi and unpack it into ~/.local/opt/ (mutation-gate looks in "
        "~/.local/opt/apache-maven-*/bin), or add the Maven wrapper to the project (mvn wrapper:wrapper)"
    )


def _java_hint():
    return (
        "no JDK found. Set JAVA_HOME, or unpack a JDK (for example Temurin 21 from https://adoptium.net) "
        "into ~/.local/opt/ (mutation-gate looks in ~/.local/opt/jdk*)"
    )


def _gradle_hint(files):
    return (
        "Gradle projects need the info.solidsoft.pitest plugin: mutation-gate runs PIT only through Maven "
        f"(pom.xml) so far, so these files were not mutation-tested: {', '.join(files)}. "
        "Run `./gradlew pitest` with gradle-pitest-plugin yourself"
    )


def _java_home():
    home = tools.java_home()
    if home is None and os.path.exists("/usr/libexec/java_home"):  # macOS-installed JDKs
        proc = subprocess.run(["/usr/libexec/java_home"], capture_output=True, text=True, check=False)
        home = proc.stdout.strip() if proc.returncode == 0 else None
    return home if home and os.path.isfile(os.path.join(home, "bin", "java")) else None


def _inside(path, base):
    return path == base or base in path.parents


def _tracked(repo, path):
    path = Path(os.path.abspath(path))
    for candidate in dict.fromkeys([path, path.resolve()]):
        if _inside(candidate, repo):
            rel = os.path.relpath(candidate, repo)
            if diff._git(repo, "ls-files", "--error-unmatch", "--", f":(literal){rel}", check=False).returncode == 0:
                return True
    return False


def _walk_up(repo, rel):
    """Directories from the file's own up to the repo root."""
    path = (repo / rel).parent
    while _inside(path, repo):
        yield path
        if path == repo:
            return
        path = path.parent


def _module_dir(repo, rel):
    return next((d for d in _walk_up(repo, rel) if (d / "pom.xml").is_file()), None)


def _gradle_dir(repo, rel):
    return next((d for d in _walk_up(repo, rel) if any((d / n).is_file() for n in GRADLE_FILES)), None)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _pom(path):
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None


def _submodules(pom_path):
    root = _pom(pom_path)
    out = set()
    for el in root.iter() if root is not None else ():
        if _local(el.tag) in ("module", "subproject") and el.text and el.text.strip():
            target = pom_path.parent / el.text.strip()
            out.add(os.path.normpath(target.parent if target.suffix == ".xml" else target))
    return out


def _pom_pitest_notes(root):
    """Notes for poms under `root` that configure pitest-maven: that configuration beats ours."""
    notes = []
    for pom in sorted(Path(root).rglob("pom.xml")):
        if "target" in pom.relative_to(root).parts:
            continue
        try:
            tree = ET.parse(pom)
        except (ET.ParseError, OSError):
            continue
        for plugin in tree.iter():
            if plugin.tag.rsplit("}", 1)[-1] != "plugin":
                continue
            children = {c.tag.rsplit("}", 1)[-1]: c for c in plugin}
            artifact = children.get("artifactId")
            if artifact is not None and (artifact.text or "").strip() == "pitest-maven" and "configuration" in children:
                notes.append(f"{pom.relative_to(root)} configures pitest-maven; its settings override mutation-gate's "
                             "and can change which mutants run")
                break
    return notes


def _reactor_root(repo, module):
    """Highest pom above `module` that aggregates it through <modules>, so sibling modules get built."""
    root = module
    for parent in module.parents:
        if not _inside(parent, repo):
            break
        pom = parent / "pom.xml"
        if pom.is_file() and os.path.normpath(root) in _submodules(pom):
            root = parent
    return root


def _artifact_id(module):
    root = _pom(module / "pom.xml")
    if root is None:
        return None
    return next((el.text.strip() for el in root if _local(el.tag) == "artifactId" and el.text), None)


def _targets(text, rel):
    """(package, PIT class globs) for one source file: its classes and their nested classes."""
    match = PACKAGE.search(text)
    package = match.group(1).replace("`", "") if match else ""
    stem = Path(rel).stem
    names = {stem, *TYPE_DECL.findall(text)}
    if rel.endswith(".kt"):
        names |= {f"{stem}Kt", *JVM_NAME.findall(text)}  # top-level functions live in FileKt
    prefix = f"{package}." if package else ""
    globs = []
    for name in sorted(names):
        globs += [f"{prefix}{name}", f"{prefix}{name}$*"]
    return package, globs


def _uses_platform(root, modules):
    """True when the tests run on the JUnit Platform (JUnit 5/6, kotest...): PIT needs pitest-junit5-plugin."""
    poms = {root / "pom.xml"}
    for module in modules:
        poms |= {d / "pom.xml" for d in [module, *module.parents] if _inside(d, root)}
    texts = [p.read_text(errors="replace") for p in poms if p.is_file()]
    if any("pitest-junit5-plugin" in t for t in texts):
        return False  # the project's own pitest configuration brings it
    if any(PLATFORM_POM.search(t) for t in texts):
        return True
    scanned = 0
    for module in modules:
        for test_set in sorted(diff.JVM_TEST_SETS):
            for path in (module / "src" / test_set).rglob("*"):
                if path.suffix not in (".java", ".kt"):
                    continue
                scanned += 1
                if scanned > SOURCE_SCAN_LIMIT:
                    return False
                try:
                    if PLATFORM_SOURCE.search(path.read_text(errors="replace")):
                        return True
                except OSError:
                    continue
    return False


def _errors(output, limit=1500):
    lines = [l for l in output.splitlines() if (l.startswith("[ERROR]") or "PIT >> SEVERE" in l) and not ERROR_NOISE.search(l)]
    return tail("\n".join(lines) or output, limit)


def _failing_tests(output):
    names = []
    for line in output.splitlines():
        match = FAILING_TEST.search(line)
        if match:
            method = re.search(r"\[method:([^\]]+)\]", match.group(2))
            name = f"{match.group(1)} {method.group(1) if method else match.group(2)}"
            if name not in names:
                names.append(name)
    return names


def _section(output, artifact):
    """Output of the pitest goal for one module."""
    heads = list(SECTION.finditer(output))
    for i, head in enumerate(heads):
        if head.group(1).startswith("pitest") and head.group(2) == artifact:
            end = heads[i + 1].start() if i + 1 < len(heads) else len(output)
            return output[head.end():end]
    return ""


def _parse(report, sources, texts, changed):
    """Mutants on changed lines, and the count PIT left unfinished.

    PIT names the class and its source file's base name; with the package that finds the repo file.
    """
    mutants, unfinished = [], 0
    for el in ET.parse(report).getroot().iter("mutation"):
        status = el.get("status", "")
        if status in DROPPED:
            continue
        cls = el.findtext("mutatedClass", "")
        package = cls.rsplit(".", 1)[0] if "." in cls else ""
        rel = sources.get((package, el.findtext("sourceFile", "")))
        line = int(el.findtext("lineNumber", "0") or 0)
        if rel is None or line not in changed[rel]:
            continue
        if status not in STATUS:  # NOT_STARTED, STARTED: the run stopped early
            unfinished += 1
            continue
        lines = texts[rel]
        original = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        mutants.append(Mutant(
            path=rel, line=line, mutator=el.findtext("mutator", "?").rsplit(".", 1)[-1],
            original=original, replacement=el.findtext("description", ""), status=STATUS[status],
            context=el.findtext("mutatedMethod", ""),
        ))
    return mutants, unfinished


def _junit5_jar(mvn, root, lib, env, timeout):
    """Fetch pitest-junit5-plugin through Maven (once per project) into our own work dir."""
    name = JUNIT5_PLUGIN.split(":")[1] + "-" + JUNIT5_PLUGIN.split(":")[2] + ".jar"
    jar = lib / name
    if jar.is_file():
        return jar, None
    proc = run_tree(
        [*mvn, "-B", "-ntp", "-q", "-N", f"{DEPENDENCY_PLUGIN}:copy", f"-Dartifact={JUNIT5_PLUGIN}",
         f"-DoutputDirectory={lib}"],
        cwd=root, env=env, timeout=timeout,
    )
    if proc.returncode != 0 or not jar.is_file():
        return None, f"Maven could not fetch {JUNIT5_PLUGIN} (exit {proc.returncode}):\n{_errors(proc.stdout + proc.stderr)}"
    return jar, None


def _run_reactor(repo, root, modules, changed, deadline, mvn, env):
    work = store.work_dir(hashlib.sha1(str(root).encode()).hexdigest()[:12] + "-pit")
    reports = work / "reports"
    sources, texts, globs = {}, {}, []
    for module, files in modules.items():
        sources[module] = {}
        for rel in sorted(files):
            try:
                text = (repo / rel).read_text(errors="replace")
            except OSError as exc:
                return AdapterResult(error=f"could not read {rel}: {exc}", error_kind="crash")
            package, file_globs = _targets(text, rel)
            sources[module][(package, Path(rel).name)] = rel
            texts[rel] = text.split("\n")
            globs += file_globs

    def left():
        return max(1, int(deadline - time.monotonic()))

    use_jar = _uses_platform(root, list(modules))
    selected = [] if root in modules else ["-pl", ",".join(sorted(os.path.relpath(m, root) for m in modules)), "-am"]
    threads = max(1, min(4, (os.cpu_count() or 2) // 2))

    def attempt(with_jar):
        extra = []
        if with_jar:
            jar, error = _junit5_jar(mvn, root, work / "lib", env, min(left(), 180))
            if error:
                offline = any(s in error for s in ("could not be resolved", "Could not transfer", "offline"))
                kind = "missing" if offline else "crash"
                return None, AdapterResult(error=error, error_kind=kind)
            extra.append(f"-DadditionalClasspathElements={jar}")
        shutil.rmtree(reports, ignore_errors=True)
        cmd = [
            *mvn, "-B", "-ntp", "test-compile", f"org.pitest:pitest-maven:{PIT_VERSION}:mutationCoverage", *selected,
            f"-DtargetClasses={','.join(dict.fromkeys(globs))}", "-DtargetTests=*", "-DoutputFormats=XML",
            "-DtimestampedReports=false", "-DfailWhenNoMutations=false", f"-Dthreads={threads}",
            # One report directory per module: Maven fills in each module's basedir.
            f"-DreportsDirectory={reports}${{project.basedir}}", *extra,
        ]
        proc = run_tree(cmd, cwd=root, env=env, timeout=left())
        return proc, None

    try:
        proc, problem = attempt(use_jar)
        if problem:
            return problem
        output = proc.stdout + proc.stderr
        # A wrong guess about the test framework: PIT names it, so try the other way once.
        if (not use_jar and NO_PLUGIN in output) or (use_jar and MINION_CRASH in output):
            retry, problem = attempt(not use_jar)
            if problem:
                return problem
            retry_output = retry.stdout + retry.stderr
            if not ((use_jar and NO_PLUGIN in retry_output) or (not use_jar and MINION_CRASH in retry_output)):
                proc, output = retry, retry_output
        return _verdict(repo, modules, changed, sources, texts, reports, proc.returncode, output)
    finally:
        shutil.rmtree(reports, ignore_errors=True)


def _reports_by_module(reports):
    """mutations.xml files keyed by module dir: each lies at <reports><module basedir>/mutations.xml."""
    found = {}
    for report in reports.rglob("mutations.xml") if reports.is_dir() else ():
        found[Path("/", report.parent.relative_to(reports)).resolve()] = report
    return found


def _verdict(repo, modules, changed, sources, texts, reports, code, output):
    if TESTS_RED in output:
        failing = _failing_tests(output)
        detail = "\n".join(failing) if failing else _errors(output, 800)
        return AdapterResult(failure="the tests are failing (PIT clean run):\n" + tail(detail, 800))
    goal = FAILED_GOAL.search(output)
    if goal:
        if goal.group(2) == "compile":
            return AdapterResult(error="the code does not compile (mvn test-compile):\n" + _errors(output), error_kind="crash")
        return AdapterResult(failure="the tests do not compile (mvn test-compile):\n" + _errors(output, 800))
    if code != 0 and "org.pitest" in output and "could not be resolved" in output:
        return AdapterResult(error=f"Maven could not download PIT {PIT_VERSION}; check the network or Maven settings:\n"
                                   + _errors(output), error_kind="missing")

    result = AdapterResult()
    found = _reports_by_module(reports)
    for module in sorted(modules):
        name = os.path.relpath(module, repo) if module != repo else "the project"
        report = found.get(module.resolve())
        if report:
            try:
                mutants, unfinished = _parse(report, sources[module], texts, changed)
            except ET.ParseError as exc:
                return AdapterResult(error=f"could not read PIT's report for {name} ({exc}):\n{_errors(output)}", error_kind="crash")
            result.mutants += mutants
            if unfinished and not result.error:
                result.error, result.error_kind = f"PIT did not finish {unfinished} mutant(s) in {name} (incomplete run)", "crash"
            continue
        if code != 0:
            return AdapterResult(error=f"PIT failed (exit {code}):\n{_errors(output)}", error_kind="crash")
        section = _section(output, _artifact_id(module) or "") or output
        if "Skipping project because" in section:
            reasons = "; ".join(SKIP_REASON.findall(section)) or "no reason given"
            if "no tests" in reasons:
                return AdapterResult(failure=f"no test runs in {name} (PIT skipped it: {reasons})")
            return AdapterResult(error=f"PIT skipped {name}: {reasons}", error_kind="crash")
        if "No mutations found" in section:
            continue  # nothing mutable on these lines (interfaces, constants...)
        return AdapterResult(
            error=f"PIT wrote no XML report for {name}; does the pom configure pitest-maven with its own "
                  f"outputFormats or reportsDirectory?\n{_errors(output)}",
            error_kind="crash",
        )
    return result


def run(repo, changed, budget):
    repo = Path(repo).resolve()
    deadline = time.monotonic() + budget
    modules, gradle, orphans = {}, [], []
    for rel in sorted(changed):
        module = _module_dir(repo, rel)
        if module is None:
            (gradle if _gradle_dir(repo, rel) else orphans).append(rel)
        elif not _inside(repo / rel, module / "target"):  # generated sources are not the change
            modules.setdefault(module, {})[rel] = changed[rel]

    merged = AdapterResult()
    if gradle:
        merged.error, merged.error_kind = _gradle_hint(gradle), "missing"
    elif orphans:
        merged.error, merged.error_kind = f"no pom.xml found for {', '.join(orphans)}; PIT runs on Maven projects", "missing"
    if not modules:
        return merged

    java_home = _java_home()
    if java_home is None:
        return AdapterResult(error=_java_hint(), error_kind="missing")
    if _tracked(repo, os.path.join(java_home, "bin", "java")):
        return AdapterResult(error=f"not running {java_home}/bin/java: git tracks it, so the repo may have put it there", error_kind="missing")
    env = tools.env_with([os.path.join(java_home, "bin")], JAVA_HOME=java_home)

    reactors = {}
    for module, files in modules.items():
        reactors.setdefault(_reactor_root(repo, module), {})[module] = files
    for root in sorted(reactors):
        merged.unverified += _pom_pitest_notes(root)
        mvn, error = _maven(repo, root)
        if error:
            result = AdapterResult(error=error, error_kind="missing")
        else:
            try:
                result = _run_reactor(repo, root, reactors[root], changed, deadline, mvn, env)
            except subprocess.TimeoutExpired:
                result = AdapterResult(error=f"PIT did not finish within {budget}s (incomplete run)", error_kind="timeout")
        merged.mutants += result.mutants
        merged.failure = merged.failure or result.failure
        if result.error and not merged.error:
            merged.error, merged.error_kind = result.error, result.error_kind
    number_occurrences(merged.mutants)
    merged.mutants.sort(key=lambda m: (m.path, m.line, m.mutator, m.replacement))
    return merged


def _maven(repo, root):
    """Command for the project's Maven wrapper, else mvn on PATH or under ~/.local/opt."""
    for base in dict.fromkeys([root, repo]):
        wrapper = base / "mvnw"
        if wrapper.is_file() and (base / ".mvn" / "wrapper" / "maven-wrapper.properties").is_file():
            return ([str(wrapper)] if os.access(wrapper, os.X_OK) else ["sh", str(wrapper)]), None
    mvn = tools.find("mvn", MAVEN_DIRS)
    if mvn is None:
        return None, _install_hint()
    if _tracked(repo, mvn):
        return None, f"not running {mvn}: git tracks it, so the repo may have put it there"
    return [mvn], None
