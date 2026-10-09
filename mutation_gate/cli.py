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
        result = {"systemMessage": f"mutation-gate 내부 오류 ({event}): {detail}"}
    if result:
        print(json.dumps(result, ensure_ascii=False))
    return 0


def _project():
    root = diff.repo_root(Path.cwd())
    if root is None:
        sys.exit("git repo 안에서 실행하라")
    return root


def _status(_args):
    cfg = store.load_config()
    root = diff.repo_root(Path.cwd())
    print(f"threshold {cfg['threshold']}% · 시간 예산 {cfg['budget_seconds']}초")
    print(f"설정 위치 {store.home()}")
    if root:
        print(f"{root}: {'켜짐' if store.is_enabled(root) else '꺼짐'}")
        for mutant_id, info in sorted(store.allowlist(root).items()):
            print(f"  예외 {mutant_id} ({info['at']}): {info['reason']}")


def _last(_args):
    root = _project()
    sessions = sorted((store.home() / "state" / "sessions").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in sessions:
        entry = store.load_session(path.stem)["verdicts"].get(root)
        if entry:
            v = entry["verdict"]
            print(f"{root} — {v['status']} (세션 {path.stem})")
            if "score" in v:
                print(f"score {v['score']}% ({v['detected']}/{v['total']}), 예외 {v['allowed']}개")
            for s in v.get("survivors", []):
                print(f"  {s['path']}:{s['line']} [{s['mutator']}] {s['original']} → {s['replacement']}  (id {s['id']})")
            for text in v.get("failures", []) + v.get("errors", []):
                print(f"  {text}")
            for p, line, text in v.get("suppressions", []):
                print(f"  억제 {p}:{line} {text.strip()}")
            return
    print("기록 없음")


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
    allowed = f", 예외 {v['allowed']}개" if v["allowed"] else ""
    print(f"mutation-gate test {Path(root).name}: {word} — score {v['score']}% ({v['detected']}/{v['total']}{allowed}), 기준 {threshold}%")
    if v["survivors"]:
        print("살아남은 mutant (코드를 이렇게 바꿔도 테스트가 통과함):")
        for s in v["survivors"]:
            print(f"  {s['path']}:{s['line']} [{s['mutator']}] {s['original']} → {s['replacement']}  (id {s['id']})")
    for text in v["failures"]:
        print(f"실패: {text}")
    for text in v["errors"]:
        print(f"오류: {text}")
    for text in v["warnings"]:
        print(f"경고: {text}")
    if v["status"] == "unverified":
        print("경고: 대상 파일에 mutant가 하나도 없음 — 테스트로 검증되지 않음")


def _test(args):
    root = _project()
    files = _source_files(root, args.paths)
    if not files:
        print("검사할 소스 파일 없음 — JS/TS 또는 Python 소스 경로를 지정하라 (테스트 파일 말고 테스트 대상)")
        return 2
    cfg = store.load_config()
    print(f"검사 대상 {len(files)}개: {', '.join(files[:10])}{' …' if len(files) > 10 else ''}", flush=True)
    lock = store.repo_lock(root)
    if not lock.acquire(timeout=gate.LOCK_WAIT):
        print("다른 세션이 이 repo를 검사 중 — 잠시 뒤 다시 실행")
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
        raise argparse.ArgumentTypeError("mutant id는 16진수 8자리")
    return text


def _threshold(text):
    n = int(text)
    if not 0 <= n <= 100:
        raise argparse.ArgumentTypeError("0~100")
    return n


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mutation-gate", description="Claude 턴 종료 시 mutation testing 게이트")
    sub = parser.add_subparsers(dest="cmd", required=True)
    hook = sub.add_parser("hook", help="Claude Code가 호출 (직접 실행 X)")
    hook.add_argument("event", choices=sorted(HOOKS))
    test = sub.add_parser("test", help="소스 파일 전체를 mutation testing (경로 없으면 커밋 안 된 변경 파일)")
    test.add_argument("paths", nargs="*")
    test.add_argument("--budget", type=int, default=540, help="최대 실행 시간(초)")
    sub.add_parser("status", help="설정과 현재 프로젝트 상태")
    sub.add_parser("last", help="현재 프로젝트의 마지막 판정 상세")
    allow = sub.add_parser("allow", help="equivalent mutant 예외 승인")
    allow.add_argument("id", type=_mutant_id)
    allow.add_argument("reason", nargs="*")
    disallow = sub.add_parser("disallow", help="예외 취소")
    disallow.add_argument("id", type=_mutant_id)
    sub.add_parser("on", help="현재 프로젝트에서 켜기")
    sub.add_parser("off", help="현재 프로젝트에서 끄기")
    threshold = sub.add_parser("threshold", help="통과 기준 점수 (0-100)")
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
        print(f"예외 승인: {args.id}")
    elif args.cmd == "disallow":
        store.disallow(_project(), args.id)
        print(f"예외 취소: {args.id}")
    elif args.cmd in ("on", "off"):
        root = _project()
        store.set_enabled(root, args.cmd == "on")
        print(f"{root}: {'켜짐' if args.cmd == 'on' else '꺼짐'}")
        if args.cmd == "on":
            tools = [t for t in gate.TOOL_PATHS if (Path(root) / t).exists()]
            print(f"설치된 도구: {', '.join(tools)}" if tools else
                  "설치된 도구 없음 — JS/TS는 @stryker-mutator/core·vitest-runner, Python은 .venv에 mutmut 설치 필요")
    elif args.cmd == "threshold":
        store.update_config(threshold=args.value)
        print(f"threshold {args.value}%")
    return 0
