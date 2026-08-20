#!/usr/bin/env python3
"""Snapshot, restore and reset the appeal board's workflow state.

The live service keeps appeal status on a mounted volume, so anything moved
while smoke-testing a deploy *stays moved* — and the public API cannot undo it
(``overturned`` is terminal by design). This is the safe way to touch a running
demo: take a snapshot first, do the smoke test, restore.

    python demo_state.py snapshot --base https://…  -o data/demo_state.json
    python demo_state.py restore  --base https://…  -i data/demo_state.json
    python demo_state.py reset    --base https://…            # all back to ready
    python demo_state.py show     --base https://…            # what is non-ready now
    python demo_state.py prune    --base https://…            # drop rows for deleted cases
    python demo_state.py drill    --base https://… --run "<cmd>" --expect DIRTY

``snapshot`` and ``show`` are read-only. ``restore``, ``reset``, ``prune`` and
``drill`` call ``POST /api/workflow/…``, which only exists when the deployment has
``DEMO_ADMIN_TOKEN`` set; pass the same value with ``--token``, in the
``DEMO_ADMIN_TOKEN`` environment variable, or leave it to be read from the agent
box's gitignored ``.env.demo`` (``python tools/bootstrap.py`` puts that file back
from the Railway service after a re-provision).

``drill`` is the four steps above done as one thing that cannot forget the last
one: snapshot, move one case, run a command against the dirtied board, put the
board back in a ``finally``, and prove it came back. See :func:`drill` (#30).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TIMEOUT = 30
DEFAULT_BASE = "http://127.0.0.1:8080"
# The agent's box keeps the admin token here, gitignored. Absent in a clone of the
# deploy repo, which is the point: the file is a local cache of a value that lives
# on the Railway service.
ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env.demo"
# what a snapshot keeps per case; anything else is derived and would go stale
FIELDS = ("denial_id", "status", "submitted_at", "outcome", "note", "updated_at")


def no_token_message() -> str:
    """Said when there is no token anywhere. It is the whole recovery path.

    The old wording — "no token: pass --token or set DEMO_ADMIN_TOKEN" — reads
    like a forgotten flag. The real situation is usually that the box lost
    standing state, while the caller is mid-smoke-test on a live client board
    with no way to put it back (#195).
    """
    return (
        "no DEMO_ADMIN_TOKEN — this is lost standing state, not a missing flag.\n"
        "The value lives on the Railway service; this box keeps a gitignored copy\n"
        f"in {ENV_FILE}, which a re-provision wipes. Put it back with:\n"
        "    python tools/bootstrap.py        # restores .env.demo from the service\n"
        "or pass --token explicitly if you have it.\n"
    )


def read_env_token() -> str:
    """The token from the box's ``.env.demo``, or "" if this is not that box."""
    try:
        text = ENV_FILE.read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "DEMO_ADMIN_TOKEN":
            return value.strip()
    return ""


def resolve_token(explicit: str = "") -> str:
    """--token, then the environment, then the box's cache file."""
    return explicit or os.environ.get("DEMO_ADMIN_TOKEN", "") or read_env_token()


def api(base: str, path: str, payload: dict | None = None, token: str | None = None):
    url = base.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Demo-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:300]
        # every admin door answers 404 when no token is configured, so match the
        # prefix — a new door must not inherit the generic "HTTP 404" message
        if exc.code == 404 and path.startswith("/api/workflow/"):
            raise SystemExit(f"the deployment has no DEMO_ADMIN_TOKEN set, so {path} "
                             "does not exist — set it on the service and redeploy")
        if exc.code == 403:
            raise SystemExit("rejected: X-Demo-Token does not match the deployment's DEMO_ADMIN_TOKEN")
        raise SystemExit(f"{url} -> HTTP {exc.code}: {body}")


def moved(case: dict) -> bool:
    """Has this case been touched, i.e. is it not in a fresh board's default state?

    One author for the question, because two things ask it and they must agree: a
    snapshot keeps exactly the cases this returns True for, and ``drill`` picks its
    victim from the ones it returns False for. If those two drifted apart, the drill
    would dirty a case the snapshot had already recorded as moved and the restore
    would look like it had lost something.
    """
    return bool(case.get("appeal_status") != "ready" or case.get("status_note")
                or case.get("submitted_at") or case.get("status_updated_at"))


def snapshot(base: str) -> dict:
    """Every case that is not in its default state, with its timeline.

    Cases sitting at ``ready`` with no history are the default, so they are left
    out — a snapshot is a diff against a fresh board, which keeps it small and
    makes ``restore`` a full description of the board.
    """
    wf = api(base, "/api/workflow") or {}
    cases = api(base, "/api/cases?limit=1000")
    rows = []
    for c in [c for c in cases if moved(c)]:
        detail = api(base, f"/api/cases/{c['denial_id']}/status")
        rows.append({
            "denial_id": c["denial_id"],
            "status": detail.get("appeal_status", c.get("appeal_status")),
            "submitted_at": detail.get("submitted_at"),
            "outcome": detail.get("outcome"),
            "note": detail.get("status_note"),
            "updated_at": detail.get("status_updated_at"),
            "events": detail.get("events") or [],
        })
    return {
        "base": base.rstrip("/"),
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cases_total": len(cases),
        "durable": wf.get("durable"),
        # a snapshot cannot restore an orphan (the API refuses unknown ids), so this
        # is carried to be *reported* — `show` is the read-only command anyone runs
        "orphans": wf.get("orphans") or 0,
        "orphan_ids": wf.get("orphan_ids") or [],
        "cases": sorted(rows, key=lambda r: r["denial_id"]),
    }


def restore(base: str, snap: dict, token: str) -> dict:
    """Make the board match ``snap`` exactly: wipe everything, replay the rows."""
    rows = [{k: c.get(k) for k in FIELDS} | {"events": c.get("events") or []} for c in snap.get("cases", [])]
    return api(base, "/api/workflow/reset", {"restore": rows}, token)


def reset(base: str, token: str, denial_ids: list[str] | None = None) -> dict:
    return api(base, "/api/workflow/reset",
               {"denial_ids": denial_ids} if denial_ids else {}, token)


def prune(base: str, token: str) -> dict:
    """Drop workflow rows for cases that no longer exist. Cannot touch a live case."""
    return api(base, "/api/workflow/prune", {}, token)


def describe(snap: dict) -> str:
    if snap.get("orphans"):
        tail = (f" — plus {snap['orphans']} orphan row(s) for cases that no longer exist "
                f"({', '.join(snap.get('orphan_ids') or []) or 'ids not reported'}); "
                "`prune` removes them")
    else:
        tail = ""
    if not snap["cases"]:
        return f"{snap['cases_total']} cases, all at ready (nothing to restore){tail}"
    by_status: dict[str, int] = {}
    for c in snap["cases"]:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
    parts = ", ".join(f"{n} {s}" for s, n in sorted(by_status.items()))
    return f"{len(snap['cases'])} of {snap['cases_total']} cases moved: {parts}{tail}"


# ------------------------------------------------------------------- the drill

# What the drill leaves on the board for the seconds the command runs. ``submitted``
# because that is the residue this whole file exists to undo — the case somebody
# moved while smoke-testing and forgot — and because it is a legal forward
# transition, so creating it needs no admin door and proves nothing about the token.
DRILL_STATUS = "submitted"
DRILL_NOTE = "DRILL — automated check, reverted immediately"

# Exit codes, so a caller can tell the outcomes apart. 1 is deliberately not among
# them: `main` already returns 1 for "refused before starting" (no token, no --run),
# and the one outcome that needs somebody to go and look at a client's board must
# not share a number with the most boring thing this file can do.
OK, REFUSED, UNMET, RESIDUE = 0, 2, 3, 4


def run_command(command: str, cwd: Path) -> tuple[int, str]:
    """Run the command under drill; return ``(exit code, stdout + stderr)``.

    Split with :func:`shlex.split` rather than handed to a shell, so a typo in the
    command raises ``FileNotFoundError`` *here* — inside the ``try`` whose
    ``finally`` puts the board back — instead of arriving as a shell's 127, which
    the drill would then report as the monitor's own verdict.
    """
    proc = subprocess.run(shlex.split(command), cwd=str(cwd), capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def put_back(base: str, before: dict, token: str, cwd: Path) -> bool:
    """Restore ``before`` and answer whether the board actually came back.

    Never raises: this is what runs in the ``finally``, and an exception escaping
    here would replace the "your client's board still has residue on it, here is
    the command" message with a traceback.
    """
    try:
        restore(base, before, token)
        after = snapshot(base)
        if after["cases"] == before["cases"]:
            return True
        why = f"the board came back different: {describe(after)}"
    except (SystemExit, OSError, ValueError, KeyError) as exc:
        why = f"the restore itself failed: {type(exc).__name__}: {exc}"

    path = cwd / f"demo_state-residue-{before['taken_at'].replace(':', '')}.json"
    try:
        path.write_text(json.dumps(before, indent=2) + "\n")
        saved = f"the board as it was found is written to {path}"
    except OSError as exc:                       # last resort: the snapshot is the repair
        saved = f"could not write the snapshot ({exc}); it is above, copy it out of this log"
        sys.stderr.write(json.dumps(before, indent=2) + "\n")
    sys.stderr.write(
        f"RESIDUE LEFT ON {base} — {why}\n"
        f"This is a client-facing board and it is not as this drill found it. {saved};\n"
        "put it back by hand with:\n"
        f"    python demo_state.py restore --base {base} -i {path}\n")
    return False


def drill(base: str, token: str, command: str, expect: list[str], cwd: Path) -> int:
    """Dirty the live board, run a command against it, and always put it back.

    Validating the sweep against the real board used to be five steps done by hand
    on a *client-facing* demo, where forgetting the fifth leaves a case sitting in
    ``submitted`` (#30). Here the fifth step is a ``finally``, and whether it worked
    is checked rather than assumed — a drill that leaves residue is worse than no
    drill, so that is the one thing that makes this exit non-zero on its own.

    Three orderings do the safety work, and each is the answer to a way the hand
    version could strand the board:

    * **The token is resolved, and the admin door is tried, before anything moves.**
      ``resolve_token`` returning a string does not mean the deployment accepts it;
      a wrong value is a 403 that the hand version discovers *after* dirtying, with
      no undo. So the drill first sends a ``reset`` scoped to the case it is about
      to touch — a no-op on a case already at ``ready`` — and only dirties once that
      has come back 200.
    * **A board that is already dirty is refused, not drilled.** The verdict under
      test is board-wide: a monitor answering DIRTY next to somebody else's residue
      would answer DIRTY with the drill's own change removed, so the run would pass
      without testing anything.
    * **The command's exit code is reported, never inherited.** A monitor that
      notices the dirt is *supposed* to fail — ``demoready_sweep`` returns 1 for a
      non-READY demo — so adopting its code would mark a working drill as broken.
      What the command said is judged by ``--expect`` instead, and when no
      ``--expect`` is given the run says out loud that nothing read the output.
    """
    before = snapshot(base)
    if before["cases"]:
        sys.stderr.write(
            f"refusing: the board is already dirty — {describe(before)}\n"
            "A DIRTY verdict from here would be about that, not about this drill's own\n"
            "change, and the run would pass with the drill removed. Clear it first:\n"
            f"    python demo_state.py show  --base {base}\n"
            f"    python demo_state.py reset --base {base}   # if that was a smoke test\n")
        return REFUSED
    cases = api(base, "/api/cases?limit=1000")
    untouched = sorted(c["denial_id"] for c in cases if not moved(c))
    if not untouched:
        sys.stderr.write(f"refusing: {base} reports no case sitting at ready, so there is "
                         "nothing this drill could dirty and then undo\n")
        return REFUSED
    victim = untouched[0]      # sorted, so a re-run drills the same case

    reset(base, token, [victim])   # no-op on a ready case; proves the door and the token
    print(f"undo proved: /api/workflow/reset took the token (no-op on {victim})", file=sys.stderr)

    code, said, blew_up = None, "", ""
    try:
        api(base, f"/api/cases/{victim}/status", {"status": DRILL_STATUS, "note": DRILL_NOTE})
        dirty = snapshot(base)
        if [c["denial_id"] for c in dirty["cases"]] != [victim]:
            blew_up = (f"the dirtying did not take: expected only {victim} to have moved, "
                       f"the board says {describe(dirty)}")
        else:
            print(f"dirtied: {describe(dirty)}\nrunning in {cwd}: {command}", file=sys.stderr)
            code, said = run_command(command, cwd)
            if said:
                # flushed: everything else here goes to stderr, which is unbuffered,
                # so a piped run would otherwise print the command's own words last
                print(said, end="" if said.endswith("\n") else "\n", flush=True)
    except Exception as exc:                     # noqa: BLE001 — the board comes first
        blew_up = f"the command could not be run: {type(exc).__name__}: {exc}"
    finally:
        clean = put_back(base, before, token, cwd)

    if not clean:
        return RESIDUE                           # loudest failure, whatever else happened
    print(f"restored: the board is as it was found — {describe(before)}", file=sys.stderr)
    if blew_up:
        sys.stderr.write(f"drill did not complete: {blew_up}\n")
        return UNMET
    missing = [t for t in expect if t not in said]
    if missing:
        sys.stderr.write(f"the command exited {code} but never said "
                         f"{', '.join(repr(t) for t in missing)} — it did not notice "
                         "a board this drill had already dirtied\n")
        return UNMET
    if expect:
        print(f"drill passed: the command exited {code} and said "
              f"{', '.join(repr(t) for t in expect)} about a dirtied board", file=sys.stderr)
    else:
        print(f"the command exited {code}; nothing checked what it said — pass --expect "
              "to make this a drill rather than a wrapper", file=sys.stderr)
    return OK


def die_on_sigterm() -> None:
    """Turn SIGTERM into an exception, so the ``finally`` that restores still runs.

    SIGINT already arrives as ``KeyboardInterrupt``. SIGTERM kills the interpreter
    outright, which on this tool means walking away from a dirtied client board.
    """
    def raise_it(signum, frame):
        raise KeyboardInterrupt("SIGTERM during drill — putting the board back")

    signal.signal(signal.SIGTERM, raise_it)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("snapshot", "restore", "reset", "show", "prune", "drill"))
    ap.add_argument("--base", default=os.environ.get("MUSC_BASE", DEFAULT_BASE))
    ap.add_argument("-o", "--out", help="snapshot: file to write (default stdout)")
    ap.add_argument("-i", "--in", dest="infile", help="restore: snapshot file to replay")
    ap.add_argument("--token", default="",
                    help="default: $DEMO_ADMIN_TOKEN, then the box's .env.demo")
    ap.add_argument("--case", action="append", default=[], help="reset: limit to these denial ids")
    ap.add_argument("--run", help="drill: the command to run against the dirtied board")
    ap.add_argument("--expect", action="append", default=[],
                    help="drill: text the command's output must contain (repeatable)")
    ap.add_argument("--cwd", default=str(Path(__file__).resolve().parent),
                    help="drill: directory to run --run from (default: this file's)")
    args = ap.parse_args(argv)

    if args.command in ("snapshot", "show"):
        snap = snapshot(args.base)
        print(describe(snap), file=sys.stderr)
        if args.command == "show":
            for c in snap["cases"]:
                print(f"{c['denial_id']:<20} {c['status']:<11} {c['updated_at'] or ''}  {c['note'] or ''}")
            return 0
        text = json.dumps(snap, indent=2) + "\n"
        if args.out:
            Path(args.out).write_text(text)
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            print(text, end="")
        return 0

    token = resolve_token(args.token)
    if not token:
        return int(bool(sys.stderr.write(no_token_message())))

    if args.command == "prune":
        res = prune(args.base, token)
        named = ", ".join(res["denial_ids"]) or "none"
        print(f"pruned {res['pruned']} orphan case(s): {named} "
              f"({res['rows']} status row(s), {res['events']} event(s)); "
              f"{res['remaining']} orphan(s) left")
    elif args.command == "drill":
        if not args.run:
            return int(bool(sys.stderr.write("drill needs --run '<command>'\n")))
        die_on_sigterm()
        return drill(args.base, token, args.run, args.expect, Path(args.cwd))
    elif args.command == "restore":
        if not args.infile:
            return int(bool(sys.stderr.write("restore needs -i <snapshot.json>\n")))
        snap = json.loads(Path(args.infile).read_text())
        res = restore(args.base, snap, token)
        print(f"restored {res['restored']} case(s), cleared {res['cleared']} — {describe(snap)}")
    else:
        res = reset(args.base, token, args.case or None)
        print(f"reset: cleared {res['cleared']} case(s) (scope: {res['scope']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
