"""Stryker4s adapter (sbt).

The user's build is never edited. sbt loads the sbt-stryker4s plugin, and a small command that
maps changed files to sbt projects, from a private global plugins directory in the gate's work
dir (-Dsbt.global.plugins). Everything the run is told comes from system properties, session
settings and the `stryker` command line. The user's ~/.sbt global settings, credentials and boot
directory stay in use; only their global plugins are not loaded during the run. Stryker4s writes its report and
sandbox under each project's target/; the gate deletes both.
"""

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

from .. import diff, store, tools
from ..model import AdapterResult, number_occurrences, run_tree, tail
from .stryker import _parse

STRYKER4S_VERSION = "1.1.1"
MIN_SBT = (1, 11, 2)  # Stryker4s 1.1.1 refuses older sbt versions
SBT_RUNNER_VERSION = "2.0.10"  # the sbt runner the install hint downloads; it launches 1.x and 2.x builds
SBT_DIRS = ["~/.local/opt/sbt/bin", "~/.local/opt/sbt-*/bin", "~/.sdkman/candidates/sbt/current/bin"]
# Below this, sbt cannot even load a build: report a timeout instead of starting it.
MIN_START_SECONDS = 20
# Kept back from the budget for cleanup and report parsing after sbt exits or is killed.
CLEANUP_RESERVE = 3
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
REQUEST_PROP = "sbt.mutationgate.request"  # sbt.* properties are not forwarded to the test JVMs
MANIFEST_PROP = "sbt.mutationgate.manifest"
FAILED_TEST_LINE = re.compile(r"==> X |\*\*\* FAILED \*\*\*|Failed tests:|^\[error\]\s+\S+\.\S+$|Test .* failed|Failed: Total")

PLUGINS_SBT = f'addSbtPlugin("io.stryker-mutator" % "sbt-stryker4s" % "{STRYKER4S_VERSION}")\n'

# Compiled by sbt as a global plugin (Scala 2.12 for sbt 1, Scala 3 for sbt 2; kept to the common
# subset). `mutationGate` reads the changed files from -Dsbt.mutationgate.request, runs
# Stryker4s once per sbt project that owns some of them, and writes a manifest: which project ran
# where, the stryker4s-* entries that existed in its target/ before, and how each run ended.
#
# The Stryker4s config is the gate's own. Stryker4s has no option to read its config file from
# elsewhere (it always reads ./stryker4s.conf), and it ranks sources: command line, then
# stryker4s.conf, then sbt settings. So the command passes the gate's values on the `stryker`
# command line, and also sets them as session settings, which replace the project's stryker*
# settings. What neither can undo is reported in the manifest: stryker4s.conf keys the command line
# cannot reset (excluded-mutations, legacy-test-runner, debug.debug-test-runner), base-dir when the
# base path has a space, and a strykerScalaDialect the build sets.
PLUGIN_SCALA = r'''// Written by mutation-gate into its own work dir; not part of any project.
import java.io.{File, FileOutputStream, OutputStreamWriter, PrintWriter}
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.util.concurrent.TimeUnit

import scala.concurrent.duration.FiniteDuration
import scala.util.Try

import com.typesafe.config.ConfigFactory
import sbt._
import sbt.internal.util.FilePosition
import sbt.Keys._
import stryker4s.sbt.Stryker4sPlugin
import stryker4s.sbt.Stryker4sPlugin.autoImport._

object MutationGatePlugin extends AutoPlugin {
  override def requires = Stryker4sPlugin
  override def trigger = allRequirements
  override def globalSettings = Seq(commands += mutationGate)

  private def propFile(name: String): File = sys.props.get(name) match {
    case Some(p) => new File(p).getCanonicalFile
    case None    => throw new MessageOnlyException("mutation-gate: -D" + name + " is not set")
  }

  private def inside(f: File, dir: File): Boolean = {
    val d = dir.getCanonicalFile.toPath
    f.toPath.startsWith(d) && f.toPath != d
  }

  private def relative(base: File, f: File): String =
    base.toPath.relativize(f.toPath).toString.replace(File.separatorChar, '/')

  // Stryker4s reads `mutate` and `files` as java.nio globs relative to its base directory.
  private def globEscape(rel: String): String =
    rel.map(c => if ("\\*?{}[]!".indexOf(c.toInt) >= 0) "\\" + c else c.toString).mkString

  // Settings the project itself defines for `label`: (file relative to the build root) per definition.
  private def definedInBuild(extracted: Extracted, root: File, label: String): Seq[String] =
    extracted.structure.settings.filter(_.key.key.label == label).flatMap { s =>
      s.pos match {
        case p: FilePosition =>
          val f0 = new File(p.path)
          val f = (if (f0.isAbsolute) f0 else new File(root, p.path)).getCanonicalFile
          if (f.isFile && inside(f, root)) Seq(relative(root, f)) else Seq.empty[String]
        case _ => Seq.empty[String]
      }
    }.distinct

  // The stryker task splits its input on whitespace; `?` matches the space in a glob.
  private def cliGlob(glob: String): String = glob.map(c => if (Character.isWhitespace(c)) '?' else c)

  // stryker4s.conf keys the stryker command line cannot reset, when they are set to something.
  private def fileOnlySettings(root: File, baseHasSpace: Boolean): Seq[String] = {
    val f = new File(root, "stryker4s.conf")
    val conf = if (f.isFile) Try(ConfigFactory.parseFile(f).getConfig("stryker4s")).toOption else None
    conf.toSeq.flatMap { c =>
      def on(key: String): Boolean = c.hasPath(key) &&
        Try(c.getBoolean(key)).orElse(Try(!c.getList(key).isEmpty)).getOrElse(true)
      Seq("excluded-mutations", "legacy-test-runner", "debug.debug-test-runner").filter(on) ++
        (if (baseHasSpace && c.hasPath("base-dir")) Seq("base-dir") else Seq.empty[String])
    }
  }

  // The gate's Stryker4s config for one project (Stryker4s defaults, json report, no break threshold).
  private def owned(ref: ProjectRef, mutate: Seq[String], sourceGlobs: Seq[String]): Seq[Def.Setting[_]] = Seq(
    ref / strykerMutate := mutate,
    ref / strykerFiles := sourceGlobs,
    ref / strykerReporters := Seq("json"),
    ref / strykerExcludedMutations := Seq.empty[String],
    ref / strykerTestFilter := Seq.empty[String],
    ref / strykerTimeout := FiniteDuration(5L, TimeUnit.SECONDS),
    ref / strykerTimeoutFactor := 1.5,
    ref / strykerLegacyTestRunner := false,
    ref / strykerThresholdsHigh := 80,
    ref / strykerThresholdsLow := 60,
    ref / strykerThresholdsBreak := 0,
    ref / strykerStaticTmpDir := false,
    ref / strykerCleanTmpDir := true,
    ref / strykerOpenReport := false,
    ref / strykerDebugLogTestRunnerStdout := false,
    ref / strykerDebugDebugTestRunner := false
  )

  private def strykerEntries(dir: File): String = {
    val names = Option(dir.list()).map(_.toSeq).getOrElse(Seq.empty[String])
    names.filter(_.startsWith("stryker4s-")).sorted.mkString("/")
  }

  private def oneLine(s: String): String = s.replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')

  private def causes(t: Throwable): Seq[Throwable] = t match {
    case inc: Incomplete => Incomplete.allExceptions(inc).toSeq
    case other           => Seq(other)
  }

  lazy val mutationGate: Command = Command.command("mutationGate") { initial =>
    val request = new String(Files.readAllBytes(propFile("sbt.mutationgate.request").toPath), StandardCharsets.UTF_8)
    val files = request.split("\n").toSeq.map(_.trim).filter(_.nonEmpty).map(p => new File(p).getCanonicalFile)
    val out = new PrintWriter(
      new OutputStreamWriter(new FileOutputStream(propFile("sbt.mutationgate.manifest")), StandardCharsets.UTF_8), true)
    var state = initial
    var failed = false
    try {
      val extracted = Project.extract(initial)
      val current = extracted.currentRef
      val root = new File(current.build).getCanonicalFile
      val refs = extracted.structure.allProjectRefs.filter(_.build == current.build)
        .sortBy(r => (r.project != current.project, r.project))
      def owner(f: File): Option[(String, File)] = refs.iterator.flatMap { ref =>
        val sources = extracted.getOpt(ref / Compile / unmanagedSourceDirectories).getOrElse(Seq.empty[File])
        extracted.getOpt(ref / strykerBaseDir).map(_.getCanonicalFile)
          .filter(base => inside(f, base) && sources.exists(d => inside(f, d)))
          .map(base => (ref.project, base))
      }.toSeq.headOption
      val owners = files.map(f => (f, owner(f)))
      owners.foreach { case (f, o) => if (o.isEmpty) out.println("unmapped\t" + f) }
      val groups = owners.collect { case (f, Some(o)) => (o, f) }.groupBy(_._1).toSeq.sortBy(_._1._1)
      groups.foreach { case ((project, base), pairs) =>
        if (!failed) {
          val ref = ProjectRef(current.build, project)
          val target = new File(base, "target")
          out.println(Seq("project", project, base.toString, strykerEntries(target),
            strykerEntries(new File(target, "stryker4s-report"))).mkString("\t"))
          val mutate = pairs.map(_._2).map(f => relative(base, f))
          mutate.foreach(m => out.println("file\t" + project + "\t" + m))
          // As the plugin's own default: the project's source directories under the base directory.
          val sourceGlobs = extracted.getOpt(ref / Compile / unmanagedSourceDirectories).getOrElse(Seq.empty[File])
            .map(_.getCanonicalFile).filter(d => inside(d, base)).map(d => globEscape(relative(base, d)) + "/**")
          val baseHasSpace = base.toString.exists(Character.isWhitespace)
          definedInBuild(extracted, root, "strykerScalaDialect")
            .foreach(where => out.println("projectsetting\t" + project + "\tstrykerScalaDialect\t" + where))
          fileOnlySettings(root, baseHasSpace)
            .foreach(key => out.println("projectsetting\t" + project + "\t" + key + "\tstryker4s.conf"))
          try {
            val configured = Project.extract(state).appendWithSession(owned(ref, mutate.map(globEscape), sourceGlobs), state)
            // The dialect the plugin derives from scalaVersion, so stryker4s.conf cannot replace it.
            val dialect = Try(Project.extract(configured).runTask(ref / strykerScalaDialect, configured)._2).toOption
            val args = mutate.map(m => " --mutate " + cliGlob(globEscape(m))).mkString +
              sourceGlobs.map(g => " --files " + cliGlob(g)).mkString +
              " --reporters json --test-filter * --timeout 5s --timeout-factor 1.5" +
              " --thresholds.high 80 --thresholds.low 60 --thresholds.break 0" +
              dialect.map(d => " --scala-dialect " + d.toString.toLowerCase).getOrElse("") +
              (if (baseHasSpace) "" else " --base-dir " + base)
            state = Project.extract(configured).runInputTask(ref / stryker, args, configured)._1
            out.println("done\t" + project)
          } catch {
            case e: Throwable =>
              failed = true
              val found = causes(e)
              val text = found.map(c => c.getClass.getName + ": " + c.getMessage).mkString(" | ")
              out.println("failed\t" + project + "\t" + oneLine(text))
              state.log.error("mutation-gate: Stryker4s failed in " + project + ": " + text)
              if (found.exists(_.getClass.getName.endsWith("InitialTestRunFailedException"))) {
                // Tell failing tests from a failure that only happens in Stryker4s's forked runner.
                // executeTests is a task in sbt 1 and 2 (test became an input task in sbt 2).
                val plain =
                  try { Project.extract(state).runTask(ref / Test / executeTests, state)._2.overall.toString.toLowerCase }
                  catch { case _: Throwable => "error" }
                out.println("plaintest\t" + project + "\t" + plain)
              }
          }
        }
      }
    } finally out.close()
    if (failed) state.fail else state
  }
}
'''


def _java_home():
    """JAVA_HOME, a JDK under ~/.local/opt, the macOS default JDK, or the JDK behind `java` on PATH."""
    home = tools.java_home()
    if home:
        return home
    if os.path.exists("/usr/libexec/java_home"):  # macOS: /usr/bin/java is only a stub without a JDK
        try:
            proc = subprocess.run(["/usr/libexec/java_home"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        found = proc.stdout.strip()
        return found if proc.returncode == 0 and os.path.isfile(os.path.join(found, "bin", "java")) else None
    java = shutil.which("java")
    if java:
        home = Path(java).resolve().parent.parent
        if (home / "bin" / "javac").is_file():
            return str(home)
    return None


def _jdk_hint():
    os_name = "mac" if platform.system() == "Darwin" else "linux"
    arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    url = f"https://api.adoptium.net/v3/binary/latest/21/ga/{os_name}/{arch}/jdk/hotspot/normal/eclipse"
    return ("No JDK found (JAVA_HOME is not set and there is none under ~/.local/opt). Install Temurin 21 in user "
            f"scope: mkdir -p ~/.local/opt && curl -fsSL {url} | tar -xz -C ~/.local/opt")


def _sbt_hint():
    url = f"https://github.com/sbt/sbt/releases/download/v{SBT_RUNNER_VERSION}/sbt-{SBT_RUNNER_VERSION}.tgz"
    return ("sbt is not installed. Install it in user scope: mkdir -p ~/.local/opt && "
            f"curl -fsSL {url} | tar -xz -C ~/.local/opt   (gives ~/.local/opt/sbt/bin/sbt; no PATH change needed)")


def _tracked(repo, path):
    path = Path(path).resolve()
    if repo not in path.parents:
        return False
    rel = os.path.relpath(path, repo)
    return diff._git(repo, "ls-files", "--error-unmatch", "--", f":(literal){rel}", check=False).returncode == 0


def _server_flag(sbt):
    """["--server"] when the sbt runner script knows it (2.x runners): without it they run sbt 2
    builds through sbtn and leave a server running. Older runners reject the flag."""
    try:
        with open(sbt, "rb") as f:
            return ["--server"] if b"--server)" in f.read(512 * 1024) else []
    except OSError:
        return []


def _build_root(repo, rel):
    """The sbt build that owns `rel`: nearest directory with project/build.properties, else the
    outermost one with a build.sbt (subprojects may have their own build.sbt), else None."""
    path = (repo / rel).parent
    outermost = None
    while True:
        if (path / "project" / "build.properties").is_file():
            return path
        if (path / "build.sbt").is_file():
            outermost = path
        if path == repo or repo not in path.parents:
            return outermost
        path = path.parent


def _sbt_version(root):
    try:
        text = (root / "project" / "build.properties").read_text(errors="replace")
    except OSError:
        return None
    match = re.search(r"^\s*sbt\.version\s*=\s*(\S+)", text, re.MULTILINE)
    return match.group(1) if match else None


def _version_tuple(version):
    return tuple(int(n) for n in re.findall(r"\d+", version)[:3])


def _min_sbt():
    return ".".join(map(str, MIN_SBT))


def _write_if_changed(path, text):
    # sbt recompiles the plugin when its sources change; unchanged content keeps the compiled copy.
    try:
        if path.read_text() == text:
            return
    except OSError:
        pass
    path.write_text(text)


def _read_manifest(path):
    """{project: run info} and the unmapped files, from the manifest the sbt command wrote."""
    runs, unmapped = {}, []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return runs, unmapped
    for line in lines:
        cols = line.split("\t")
        if cols[0] == "unmapped" and len(cols) >= 2:
            unmapped.append(cols[1])
        elif cols[0] == "project" and len(cols) >= 5:
            runs[cols[1]] = {
                "base": Path(cols[2]), "status": None, "message": "", "plain": None, "settings": [],
                "before": set(filter(None, cols[3].split("/"))),
                "before_reports": set(filter(None, cols[4].split("/"))),
            }
        elif len(cols) >= 2 and cols[1] in runs:
            run = runs[cols[1]]
            if cols[0] == "done":
                run["status"] = "done"
            elif cols[0] == "failed":
                run["status"], run["message"] = "failed", cols[2] if len(cols) >= 3 else ""
            elif cols[0] == "plaintest" and len(cols) >= 3:
                run["plain"] = cols[2]
            elif cols[0] == "projectsetting" and len(cols) >= 4:
                run["settings"].append((cols[2], cols[3]))
    return runs, unmapped


def _own_dir(path, parent):
    return path.parent == parent and path.is_dir() and not path.is_symlink()


def _collect_and_clean(repo, run):
    """This run's report for one sbt project; then delete what Stryker4s left in its target/."""
    base = run["base"]
    target = base / "target"
    if (base != repo and repo not in base.parents) or not _own_dir(target, base):
        return None
    report = None
    report_dir = target / "stryker4s-report"
    if _own_dir(report_dir, target):
        for entry in sorted(report_dir.iterdir()):
            if entry.name in run["before_reports"] or not _own_dir(entry, report_dir):
                continue
            try:
                report = json.loads((entry / "report.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            shutil.rmtree(entry, ignore_errors=True)
        if "stryker4s-report" not in run["before"] and not any(report_dir.iterdir()):
            report_dir.rmdir()
    for entry in target.iterdir():
        # Sandboxes Stryker4s keeps after a failed run ("Not deleting ... after error").
        if (entry.name.startswith("stryker4s-") and entry.name != "stryker4s-report"
                and entry.name not in run["before"] and _own_dir(entry, target)):
            shutil.rmtree(entry, ignore_errors=True)
    return report


def _failing_tests(output):
    lines = [l for l in output.splitlines() if FAILED_TEST_LINE.search(l)]
    return tail("\n".join(lines), 800) if lines else "(sbt printed no test names; run `sbt test` to see them)"


def _crash(output, code, sbt_version):
    """AdapterResult for a run that did not produce a report."""
    if "is not supported by Stryker4s" in output:
        return AdapterResult(error=f"Stryker4s {STRYKER4S_VERSION} needs sbt {_min_sbt()} or later and this build uses "
                                   f"sbt {sbt_version or '?'}. Raise sbt.version in project/build.properties",
                             error_kind="missing")
    lowered = output.lower()
    if "sbt-stryker4s" in output and any(w in lowered for w in ("not found", "download", "unresolved")):
        return AdapterResult(error=f"sbt could not download the sbt-stryker4s {STRYKER4S_VERSION} plugin (offline?):\n"
                                   f"{tail(output, 800)}", error_kind="missing")
    if "UnableToFixCompilerErrors" in output or "TestSetupException" in output:
        return AdapterResult(error=f"Stryker4s could not compile the project (does the code compile?):\n{tail(output)}",
                             error_kind="crash")
    return AdapterResult(error=f"sbt/Stryker4s failed (exit {code}):\n{tail(output)}", error_kind="crash")


def _run_build(repo, root, changed, deadline, java_home, sbt):
    sbt_version = _sbt_version(root)
    if sbt_version and _version_tuple(sbt_version) < MIN_SBT:
        props = os.path.relpath(root / "project" / "build.properties", repo)
        return AdapterResult(error=f"Stryker4s {STRYKER4S_VERSION} needs sbt {_min_sbt()} or later and {props} pins "
                                   f"sbt {sbt_version}. Raise sbt.version to mutation-test Scala code",
                             error_kind="missing")
    left = deadline - time.monotonic() - CLEANUP_RESERVE
    if left < MIN_START_SECONDS:
        return AdapterResult(error=f"only {max(0, int(left))}s of the budget left, too little to start sbt",
                             error_kind="timeout")

    work = store.work_dir(hashlib.sha1(str(root).encode()).hexdigest()[:12] + "-stryker4s")
    plugins = work / "sbt-plugins"
    plugins.mkdir(exist_ok=True)
    _write_if_changed(plugins / "stryker4s.sbt", PLUGINS_SBT)
    _write_if_changed(plugins / "MutationGatePlugin.scala", PLUGIN_SCALA)
    request, manifest = work / "request.txt", work / "manifest.tsv"
    request.write_text("".join(f"{(repo / rel).resolve()}\n" for rel in sorted(changed)), encoding="utf-8")
    manifest.unlink(missing_ok=True)

    cmd = [sbt, "--batch", *_server_flag(sbt), f"-Dsbt.global.plugins={plugins}",
           f"-D{REQUEST_PROP}={request}", f"-D{MANIFEST_PROP}={manifest}",
           "-Dsbt.server.autostart=false", "-Dsbt.color=false", "-Dsbt.supershell=false", "mutationGate"]
    env = tools.env_with([os.path.join(java_home, "bin")], JAVA_HOME=java_home)
    output, code, timed_out = "", None, False
    try:
        proc = run_tree(cmd, cwd=root, env=env, timeout=left)
        output, code = ANSI.sub("", proc.stdout + proc.stderr), proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
    except OSError as exc:
        output = f"could not run {sbt}: {exc}"
    finally:
        runs, unmapped = _read_manifest(manifest)
        reports = {name: _collect_and_clean(repo, run) for name, run in runs.items()}

    result = AdapterResult(unverified=[
        f"changed file is in no sbt project's main sources, so Stryker4s does not mutate it: {os.path.relpath(p, repo)}"
        for p in unmapped
    ] + [
        f"project stryker4s settings apply: {key} is set in {os.path.relpath(root / where, repo)} (sbt project {name})"
        for name, run in sorted(runs.items()) for key, where in run["settings"]
    ])
    if timed_out:
        result.error = (f"Stryker4s did not finish within {int(left)}s (incomplete run). The first run in a build also "
                        "downloads sbt, Stryker4s and their dependencies; run it once with a larger --budget")
        result.error_kind = "timeout"
        return result
    if not runs and not unmapped:
        return _crash(output, code, sbt_version)
    # The sbt command stops at the first failing project, so later projects have no status.
    failed = [(name, run) for name, run in sorted(runs.items()) if run["status"] == "failed"]
    for name, run in failed[:1]:
        if "InitialTestRunFailedException" not in run["message"]:
            crash = _crash(output, code, sbt_version)
            crash.unverified = result.unverified
            return crash
        if run["plain"] == "passed":
            result.error = (f"the tests pass under `sbt {name}/test` but fail when Stryker4s runs them in a forked "
                            f"JVM (sbt project {name}). Make them pass with `Test / fork := true` too")
            result.error_kind = "crash"
        else:
            result.failure = (f"the tests are failing (Stryker4s initial test run, sbt project {name}):\n"
                              + _failing_tests(output))
        return result
    for name, run in sorted(runs.items()):
        report = reports.get(name)
        if run["status"] != "done" or report is None:
            crash = _crash(output, code, sbt_version)
            crash.unverified = result.unverified
            return crash
        statuses = {m.get("status") for f in report.get("files", {}).values() for m in f.get("mutants", [])}
        if not report.get("testFiles") and statuses <= {"NoCoverage", "Ignored"}:
            # The legacy runner (forced on by a stryker4s.conf) reports no test names, but runs them.
            result.failure = f"no test runs in sbt project {name} (Stryker4s found no tests)"
            return result
        prefix = os.path.relpath(run["base"], repo)
        mutants, ignored = _parse(report, changed, "" if prefix == "." else prefix)
        result.mutants += mutants
        result.ignored += ignored
    return result


def run(repo, changed, budget):
    repo = Path(repo).resolve()
    deadline = time.monotonic() + budget
    java_home = _java_home()
    if java_home is None:
        return AdapterResult(error=_jdk_hint(), error_kind="missing")
    sbt = tools.find("sbt", SBT_DIRS)
    if sbt is None:
        return AdapterResult(error=_sbt_hint(), error_kind="missing")
    for binary in (sbt, os.path.join(java_home, "bin", "java")):
        if _tracked(repo, binary):
            return AdapterResult(error=f"not running {binary}: git tracks it, so the repo may have put it there",
                                 error_kind="missing")

    merged, groups = AdapterResult(), {}
    for rel, lines in changed.items():
        root = _build_root(repo, rel) if (repo / rel).is_file() else None
        if root is None:
            merged.unverified.append(f"changed Scala file is in no sbt build (no build.sbt above it): {rel}")
            continue
        groups.setdefault(root, {})[rel] = lines
    for root in sorted(groups):
        result = _run_build(repo, root, groups[root], deadline, java_home, sbt)
        merged.mutants += result.mutants
        merged.ignored += result.ignored
        merged.unverified += result.unverified
        merged.failure = merged.failure or result.failure
        if result.error and not merged.error:
            merged.error, merged.error_kind = result.error, result.error_kind
    number_occurrences(merged.mutants)
    merged.mutants.sort(key=lambda m: (m.path, m.line, m.mutator, m.replacement))
    return merged
