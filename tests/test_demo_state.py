#!/usr/bin/env python3
"""Putting the demo board back: the admin reset door and the snapshot/restore CLI.

The live board is durable, so verifying a deploy moves real cases. These cover
the only way to undo that safely — and the lock on it, because the endpoint can
rewrite history the public API deliberately refuses to.
"""

from __future__ import annotations

import json
import os
import signal
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


# --------------------------------------------------------------- the drill (#30)
# Validating a monitor against the real board is five hand steps on a client-facing
# demo — snapshot, dirty, run, restore, re-verify — and forgetting the fourth leaves
# a case sitting in `submitted` in front of the client. `drill` is those five as one
# thing, so what these pin is mostly the *unhappy* halves: the command blowing up,
# the token being wrong, the restore itself failing. A drill that leaves residue is
# worse than no drill.


@pytest.fixture()
def watcher(cli, monkeypatch):
    """Stand in for the command under drill, and record the board it ran against.

    A real subprocess cannot see this in-memory board, so the fake is the honest
    one here: it asks the same API the monitor would, at the moment the monitor
    would, and hands back what it found.
    """
    seen = {"ran": 0}

    def fake(command, cwd):
        seen["ran"] += 1
        seen["board"] = cli.describe(cli.snapshot("http://test"))
        seen["cwd"] = cwd
        return seen.get("code", 1), seen.get("says", "board state: DIRTY\n")

    monkeypatch.setattr(demo_state, "run_command", fake)
    return seen


# The drill's exit codes, by their own names — so a wrong verdict fails as
# `assert 'OK' == 'UNMET'` rather than as `assert 0 == 3`, which says nothing to
# whoever reads it next (#482). The numbers come from the module, not from here.
VERDICT = {demo_state.OK: "OK", demo_state.REFUSED: "REFUSED",
           demo_state.UNMET: "UNMET", demo_state.RESIDUE: "RESIDUE"}


@pytest.fixture()
def run_drill(tmp_path):
    """Run the drill, always from a scratch directory.

    `--cwd` is where a restore that could not put the board back writes the snapshot
    it is handing over, and under `mutate.py` the residue path fires in tests that
    were not written for it — which dropped three `demo_state-residue-*.json` files
    into the app directory before this defaulted. A test that wants to read that file
    passes its own `--cwd`, and argparse lets the later flag win.
    """
    def run(*extra, base="http://test"):
        return VERDICT[demo_state.main(
            ["drill", "--base", base, "--run", "monitor", "--cwd", str(tmp_path), *extra])]

    return run


def test_the_command_runs_against_a_board_that_is_really_dirty(run_drill, watcher, cli, ids):
    """The point of the whole thing: the monitor is asked about a board that has
    something on it, not about the clean one it would have seen anyway."""
    assert run_drill("--expect", "DIRTY") == "OK"
    assert "1 submitted" in watcher["board"], \
        f"the command was run against a board the drill had not dirtied: {watcher['board']}"
    assert watcher["ran"] == 1


def test_the_board_comes_back_to_exactly_what_was_there_before(run_drill, watcher, cli, client, ids):
    assert run_drill("--expect", "DIRTY") == "OK"
    after = cli.snapshot("http://test")
    assert after["cases"] == [], f"the drill left {cli.describe(after)} on a client board"
    # not merely "back at ready": the drill's own timeline entry is gone too, so the
    # client's case history does not carry a note about an automated check
    victim = sorted(c["denial_id"] for c in client.get("/api/cases?limit=1000").json())[0]
    assert client.get(f"/api/cases/{victim}/status").json()["events"] == []


def test_a_command_that_raises_still_puts_the_board_back(run_drill, cli, ids, monkeypatch, capsys):
    """The failure the `finally` exists for. A typo in --run is a FileNotFoundError
    out of subprocess, and the hand version of this step is where the case gets
    stranded."""
    def boom(command, cwd):
        raise FileNotFoundError(2, "No such file or directory", "monitor")

    monkeypatch.setattr(demo_state, "run_command", boom)

    assert run_drill("--expect", "DIRTY") == "UNMET"
    assert cli.snapshot("http://test")["cases"] == []
    said = capsys.readouterr().err
    assert "FileNotFoundError" in said, "the drill did not say why the command never ran"
    assert "restored" in said, "the drill did not say it had put the board back"


def test_a_ctrl_c_mid_command_still_puts_the_board_back(run_drill, cli, ids, monkeypatch):
    """KeyboardInterrupt is not an Exception, so it is the `finally` and not the
    `except` that has to be doing the restoring — and SIGTERM is turned into this
    one for the same reason."""
    def interrupted(command, cwd):
        raise KeyboardInterrupt

    monkeypatch.setattr(demo_state, "run_command", interrupted)

    with pytest.raises(KeyboardInterrupt):
        run_drill("--expect", "DIRTY")
    assert cli.snapshot("http://test")["cases"] == []


def test_a_monitor_that_did_not_notice_the_dirt_fails_the_drill(run_drill, watcher, cli, ids, capsys):
    """Without this the drill only ever tested the restore: any command at all would
    have passed, including one that answered READY about a board with a case in
    `submitted` on it."""
    watcher["code"], watcher["says"] = 0, "board state: every case at ready\n"

    assert run_drill("--expect", "DIRTY") == "UNMET"
    assert "never said 'DIRTY'" in capsys.readouterr().err
    assert cli.snapshot("http://test")["cases"] == []      # a failed drill still tidies up


def test_the_commands_own_exit_code_is_reported_and_not_inherited(run_drill, watcher, cli, ids, capsys):
    """demoready_sweep returns 1 for a demo that is not READY — which is the correct
    answer to a board this drill just dirtied. Adopting it would mark every working
    drill as broken."""
    watcher["code"] = 1

    assert run_drill("--expect", "DIRTY") == "OK"
    assert "exited 1" in capsys.readouterr().err


def test_a_drill_with_nothing_to_expect_says_nobody_read_the_output(run_drill, watcher, cli, ids, capsys):
    """It still proves the restore, so it is not a failure — but it must not print
    the same line as a run that checked what the monitor said."""
    assert run_drill() == "OK"
    assert "nothing checked what it said" in capsys.readouterr().err


def test_an_already_dirty_board_is_refused_untouched(run_drill, watcher, cli, client, ids, capsys):
    """A monitor answering DIRTY next to somebody else's residue would answer DIRTY
    with this drill deleted, so the run would pass without testing anything."""
    move(client, ids[0], "submitted", "someone else's smoke test")

    assert run_drill("--expect", "DIRTY") == "REFUSED"
    assert watcher["ran"] == 0
    said = capsys.readouterr().err
    assert "already dirty" in said, "the refusal did not say what was wrong with the board"
    assert "demo_state.py reset" in said, "the refusal did not say how to clear it"
    kept = client.get(f"/api/cases/{ids[0]}/status").json()
    assert kept["appeal_status"] == "submitted" and kept["status_note"] == "someone else's smoke test"


def test_a_board_with_no_case_at_ready_is_refused(run_drill, watcher, cli, monkeypatch, capsys):
    monkeypatch.setattr(demo_state, "snapshot", lambda base: {"cases": [], "cases_total": 0,
                                                              "orphans": 0, "taken_at": "x"})
    monkeypatch.setattr(demo_state, "api", lambda *a, **k: [])

    assert run_drill("--expect", "DIRTY") == "REFUSED"
    assert watcher["ran"] == 0
    assert "no case sitting at ready" in capsys.readouterr().err


def test_a_token_the_deployment_refuses_is_found_before_anything_moves(run_drill, watcher, cli, ids):
    """`resolve_token` returning a string is not the deployment accepting it. The
    hand version learns that from a 403 on the *undo*, with a case already moved and
    no way back; the drill sends a no-op through the same door first."""
    with pytest.raises(SystemExit):
        run_drill("--token", "wrong-token")

    assert watcher["ran"] == 0
    assert cli.snapshot("http://test")["cases"] == []


@pytest.fixture()
def restore_fails(monkeypatch):
    """The undo refused — a 502, a rotated token, the service restarting mid-drill."""
    def refuse(base, snap, token):
        raise SystemExit("/api/workflow/reset -> HTTP 502")

    monkeypatch.setattr(demo_state, "restore", refuse)


def test_a_restore_that_fails_names_the_residue_and_hands_over_the_repair(run_drill, 
        watcher, cli, ids, restore_fails, tmp_path, capsys):
    """The one outcome worse than a red drill: a client board left moved. It cannot
    end in a traceback — it has to end in the command that fixes it, and in a file
    holding the board as it was found, because the process that knows is exiting."""
    code = run_drill("--expect", "DIRTY", "--cwd", str(tmp_path))
    said = capsys.readouterr().err

    assert code == "RESIDUE"
    assert "RESIDUE LEFT ON" in said, "a board left moved was not announced as residue"
    assert "HTTP 502" in said, "the residue report did not say why the restore failed"
    written = list(tmp_path.glob("demo_state-residue-*.json"))
    assert len(written) == 1, f"expected one saved snapshot, found {len(written)}"
    assert f"-i {written[0]}" in said, "the residue report did not hand over a runnable repair"
    assert json.loads(written[0].read_text())["cases"] == []


def test_a_restore_that_says_it_worked_is_still_checked(run_drill, watcher, cli, ids, monkeypatch,
                                                        tmp_path, capsys):
    """The quiet version of the same disaster: the reset door answers 200 and the
    case is still sitting in `submitted`. Nothing else in this run would notice —
    which is why the drill re-reads the board instead of trusting the reply."""
    monkeypatch.setattr(demo_state, "restore", lambda base, snap, token: {"restored": 0})

    assert run_drill("--expect", "DIRTY", "--cwd", str(tmp_path)) == "RESIDUE"
    said = capsys.readouterr().err
    assert "came back different" in said, "a silent half-restore was reported as a success"
    assert "1 submitted" in said, "the residue report did not say what was left on the board"


def test_residue_outranks_every_other_verdict(run_drill, watcher, cli, ids, restore_fails, tmp_path, capsys):
    """A drill whose monitor also missed the dirt still reports the board first:
    a monitor to fix is tomorrow's work, a moved case on a client's demo is now."""
    watcher["says"] = "board state: every case at ready\n"

    assert run_drill("--expect", "DIRTY", "--cwd", str(tmp_path)) == "RESIDUE"
    assert "RESIDUE LEFT ON" in capsys.readouterr().err


def test_a_sigterm_becomes_something_the_finally_can_survive():
    """SIGINT already arrives as KeyboardInterrupt. SIGTERM kills the interpreter
    outright — which on this tool means walking away from a dirtied client board —
    so the drill converts it into the same exception the restore already survives."""
    was = signal.getsignal(signal.SIGTERM)
    try:
        demo_state.die_on_sigterm()
        with pytest.raises(KeyboardInterrupt, match="SIGTERM"):
            os.kill(os.getpid(), signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, was)


def test_the_drill_needs_a_command_to_run(cli, capsys):
    assert demo_state.main(["drill", "--base", "http://test"]) == 1, \
        "a drill with no command to run started anyway"
    assert "--run" in capsys.readouterr().err


def test_a_wiped_box_cannot_drill_at_all(cli, env_file, monkeypatch, capsys):
    """The drill is in the same write group as reset: no token, no undo, so it must
    refuse before it moves anything rather than discover this halfway through."""
    monkeypatch.delenv("DEMO_ADMIN_TOKEN", raising=False)

    assert demo_state.main(["drill", "--base", "http://test", "--run", "monitor"]) == 1, \
        "a box with no token began a drill it could not undo"
    assert "standing state" in capsys.readouterr().err


# ------------------------------------------------- where the token comes from
# The token is standing state that lives on the Railway service and is cached in
# a gitignored `.env.demo`. A re-provision wipes the cache, and the failure was a
# one-line "no token: pass --token", which reads like a forgotten flag rather
# than like the only undo for a live smoke test being gone (#195).

@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    """A stand-in .env.demo — the real one must never be what a test reads."""
    path = tmp_path / ".env.demo"
    monkeypatch.setattr(demo_state, "ENV_FILE", path)
    return path


def test_the_token_is_read_from_the_boxs_file_when_nothing_else_supplies_it(env_file, monkeypatch):
    """A healthy box should not need `--token` at all; the flag being effectively
    mandatory is what made the missing file look like an ordinary usage error."""
    monkeypatch.delenv("DEMO_ADMIN_TOKEN", raising=False)
    env_file.write_text(f"DEMO_ADMIN_TOKEN={TOKEN}\n")

    assert demo_state.resolve_token() == TOKEN, \
        "the cached token on the box was not found, so every command needs --token by hand"


def test_the_explicit_flag_and_the_environment_outrank_the_cached_file(env_file, monkeypatch):
    """The file is a cache of the deployed value. Anything said out loud is more
    likely to be the current one — a rotation reaches the flag first."""
    env_file.write_text("DEMO_ADMIN_TOKEN=from-file\n")
    monkeypatch.setenv("DEMO_ADMIN_TOKEN", "from-env")

    assert demo_state.resolve_token("explicit") == "explicit"
    assert demo_state.resolve_token() == "from-env"
    monkeypatch.delenv("DEMO_ADMIN_TOKEN")
    assert demo_state.resolve_token() == "from-file"


def test_a_file_without_the_line_is_the_same_as_no_file(env_file):
    env_file.write_text("SOMETHING_ELSE=x\n")
    assert demo_state.read_env_token() == ""
    env_file.write_text(f"SOMETHING_ELSE=x\nDEMO_ADMIN_TOKEN={TOKEN}\n")
    assert demo_state.read_env_token() == TOKEN


def test_a_wiped_box_is_told_it_lost_state_and_how_to_get_it_back(cli, env_file, monkeypatch, capsys):
    """The message is the entire recovery path for whoever is mid-smoke-test, so
    it has to name the file that went missing and the command that restores it —
    not the flag they did not type."""
    monkeypatch.delenv("DEMO_ADMIN_TOKEN", raising=False)

    code = demo_state.main(["reset", "--base", "http://test"])
    said = capsys.readouterr().err

    assert code == 1
    assert "tools/bootstrap.py" in said
    assert str(env_file) in said
    assert "standing state" in said


def test_read_only_commands_never_ask_for_a_token(cli, client, env_file, monkeypatch):
    """`show` on a live board is the first thing anyone runs, and it must keep
    working on a box that has lost the file."""
    monkeypatch.delenv("DEMO_ADMIN_TOKEN", raising=False)
    assert demo_state.main(["show", "--base", "http://test"]) == 0
