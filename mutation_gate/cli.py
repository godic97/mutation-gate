"""Command line: hook entry points for Claude Code and commands Max runs himself."""

import argparse
import json
import re
import sys
import traceback
from pathlib import Path

from . import diff, gate, guard, store

HOOKS = {
    "session-start": gate.on_session_start,
    "prompt": gate.on_prompt,
    "pre-tool": guard.on_pre_tool,
    "stop": gate.on_stop,
}


def _hook(event):
    try:
        result = HOOKS[event](json.loads(sys.stdin.read()))
    except Exception:
        # Never fail silently: Max sees the crash, and Claude is not blocked by a gate bug.
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
    print(f"threshold {cfg['threshold']}% · 최대 차단 {cfg['max_blocks']}회 · 시간 예산 {cfg['budget_seconds']}초")
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
        store.set_enabled(_project(), args.cmd == "on")
        print(f"{_project()}: {'켜짐' if args.cmd == 'on' else '꺼짐'}")
    elif args.cmd == "threshold":
        store.update_config(threshold=args.value)
        print(f"threshold {args.value}%")
    return 0
