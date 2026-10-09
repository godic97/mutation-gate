"""Mutation-test verdicts: for the lines changed in a session (Stop hook) or for whole files (CLI)."""

import os
import re
import time
from pathlib import Path

from . import diff, store
from .adapters import mutmut, stryker
from .model import DETECTED

ADAPTERS = {"js": stryker, "py": mutmut}
# Survivors shown in the Stop banner; `mutation-gate last` has the rest.
BANNER_SURVIVORS = 5
# Seconds a run waits for another session's run on the same repo before reporting it busy.
LOCK_WAIT = 60
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


def _run_adapters(repo, by_lang, budget, adapters, full_budget):
    out = {"mutants": [], "failures": [], "errors": [], "warnings": [], "ignored": 0, "cacheable": True}
    deadline = time.monotonic() + budget
    for lang in sorted(by_lang):
        if lang not in adapters:
            out["warnings"].append(
                f"no mutation adapter for {lang} yet: {', '.join(sorted(by_lang[lang]))} — "
                "use LLM mode: write a find/replace manifest and run `mutation-gate mutate` (mutation-test skill)"
            )
            continue
        remaining = max(1, int(deadline - time.monotonic()))
        result = adapters[lang].run(repo, by_lang[lang], remaining)
        out["mutants"] += result.mutants
        out["ignored"] += result.ignored
        out["warnings"] += result.unverified
        out["cacheable"] = out["cacheable"] and result.cacheable and not (result.error_kind == "timeout" and not full_budget)
        if result.failure:
            out["failures"].append(result.failure)
        if result.error:
            out["errors"].append(result.error)
    if out["ignored"]:
        out["warnings"].append(f"{out['ignored']} mutant(s) were not run because of existing suppression comments")
    return out


def _verdict_dict(run, threshold, allowed, executable):
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
    if run["failures"] or raw < threshold:
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
        "errors": run["errors"], "warnings": run["warnings"], "cacheable": run["cacheable"],
    }


def evaluate(repo, base, current, threshold, allowed, budget, adapters=None, full_budget=True):
    """Verdict for the changes between two snapshots.

    full_budget: the run got the whole configured budget, so a timeout is worth caching.
    """
    adapters = ADAPTERS if adapters is None else adapters
    ch = diff.changes(repo, base, current)
    # Things that change what the score means: reported next to it, never hidden.
    notes = [f"suppression comment added {p}:{n} `{t.strip()}`" for p, n, t in diff.find_suppressions(ch.added)]
    notes += [f"test disabled {p}:{n} `{t.strip()}`" for p, n, t in diff.find_test_skips(ch.added)]
    notes += [f"mutation tool settings changed ({name})" for name in diff.mutation_config_changes(repo, base)]
    notes += [f"test file deleted: {rel}" for rel in ch.deleted_tests]
    by_lang = {}
    for rel, lines in ch.added.items():
        lang = diff.classify(rel)
        if lang:
            by_lang.setdefault(lang, {})[rel] = set(lines)
    if not by_lang and not notes:
        return {"status": "clean"}

    run = _run_adapters(repo, by_lang, budget, adapters, full_budget)
    run["warnings"] = notes + run["warnings"]
    executable = any(
        not NOT_CODE.match(ch.added[rel][n]) for files in by_lang.values() for rel, lines in files.items() for n in lines
    )
    return _verdict_dict(run, threshold, allowed, executable)


def check(repo, files, threshold, allowed, budget, adapters=None):
    """On-demand verdict for whole source files (mutation-gate test), not just changed lines."""
    adapters = ADAPTERS if adapters is None else adapters
    by_lang = {}
    for rel in files:
        lang = diff.classify(rel)
        if lang:
            count = len((Path(repo) / rel).read_text(errors="replace").split("\n"))
            by_lang.setdefault(lang, {})[rel] = set(range(1, count + 1))
    run = _run_adapters(repo, by_lang, budget, adapters, full_budget=True)
    return _verdict_dict(run, threshold, allowed, executable=bool(by_lang))


def _name(repo):
    return Path(repo).name


def _summary(repo, v):
    allowed = f", {v['allowed']} allowed" if v.get("allowed") else ""
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
        return {"status": "error", "errors": ["another session is testing this repo — will retry at the next stop"]}
    try:
        budget = max(1, int(deadline - time.monotonic()))
        verdict = evaluate(repo, entry["base"], current, cfg["threshold"], allowed, budget,
                           full_budget=budget >= cfg["budget_seconds"] * 0.9)
    finally:
        lock.release()
    if verdict.get("cacheable", True):
        store.save_verdict(sid, repo, fp, verdict)
    return verdict


def _project_root(payload):
    return diff.repo_root(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".")


def _restore_note():
    from . import manifest  # manifest imports gate
    restored = manifest.restore_all()
    return [f"mutation-gate: restored {p} (a mutate/verify run was killed mid-mutation)" for p in restored]


def on_session_start(payload):
    store.prune_sessions()
    lines = _restore_note()
    root = _project_root(payload)
    if root:
        track(payload["session_id"], root)
    return {"systemMessage": "\n".join(lines)} if lines else None


def report_lines(repo, v, threshold):
    """Banner lines for one repo's verdict."""
    name = _name(repo)
    lines = []
    if v["status"] == "pass":
        lines.append(f"mutation-gate ✓ {_summary(repo, v)}" if v["total"] else f"mutation-gate ✓ {name}: only comments or blank lines changed")
    elif v["status"] == "fail":
        lines.append(f"mutation-gate ✗ {_summary(repo, v)} — threshold {threshold}%, {len(v['survivors'])} surviving mutant(s)")
        for s in v["survivors"][:BANNER_SURVIVORS]:
            lines.append(f"  {s['path']}:{s['line']} {s['original']} → {s['replacement']}  (id {s['id']})")
        if len(v["survivors"]) > BANNER_SURVIVORS:
            lines.append(f"  … {len(v['survivors']) - BANNER_SURVIVORS} more (mutation-gate last)")
    elif v["status"] == "unverified":
        lines.append(f"mutation-gate ⚠ {name}: the changed code has no mutants — not verified by the tests")
    for failure in v.get("failures", []):
        lines.append(f"mutation-gate ✗ {name}: {failure}")
    for error in v.get("errors", []):
        lines.append(f"mutation-gate ⚠ {name}: could not test — {error}")
    for warning in v.get("warnings", []):
        lines.append(f"mutation-gate ⚠ {name}: {warning}")
    return lines


def on_stop(payload):
    """Mutation-test this session's changes in every enabled repo and report; never blocks."""
    sid = payload["session_id"]
    cfg = store.load_config()
    lines = _restore_note()
    root = _project_root(payload)
    if root and not track(sid, root):
        session = store.load_session(sid)
        if not session.get("hinted") and any((Path(root) / p).exists() for p in TOOL_PATHS):
            store.set_session_value(sid, "hinted", True)
            lines.append(f"mutation-gate: off in {_name(root)}. To test every turn: `! mutation-gate on`")
    session = store.load_session(sid)

    deadline = time.monotonic() + cfg["budget_seconds"]
    for repo, entry in session["repos"].items():
        if not isinstance(entry, dict) or not store.is_enabled(repo):
            continue
        if not Path(repo).is_dir():
            lines.append(f"mutation-gate ⚠ {_name(repo)}: the repo is gone, not tested ({repo})")
            continue
        try:
            verdict = _verdict(sid, repo, entry, cfg, deadline)
            if diff.stash_ref(repo) != entry.get("stash", ""):
                lines.append(f"mutation-gate ⚠ {_name(repo)}: git stash was used this session — stashed changes are not tested")
        except Exception as exc:  # one broken repo must not skip the others
            verdict = {"status": "error", "errors": [f"internal error: {type(exc).__name__}: {exc}"]}
        if verdict["status"] != "clean":
            lines += report_lines(repo, verdict, cfg["threshold"])
    return {"systemMessage": "\n".join(lines)} if lines else None
