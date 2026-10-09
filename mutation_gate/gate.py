"""Stop-hook verdicts: run adapters on changed lines and decide pass, block or warn."""

import hashlib
import os
import time
from pathlib import Path

from . import diff, store
from .adapters import mutmut, stryker
from .model import DETECTED

ADAPTERS = {"js": stryker, "py": mutmut}
MAX_LISTED = 20
# Files whose change mid-session means someone edited the gate itself.
PLUGIN_GLOBS = ["mutation_gate/**/*.py", "bin/*", "hooks/*.json", ".claude-plugin/*.json"]


def evaluate(repo, base, threshold, allowed, budget, adapters=None):
    adapters = ADAPTERS if adapters is None else adapters
    added = diff.added_lines(repo, base)
    suppressions = [list(s) for s in diff.find_suppressions(added)]
    by_lang = {}
    for rel, lines in added.items():
        lang = diff.classify(rel)
        if lang:
            by_lang.setdefault(lang, {})[rel] = set(lines)
    if not by_lang and not suppressions:
        return {"status": "clean"}

    mutants, failures, errors = [], [], []
    cacheable = True
    deadline = time.monotonic() + budget
    for lang in sorted(by_lang):
        remaining = max(1, int(deadline - time.monotonic()))
        result = adapters[lang].run(repo, by_lang[lang], remaining)
        mutants += result.mutants
        cacheable = cacheable and result.cacheable
        if result.failure:
            failures.append(result.failure)
        if result.error:
            errors.append(result.error)

    counted = [m for m in mutants if m.id not in allowed]
    detected = sum(m.status == DETECTED for m in counted)
    total = len(counted)
    score = round(100.0 * detected / total, 1) if total else 100.0
    survivors = [
        {"id": m.id, "path": m.path, "line": m.line, "mutator": m.mutator,
         "original": m.original, "replacement": m.replacement}
        for m in counted if m.status != DETECTED
    ]
    if suppressions or failures or score < threshold:
        status = "fail"
    elif errors:
        status = "error"
    else:
        status = "pass"
    return {
        "status": status, "score": score, "detected": detected, "total": total,
        "allowed": len(mutants) - total, "survivors": survivors,
        "failures": failures, "errors": errors, "suppressions": suppressions, "cacheable": cacheable,
    }


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
        "- 억제 주석(Stryker disable, pragma: no mutate), mutation 도구 config 변경, 테스트 skip은 금지.",
        "- 어떤 테스트로도 죽일 수 없는 equivalent mutant라고 판단되면 최종 답변에 id와 근거를 적어 Max에게 보고하라. 예외 승인은 Max만 한다.",
    ]
    return "\n".join(out)


def _summary(repo, v):
    return f"{_name(repo)} {v['score']}% ({v['detected']}/{v['total']})"


def _pass_line(repo, v):
    if v["total"] == 0:
        return f"mutation-gate ✓ {_name(repo)}: 바뀐 줄에 변이 대상 없음 (주석·선언·모듈 수준 코드 등)"
    return f"mutation-gate ✓ {_summary(repo, v)}"


def _verdict(sid, repo, base, cfg, deadline):
    allowed = store.allowed_ids(repo)
    fp = f"{diff.fingerprint(repo, base)}:{cfg['threshold']}:{','.join(sorted(allowed))}"
    verdict = store.cached_verdict(sid, repo, fp)
    if verdict is None:
        # One mutation run per repo at a time: sessions share mutants/ and the Stryker work dir.
        with store.repo_lock(repo):
            budget = max(1, int(deadline - time.monotonic()))
            verdict = evaluate(repo, base, cfg["threshold"], allowed, budget)
        if verdict.get("cacheable", True):
            store.save_verdict(sid, repo, fp, verdict)
    return verdict


def _plugin_hash():
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        return None
    digest = hashlib.sha256()
    for pattern in PLUGIN_GLOBS:
        for path in sorted(Path(root).glob(pattern)):
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _project_root(payload):
    return diff.repo_root(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".")


def on_session_start(payload):
    sid = payload["session_id"]
    root = _project_root(payload)
    if root:
        store.remember_repo(sid, root, diff.head_sha(root))
    if store.load_session(sid).get("plugin_hash") is None:
        store.set_session_value(sid, "plugin_hash", _plugin_hash())


def on_prompt(payload):
    store.reset_blocks(payload["session_id"])


def on_stop(payload):
    sid = payload["session_id"]
    cfg = store.load_config()
    root = _project_root(payload)
    if root:
        store.remember_repo(sid, root, diff.head_sha(root))
    session = store.load_session(sid)

    verdicts = {}
    deadline = time.monotonic() + cfg["budget_seconds"]
    for repo, base in session["repos"].items():
        if not Path(repo).is_dir() or not store.is_enabled(repo):
            continue
        try:
            verdict = _verdict(sid, repo, base, cfg, deadline)
        except Exception as exc:  # one broken repo must not skip the others
            verdict = {"status": "error", "errors": [f"내부 오류: {type(exc).__name__}: {exc}"]}
        if verdict["status"] != "clean":
            verdicts[repo] = verdict

    fails = {r: v for r, v in verdicts.items() if v["status"] == "fail"}
    expected = session.get("plugin_hash")
    if expected and _plugin_hash() != expected:
        fails["mutation-gate"] = {"failures": ["플러그인 파일이 세션 중 변경됨. 변경을 되돌리고 Max에게 보고하라."]}

    lines = [_pass_line(r, v) for r, v in verdicts.items() if v["status"] == "pass"]
    for repo, v in verdicts.items():
        for error in v.get("errors", []):
            lines.append(f"mutation-gate ⚠ {_name(repo)}: 검사 못 함 — {error}")

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
