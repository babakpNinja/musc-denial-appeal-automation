#!/usr/bin/env python3
"""Re-anchor the demo timeline and ship it, without losing the board's state.

The claim timeline is generated relative to *build day* (issue #11): each denial
sits a fraction of its payer's appeal window in the past. That keeps ~80% of the
board appealable on the day it is built — and then quietly decays, because the
deadlines stay put while today moves. So the data has to be rebuilt now and
then. Ids are stable across a rebuild, so this is cheap and does not touch the
letter cache (no model spend) or orphan the live appeal status. When they are *not*
stable, the run prunes the letters the old ids left behind before re-rendering, and
says how many it dropped — see ``prune_letters``.

    python refresh_demo.py --dry-run      # rebuild locally, test, report, revert
    python refresh_demo.py                # ship it, but only if the board has aged
    python refresh_demo.py --force --json

Safe to run often: a rebuild rewrites the DB and re-renders every letter (they
cite dates), so "files changed" is not the signal — the run only pushes once the
timeline has drifted ``--min-drift-days`` or more than ``--max-lapsed`` cases have run past
their deadline. It asks the live ``/api/health`` how old the deployed data is
before rebuilding anything, so a fresh board costs one request, not a rebuild. Any
failing test aborts before anything is pushed. After the push it waits — via
``mirror.py push --wait``, which polls until the deploy reports back the commit it
just pushed — before touching workflow state, because writes to the container that
is about to be replaced are thrown away with it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import demo_state

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DB = HERE / "data" / "musc_appeals.db"
LIVE = "https://musc-appeals-production.up.railway.app"
# versioned inputs the deploy is built from; a rebuild that changes none of them is a
# no-op. The letter PDFs are deliberately absent: they are untracked build artifacts
# rendered by the mirror's prepare step, so git could neither report nor revert them.
SHIPPED = ("data/musc_appeals.db", "data/letter_drafts.json")
MIN_DRIFT_DAYS = 21     # below this the board still looks live; do not churn the deploy
MAX_LAPSED = 12         # …unless this many cases have already run past their deadline
UPTIME_TARGET = "musc-appeals"   # the name in tools/uptime_targets.json


def run(cmd: list[str], cwd: Path = HERE, timeout: int = 1800) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def board_health(db_path: Path = DB) -> dict:
    """How much of the queue is still worth showing a client."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = [r[0] for r in con.execute("SELECT appeal_deadline FROM denials")]
    finally:
        con.close()
    today = date.today()
    left = [(date.fromisoformat(d) - today).days for d in rows if d]
    return {
        "denials": len(rows),
        "appealable": sum(1 for d in left if d > 0),
        "due_soon": sum(1 for d in left if 0 < d <= 14),
        "lapsed": sum(1 for d in left if d <= 0),
        "worst_lapse": min(left) if left else None,
    }


def git_changes() -> list[str]:
    r = run(["git", "status", "--porcelain", "--"] + [str(HERE / p) for p in SHIPPED], cwd=REPO)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def revert_rebuild() -> None:
    """Put the tree back — letters included, which git cannot do (#125).

    ``git checkout`` restores SHIPPED, but the PDFs are untracked artifacts and the
    prune deleted some of them for real, so a plain revert leaves letters that belong
    to the abandoned build: exactly the orphans the suite now fails on, one run later.
    Re-align them with the restored inputs instead — drop what the reverted board does
    not have, then re-render from the restored drafts (~5s, no model calls).
    """
    run(["git", "checkout", "--"] + [str(HERE / p) for p in SHIPPED], cwd=REPO)
    run([sys.executable, "generate_letters.py", "--prune-orphans", "--json"])
    run([sys.executable, "generate_letters.py", "--rerender"])


def prune_letters() -> dict:
    """Drop letter artifacts whose denial id this rebuild no longer has (#125).

    A rebuild that renumbers ids leaves the previous board's PDFs and cached drafts
    behind, and the suite fails on them — so without this the run would revert and
    repeat every night, logging only "tests failed". This is the one place where the
    removal is unambiguous: the ids were rewritten a moment ago, and the ``--rerender``
    that follows re-creates a letter for every case that survived. Returns
    ``{"ok", "pdfs", "drafts"}``; a report it cannot parse is a failure, not a zero.
    """
    res = run([sys.executable, "generate_letters.py", "--prune-orphans", "--json"])
    if res.returncode != 0:
        return {"ok": False, "note": f"prune failed: {(res.stderr or res.stdout).strip()[-300:]}"}
    try:
        pruned = (json.loads(res.stdout) or {})["pruned"]
    except (ValueError, KeyError, TypeError):
        return {"ok": False,
                "note": f"prune produced no report: {(res.stdout or res.stderr).strip()[-200:]}"}
    return {"ok": True, "pdfs": pruned.get("pdfs", 0), "drafts": pruned.get("drafts", 0)}


def deadlines(db_path: Path = DB) -> dict[str, str]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT denial_id, appeal_deadline FROM denials ORDER BY denial_id"))
    finally:
        con.close()


def drift_days(before: dict[str, str], after: dict[str, str]) -> int:
    """How far the timeline moved, i.e. how many days old the shipped board was.

    Denial ids are stable across a rebuild and every deadline is anchored to
    build day, so the shift is the same for every case; take the median in case
    a case appeared or the payer window changed.
    """
    moved = sorted((date.fromisoformat(after[d]) - date.fromisoformat(b)).days
                   for d, b in before.items() if after.get(d))
    return moved[len(moved) // 2] if moved else 0


def live_build_age(base: str) -> int | None:
    """Days since the *deployed* data was built, straight from /api/health.

    The honest measure of drift is a local rebuild, which costs a minute and
    re-renders every letter. This is the same number for free, so it can be asked
    first and the whole rebuild skipped when the board is obviously fresh.
    None when the deploy is unreachable or predates the ``meta`` table — then
    fall back to rebuilding and measuring.
    """
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/api/health", timeout=20) as resp:
            return (json.loads(resp.read()) or {}).get("built_days_ago")
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError, OSError):
        return None


def push_and_wait(message: str, timeout: int = 900) -> dict:
    """Push the mirror and don't come back until the new container is answering.

    The waiting used to live here and identified the new build by polling a
    denial deadline — a signal only this app has. ``mirror.py`` does it generically
    now (issue #26): the deploy reports the commit it is running, so this returns
    ``pushed``/``live``/``proven`` and the caller can refuse to write state into a
    container that is about to be replaced.
    """
    res = run([sys.executable, "tools/mirror.py", "push", UPTIME_TARGET, "--wait", "--json",
               "-m", message], cwd=REPO, timeout=timeout + 300)
    try:
        out = json.loads(res.stdout)
    except ValueError:
        return {"pushed": False, "live": False, "proven": False,
                "note": f"mirror push produced no report: {(res.stderr or res.stdout).strip()[-300:]}"}
    wait = out.get("wait") or {}
    return {"pushed": bool(out.get("pushed")), "commit": out.get("commit"),
            "live": wait.get("live"), "proven": bool(wait.get("proven")),
            "note": wait.get("note", "nothing to push")}


def verify_live(target: str = UPTIME_TARGET) -> tuple[bool, list[str]]:
    """The full uptime check, browser and all — a 200 is not proof of a live demo.

    Returns (ok, problems). An uptime that cannot run at all counts as not-ok:
    an unattended push must never report success it did not verify.
    """
    res = run([sys.executable, "tools/uptime.py", target, "--json"], cwd=REPO, timeout=600)
    try:
        results = json.loads(res.stdout)
    except ValueError:
        return False, [f"uptime produced no report: {(res.stderr or res.stdout).strip()[-300:]}"]
    problems = [f"{c['url']} — {'; '.join(c['problems'])}"
                for r in results for c in r["checks"] if not c["ok"]]
    return res.returncode == 0 and not problems, problems


def refresh(base: str, token: str, dry_run: bool, commit: bool,
            min_drift: int = MIN_DRIFT_DAYS, max_lapsed: int = MAX_LAPSED) -> dict:
    out: dict = {"before": board_health(), "pushed": False, "restored": 0, "dry_run": dry_run}

    # Cheapest gate first: ask the live app how old its data is. A board that is
    # young and has few lapsed cases cannot possibly need a push, so do not pay
    # for a rebuild to find that out. Unknown age (old deploy, or down) falls
    # through to the rebuild, which measures drift the expensive but sure way.
    if not dry_run:
        out["live_build_age"] = age = live_build_age(base)
        if age is not None and age < min_drift and out["before"]["lapsed"] <= max_lapsed:
            return {**out, "ok": True, "drift_days": age,
                    "note": (f"skipped without rebuilding: live data is {age}d old and "
                             f"{out['before']['lapsed']} case(s) lapsed — pushes at {min_drift}d "
                             f"or >{max_lapsed} lapsed")}

    was = deadlines()

    build = run([sys.executable, "build_db.py"])
    if build.returncode != 0:
        revert_rebuild()
        return {**out, "ok": False, "error": f"build_db failed: {build.stderr.strip()[-400:]}"}
    print(build.stdout.strip()[-300:], flush=True)

    # before the rerender, not after: a rerender re-creates a PDF from every cached
    # draft, orphans included, so the stale drafts have to go while the ids are fresh.
    out["pruned"] = pruned = prune_letters()
    if not pruned["ok"]:
        revert_rebuild()
        return {**out, "ok": False, "error": pruned["note"]}

    letters = run([sys.executable, "generate_letters.py", "--rerender"])
    if letters.returncode != 0:
        revert_rebuild()
        return {**out, "ok": False, "error": f"generate_letters failed: {letters.stderr.strip()[-400:]}"}

    tests = run([sys.executable, "-m", "pytest", "tests", "-q"])
    out["tests"] = tests.stdout.strip().splitlines()[-1] if tests.stdout else ""
    if tests.returncode != 0:
        revert_rebuild()   # never leave a board that failed its own plausibility rules
        return {**out, "ok": False, "error": f"tests failed, nothing pushed: {out['tests']}"}

    out["after"] = board_health()
    out["drift_days"] = drift = drift_days(was, deadlines())
    out["changed_files"] = len(git_changes())

    # a rebuild rewrites the whole DB whether or not the board moved, so "the
    # files changed" is not a reason to ship — only a board that has visibly aged is.
    stale = drift >= min_drift or out["before"]["lapsed"] > max_lapsed
    if dry_run or not stale:
        revert_rebuild()
        note = (f"dry run: rebuilt, tested and reverted (drift {drift}d)" if dry_run else
                f"skipped: only {drift}d of drift and {out['before']['lapsed']} lapsed case(s) "
                f"— pushes at {min_drift}d or >{max_lapsed} lapsed")
        return {**out, "ok": True, "note": note}

    if commit:
        run(["git", "add", "-A", str(HERE)], cwd=REPO)
        run(["git", "commit", "-q", "-m",
             f"musc: refresh demo timeline ({out['after']['appealable']}/{out['after']['denials']} appealable)"],
            cwd=REPO)
        out["commit"] = run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO).stdout.strip()

    snap = demo_state.snapshot(base)          # the volume should survive a deploy; prove it, or put it back
    out["snapshot"] = demo_state.describe(snap)
    print(f"live board before push: {out['snapshot']}", flush=True)

    deploy = push_and_wait("refresh demo timeline")
    out["deploy"] = deploy
    if not deploy["pushed"]:
        return {**out, "ok": False, "error": f"mirror push failed: {deploy['note']}"}
    out["pushed"] = True
    if deploy["live"] is False:
        return {**out, "ok": False,
                "error": f"pushed, but the live app never served the new build ({deploy['note']}) — "
                         "board state NOT restored yet, run demo_state.py restore by hand"}

    if snap["cases"] and demo_state.snapshot(base)["cases"] != snap["cases"]:
        res = demo_state.restore(base, snap, token)   # the deploy lost the volume; put it back
        out["restored"] = res["restored"]

    verified, problems = verify_live()
    out["verified"] = verified
    if not verified:
        out["problems"] = problems
        return {**out, "ok": False,
                "error": f"pushed, but the live demo is broken: {'; '.join(problems) or 'uptime failed'}"
                         f" — roll back with: {rollback_hint(out.get('commit'))}"}
    return {**out, "ok": True}


def rollback_hint(commit: str | None) -> str:
    """The two commands that put the previous board back on the live URL."""
    ref = commit or "HEAD"
    return (f"git revert --no-edit {ref} && "
            f"python tools/mirror.py push musc-appeals -m 'revert refresh {ref}'")


def describe_pruned(res: dict) -> str:
    """What the rebuild deleted. Silent when it deleted nothing — but never implicit."""
    p = res.get("pruned") or {}
    if not (p.get("pdfs") or p.get("drafts")):
        return ""
    return f"; dropped {p['pdfs']} orphan letter(s) and {p['drafts']} stale draft(s)"


def summarise(res: dict) -> str:
    b, a = res.get("before"), res.get("after")
    if not res.get("ok"):
        return f"refresh FAILED: {res.get('error', 'unknown')}"
    if not res.get("pushed"):
        return (f"{res.get('note', 'nothing to do')} ({b['appealable']}/{b['denials']} appealable)"
                f"{describe_pruned(res)}")
    return (f"demo timeline refreshed and verified live: {a['appealable']}/{a['denials']} appealable "
            f"(was {b['appealable']}), {a['due_soon']} due within 14 days, {a['lapsed']} lapsed; "
            f"{res['restored']} in-flight appeal(s) preserved{describe_pruned(res)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=LIVE)
    ap.add_argument("--token", default="", help="DEMO_ADMIN_TOKEN (default: from .env.demo)")
    ap.add_argument("--dry-run", action="store_true", help="rebuild and test, then revert")
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument("--min-drift-days", type=int, default=MIN_DRIFT_DAYS,
                    help=f"days of timeline drift before a push is worth it (default {MIN_DRIFT_DAYS})")
    ap.add_argument("--max-lapsed", type=int, default=MAX_LAPSED,
                    help=f"lapsed cases that force a push regardless of drift (default {MAX_LAPSED})")
    ap.add_argument("--force", action="store_true", help="push even if the board is still fresh")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    # one reader for the token, in demo_state — this is the same cache file, and
    # two parsers of it drift apart in exactly the situation neither can report
    token = demo_state.resolve_token(args.token)
    if not token and not args.dry_run:
        print("state could not be restored after a push, so this run refuses to push.\n"
              + demo_state.no_token_message() + "or run with --dry-run.", file=sys.stderr)
        return 2

    res = refresh(args.base, token, args.dry_run, not args.no_commit,
                  0 if args.force else args.min_drift_days, args.max_lapsed)
    print(json.dumps(res, indent=2) if args.json else summarise(res))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
