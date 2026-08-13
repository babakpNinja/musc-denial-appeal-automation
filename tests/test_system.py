#!/usr/bin/env python3
"""End-to-end checks for the MUSC appeal automation system.

Run:  python -m pytest tests -q     (from apps/musc-appeal-automation)
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import app as webapp  # noqa: E402

DB = APP_DIR / "data" / "musc_appeals.db"
LETTERS = APP_DIR / "letters"

client = TestClient(webapp.app)


def rows(sql, params=()):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


ALL_DENIALS = rows("SELECT d.*, p.mrn, p.full_name FROM denials d JOIN patients p USING(patient_id)")


# ----------------------------------------------------------------- database

def test_db_populated():
    counts = rows("""SELECT (SELECT COUNT(*) FROM patients) patients,
                            (SELECT COUNT(*) FROM claims) claims,
                            (SELECT COUNT(*) FROM denials) denials,
                            (SELECT COUNT(*) FROM payers) payers,
                            (SELECT COUNT(*) FROM appeals) appeals""")[0]
    assert counts["patients"] == 50
    assert counts["denials"] >= 50
    assert counts["claims"] == counts["denials"]
    assert counts["appeals"] == counts["denials"], "every denial needs a pre-generated appeal"
    assert counts["payers"] >= 8


def test_referential_integrity():
    assert not rows("SELECT 1 FROM denials d LEFT JOIN claims c USING(claim_id) WHERE c.claim_id IS NULL")
    assert not rows("SELECT 1 FROM denials d LEFT JOIN patients p USING(patient_id) WHERE p.patient_id IS NULL")
    assert not rows("SELECT 1 FROM denials d LEFT JOIN payers pay USING(payer_id) WHERE pay.payer_id IS NULL")
    assert not rows("SELECT 1 FROM coverage c LEFT JOIN payers p USING(payer_id) WHERE p.payer_id IS NULL")


def test_denial_fields_are_coded_and_costed():
    bad = rows("""SELECT denial_id FROM denials
                  WHERE carc_code IS NULL OR carc_description IS NULL
                     OR denied_amount IS NULL OR denied_amount <= 0
                     OR appeal_deadline IS NULL OR category IS NULL""")
    assert not bad, bad


def test_every_payer_has_an_appeal_portal_link():
    for p in rows("SELECT payer_id, name, portal_url, appeal_url FROM payers"):
        url = p["appeal_url"] or p["portal_url"]
        assert url and url.startswith("https://"), p


# ----------------------------------------------------------------- letters

@pytest.mark.parametrize("denial", ALL_DENIALS, ids=lambda d: d["denial_id"])
def test_letter_pdf_is_valid_and_case_specific(denial):
    """Every case has a real PDF naming that patient, claim and denial code."""
    path = LETTERS / f"{denial['denial_id']}.pdf"
    assert path.exists(), f"missing letter for {denial['denial_id']}"
    assert path.stat().st_size > 4000, "PDF suspiciously small"

    doc = pdfium.PdfDocument(path)
    assert len(doc) >= 1
    text = "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    squashed = re.sub(r"\s+", " ", text)

    assert denial["mrn"] in squashed
    assert denial["claim_id"] in squashed
    assert denial["full_name"].split()[-1] in squashed
    assert f"CARC {denial['carc_code']}" in squashed
    assert "MUSC" in squashed
    assert "169 Ashley Avenue" in squashed          # letterhead address block
    assert "synthetic" in squashed.lower()          # synthetic-data disclosure
    assert len(squashed.split()) > 250, "letter body too short"
    assert "[insert" not in squashed.lower() and "lorem ipsum" not in squashed.lower()


def test_letters_carry_the_musc_logo():
    """The letterhead image must actually be embedded on page 1."""
    for denial in ALL_DENIALS[:5]:
        doc = pdfium.PdfDocument(LETTERS / f"{denial['denial_id']}.pdf")
        kinds = {obj.type for obj in doc[0].get_objects()}
        assert 3 in kinds, "no image object (logo) on page 1"  # 3 == FPDF_PAGEOBJ_IMAGE


def test_letter_is_addressed_to_the_right_payer():
    for d in ALL_DENIALS[:15]:
        payer = rows("SELECT name FROM payers WHERE payer_id=?", (d["payer_id"],))[0]["name"]
        doc = pdfium.PdfDocument(LETTERS / f"{d['denial_id']}.pdf")
        text = re.sub(r"\s+", " ", doc[0].get_textpage().get_text_range())
        head = payer.split("(")[0].strip()
        assert head[:18] in text, f"{d['denial_id']} not addressed to {payer}"


def test_stored_draft_allows_offline_rerender():
    missing = rows("SELECT denial_id FROM appeals WHERE sections_json IS NULL OR sections_json = ''")
    assert not missing, "drafts must be persisted so PDFs re-render without LLM calls"
    for r in rows("SELECT sections_json FROM appeals LIMIT 5"):
        assert json.loads(r["sections_json"])["sections"]


# ----------------------------------------------------------------- api / ui

def test_health():
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["letters_on_disk"] >= body["denials"]
    # >= let a PDF for a deleted case pad the count forever; the mismatch is the
    # thing worth reporting, so the count has to be exactly the live cases (#118).
    assert (body["orphan_letters"], body["orphan_letter_ids"]) == (0, []), (
        "letter PDFs on disk for cases that are no longer in denials — "
        "generate_letters.py --prune-orphans")


def test_health_says_how_old_the_shipped_data_is():
    """The deployed board has to be able to answer "how old are you?" itself —
    otherwise the only way to know is to rebuild it locally and diff."""
    body = client.get("/api/health").json()
    assert body["built_at"], "shipped DB has no meta.built_at — run build_db.py --stamp"
    assert body["built_days_ago"] >= 0


def test_index_renders():
    r = client.get("/")
    assert r.status_code == 200
    assert "Denial Appeal Automation" in r.text
    assert "synthetic" in r.text.lower()


def test_stats_endpoint():
    s = client.get("/api/stats").json()
    assert s["totals"]["denials"] == len(ALL_DENIALS)
    assert s["totals"]["denied_total"] > 0
    assert len(s["by_payer"]) >= 8
    assert sum(p["denials"] for p in s["by_payer"]) == len(ALL_DENIALS)
    assert abs(sum(p["denied_amount"] for p in s["by_payer"]) - s["totals"]["denied_total"]) < 1


def test_cases_list_and_filters():
    all_cases = client.get("/api/cases").json()
    assert len(all_cases) == len(ALL_DENIALS)
    assert all(c["letter_path"] for c in all_cases), "every case must expose a letter"

    payer = all_cases[0]["payer_id"]
    filtered = client.get(f"/api/cases?payer={payer}").json()
    assert filtered and all(c["payer_id"] == payer for c in filtered)

    searched = client.get(f"/api/cases?search={all_cases[0]['mrn']}").json()
    assert any(c["mrn"] == all_cases[0]["mrn"] for c in searched)

    empty = client.get("/api/cases?search=zzzz-no-such-case").json()
    assert empty == []


def test_case_detail_has_clinical_context_and_portal():
    d = ALL_DENIALS[0]["denial_id"]
    c = client.get(f"/api/cases/{d}").json()
    assert c["denial_id"] == d
    assert c["letter_text"] and len(c["letter_text"]) > 500
    assert c["argument_summary"]
    assert (c["appeal_url"] or c["portal_url"]).startswith("https://")
    assert isinstance(c["conditions"], list)
    assert c["coverage"]["member_id"]
    assert client.get("/api/cases/DEN-does-not-exist").status_code == 404


def test_pdf_download_endpoint():
    d = ALL_DENIALS[0]["denial_id"]
    r = client.get(f"/letters/{d}.pdf?download=1")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"
    assert client.get("/letters/DEN-nope.pdf").status_code == 404


def test_bulk_zip_contains_every_letter():
    import io
    import zipfile
    r = client.get("/letters.zip")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert len(z.namelist()) == len(ALL_DENIALS)
        assert all(n.endswith(".pdf") for n in z.namelist())


def test_payers_endpoint_totals_match():
    payers = client.get("/api/payers").json()
    assert sum(p["denials"] for p in payers) == len(ALL_DENIALS)
    assert all(p["appeal_url"] or p["portal_url"] for p in payers)
