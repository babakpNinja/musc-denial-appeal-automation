#!/usr/bin/env python3
"""The letter cache has to survive the demo timeline moving.

Claims are anchored to the build date, so rebuilding tomorrow shifts every date.
Ids stay put (so drafts still restore) and retime_drafts.py rewrites the prose to
match -- without either, the demo ships letters arguing about the wrong dates.

Run:  python -m pytest tests/test_retime.py -q
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import retime_drafts as rt  # noqa: E402


WAS = {
    "claim_id": "MUSC-ABCD-1", "denial_id": "DEN-MUSC-ABCD-1",
    "service_date": "2026-05-06", "submitted_date": "2026-05-20",
    "denial_date": "2026-06-18", "appeal_deadline": "2026-09-16", "mrn": "MUSC0001234",
}
NOW = {
    "claim_id": "MUSC-ABCD-1", "denial_id": "DEN-MUSC-ABCD-1",
    "service_date": "2026-06-06", "submitted_date": "2026-06-20",
    "denial_date": "2026-07-18", "appeal_deadline": "2026-10-16", "mrn": "MUSC0001234",
}


def recorded(facts: dict) -> dict:
    """The subset of a fact dict a draft writes down — the values that can move.

    Not every fact is one of them: the MRN is re-pointed against the database
    unconditionally, so there is nothing to remember. Comparing against the whole
    dict made these tests fail the moment a new fact was added.
    """
    return {k: facts[k] for k in ("claim_id", "denial_id") + rt.DATE_KEYS}


def draft(text: str) -> dict:
    return {"letter_text": text, "argument_summary": text,
            "sections_json": json.dumps({"sections": {"body": text}}),
            "draft_dates": dict(WAS)}


@pytest.mark.parametrize("before,after", [
    ("service rendered 2026-05-06", "service rendered 2026-06-06"),
    ("service rendered on May 6, 2026", "service rendered on June 6, 2026"),
    ("denied May 06, 2026", "denied June 06, 2026"),
    ("filed 5/6/2026", "filed 6/6/2026"),
    ("filed 05/06/2026", "filed 06/06/2026"),
    ("the May 2026 admission", "the June 2026 admission"),
    ("appeal is due by 2026-09-16", "appeal is due by 2026-10-16"),
    ("claim MUSC-ABCD-1 stays put", "claim MUSC-ABCD-1 stays put"),
])
def test_every_date_format_the_letters_use_is_rewritten(before, after):
    cache, changed, _ = rt.retime({"DEN-MUSC-ABCD-1": draft(before)}, {"DEN-MUSC-ABCD-1": NOW})
    assert cache["DEN-MUSC-ABCD-1"]["letter_text"] == after


def test_substitutions_do_not_chain():
    """A real case had one date's *new* value equal to another's old value.

    Replacing sequentially rewrote what the previous replacement had just written,
    so the whole set has to be applied in a single pass.
    """
    was = {**WAS, "service_date": "2026-03-08", "submitted_date": "2026-03-12"}
    now = {**NOW, "service_date": "2026-03-04", "submitted_date": "2026-03-08"}
    row = draft("seen March 8, 2026; billed March 12, 2026")
    row["draft_dates"] = was
    cache, _, _ = rt.retime({"DEN-MUSC-ABCD-1": row}, {"DEN-MUSC-ABCD-1": now})
    assert cache["DEN-MUSC-ABCD-1"]["letter_text"] == "seen March 4, 2026; billed March 8, 2026"


def test_unrelated_dates_are_left_alone():
    """DOBs and clinical history are facts about the patient, not the claim."""
    text = "DOB 1977-08-10; coverage 2024-01-01 through 2026-12-31; stent placed March 10, 2020"
    cache, _, _ = rt.retime({"DEN-MUSC-ABCD-1": draft(text)}, {"DEN-MUSC-ABCD-1": NOW})
    assert cache["DEN-MUSC-ABCD-1"]["letter_text"] == text


def test_a_renamed_case_is_repointed():
    was = dict(WAS)
    now = {**NOW, "claim_id": "MUSC-WXYZ-2", "denial_id": "DEN-MUSC-WXYZ-2"}
    row = draft("claim MUSC-ABCD-1 (denial DEN-MUSC-ABCD-1)")
    row["draft_dates"] = was
    cache, changed, _ = rt.retime({"DEN-MUSC-WXYZ-2": row}, {"DEN-MUSC-WXYZ-2": now})
    assert cache["DEN-MUSC-WXYZ-2"]["letter_text"] == "claim MUSC-WXYZ-2 (denial DEN-MUSC-WXYZ-2)"
    assert changed == ["DEN-MUSC-WXYZ-2"]


def test_retiming_records_the_new_dates_so_it_is_idempotent():
    cache, _, _ = rt.retime({"DEN-MUSC-ABCD-1": draft("dated 2026-05-06")}, {"DEN-MUSC-ABCD-1": NOW})
    assert cache["DEN-MUSC-ABCD-1"]["draft_dates"] == recorded(NOW)
    again, changed, _ = rt.retime(cache, {"DEN-MUSC-ABCD-1": NOW})
    assert changed == [] and again["DEN-MUSC-ABCD-1"]["letter_text"] == "dated 2026-06-06"


def test_a_draft_with_no_recorded_dates_is_adopted_not_mangled():
    row = draft("dated 2026-05-06")
    row.pop("draft_dates")
    cache, changed, _ = rt.retime({"DEN-MUSC-ABCD-1": row}, {"DEN-MUSC-ABCD-1": NOW})
    assert changed == []
    assert cache["DEN-MUSC-ABCD-1"]["draft_dates"] == recorded(NOW)


def test_shipped_cache_is_in_step_with_the_shipped_database():
    """Whatever day the DB was built, the committed prose must match it."""
    cache = json.loads(rt.DRAFTS_PATH.read_text())
    facts = rt.current_facts()
    assert set(facts) <= set(cache)
    _, changed, _ = rt.retime({k: dict(v) for k, v in cache.items()}, facts)
    assert changed == [], f"letters cite stale dates -- run retime_drafts.py --apply: {changed[:5]}"


# --- adding volume must not rewrite the cases already drafted (#8) -----------
# The board was thinned by a plausibility pass and needed to grow again. The
# tempting way — widen `n_claims` — shifts every later draw from a patient's RNG,
# so DEN-MUSC-17FF-1 keeps its id while becoming a different claim: different
# amount, CPT, payer, denial reason. Its cached LLM letter still restores under
# that id and now argues about a claim that does not exist. Nothing raises.
# So the extra claims draw from a stream of their own, and this is the check that
# says so: build with and without them and compare the overlap row by row.

def _build(tmp_path, name, extra):
    import build_db

    was = build_db.EXTRA_CLAIMS
    build_db.EXTRA_CLAIMS = extra
    try:
        db = tmp_path / f"{name}.db"
        build_db.build(db_path=db, drafts_path=tmp_path / f"{name}-drafts.json")
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        out = {t: {r[0]: dict(r) for r in con.execute(f"SELECT * FROM {t}")}
               for t in ("claims", "denials")}
        con.close()
        return out
    finally:
        build_db.EXTRA_CLAIMS = was


def test_the_extra_claims_leave_every_original_case_untouched(tmp_path):
    thin = _build(tmp_path, "thin", [0])
    full = _build(tmp_path, "full", [0, 0, 0, 1, 1, 2])
    for table in ("claims", "denials"):
        assert set(thin[table]) < set(full[table]), f"{table} did not grow"
        for key, row in thin[table].items():
            assert full[table][key] == row, (
                f"{table} {key} changed when volume was added — every cached letter "
                "under this id is now describing a different claim")


def test_the_extra_claims_bill_encounters_of_their_own(tmp_path):
    """Two claims against one encounter would be a duplicate, not extra volume."""
    full = _build(tmp_path, "full", [0, 0, 0, 1, 1, 2])
    seen = [r["encounter_id"] for r in full["claims"].values()]
    assert len(seen) == len(set(seen))


def test_no_shipped_draft_argues_a_claim_the_board_no_longer_has():
    """The check the id-stability contract needs, on the committed data.

    `draft_dates` only made date drift detectable. If a claim's amount, CPT, payer
    or denial reason moves under a stable denial id — which is what happens when
    the draw order in the claim loop changes — the letter restores and renders
    perfectly while arguing a case that does not exist.
    """
    cache = json.loads(rt.DRAFTS_PATH.read_text())
    moved = rt.rewritten_claims(cache, rt.current_facts())
    assert not moved, ("these letters argue the wrong claim and need re-drafting: "
                       f"{list(moved.items())[:3]}")


def test_a_moved_amount_is_reported_and_not_quietly_repointed():
    facts = {"DEN-MUSC-ABCD-1": dict(NOW, payer_id="bcbs", cpt_code="99213",
                                     billed_amount=250.0, carc_code="50",
                                     category="Medical necessity")}
    row = draft("billed $250.00")
    row["draft_facts"] = {"payer_id": "bcbs", "cpt_code": "99213", "billed_amount": 250.0,
                          "carc_code": "50", "category": "Medical necessity"}
    assert rt.rewritten_claims({"DEN-MUSC-ABCD-1": row}, facts) == {}

    facts["DEN-MUSC-ABCD-1"]["billed_amount"] = 981.4
    moved = rt.rewritten_claims({"DEN-MUSC-ABCD-1": row}, facts)
    assert moved == {"DEN-MUSC-ABCD-1": {"billed_amount": (250.0, 981.4)}}
    # and the prose is left alone: only a re-draft can argue a different figure
    cache, _, _ = rt.retime({"DEN-MUSC-ABCD-1": row}, facts)
    assert cache["DEN-MUSC-ABCD-1"]["letter_text"] == "billed $250.00"


def test_a_draft_seen_for_the_first_time_is_adopted_not_accused():
    row = draft("billed $250.00")
    facts = {"DEN-MUSC-ABCD-1": dict(NOW, payer_id="bcbs", cpt_code="99213",
                                     billed_amount=250.0, carc_code="50",
                                     category="Medical necessity")}
    assert rt.rewritten_claims({"DEN-MUSC-ABCD-1": row}, facts) == {}
    cache, _, _ = rt.retime({"DEN-MUSC-ABCD-1": row}, facts)
    assert cache["DEN-MUSC-ABCD-1"]["draft_facts"]["billed_amount"] == 250.0
