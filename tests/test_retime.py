#!/usr/bin/env python3
"""The letter cache has to survive the demo timeline moving.

Claims are anchored to the build date, so rebuilding tomorrow shifts every date.
Ids stay put (so drafts still restore) and retime_drafts.py rewrites the prose to
match -- without either, the demo ships letters arguing about the wrong dates.

Run:  python -m pytest tests/test_retime.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import retime_drafts as rt  # noqa: E402


WAS = {
    "claim_id": "MUSC-ABCD-1", "denial_id": "DEN-MUSC-ABCD-1",
    "service_date": "2026-05-06", "submitted_date": "2026-05-20",
    "denial_date": "2026-06-18", "appeal_deadline": "2026-09-16",
}
NOW = {
    "claim_id": "MUSC-ABCD-1", "denial_id": "DEN-MUSC-ABCD-1",
    "service_date": "2026-06-06", "submitted_date": "2026-06-20",
    "denial_date": "2026-07-18", "appeal_deadline": "2026-10-16",
}


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
    cache, changed = rt.retime({"DEN-MUSC-ABCD-1": draft(before)}, {"DEN-MUSC-ABCD-1": NOW})
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
    cache, _ = rt.retime({"DEN-MUSC-ABCD-1": row}, {"DEN-MUSC-ABCD-1": now})
    assert cache["DEN-MUSC-ABCD-1"]["letter_text"] == "seen March 4, 2026; billed March 8, 2026"


def test_unrelated_dates_are_left_alone():
    """DOBs and clinical history are facts about the patient, not the claim."""
    text = "DOB 1977-08-10; coverage 2024-01-01 through 2026-12-31; stent placed March 10, 2020"
    cache, _ = rt.retime({"DEN-MUSC-ABCD-1": draft(text)}, {"DEN-MUSC-ABCD-1": NOW})
    assert cache["DEN-MUSC-ABCD-1"]["letter_text"] == text


def test_a_renamed_case_is_repointed():
    was = dict(WAS)
    now = {**NOW, "claim_id": "MUSC-WXYZ-2", "denial_id": "DEN-MUSC-WXYZ-2"}
    row = draft("claim MUSC-ABCD-1 (denial DEN-MUSC-ABCD-1)")
    row["draft_dates"] = was
    cache, changed = rt.retime({"DEN-MUSC-WXYZ-2": row}, {"DEN-MUSC-WXYZ-2": now})
    assert cache["DEN-MUSC-WXYZ-2"]["letter_text"] == "claim MUSC-WXYZ-2 (denial DEN-MUSC-WXYZ-2)"
    assert changed == ["DEN-MUSC-WXYZ-2"]


def test_retiming_records_the_new_dates_so_it_is_idempotent():
    cache, _ = rt.retime({"DEN-MUSC-ABCD-1": draft("dated 2026-05-06")}, {"DEN-MUSC-ABCD-1": NOW})
    assert cache["DEN-MUSC-ABCD-1"]["draft_dates"] == NOW
    again, changed = rt.retime(cache, {"DEN-MUSC-ABCD-1": NOW})
    assert changed == [] and again["DEN-MUSC-ABCD-1"]["letter_text"] == "dated 2026-06-06"


def test_a_draft_with_no_recorded_dates_is_adopted_not_mangled():
    row = draft("dated 2026-05-06")
    row.pop("draft_dates")
    cache, changed = rt.retime({"DEN-MUSC-ABCD-1": row}, {"DEN-MUSC-ABCD-1": NOW})
    assert changed == []
    assert cache["DEN-MUSC-ABCD-1"]["draft_dates"] == NOW


def test_shipped_cache_is_in_step_with_the_shipped_database():
    """Whatever day the DB was built, the committed prose must match it."""
    cache = json.loads(rt.DRAFTS_PATH.read_text())
    facts = rt.current_facts()
    assert set(facts) <= set(cache)
    _, changed = rt.retime({k: dict(v) for k, v in cache.items()}, facts)
    assert changed == [], f"letters cite stale dates -- run retime_drafts.py --apply: {changed[:5]}"
