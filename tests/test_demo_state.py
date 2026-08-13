#!/usr/bin/env python3
"""Putting the demo board back: the admin reset door and the snapshot/restore CLI.

The live board is durable, so verifying a deploy moves real cases. These cover
the only way to undo that safely — and the lock on it, because the endpoint can
rewrite history the public API deliberately refuses to.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import app as webapp  # noqa: E402
import demo_state  # noqa: E402

TOKEN = "test-token"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APPEAL_STATUS_DB", str(tmp_path / "status.db"))
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    monkeypatch.setenv("DEMO_ADMIN_TOKEN", TOKEN)
    return TestClient(webapp.app)


@pytest.fixture()
def ids(client):
    return [c["denial_id"] for c in client.get("/api/cases?limit=3").json()]


@pytest.fixture()
def cli(client, monkeypatch):
    """Point demo_state's HTTP calls at the test app instead of a live URL."""
    def api(base, path, payload=None, token=None):
        if payload is None:
            resp = client.get(path)
        else:
            resp = client.post(path, json=payload,
                               headers={"X-Demo-Token": token} if token else {})
        if resp.status_code >= 400:
            raise SystemExit(f"{path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    monkeypatch.setattr(demo_state, "api", api)
    return demo_state


def move(client, did, status, note=""):
    return client.post(f"/api/cases/{did}/status", json={"status": status, "note": note})


# ------------------------------------------------------------------ the lock

def test_without_a_configured_token_the_endpoint_does_not_exist(client, monkeypatch):
    monkeypatch.delenv("DEMO_ADMIN_TOKEN", raising=False)
    assert client.post("/api/workflow/reset", json={}).status_code == 404


def test_a_wrong_or_missing_token_is_refused(client):
    assert client.post("/api/workflow/reset", json={}).status_code == 403
    assert client.post("/api/workflow/reset", json={},
                       headers={"X-Demo-Token": "nope"}).status_code == 403


# ------------------------------------------------------------------ resetting

def test_reset_puts_every_case_back_to_ready(client, ids):
    for did in ids:
        move(client, did, "submitted")
    res = client.post("/api/workflow/reset", json={}, headers={"X-Demo-Token": TOKEN})
    assert res.status_code == 200 and res.json()["cleared"] == len(ids)

    for did in ids:
        row = client.get(f"/api/cases/{did}/status").json()
        assert row["appeal_status"] == "ready"
        assert row["submitted_at"] is None
        assert row["events"] == []   # timeline wiped, not appended to


def test_reset_can_be_scoped_to_named_cases(client, ids):
    for did in ids:
        move(client, did, "submitted")
    res = client.post("/api/workflow/reset", json={"denial_ids": ids[:1]},
                      headers={"X-Demo-Token": TOKEN}).json()
    assert res["cleared"] == 1
    assert client.get(f"/api/cases/{ids[0]}/status").json()["appeal_status"] == "ready"
    assert client.get(f"/api/cases/{ids[1]}/status").json()["appeal_status"] == "submitted"


def test_reset_undoes_a_terminal_status_the_public_api_cannot(client, ids):
    did = ids[0]
    move(client, did, "submitted")
    move(client, did, "overturned")
    assert move(client, did, "submitted").status_code == 409     # terminal, as designed
    client.post("/api/workflow/reset", json={"denial_ids": [did]},
                headers={"X-Demo-Token": TOKEN})
    assert move(client, did, "submitted").status_code == 200


# ----------------------------------------------------------------- restoring

def test_restore_replays_a_row_verbatim(client, ids):
    row = {"denial_id": ids[0], "status": "submitted", "submitted_at": "2026-01-02T03:04:05+00:00",
           "outcome": None, "note": "portal batch 7", "updated_at": "2026-01-02T03:04:05+00:00"}
    client.post("/api/workflow/reset", json={"restore": [row]}, headers={"X-Demo-Token": TOKEN})
    got = client.get(f"/api/cases/{ids[0]}/status").json()
    assert got["appeal_status"] == "submitted"
    assert got["submitted_at"] == row["submitted_at"]        # not "now"
    assert got["status_note"] == "portal batch 7"
    assert [e["to_status"] for e in got["events"]] == ["submitted"]


def test_restore_refuses_an_unknown_case_or_a_bogus_status(client, ids):
    bad_case = client.post("/api/workflow/reset", json={"restore": [{"denial_id": "NOPE", "status": "submitted"}]},
                           headers={"X-Demo-Token": TOKEN})
    assert bad_case.status_code == 404
    bad_status = client.post("/api/workflow/reset", json={"restore": [{"denial_id": ids[0], "status": "posted"}]},
                             headers={"X-Demo-Token": TOKEN})
    assert bad_status.status_code == 422
    # a refused restore changes nothing
    assert client.get(f"/api/cases/{ids[0]}/status").json()["appeal_status"] == "ready"


# ------------------------------------------------------- orphaned rows (#19)

GONE = "DEN-MUSC-DELETED-1"


def plant_orphan(did=GONE, *, status="submitted", events=True):
    """Write workflow rows for a case that does not exist.

    This is what #11 left behind when it renumbered denial ids: the workflow DB is
    a separate file with no foreign key into `denials`, so nothing stops it.
    """
    con = webapp.status_con()
    try:
        con.execute("INSERT INTO appeal_status(denial_id,status,submitted_at,outcome,note,updated_at)"
                    " VALUES(?,?,?,?,?,?)", (did, status, None, None, "left over", "2026-01-01T00:00:00+00:00"))
        if events:
            con.execute("INSERT INTO appeal_status_events(denial_id,from_status,to_status,note,at)"
                        " VALUES(?,?,?,?,?)", (did, "ready", status, None, "2026-01-01T00:00:00+00:00"))
        con.commit()
    finally:
        con.close()


def test_an_orphan_is_counted_and_named_but_renders_nowhere(client):
    plant_orphan()
    wf = client.get("/api/workflow").json()
    assert wf["orphans"] == 1 and wf["orphan_ids"] == [GONE]
    # why it needed counting: every read path joins against `denials`
    assert GONE not in [c["denial_id"] for c in client.get("/api/cases?limit=1000").json()]
    assert client.get(f"/api/cases/{GONE}/status").status_code == 404
    assert client.get("/api/stats").json()["workflow"]["submitted"]["denials"] == 0


def test_a_moved_case_is_not_an_orphan(client, ids):
    move(client, ids[0], "submitted")
    wf = client.get("/api/workflow").json()
    assert (wf["orphans"], wf["orphan_ids"]) == (0, [])


def test_an_event_whose_status_row_is_gone_still_counts(client):
    """The half that a status-table-only query would call clean."""
    plant_orphan()
    con = webapp.status_con()
    try:
        con.execute("DELETE FROM appeal_status WHERE denial_id=?", (GONE,))
        con.commit()
    finally:
        con.close()
    assert client.get("/api/workflow").json()["orphans"] == 1


def test_prune_removes_the_orphan_and_leaves_live_cases_alone(client, ids):
    move(client, ids[0], "submitted", "real work")
    move(client, ids[1], "submitted")
    move(client, ids[1], "overturned", "payer paid")
    plant_orphan()

    res = client.post("/api/workflow/prune", headers={"X-Demo-Token": TOKEN}).json()
    assert res["pruned"] == 1 and res["denial_ids"] == [GONE]
    assert (res["rows"], res["events"]) == (1, 1)
    assert res["remaining"] == 0

    assert client.get("/api/workflow").json()["orphans"] == 0
    kept = client.get(f"/api/cases/{ids[0]}/status").json()
    assert kept["appeal_status"] == "submitted" and kept["status_note"] == "real work"
    assert [e["to_status"] for e in kept["events"]] == ["submitted"]
    assert client.get(f"/api/cases/{ids[1]}/status").json()["appeal_status"] == "overturned"


def test_prune_on_a_clean_board_changes_nothing(client, ids):
    move(client, ids[0], "submitted")
    res = client.post("/api/workflow/prune", headers={"X-Demo-Token": TOKEN}).json()
    assert (res["pruned"], res["denial_ids"], res["rows"], res["events"]) == (0, [], 0, 0)
    assert client.get(f"/api/cases/{ids[0]}/status").json()["appeal_status"] == "submitted"


def test_prune_is_behind_the_same_admin_door(client, monkeypatch):
    plant_orphan()
    assert client.post("/api/workflow/prune").status_code == 403
    assert client.post("/api/workflow/prune", headers={"X-Demo-Token": "nope"}).status_code == 403
    monkeypatch.delenv("DEMO_ADMIN_TOKEN", raising=False)
    assert client.post("/api/workflow/prune").status_code == 404
    assert client.get("/api/workflow").json()["orphans"] == 1   # refused, not partially done


def test_the_cli_reports_orphans_read_only_and_prunes_them(cli, client, ids):
    move(client, ids[0], "submitted")
    plant_orphan()
    said = cli.describe(cli.snapshot("http://test"))
    assert "1 orphan row(s)" in said and GONE in said, said
    assert cli.prune("http://test", TOKEN)["pruned"] == 1
    after = cli.describe(cli.snapshot("http://test"))
    assert "orphan" not in after, after


# ---------------------------------------------------------------- the CLI use

def test_snapshot_only_carries_the_cases_that_moved(cli, client, ids):
    move(client, ids[0], "submitted", "batch 1")
    snap = cli.snapshot("http://test")
    assert [c["denial_id"] for c in snap["cases"]] == [ids[0]]
    assert snap["cases_total"] > len(snap["cases"])
    assert snap["cases"][0]["events"][0]["to_status"] == "submitted"


def test_smoke_test_then_restore_leaves_the_board_exactly_as_found(cli, client, ids):
    move(client, ids[0], "submitted", "real work from the demo")
    move(client, ids[1], "submitted")
    move(client, ids[1], "overturned", "payer paid")
    before = cli.snapshot("http://test")

    # ... a deploy smoke test happens: move a third case, and push one further
    move(client, ids[2], "submitted", "smoke test")
    move(client, ids[0], "upheld", "smoke test")

    cli.restore("http://test", before, TOKEN)
    after = cli.snapshot("http://test")
    assert after["cases"] == before["cases"]                 # statuses, notes, timestamps, events
    assert client.get(f"/api/cases/{ids[2]}/status").json()["events"] == []


def test_restoring_an_empty_snapshot_clears_the_board(cli, client, ids):
    empty = cli.snapshot("http://test")
    move(client, ids[0], "submitted")
    cli.restore("http://test", empty, TOKEN)
    assert cli.snapshot("http://test")["cases"] == []


def test_describe_says_what_a_snapshot_holds(cli, client, ids):
    assert "all at ready" in cli.describe(cli.snapshot("http://test"))
    move(client, ids[0], "submitted")
    assert "1 submitted" in cli.describe(cli.snapshot("http://test"))
