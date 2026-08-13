#!/usr/bin/env python3
"""Every case has to survive a revenue-cycle manager reading it.

Synthea's cohort contains people who died in 1950 and people over 100; billing
them a current outpatient procedure discredits the whole demo. These checks pin
the plausibility rules enforced in build_db.py.

Run:  python -m pytest tests/test_plausibility.py -q
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import build_db  # noqa: E402

DB = APP_DIR / "data" / "musc_appeals.db"


def rows(sql, params=()):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


CASES = rows(
    """SELECT d.denial_id, d.denial_date, d.appeal_deadline, p.full_name, p.birth_date,
              p.deceased_date, c.service_date, c.submitted_date, c.cpt_description,
              cov.period_start, cov.period_end
       FROM denials d
       JOIN claims c USING(claim_id)
       JOIN patients p ON p.patient_id = d.patient_id
       LEFT JOIN coverage cov ON cov.patient_id = d.patient_id"""
)


def _age_at(birth: str, on: str) -> int:
    b = datetime.strptime(birth[:10], "%Y-%m-%d").date()
    o = datetime.strptime(on[:10], "%Y-%m-%d").date()
    return o.year - b.year - ((o.month, o.day) < (b.month, b.day))


def test_cases_exist():
    assert len(CASES) >= 50


def test_no_patient_is_billed_at_an_implausible_age():
    bad = [
        (c["full_name"], _age_at(c["birth_date"], c["service_date"]), c["cpt_description"])
        for c in CASES
        if not 0 <= _age_at(c["birth_date"], c["service_date"]) <= build_db.MAX_CLAIM_AGE
    ]
    assert not bad, f"implausible ages on live claims: {bad}"


def test_no_deceased_patient_has_a_claim():
    bad = [(c["full_name"], c["deceased_date"], c["service_date"]) for c in CASES if c["deceased_date"]]
    assert not bad, f"claims billed for deceased patients: {bad}"


def test_deceased_patients_age_is_age_at_death():
    """A patient who died in 1993 must not render as '113y' in the UI."""
    for p in rows("SELECT full_name, birth_date, deceased_date, age FROM patients WHERE deceased_date != ''"):
        assert p["age"] == _age_at(p["birth_date"], p["deceased_date"]), p


def test_clinical_cohort_is_intact():
    """Excluding patients from billing must not drop them from the clinical data."""
    assert rows("SELECT COUNT(*) n FROM patients")[0]["n"] == 50
    assert rows("SELECT COUNT(*) n FROM conditions")[0]["n"] > 100


def test_service_dates_are_inside_the_coverage_period_and_not_in_the_future():
    today = date.today().isoformat()
    for c in CASES:
        assert c["period_start"] <= c["service_date"] <= c["period_end"], c
        assert c["service_date"] <= today, f"future date of service: {c}"


def test_claim_timeline_is_ordered():
    for c in CASES:
        assert c["service_date"] <= c["submitted_date"] <= c["denial_date"] < c["appeal_deadline"], c


def _days_left(c) -> int:
    return (datetime.strptime(c["appeal_deadline"], "%Y-%m-%d").date() - date.today()).days


def test_the_queue_is_mostly_still_actionable():
    """A work queue of expired deadlines is a dead demo, not a backlog.

    Timelines are anchored to today at build time (build_db._window_fraction), so
    this holds on whatever day the database was last built.
    """
    left = [_days_left(c) for c in CASES]
    open_now = sum(1 for d in left if d > 0)
    assert open_now / len(left) >= 0.70, f"only {open_now}/{len(left)} cases can still be appealed"


def test_a_few_deadlines_are_lapsed_or_imminent():
    """...but not *all* of them are comfortable: the urgency is the point."""
    left = [_days_left(c) for c in CASES]
    assert any(d < 0 for d in left), "nothing overdue -- the escalation path is untested"
    assert sum(1 for d in left if d < 0) <= len(left) * 0.20, "too much of the queue has lapsed"
    assert any(0 <= d <= 14 for d in left), "nothing due soon -- the amber warning never shows"


def test_lapsed_deadlines_are_recent_misses():
    """A deadline blown by a year reads as neglect; by a fortnight, as a backlog."""
    for c in CASES:
        assert _days_left(c) > -60, f"deadline lapsed long ago: {c['denial_id']} {c['appeal_deadline']}"


def test_deadline_matches_the_payers_contractual_window():
    windows = {p["payer_id"]: p["appeal_window_days"] for p in build_db.PAYERS}
    for c in rows("SELECT denial_id, payer_id, denial_date, appeal_deadline FROM denials"):
        denied = datetime.strptime(c["denial_date"], "%Y-%m-%d").date()
        due = datetime.strptime(c["appeal_deadline"], "%Y-%m-%d").date()
        assert (due - denied).days == windows[c["payer_id"]], c


def test_claim_ids_carry_no_date():
    """Ids must survive a rebuild on a different day, or the letter cache dies."""
    for c in rows("SELECT claim_id, denial_id FROM denials JOIN claims USING(claim_id)"):
        assert not re.search(r"20\d{6}", c["claim_id"]), c
        assert c["denial_id"] == f"DEN-{c['claim_id']}"


def test_window_fraction_spans_the_deadline():
    lo = build_db._window_fraction(45)
    hi = build_db._window_fraction(419)
    assert lo == build_db.AGE_MIN_FRACTION and hi == build_db.AGE_MAX_FRACTION
    assert lo < 1 < hi, "the range has to straddle the deadline to produce both states"
    assert build_db._window_fraction(45) <= build_db._window_fraction(200) <= hi


def test_is_billable_rules():
    assert build_db.is_billable("1980-05-01", "", "2026-01-01")
    assert not build_db.is_billable("1980-05-01", "2012-06-16", "2026-01-01")  # deceased
    assert not build_db.is_billable("1913-01-13", "", "2026-01-01")  # 113 years old
    assert not build_db.is_billable("2027-01-01", "", "2026-01-01")  # unborn
    assert build_db.is_billable("2020-03-02", "", "2026-01-01")  # paediatric is fine


def test_letter_drafts_survive_a_rebuild(tmp_path):
    """A rebuild must restore cached drafts, never trigger a re-generation."""
    cache = build_db.DRAFTS_PATH
    assert cache.exists(), "letter draft cache must be committed alongside the DB"
    live = {r["denial_id"] for r in rows("SELECT denial_id FROM denials")}
    import json

    cached = json.loads(cache.read_text())
    assert live <= set(cached), "every denial needs a cached draft to re-render from"


# --- identity a rebuild must not reinvent (#8) -------------------------------

def test_the_mrn_is_the_same_on_every_rebuild():
    """`hash()` is salted per process, so this used to change on every build.

    Run in child processes: within one interpreter the salt is fixed, so the old
    `abs(hash(pid))` would have looked perfectly stable here.
    """
    import subprocess

    code = ("import sys; sys.path.insert(0, %r); import build_db; "
            "print(build_db.mrn_for('c-fake-patient-1'))" % str(APP_DIR))
    seen = {subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, check=True).stdout.strip() for _ in range(3)}
    assert len(seen) == 1, f"MRN differs between processes: {seen}"
    assert re.fullmatch(r"MUSC\d{7}", seen.pop())


def test_the_mrn_a_letter_cites_is_the_one_on_the_chart():
    """A letter naming an MRN its patient does not have contradicts the dashboard.

    Not hypothetical: before mrn_for(), a single shipped PDF cited two MRNs, and
    neither belonged to the patient it was appealing for.
    """
    import json

    mrn = {r["patient_id"]: r["mrn"] for r in rows("SELECT patient_id, mrn FROM patients")}
    owner = {r["denial_id"]: r["patient_id"] for r in rows("SELECT denial_id, patient_id FROM denials")}
    cache = json.loads(build_db.DRAFTS_PATH.read_text())
    wrong = {did: sorted(set(re.findall(r"MUSC\d{7}", json.dumps(rec))) - {mrn[owner[did]]})
             for did, rec in cache.items() if did in owner}
    assert not {k: v for k, v in wrong.items() if v}, "drafts cite an MRN their patient lacks"


def test_no_patient_carries_an_implausible_pile_of_open_denials():
    counts = [r["n"] for r in rows(
        "SELECT COUNT(*) n FROM denials GROUP BY patient_id")]
    assert max(counts) <= build_db.MAX_CLAIMS_PER_PATIENT, (
        f"a patient has {max(counts)} open denials, which reads as generated")


def test_the_queue_is_big_enough_to_be_worth_automating():
    """The point of the product is volume; a handful of cases undersells it."""
    n = rows("SELECT COUNT(*) n FROM denials")[0]["n"]
    assert n >= 80, f"only {n} denials on the board"


def test_no_cached_letter_argues_a_claim_that_moved_under_it():
    """A draft's id staying put does not mean its claim did.

    The cache is keyed by denial id, so a build that reshuffles the draws inside
    the claim loop hands DEN-MUSC-3CE3-3 a different amount, CPT and denial
    reason while the cached prose restores, renders and reads perfectly. Proven
    live: widening `n_claims` by one choice moved 6 drafts onto other claims
    (billed_amount 1082.64 -> 537.56, carc_code 27 -> 197, ...), which is why
    extra volume is drawn from `random.Random(f"{pid}:extra")` instead.
    """
    import json

    import retime_drafts

    moved = retime_drafts.rewritten_claims(
        json.loads(build_db.DRAFTS_PATH.read_text()), retime_drafts.current_facts())
    assert not moved, "cached letters argue claims the database reassigned: " + ", ".join(
        f"{did} ({', '.join(f'{k} {a} -> {b}' for k, (a, b) in m.items())})"
        for did, m in list(moved.items())[:3])


def test_the_readme_quotes_the_board_it_actually_ships():
    """The one count a client reads before opening the demo.

    Grown 67 -> 86 by #8, and the README still said 67 an hour later; a hand-typed
    number next to a generated one drifts every single time.
    """
    import re

    readme = (build_db.HERE / "README.md").read_text()
    claimed = re.search(r"\|\s*\*\*Denials\*\*\s*\|\s*(\d+)\s+denials", readme)
    assert claimed, "README no longer states a denial count in the shape this checks"
    actual = rows("SELECT COUNT(*) n FROM denials")[0]["n"]
    assert int(claimed.group(1)) == actual, (
        f"README advertises {claimed.group(1)} denials, the database has {actual}")
