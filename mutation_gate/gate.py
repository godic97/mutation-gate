"""Stop-hook verdicts: run adapters on changed lines and decide pass, block or warn."""

import hashlib
import os
import re
import time
from pathlib import Path

from . import diff, store
from .adapters import mutmut, stryker
from .model import DETECTED

ADAPTERS = {"js": stryker, "py": mutmut}
MAX_LISTED = 20
# Seconds a Stop waits for another session's run on the same repo before reporting it busy.
LOCK_WAIT = 60
# Files whose change mid-session means someone edited the gate itself.
PLUGIN_GLOBS = ["mutation_gate/**/*.py", "bin/*", "hooks/*.json", ".claude-plugin/*.json"]
NOT_CODE = re.compile(r"^\s*(#|//|/\*|\*|$)")
# Tools whose presence means the user set the repo up for mutation testing.
TOOL_PATHS = ["node_modules/.bin/stryker", ".venv/bin/mutmut", "venv/bin/mutmut"]


def track(session_id, repo):
    """Record the repo's snapshot as this session's base the first time an enabled repo is touched."""
    if not store.is_enabled(repo):
        return False
    if repo not in store.load_session(session_id)["repos"]:
        store.remember_repo(session_id, repo, {"base": diff.snapshot(repo), "stash": diff.stash_ref(repo)})
    return True


def _run_adapters(repo, by_lang, budget, adapters, strict, full_budget):
    out = {"mutants": [], "failures": [], "errors": [], "warnings": [], "ignored": 0, "cacheable": True, "ran_ok": bool(by_lang)}
    deadline = time.monotonic() + budget
    for lang in sorted(by_lang):
        remaining = max(1, int(deadline - time.monotonic()))
        result = adapters[lang].run(repo, by_lang[lang], remaining)
        out["mutants"] += result.mutants
        out["ignored"] += result.ignored
        out["warnings"] += result.unverified
        out["cacheable"] = out["cacheable"] and result.cacheable and not (result.error_kind == "timeout" and not full_budget)
        if result.failure:
            out["failures"].append(result.failure)
        if result.error:
            out["ran_ok"] = False
            if strict and result.error_kind == "crash":
                out["failures"].append(f"도구 실행 실패 (이 repo에서 앞서 정상 동작함): {result.error}")
            else:
                out["errors"].append(result.error)
    if out["ignored"]:
        out["warnings"].append(f"검사 대상 mutant {out['ignored']}개가 기존 억제 주석 때문에 실행되지 않음")
    return out


def _verdict_dict(run, threshold, allowed, executable, suppressions=()):
    mutants = run["mutants"]
    counted = [m for m in mutants if m.id not in allowed]
    detected = sum(m.status == DETECTED for m in counted)
    total = len(counted)
    raw = 100.0 * detected / total if total else 100.0
    survivors = [
        {"id": m.id, "path": m.path, "line": m.line, "mutator": m.mutator,
         "original": m.original, "replacement": m.replacement}
        for m in counted if m.status != DETECTED
    ]
    if suppressions or run["failures"] or raw < threshold:
        status = "fail"
    elif run["errors"]:
        status = "error"
    elif not mutants and executable:
        status = "unverified"
    else:
        status = "pass"
    return {
        "status": status, "score": round(raw, 1), "detected": detected, "total": total,
        "allowed": len(mutants) - total, "survivors": survivors, "failures": run["failures"],
        "errors": run["errors"], "suppressions": list(suppressions), "warnings": run["warnings"],
        "cacheable": run["cacheable"], "ran_ok": run["ran_ok"],
    }


def evaluate(repo, base, current, threshold, allowed, budget, adapters=None, strict=False, full_budget=True):
    """Verdict for the changes between two snapshots.

    strict: the tools have worked on this repo before, so an unrecognised crash blocks.
    full_budget: the run got the whole configured budget, so a timeout is worth caching.
    """
    adapters = ADAPTERS if adapters is None else adapters
    ch = diff.changes(repo, base, current)
    suppressions = [list(s) for s in diff.find_suppressions(ch.added)]
    failures = [f"테스트 비활성화 {p}:{n} `{t.strip()}` — 되돌려라" for p, n, t in diff.find_test_skips(ch.added)]
    failures += [f"mutation 설정 변경 ({name}) — 되돌려라" for name in diff.mutation_config_changes(repo, base)]
    warnings = [f"테스트 파일 삭제됨: {rel}" for rel in ch.deleted_tests]
    by_lang = {}
    for rel, lines in ch.added.items():
        lang = diff.classify(rel)
        if lang:
            by_lang.setdefault(lang, {})[rel] = set(lines)
    if not by_lang and not suppressions and not failures and not warnings:
        return {"status": "clean"}

    run = _run_adapters(repo, by_lang, budget, adapters, strict, full_budget)
    run["failures"] = failures + run["failures"]
    run["warnings"] = warnings + run["warnings"]
    executable = any(
        not NOT_CODE.match(ch.added[rel][n]) for files in by_lang.values() for rel, lines in files.items() for n in lines
    )
    return _verdict_dict(run, threshold, allowed, executable, suppressions)


def check(repo, files, threshold, allowed, budget, adapters=None):
    """On-demand verdict for whole source files (mutation-gate test), not just changed lines."""
    adapters = ADAPTERS if adapters is None else adapters
    by_lang = {}
    for rel in files:
        lang = diff.classify(rel)
        if lang:
            count = len((Path(repo) / rel).read_text(errors="replace").split("\n"))
            by_lang.setdefault(lang, {})[rel] = set(range(1, count + 1))
    run = _run_adapters(repo, by_lang, budget, adapters, strict=False, full_budget=True)
    return _verdict_dict(run, threshold, allowed, executable=bool(by_lang))


def _name(repo):
    return Path(repo).name


def block_reason(fails, threshold):
    out = ["mutation-gate: 테스트가 이번에 바뀐 코드의 버그를 충분히 잡지 못함. 턴을 끝내기 전에 테스트를 보강하라.", ""]
    for repo, v in fails.items():
        if "score" in v:
            out.append(f"[{repo}] mutation score {v['score']}% ({v['detected']}/{v['total']}) — 기준 {threshold}%")
        for failure in v.get("failures", []):
            out.append(f"- {failure}")
        for path, line, text in v.get("suppressions", []):
            out.append(f"- 억제 시도 {path}:{line} `{text.strip()}` — 이 줄을 되돌려라")
        survivors = v.get("survivors", [])
        if survivors:
            out.append("살아남은 mutant (코드를 이렇게 바꿔도 테스트가 통과함):")
            for s in survivors[:MAX_LISTED]:
                out.append(f"- {s['path']}:{s['line']} [{s['mutator']}] {s['original']} → {s['replacement']}  (id {s['id']})")
            if len(survivors) > MAX_LISTED:
                out.append(f"- … 외 {len(survivors) - MAX_LISTED}개")
        out.append("")
    out += [
        "규칙:",
        "- 각 mutant가 실패하도록 assertion을 추가하라 (경계값, 반환값, 분기 결과를 직접 검사).",
        "- 억제 주석(Stryker disable, pragma: no mutate), mutation 도구 config 변경, 테스트 skip·only는 금지.",
        "- 어떤 테스트로도 죽일 수 없는 equivalent mutant라고 판단되면 최종 답변에 id와 근거를 적어 사용자에게 보고하라. 예외 승인은 사용자만 한다.",
    ]
    return "\n".join(out)


def _summary(repo, v):
    allowed = f", 예외 {v['allowed']}개" if v.get("allowed") else ""
    return f"{_name(repo)} {v['score']}% ({v['detected']}/{v['total']}{allowed})"


def _verdict(sid, repo, entry, cfg, deadline):
    allowed = store.allowed_ids(repo)
    current = diff.snapshot(repo)
    fp = f"{current}:{cfg['threshold']}:{','.join(sorted(allowed))}"
    verdict = store.cached_verdict(sid, repo, fp)
    if verdict is not None:
        return verdict
    # One mutation run per repo at a time: sessions share mutants/ and the Stryker work dir.
    lock = store.repo_lock(repo)
    if not lock.acquire(timeout=min(LOCK_WAIT, max(0, deadline - time.monotonic() - 10))):
        return {"status": "error", "errors": ["다른 세션이 이 repo를 검사 중 — 다음 종료 때 다시 검사"]}
    try:
        budget = max(1, int(deadline - time.monotonic()))
        verdict = evaluate(
            repo, entry["base"], current, cfg["threshold"], allowed, budget,
            strict=entry.get("tool_ok", False), full_budget=budget >= cfg["budget_seconds"] * 0.9,
        )
    finally:
        lock.release()
    if verdict.get("ran_ok"):
        store.update_repo(sid, repo, tool_ok=True)
    if verdict.get("cacheable", True):
        store.save_verdict(sid, repo, fp, verdict)
    return verdict


def _hash_files(root, patterns):
    digest = hashlib.sha256()
    for pattern in patterns:
        for path in sorted(Path(root).glob(pattern)):
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _plugin_hash():
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    return _hash_files(root, PLUGIN_GLOBS) if root else None


def _settings_hash():
    return _hash_files(store.home(), ["config.json", "allow.json"])


def _project_root(payload):
    return diff.repo_root(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".")


def on_session_start(payload):
    sid = payload["session_id"]
    store.prune_sessions()
    root = _project_root(payload)
    if root:
        track(sid, root)
    session = store.load_session(sid)
    if session.get("plugin_hash") is None:
        store.set_session_value(sid, "plugin_hash", _plugin_hash())
    if session.get("settings_hash") is None:
        store.set_session_value(sid, "settings_hash", _settings_hash())


def on_prompt(payload):
    store.reset_blocks(payload["session_id"])


def _status_lines(repo, v):
    name = _name(repo)
    lines = []
    if v["status"] == "pass":
        lines.append(f"mutation-gate ✓ {_summary(repo, v)}" if v["total"] else f"mutation-gate ✓ {name}: 바뀐 줄이 주석·빈 줄뿐")
    elif v["status"] == "unverified":
        lines.append(f"mutation-gate ⚠ {name}: 바뀐 코드에 mutant 0개 — 테스트로 검증되지 않음 (mutation-gate last)")
    for error in v.get("errors", []):
        lines.append(f"mutation-gate ⚠ {name}: 검사 못 함 — {error}")
    for warning in v.get("warnings", []):
        lines.append(f"mutation-gate ⚠ {name}: {warning}")
    return lines


def on_stop(payload):
    sid = payload["session_id"]
    cfg = store.load_config()
    lines = []
    root = _project_root(payload)
    if root and not track(sid, root):
        session = store.load_session(sid)
        if not session.get("hinted") and any((Path(root) / p).exists() for p in TOOL_PATHS):
            store.set_session_value(sid, "hinted", True)
            lines.append(f"mutation-gate: {_name(root)}에서는 꺼져 있음. 켜려면 `! mutation-gate on`")
    session = store.load_session(sid)

    verdicts = {}
    deadline = time.monotonic() + cfg["budget_seconds"]
    for repo, entry in session["repos"].items():
        if not isinstance(entry, dict) or not store.is_enabled(repo):
            continue
        if not Path(repo).is_dir():
            lines.append(f"mutation-gate ⚠ {_name(repo)}: repo가 사라져 검사 못 함 ({repo})")
            continue
        try:
            verdict = _verdict(sid, repo, entry, cfg, deadline)
            if diff.stash_ref(repo) != entry.get("stash", ""):
                lines.append(f"mutation-gate ⚠ {_name(repo)}: 세션 중 git stash가 생김 — stash된 변경은 검사되지 않음")
        except Exception as exc:  # one broken repo must not skip the others
            verdict = {"status": "error", "errors": [f"내부 오류: {type(exc).__name__}: {exc}"]}
        if verdict["status"] != "clean":
            verdicts[repo] = verdict

    fails = {r: v for r, v in verdicts.items() if v["status"] == "fail"}
    expected = session.get("plugin_hash")
    if expected and _plugin_hash() != expected:
        fails["mutation-gate"] = {"failures": ["플러그인 파일이 세션 중 변경됨. 변경을 되돌리고 사용자에게 보고하라."]}
    if session.get("settings_hash") and _settings_hash() != session["settings_hash"]:
        lines.append("mutation-gate ⚠ 세션 중 mutation-gate 설정 또는 예외 목록이 바뀜 (mutation-gate status)")

    for repo, v in verdicts.items():
        lines += _status_lines(repo, v)

    if fails:
        short = ", ".join(_summary(r, v) if "score" in v else _name(r) for r, v in fails.items())
        if session["blocks"] < cfg["max_blocks"]:
            n = store.bump_blocks(sid)
            lines.append(f"mutation-gate ✗ {short} — 기준 {cfg['threshold']}%, 차단 {n}/{cfg['max_blocks']}")
            return {"decision": "block", "reason": block_reason(fails, cfg["threshold"]), "systemMessage": "\n".join(lines)}
        unresolved = sum(len(v.get("survivors", [])) for v in fails.values())
        lines.append(
            f"mutation-gate ✗ 게이트 실패: {short}, 미해결 mutant {unresolved}개 — "
            f"{cfg['max_blocks']}회 차단 후 종료 허용. 테스트를 직접 확인할 것 (mutation-gate last)"
        )
    return {"systemMessage": "\n".join(lines)} if lines else None
