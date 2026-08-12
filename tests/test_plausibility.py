#!/usr/bin/env python3
"""Every case has to survive a revenue-cycle manager reading it.

Synthea's cohort contains people who died in 1950 and people over 100; billing
them a current outpatient procedure discredits the whole demo. These checks pin
the plausibility rules enforced in build_db.py.

Run:  python -m pytest tests/test_plausibility.py -q
"""

from __future__ import annotations

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
