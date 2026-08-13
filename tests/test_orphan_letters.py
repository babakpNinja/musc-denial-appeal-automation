#!/usr/bin/env python3
"""Letter artifacts for a denial id that no longer exists (#118).

`letters/*.pdf` and `data/letter_drafts.json` are two stores beside `denials`, and
neither can be constrained by it: the mirror's `keep: letters/*.pdf` rule holds the
PDFs in the deploy repo across ships, and `dump_drafts` writes the whole cache back
out. So a rebuild that renumbers ids (#11 did) leaves files that get served, get
counted in `letters_on_disk`, and are linked from nowhere.

Sibling of the workflow-row orphans in test_demo_state.py.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import app as webapp  # noqa: E402
import generate_letters  # noqa: E402

GONE = "DEN-MUSC-GONE-1"
client = TestClient(webapp.app)


def live_id() -> str:
    return webapp.q("SELECT denial_id FROM denials ORDER BY denial_id LIMIT 1")[0]["denial_id"]


@pytest.fixture
def letters(tmp_path, monkeypatch):
    """A letters dir holding one real case's PDF and one orphan."""
    d = tmp_path / "letters"
    d.mkdir()
    did = live_id()
    shutil.copy(APP_DIR / "letters" / f"{did}.pdf", d / f"{did}.pdf")
    (d / f"{GONE}.pdf").write_bytes(b"%PDF-1.4 left over by a rebuild\n")
    monkeypatch.setattr(webapp, "LETTERS", d)
    return d


def test_health_names_an_orphan_pdf(letters):
    body = client.get("/api/health").json()
    assert body["orphan_letters"] == 1
    assert body["orphan_letter_ids"] == [GONE]
    # the raw count is exactly why it needed naming: 2 >= 67 is false here, but on
    # the live board an orphan simply pads a number that already passes
    assert body["letters_on_disk"] == 2


def test_the_real_tree_has_none():
    body = client.get("/api/health").json()
    assert (body["orphan_letters"], body["orphan_letter_ids"]) == (0, [])


def test_an_orphan_pdf_is_not_served_even_though_the_file_exists(letters):
    assert (letters / f"{GONE}.pdf").exists()
    r = client.get(f"/letters/{GONE}.pdf")
    assert r.status_code == 404
    assert r.json()["detail"] == "no such denial"


def test_a_live_case_still_downloads(letters):
    r = client.get(f"/letters/{live_id()}.pdf?download=1")
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"


def test_the_bulk_zip_never_picks_up_an_orphan(letters):
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(client.get("/letters.zip").content)) as z:
        assert z.namelist() == [f"MUSC-Appeal-{live_id()}.pdf"]


# ------------------------------------------------------- generate_letters.py


@pytest.fixture
def artifacts(tmp_path):
    """A letters dir and a drafts cache, each with one live and one dead id."""
    d = tmp_path / "letters"
    d.mkdir()
    did = live_id()
    (d / f"{did}.pdf").write_bytes(b"%PDF-live\n")
    (d / f"{GONE}.pdf").write_bytes(b"%PDF-dead\n")
    drafts = tmp_path / "letter_drafts.json"
    drafts.write_text(json.dumps({did: {"denial_id": did}, GONE: {"denial_id": GONE}}))
    return d, drafts


def found(artifacts):
    d, drafts = artifacts
    return generate_letters.orphans(letters=d, drafts_path=drafts)


def test_orphans_reports_both_stores(artifacts):
    assert found(artifacts) == {"pdfs": [GONE], "drafts": [GONE]}


def test_a_report_does_not_delete_anything(artifacts):
    d, drafts = artifacts
    generate_letters.describe_orphans(found(artifacts))
    assert (d / f"{GONE}.pdf").exists()
    assert GONE in json.loads(drafts.read_text())


def test_pruning_removes_the_dead_ids_and_keeps_the_live_one(artifacts):
    d, drafts = artifacts
    assert generate_letters.prune_orphans(found(artifacts), letters=d, drafts_path=drafts) == {
        "pdfs": 1, "drafts": 1}
    assert sorted(p.name for p in d.glob("*.pdf")) == [f"{live_id()}.pdf"]
    assert list(json.loads(drafts.read_text())) == [live_id()]
    assert found(artifacts) == {"pdfs": [], "drafts": []}


def test_an_orphan_draft_is_reported_even_with_no_pdf_left(artifacts):
    """The draft is what silently re-renders the PDF, so pruning the file is half a fix."""
    d, drafts = artifacts
    (d / f"{GONE}.pdf").unlink()
    assert found(artifacts) == {"pdfs": [], "drafts": [GONE]}


def test_describe_names_the_ids_and_says_nothing_when_clean(artifacts):
    text = generate_letters.describe_orphans(found(artifacts))
    assert GONE in text and "1 orphan pdfs" in text and "1 orphan drafts" in text
    assert generate_letters.describe_orphans({"pdfs": [], "drafts": []}) == ""


def test_the_committed_tree_is_clean():
    assert generate_letters.orphans() == {"pdfs": [], "drafts": []}
