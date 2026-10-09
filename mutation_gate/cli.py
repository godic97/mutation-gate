"""Command line: hook entry points for Claude Code and commands the user runs directly."""

import argparse
import json
import re
import signal
import sys
import traceback
from pathlib import Path

from . import diff, gate, store, tracker
from .model import kill_active

HOOKS = {
    "session-start": gate.on_session_start,
    "pre-tool": tracker.on_pre_tool,
    "stop": gate.on_stop,
}


def _on_signal(signum, _frame):
    # Claude Code kills a hook on timeout or interrupt: take the tool's process groups along, and
    # unwind so `finally` blocks clean up (mutmut's mutants/ dir, the repo lock).
    kill_active()
    raise SystemExit(128 + signum)


def _hook(event):
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _on_signal)
    try:
        result = HOOKS[event](json.loads(sys.stdin.read()))
    except Exception:
        # Never fail silently: the user sees the crash.
        detail = traceback.format_exc(limit=3).strip().splitlines()[-1]
        result = {"systemMessage": f"mutation-gate internal error ({event}): {detail}"}
    if result:
        print(json.dumps(result, ensure_ascii=False))
    return 0


def _project():
    root = diff.repo_root(Path.cwd())
    if root is None:
        sys.exit("run this inside a git repo")
    return root


def _status(_args):
    cfg = store.load_config()
    root = diff.repo_root(Path.cwd())
    print(f"threshold {cfg['threshold']}% · budget {cfg['budget_seconds']}s")
    print(f"config dir {store.home()}")
    if root:
        print(f"{root}: {'on' if store.is_enabled(root) else 'off'}")
        for mutant_id, info in sorted(store.allowlist(root).items()):
            print(f"  allowed {mutant_id} ({info['at']}): {info['reason']}")


def _last(_args):
    root = _project()
    sessions = sorted((store.home() / "state" / "sessions").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in sessions:
        entry = store.load_session(path.stem)["verdicts"].get(root)
        if entry:
            v = entry["verdict"]
            print(f"{root} — {v['status']} (session {path.stem})")
            if "score" in v:
                print(f"score {v['score']}% ({v['detected']}/{v['total']}), {v['allowed']} allowed")
            for s in v.get("survivors", []):
                print(f"  {s['path']}:{s['line']} [{s['mutator']}] {s['original']} → {s['replacement']}  (id {s['id']})")
            for text in v.get("failures", []) + v.get("errors", []):
                print(f"  {text}")
            for p, line, text in v.get("suppressions", []):
                print(f"  suppression {p}:{line} {text.strip()}")
            return
    print("no results yet")


def _source_files(root, paths):
    """Repo-relative mutable source files under `paths`; without paths, the uncommitted ones."""
    if paths:
        listed = []
        for arg in paths:
            target = (Path.cwd() / arg).resolve()
            rel = str(target.relative_to(root)) if target != Path(root) else "."
            out = diff._git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", f":(literal){rel}")
            listed += [f for f in out.stdout.split("\0") if f]
    else:
        out = diff._git(root, "diff", "--name-only", "-z", "--diff-filter=d", diff.head_sha(root), "--", check=False)
        listed = [f for f in out.stdout.split("\0") if f]
        out = diff._git(root, "ls-files", "-z", "--others", "--exclude-standard")
        listed += [f for f in out.stdout.split("\0") if f]
    return sorted({f for f in listed if diff.classify(f) and (Path(root) / f).is_file()})


def _print_verdict(root, v, threshold):
    word = {"pass": "PASS", "fail": "FAIL", "error": "ERROR", "unverified": "UNVERIFIED"}[v["status"]]
    allowed = f", {v['allowed']} allowed" if v["allowed"] else ""
    print(f"mutation-gate test {Path(root).name}: {word} — score {v['score']}% ({v['detected']}/{v['total']}{allowed}), threshold {threshold}%")
    if v["survivors"]:
        print("Surviving mutants (the tests still pass with these changes):")
        for s in v["survivors"]:
            print(f"  {s['path']}:{s['line']} [{s['mutator']}] {s['original']} → {s['replacement']}  (id {s['id']})")
    for text in v["failures"]:
        print(f"failure: {text}")
    for text in v["errors"]:
        print(f"error: {text}")
    for text in v["warnings"]:
        print(f"warning: {text}")
    if v["status"] == "unverified":
        print("warning: the target files have no mutants — nothing was verified by the tests")


def _test(args):
    root = _project()
    files = _source_files(root, args.paths)
    if not files:
        print("No source files to test — give JS/TS or Python source paths (the code under test, not the test files)")
        return 2
    cfg = store.load_config()
    print(f"Testing {len(files)} file(s): {', '.join(files[:10])}{' …' if len(files) > 10 else ''}", flush=True)
    lock = store.repo_lock(root)
    if not lock.acquire(timeout=gate.LOCK_WAIT):
        print("Another session is testing this repo — try again shortly")
        return 2
    try:
        verdict = gate.check(root, files, cfg["threshold"], store.allowed_ids(root), args.budget)
    finally:
        lock.release()
    store.save_verdict("manual-test", root, "manual", verdict)
    _print_verdict(root, verdict, cfg["threshold"])
    return {"pass": 0, "fail": 1}.get(verdict["status"], 2)


def _mutant_id(text):
    if not re.fullmatch(r"[0-9a-f]{8}", text):
        raise argparse.ArgumentTypeError("a mutant id is 8 hex digits")
    return text


def _threshold(text):
    n = int(text)
    if not 0 <= n <= 100:
        raise argparse.ArgumentTypeError("0~100")
    return n


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mutation-gate", description="Mutation testing for Claude Code")
    sub = parser.add_subparsers(dest="cmd", required=True)
    hook = sub.add_parser("hook", help="called by Claude Code hooks; do not run by hand")
    hook.add_argument("event", choices=sorted(HOOKS))
    test = sub.add_parser("test", help="mutation-test whole source files (default: the uncommitted ones)")
    test.add_argument("paths", nargs="*")
    test.add_argument("--budget", type=int, default=540, help="maximum run time in seconds")
    sub.add_parser("status", help="settings and the current repo's state")
    sub.add_parser("last", help="details of the last result for the current repo")
    allow = sub.add_parser("allow", help="accept an equivalent mutant so it no longer counts")
    allow.add_argument("id", type=_mutant_id)
    allow.add_argument("reason", nargs="*")
    disallow = sub.add_parser("disallow", help="count an accepted mutant again")
    disallow.add_argument("id", type=_mutant_id)
    sub.add_parser("on", help="turn the end-of-turn report on for the current repo")
    sub.add_parser("off", help="turn the end-of-turn report off for the current repo")
    threshold = sub.add_parser("threshold", help="passing score (0-100)")
    threshold.add_argument("value", type=_threshold)
    args = parser.parse_args(argv)

    if args.cmd == "hook":
        return _hook(args.event)
    if args.cmd == "test":
        return _test(args)
    if args.cmd == "status":
        _status(args)
    elif args.cmd == "last":
        _last(args)
    elif args.cmd == "allow":
        store.allow(_project(), args.id, " ".join(args.reason))
        print(f"allowed: {args.id}")
    elif args.cmd == "disallow":
        store.disallow(_project(), args.id)
        print(f"no longer allowed: {args.id}")
    elif args.cmd in ("on", "off"):
        root = _project()
        store.set_enabled(root, args.cmd == "on")
        print(f"{root}: {args.cmd}")
        if args.cmd == "on":
            tools = [t for t in gate.TOOL_PATHS if (Path(root) / t).exists()]
            print(f"installed tools: {', '.join(tools)}" if tools else
                  "no mutation tool installed — JS/TS needs @stryker-mutator/core and @stryker-mutator/vitest-runner, "
                  "Python needs mutmut in .venv")
    elif args.cmd == "threshold":
        store.update_config(threshold=args.value)
        print(f"threshold {args.value}%")
    return 0
