#!/usr/bin/env python3
"""When the scheduled refresh decides to ship, and when it keeps quiet.

The rebuild rewrites every letter each time it runs, so the deploy would churn
daily if "the files changed" were the trigger. These pin the actual rule: age.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import refresh_demo as rd  # noqa: E402


GREEN = json.dumps([{"target": "musc-appeals", "ok": True,
                     "checks": [{"url": "https://x/", "ok": True, "problems": []}]}])
RED = json.dumps([{"target": "musc-appeals", "ok": False,
                   "checks": [{"url": "https://x/", "ok": False,
                               "problems": ["'#caseRows tr' never rendered"]}]}])


def mirror_report(live: bool = True, pushed: bool = True) -> str:
    """What `mirror.py push --wait --json` says: pushed, and is the new build serving?"""
    return json.dumps({"target": "musc-appeals", "repo": "x/y", "changes": [" M data/musc_appeals.db"],
                       "pushed": pushed, "commit": "deadbee1234",
                       "wait": {"live": live, "proven": live, "waited": 61,
                                "note": "serving deadbee after 61s" if live
                                        else "never served deadbee within 900s"}})


def prune_report(pdfs: int = 0, drafts: int = 0) -> str:
    """What `generate_letters.py --prune-orphans --json` says it removed."""
    ids = [f"DEN-MUSC-OLD-{i}" for i in range(pdfs)]
    return json.dumps({"found": {"pdfs": ids, "drafts": ids[:drafts]},
                       "pruned": {"pdfs": pdfs, "drafts": drafts}})


class FakeRun:
    """Stand in for build_db/generate_letters/pytest/git/mirror.py/uptime.py."""

    def __init__(self, fail: str | None = None, uptime: str = GREEN, mirror: str | None = None,
                 prune: str | None = None):
        self.fail, self.uptime, self.calls = fail, uptime, []
        self.mirror = mirror or mirror_report()
        self.prune = prune or prune_report()

    def __call__(self, cmd, cwd=None, timeout=None):
        joined = " ".join(str(c) for c in cmd)
        self.calls.append(joined)
        rc = 1 if self.fail and self.fail in joined else 0
        if "uptime.py" in joined:
            out, rc = self.uptime, 0 if self.uptime == GREEN else 1
        elif "mirror.py" in joined:
            out = self.mirror
        elif "--prune-orphans" in joined:
            out = self.prune
        elif "pytest" in joined:
            out = "155 passed in 4.8s"
        elif "rev-parse" in joined:
            out = "abc1234"
        else:
            out = " M data/musc_appeals.db"
        return type("P", (), {"returncode": rc, "stdout": out, "stderr": "boom"})()

    def ran(self, needle: str, before_revert: bool = False) -> bool:
        """Did this command run — optionally only counting the run itself.

        The revert re-runs generate_letters to rebuild the letters git cannot restore,
        so "did the rebuild get that far" has to stop at the `git checkout`.
        """
        calls = self.calls
        if before_revert:
            cut = next((i for i, c in enumerate(calls) if "git checkout" in c), len(calls))
            calls = calls[:cut]
        return any(needle in c for c in calls)


@pytest.fixture()
def rig(monkeypatch):
    """A board that is `drift` days old, with the plumbing faked out."""
    run = FakeRun()
    monkeypatch.setattr(rd, "run", run)
    # by default the live deploy cannot say how old it is, so every run has to
    # rebuild to find out — the tests below that care set an age explicitly
    monkeypatch.setattr(rd, "live_build_age", lambda base: None)

    def setup(drift: int, lapsed: int = 5, fail: str | None = None, uptime: str = GREEN,
              prune: str | None = None):
        run.fail, run.uptime = fail, uptime
        if prune is not None:
            run.prune = prune
        state = {"n": 0}

        def deadlines(db_path=None):
            state["n"] += 1
            day = date(2026, 9, 10)                        # before the rebuild, then after
            return {"DEN-1": (day if state["n"] == 1 else day + timedelta(days=drift)).isoformat()}

        monkeypatch.setattr(rd, "deadlines", deadlines)
        monkeypatch.setattr(rd, "board_health",
                            lambda db_path=None: {"denials": 67, "appealable": 67 - lapsed,
                                                  "due_soon": 3, "lapsed": lapsed, "worst_lapse": -drift})
        return run

    return setup


def snapshots(monkeypatch, cases, after=None):
    seen = {"restored": 0}
    calls = {"n": 0}

    def snapshot(base):
        calls["n"] += 1
        return {"cases": cases if calls["n"] == 1 or after is None else after,
                "cases_total": 67}

    monkeypatch.setattr(rd.demo_state, "snapshot", snapshot)
    monkeypatch.setattr(rd.demo_state, "describe", lambda s: f"{len(s['cases'])} moved")
    monkeypatch.setattr(rd.demo_state, "restore",
                        lambda base, snap, token: seen.update(restored=len(snap["cases"])) or {"restored": len(snap["cases"])})
    return seen


def test_a_board_that_is_barely_a_week_old_is_left_alone(rig):
    run = rig(drift=6)
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)
    assert res["ok"] and not res["pushed"]
    assert "skipped" in res["note"] and res["drift_days"] == 6
    assert run.ran("git checkout")          # the local rebuild is undone
    assert not run.ran("mirror.py")


def test_a_stale_board_is_rebuilt_and_pushed(rig, monkeypatch):
    run = rig(drift=30)
    snapshots(monkeypatch, [])
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)
    assert res["ok"] and res["pushed"] and res["drift_days"] == 30
    assert run.ran("build_db.py") and run.ran("--rerender") and run.ran("pytest")
    assert run.ran("mirror.py push musc-appeals")


def test_too_many_lapsed_cases_force_a_push_before_the_drift_threshold(rig, monkeypatch):
    run = rig(drift=8, lapsed=20)
    snapshots(monkeypatch, [])
    assert rd.refresh("http://x", "t", dry_run=False, commit=True)["pushed"]
    assert run.ran("mirror.py")


def test_a_fresh_live_board_is_left_alone_without_paying_for_a_rebuild(rig, monkeypatch):
    """/api/health already knows the age; rebuilding to learn it costs a full re-render."""
    run = rig(drift=6)
    monkeypatch.setattr(rd, "live_build_age", lambda base: 6)
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)

    assert res["ok"] and not res["pushed"] and res["live_build_age"] == 6
    assert "without rebuilding" in res["note"]
    assert not run.ran("build_db.py") and not run.ran("--rerender") and not run.ran("mirror.py")


def test_lapsed_cases_still_force_a_rebuild_however_young_the_build_is(rig, monkeypatch):
    run = rig(drift=8, lapsed=20)
    monkeypatch.setattr(rd, "live_build_age", lambda base: 8)
    snapshots(monkeypatch, [])
    assert rd.refresh("http://x", "t", dry_run=False, commit=True)["pushed"]
    assert run.ran("build_db.py")


def test_a_deploy_that_cannot_say_its_age_is_measured_the_expensive_way(rig, monkeypatch):
    """Unknown age must never read as fresh — fall through and rebuild."""
    run = rig(drift=30)
    monkeypatch.setattr(rd, "live_build_age", lambda base: None)
    snapshots(monkeypatch, [])
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)
    assert res["pushed"] and res["live_build_age"] is None and run.ran("build_db.py")


def test_force_skips_the_cheap_gate_too(rig, monkeypatch):
    run = rig(drift=1)
    monkeypatch.setattr(rd, "live_build_age", lambda base: 0)
    snapshots(monkeypatch, [])
    assert rd.refresh("http://x", "t", dry_run=False, commit=True, min_drift=0)["pushed"]
    assert run.ran("build_db.py")


def test_a_dry_run_still_rebuilds_because_that_is_what_it_is_for(rig, monkeypatch):
    run = rig(drift=2)
    monkeypatch.setattr(rd, "live_build_age", lambda base: 2)
    res = rd.refresh("http://x", "", dry_run=True, commit=True)
    assert res["ok"] and "dry run" in res["note"]
    assert run.ran("build_db.py") and run.ran("pytest") and not run.ran("mirror.py")


def test_a_failing_test_aborts_before_anything_is_pushed(rig):
    run = rig(drift=60, fail="pytest")
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)
    assert not res["ok"] and "tests failed" in res["error"]
    assert run.ran("git checkout") and not run.ran("mirror.py")


def test_a_failing_rebuild_leaves_the_working_tree_clean(rig):
    run = rig(drift=60, fail="build_db.py")
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)
    assert not res["ok"] and "build_db failed" in res["error"]
    assert run.ran("git checkout")
    assert not run.ran("generate_letters.py", before_revert=True)


def test_dry_run_never_pushes_however_stale_the_board_is(rig, monkeypatch):
    run = rig(drift=365)
    monkeypatch.setattr(rd.demo_state, "snapshot",
                        lambda base: pytest.fail("dry run must not touch the live service"))
    res = rd.refresh("http://x", "", dry_run=True, commit=True)
    assert res["ok"] and not res["pushed"] and "dry run" in res["note"]
    assert run.ran("git checkout") and not run.ran("mirror.py")


def test_workflow_state_is_only_rewritten_when_the_deploy_actually_lost_it(rig, monkeypatch):
    rig(drift=30)
    kept = [{"denial_id": "DEN-1", "status": "submitted"}]
    seen = snapshots(monkeypatch, kept)                       # same board after the deploy
    assert rd.refresh("http://x", "t", dry_run=False, commit=True)["restored"] == 0
    assert seen["restored"] == 0

    rig(drift=30)                                             # a second, equally stale run
    seen = snapshots(monkeypatch, kept, after=[])             # volume came back empty
    assert rd.refresh("http://x", "t", dry_run=False, commit=True)["restored"] == 1
    assert seen["restored"] == 1


def test_a_deploy_that_never_serves_the_new_data_stops_short_of_restoring(rig, monkeypatch):
    run = rig(drift=30)
    run.mirror = mirror_report(live=False)
    snapshots(monkeypatch, [{"denial_id": "DEN-1", "status": "submitted"}])
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)
    assert not res["ok"] and "never served" in res["error"] and res["pushed"]
    assert "restore by hand" in res["error"]
    assert not run.ran("uptime.py")          # nothing is verified against the old container


def test_the_push_asks_the_mirror_to_wait_for_the_new_container(rig, monkeypatch):
    run = rig(drift=30)
    snapshots(monkeypatch, [])
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)
    assert res["ok"] and run.ran("mirror.py push musc-appeals --wait --json")
    assert res["deploy"]["proven"] and res["deploy"]["commit"] == "deadbee1234"


def test_a_mirror_that_reports_nothing_is_a_failed_push_not_a_silent_pass(rig, monkeypatch):
    run = rig(drift=30)
    run.mirror = "Traceback (most recent call last): gh: not found"
    snapshots(monkeypatch, [])
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)
    assert not res["ok"] and not res["pushed"] and "no report" in res["error"]


def test_drift_is_the_median_shift_so_one_odd_case_cannot_skew_it():
    before = {"a": "2026-01-01", "b": "2026-01-02", "c": "2026-01-03"}
    after = {"a": "2026-02-01", "b": "2026-02-01", "c": "2026-06-01"}
    assert rd.drift_days(before, after) == 31   # 31, 30, 149 -> the middle one

    assert rd.drift_days({}, {}) == 0


def test_summary_reads_like_a_slack_line(rig, monkeypatch):
    rig(drift=30)
    snapshots(monkeypatch, [])
    line = rd.summarise(rd.refresh("http://x", "t", dry_run=False, commit=True))
    assert "appealable" in line and "62/67" in line
    assert "FAILED" in rd.summarise({"ok": False, "error": "nope"})


def test_a_push_that_leaves_the_demo_broken_is_reported_as_a_failure(rig, monkeypatch):
    run = rig(drift=30, uptime=RED)
    snapshots(monkeypatch, [])
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)

    assert res["pushed"] and res["verified"] is False and not res["ok"]
    assert "never rendered" in res["error"]
    assert "git revert --no-edit abc1234" in res["error"]     # the way back is in the message
    assert run.ran("uptime.py musc-appeals --json")
    assert "FAILED" in rd.summarise(res)                      # never a cheerful line


def test_an_uptime_that_cannot_even_run_counts_as_unverified(rig, monkeypatch):
    rig(drift=30, uptime="not json at all")
    snapshots(monkeypatch, [])
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)
    assert not res["ok"] and res["verified"] is False
    assert "uptime produced no report" in res["error"]


# ---- the rebuild has to clear up after itself, or it reverts forever (#125)


def order(run, *needles: str) -> list[int]:
    return [next(i for i, c in enumerate(run.calls) if n in c) for n in needles]


def after_revert(run) -> list[str]:
    cut = next((i for i, c in enumerate(run.calls) if "git checkout" in c), len(run.calls))
    return run.calls[cut:]


def test_a_revert_puts_back_the_letters_git_cannot_restore(rig):
    """The prune deleted untracked files; only a rerender brings the live ones back."""
    run = rig(drift=60, fail="pytest", prune=prune_report(pdfs=4, drafts=4))
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)

    assert not res["ok"] and "tests failed" in res["error"]
    tail = after_revert(run)
    assert any("--prune-orphans" in c for c in tail), "letters left over from the abandoned build"
    assert any("--rerender" in c for c in tail), "reverted drafts with no PDFs behind them"


def test_the_rebuild_prunes_the_old_ids_before_the_rerender(rig, monkeypatch):
    """A rerender re-creates a PDF from every cached draft, orphans included."""
    run = rig(drift=30, prune=prune_report(pdfs=4, drafts=4))
    snapshots(monkeypatch, [])
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)

    assert res["ok"] and res["pruned"] == {"ok": True, "pdfs": 4, "drafts": 4}
    build, prune, rerender = order(run, "build_db.py", "--prune-orphans", "--rerender")
    assert build < prune < rerender
    assert "dropped 4 orphan letter(s) and 4 stale draft(s)" in rd.summarise(res)


def test_a_rebuild_with_nothing_to_drop_says_nothing_about_it(rig, monkeypatch):
    run = rig(drift=30)
    snapshots(monkeypatch, [])
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)
    assert res["pruned"] == {"ok": True, "pdfs": 0, "drafts": 0} and run.ran("--prune-orphans")
    assert "dropped" not in rd.summarise(res)


def test_a_prune_that_cannot_report_is_a_failure_not_a_zero(rig):
    """Silently reading 0 removed would put the run straight back in the revert loop."""
    run = rig(drift=60, prune="Traceback (most recent call last): sqlite3.OperationalError")
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)
    assert not res["ok"] and "prune produced no report" in res["error"]
    assert run.ran("git checkout") and not run.ran("mirror.py")
    assert not run.ran("--rerender", before_revert=True)


def test_a_failing_prune_reverts_before_the_rerender(rig):
    run = rig(drift=60, fail="--prune-orphans")
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)
    assert not res["ok"] and "prune failed" in res["error"]
    assert run.ran("git checkout") and not run.ran("pytest")
    assert not run.ran("--rerender", before_revert=True)


def test_a_dry_run_prunes_too_because_that_is_what_it_is_rehearsing(rig, monkeypatch):
    run = rig(drift=2, prune=prune_report(pdfs=2, drafts=2))
    res = rd.refresh("http://x", "", dry_run=True, commit=True)
    assert res["ok"] and run.ran("--prune-orphans") and res["pruned"]["pdfs"] == 2
    assert "dropped 2 orphan letter(s)" in rd.summarise(res)   # even the skipped line owns up


def test_a_prune_report_is_named_in_the_json_so_a_deletion_is_never_silent(rig, monkeypatch):
    rig(drift=30, prune=prune_report(pdfs=3, drafts=1))
    snapshots(monkeypatch, [])
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)
    assert res["pruned"]["pdfs"] == 3 and res["pruned"]["drafts"] == 1


def test_a_verified_push_says_so(rig, monkeypatch):
    rig(drift=30)
    snapshots(monkeypatch, [])
    res = rd.refresh("http://x", "t", dry_run=False, commit=True)
    assert res["ok"] and res["verified"] is True
    assert "verified live" in rd.summarise(res)
