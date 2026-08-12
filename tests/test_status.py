#!/usr/bin/env python3
"""Appeal lifecycle: status transitions, persistence, filtering and KPIs.

Each test gets its own status DB (env APPEAL_STATUS_DB, resolved per call in
app.status_db_path) so nothing here touches the shipped musc_appeals.db.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import app as webapp  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APPEAL_STATUS_DB", str(tmp_path / "status.db"))
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    monkeypatch.delenv("APPEAL_STATUS_DURABLE", raising=False)
    return TestClient(webapp.app)


@pytest.fixture()
def denial_id(client):
    return client.get("/api/cases?limit=1").json()[0]["denial_id"]


def set_status(client, did, status, note=""):
    return client.post(f"/api/cases/{did}/status", json={"status": status, "note": note})


# ------------------------------------------------------------------ defaults

def test_cases_default_to_ready(client):
    cases = client.get("/api/cases").json()
    assert cases and all(c["appeal_status"] == "ready" for c in cases)
    assert all(c["submitted_at"] is None and c["outcome"] is None for c in cases)


def test_case_detail_exposes_next_statuses(client, denial_id):
    c = client.get(f"/api/cases/{denial_id}").json()
    assert c["appeal_status"] == "ready"
    assert c["next_statuses"] == ["submitted"]
    assert c["status_events"] == []


# --------------------------------------------------------------- transitions

def test_happy_path_ready_submitted_overturned(client, denial_id):
    r = set_status(client, denial_id, "submitted", "faxed, ref 8821")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["appeal_status"] == "submitted"
    assert body["submitted_at"] and body["outcome"] is None
    assert body["status_note"] == "faxed, ref 8821"

    body = set_status(client, denial_id, "overturned").json()
    assert body["appeal_status"] == "overturned"
    assert body["outcome"] == "overturned"
    assert body["submitted_at"], "submission timestamp survives the outcome"
    assert body["next_statuses"] == [], "overturned is terminal"
    assert [e["to_status"] for e in body["events"]] == ["submitted", "overturned"]


def test_upheld_can_be_escalated_again(client, denial_id):
    set_status(client, denial_id, "submitted")
    assert set_status(client, denial_id, "upheld").json()["outcome"] == "upheld"
    assert set_status(client, denial_id, "submitted").status_code == 200


def test_withdraw_back_to_ready_clears_submission(client, denial_id):
    set_status(client, denial_id, "submitted")
    body = set_status(client, denial_id, "ready", "pulled to fix coding").json()
    assert body["appeal_status"] == "ready"
    assert body["submitted_at"] is None


def test_illegal_transitions_are_rejected(client, denial_id):
    assert set_status(client, denial_id, "overturned").status_code == 409
    assert set_status(client, denial_id, "upheld").status_code == 409
    set_status(client, denial_id, "submitted")
    set_status(client, denial_id, "overturned")
    assert set_status(client, denial_id, "submitted").status_code == 409, "terminal state is closed"


def test_unknown_status_and_unknown_case(client, denial_id):
    assert set_status(client, denial_id, "paid").status_code == 422
    assert set_status(client, "DEN-nope", "submitted").status_code == 404
    assert client.get("/api/cases/DEN-nope/status").status_code == 404


# ---------------------------------------------------------------- persistence

def test_status_persists_to_sqlite(client, denial_id, tmp_path):
    set_status(client, denial_id, "submitted", "note here")
    con = sqlite3.connect(tmp_path / "status.db")
    row = con.execute("SELECT status, submitted_at, note FROM appeal_status WHERE denial_id=?",
                      (denial_id,)).fetchone()
    events = con.execute("SELECT COUNT(*) FROM appeal_status_events WHERE denial_id=?", (denial_id,)).fetchone()[0]
    con.close()
    assert row[0] == "submitted" and row[1] and row[2] == "note here"
    assert events == 1
    # a fresh client against the same file sees it
    assert client.get(f"/api/cases/{denial_id}").json()["appeal_status"] == "submitted"


def test_workflow_endpoint_declares_durability(client, tmp_path, monkeypatch):
    wf = client.get("/api/workflow").json()
    assert wf["statuses"] == ["ready", "submitted", "overturned", "upheld"]
    assert wf["durable"] is False and "resets" in wf["note"]
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path / "vol"))
    monkeypatch.delenv("APPEAL_STATUS_DB")
    wf = client.get("/api/workflow").json()
    assert wf["durable"] is True and wf["store"].endswith("vol/appeal_status.db")


# ------------------------------------------------------------ filters + KPIs

def test_status_filter_and_stats_track_the_workflow(client, denial_id):
    total = len(client.get("/api/cases").json())
    set_status(client, denial_id, "submitted")

    submitted = client.get("/api/cases?status=submitted").json()
    assert [c["denial_id"] for c in submitted] == [denial_id]
    assert len(client.get("/api/cases?status=ready").json()) == total - 1
    assert client.get("/api/cases?status=upheld").json() == []

    wf = client.get("/api/stats").json()["workflow"]
    assert wf["submitted"]["denials"] == 1
    assert wf["ready"]["denials"] == total - 1
    assert wf["outstanding"] == total
    assert wf["submitted"]["denied_amount"] == pytest.approx(submitted[0]["denied_amount"], abs=0.01)
    assert wf["durable"] is False


def test_ui_ships_the_workflow_controls(client):
    html = client.get("/").text
    assert 'id="statusF"' in html and "Submitted / outstanding" in html
    assert "setStatus(" in html and "workflowPanel(" in html
    assert "@media(max-width:760px)" in html, "mobile breakpoint present"
    assert 'table class="responsive"' in html
