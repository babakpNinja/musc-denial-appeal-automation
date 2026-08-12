#!/usr/bin/env python3
"""Bulk status changes and the per-payer submission batch view.

Same isolation rule as test_status.py: every test gets its own APPEAL_STATUS_DB
so the shipped musc_appeals.db is never written to.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
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
def ids(client):
    return [c["denial_id"] for c in client.get("/api/cases?limit=5").json()]


def bulk(client, denial_ids, status, note=""):
    return client.post("/api/cases/status/bulk",
                       json={"denial_ids": denial_ids, "status": status, "note": note})


# ------------------------------------------------------------------ bulk moves

def test_bulk_moves_every_case(client, ids):
    r = bulk(client, ids, "submitted", "batch upload REF-1")
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == len(ids) and body["failed"] == 0
    assert {x["denial_id"] for x in body["results"]} == set(ids)
    for did in ids:
        assert client.get(f"/api/cases/{did}/status").json()["appeal_status"] == "submitted"


def test_bulk_writes_one_event_and_the_shared_note_per_case(client, ids):
    bulk(client, ids, "submitted", "faxed 2026-08-12")
    for did in ids:
        events = client.get(f"/api/cases/{did}/status").json()["events"]
        assert len(events) == 1
        assert events[0]["from_status"] == "ready" and events[0]["to_status"] == "submitted"
        assert events[0]["note"] == "faxed 2026-08-12"


def test_partial_failure_does_not_abort_the_batch(client, ids):
    """A case someone already closed is reported; the rest still move."""
    closed, rest = ids[0], ids[1:]
    bulk(client, [closed], "submitted")
    bulk(client, [closed], "overturned")          # terminal

    r = bulk(client, ids, "submitted")
    body = r.json()
    assert r.status_code == 200
    assert body["updated"] == len(rest) and body["failed"] == 1
    bad = next(x for x in body["results"] if not x["ok"])
    assert bad["denial_id"] == closed and bad["status"] == 409 and bad["from"] == "overturned"
    for did in rest:
        assert client.get(f"/api/cases/{did}/status").json()["appeal_status"] == "submitted"


def test_unknown_case_is_reported_not_raised(client, ids):
    r = bulk(client, ids[:2] + ["DEN-NOPE-0000"], "submitted")
    body = r.json()
    assert body["updated"] == 2 and body["failed"] == 1
    bad = next(x for x in body["results"] if not x["ok"])
    assert bad["status"] == 404


def test_duplicate_ids_move_once(client, ids):
    body = bulk(client, [ids[0], ids[0], ids[0]], "submitted").json()
    assert body["requested"] == 1 and body["updated"] == 1
    assert len(client.get(f"/api/cases/{ids[0]}/status").json()["events"]) == 1


@pytest.mark.parametrize("payload,code", [
    ({"denial_ids": [], "status": "submitted"}, 422),
    ({"status": "submitted"}, 422),
    ({"denial_ids": ["x"], "status": "shipped"}, 422),
])
def test_bad_bulk_payloads_rejected(client, payload, code):
    assert client.post("/api/cases/status/bulk", json=payload).status_code == code


def test_bulk_batch_size_capped(client, ids):
    assert bulk(client, [f"DEN-X-{i}" for i in range(501)], "submitted").status_code == 422


def test_bulk_updates_the_kpis(client, ids):
    before = client.get("/api/stats").json()["workflow"]
    bulk(client, ids, "submitted")
    after = client.get("/api/stats").json()["workflow"]
    assert after["submitted"]["denials"] == before["submitted"]["denials"] + len(ids)
    assert after["outstanding"] == before["outstanding"]          # still outstanding, just in flight


# ---------------------------------------------------------------- payer batches

def test_batches_group_outstanding_by_payer(client):
    body = client.get("/api/batches").json()
    stats = client.get("/api/stats").json()["workflow"]
    assert body["totals"]["denials"] == stats["outstanding"]
    ids = [d for b in body["batches"] for d in b["denial_ids"]]
    assert len(ids) == len(set(ids)) == body["totals"]["denials"]
    for b in body["batches"]:
        assert b["ready"] + b["submitted"] == b["denials"] == len(b["denial_ids"])
        assert b["zip_url"] == f"/letters.zip?payer={b['payer_id']}"
        assert b["appeal_url"]


def test_batches_sorted_by_soonest_deadline(client):
    deadlines = [b["next_deadline"] for b in client.get("/api/batches").json()["batches"]]
    assert deadlines == sorted(deadlines)


def test_batch_next_deadline_is_the_earliest_in_the_batch(client):
    cases = client.get("/api/cases?limit=500").json()
    for b in client.get("/api/batches").json()["batches"]:
        mine = [c["appeal_deadline"] for c in cases if c["denial_id"] in b["denial_ids"]]
        assert b["next_deadline"] == min(mine)
        assert b["days_to_deadline"] == webapp.days_left(b["next_deadline"])


def test_closed_cases_leave_the_batch(client):
    body = client.get("/api/batches").json()
    batch = body["batches"][0]
    ready = batch["ready_ids"]
    bulk(client, ready, "submitted")
    bulk(client, ready, "overturned")
    after = client.get("/api/batches").json()
    now = next((b for b in after["batches"] if b["payer_id"] == batch["payer_id"]), None)
    assert now is None or set(ready) & set(now["denial_ids"]) == set()
    assert after["totals"]["denials"] == body["totals"]["denials"] - len(ready)


def test_batch_zip_downloads_that_payers_letters(client):
    b = client.get("/api/batches").json()["batches"][0]
    r = client.get(b["zip_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert len(r.content) > 4000


# ------------------------------------------------------------ days to deadline

@pytest.mark.parametrize("delta,expected", [(0, 0), (5, 5), (-3, -3)])
def test_days_left_counts_calendar_days(delta, expected):
    d = (date.today() + timedelta(days=delta)).isoformat()
    assert webapp.days_left(d) == expected


def test_days_left_tolerates_missing_or_junk():
    assert webapp.days_left(None) is None
    assert webapp.days_left("not-a-date") is None


def test_cases_carry_days_to_deadline(client):
    for c in client.get("/api/cases?limit=20").json():
        assert c["days_to_deadline"] == webapp.days_left(c["appeal_deadline"])


def test_warn_threshold_published_to_the_ui(client):
    assert client.get("/api/workflow").json()["warn_days"] == webapp.DEADLINE_WARN_DAYS
    assert client.get("/api/batches").json()["warn_days"] == webapp.DEADLINE_WARN_DAYS


def test_due_soon_and_overdue_counts_match_the_cases(client):
    cases = {c["denial_id"]: c["days_to_deadline"] for c in client.get("/api/cases?limit=500").json()}
    warn = webapp.DEADLINE_WARN_DAYS
    for b in client.get("/api/batches").json()["batches"]:
        mine = [cases[d] for d in b["denial_ids"]]
        assert b["overdue"] == sum(1 for d in mine if d < 0)
        assert b["due_soon"] == sum(1 for d in mine if 0 <= d <= warn)
