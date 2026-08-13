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

``snapshot`` and ``show`` are read-only. ``restore`` and ``reset`` call
``POST /api/workflow/reset``, which only exists when the deployment has
``DEMO_ADMIN_TOKEN`` set; pass the same value with ``--token`` or in the
``DEMO_ADMIN_TOKEN`` environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TIMEOUT = 30
DEFAULT_BASE = "http://127.0.0.1:8080"
# what a snapshot keeps per case; anything else is derived and would go stale
FIELDS = ("denial_id", "status", "submitted_at", "outcome", "note", "updated_at")


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
        if exc.code == 404 and path == "/api/workflow/reset":
            raise SystemExit("the deployment has no DEMO_ADMIN_TOKEN set, so /api/workflow/reset "
                             "does not exist — set it on the service and redeploy")
        if exc.code == 403:
            raise SystemExit("rejected: X-Demo-Token does not match the deployment's DEMO_ADMIN_TOKEN")
        raise SystemExit(f"{url} -> HTTP {exc.code}: {body}")


def snapshot(base: str) -> dict:
    """Every case that is not in its default state, with its timeline.

    Cases sitting at ``ready`` with no history are the default, so they are left
    out — a snapshot is a diff against a fresh board, which keeps it small and
    makes ``restore`` a full description of the board.
    """
    cases = api(base, "/api/cases?limit=1000")
    moved = [c for c in cases if c.get("appeal_status") != "ready" or c.get("status_note")
             or c.get("submitted_at") or c.get("status_updated_at")]
    rows = []
    for c in moved:
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
        "durable": (api(base, "/api/workflow") or {}).get("durable"),
        "cases": sorted(rows, key=lambda r: r["denial_id"]),
    }


def restore(base: str, snap: dict, token: str) -> dict:
    """Make the board match ``snap`` exactly: wipe everything, replay the rows."""
    rows = [{k: c.get(k) for k in FIELDS} | {"events": c.get("events") or []} for c in snap.get("cases", [])]
    return api(base, "/api/workflow/reset", {"restore": rows}, token)


def reset(base: str, token: str, denial_ids: list[str] | None = None) -> dict:
    return api(base, "/api/workflow/reset",
               {"denial_ids": denial_ids} if denial_ids else {}, token)


def describe(snap: dict) -> str:
    if not snap["cases"]:
        return f"{snap['cases_total']} cases, all at ready (nothing to restore)"
    by_status: dict[str, int] = {}
    for c in snap["cases"]:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
    parts = ", ".join(f"{n} {s}" for s, n in sorted(by_status.items()))
    return f"{len(snap['cases'])} of {snap['cases_total']} cases moved: {parts}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("snapshot", "restore", "reset", "show"))
    ap.add_argument("--base", default=os.environ.get("MUSC_BASE", DEFAULT_BASE))
    ap.add_argument("-o", "--out", help="snapshot: file to write (default stdout)")
    ap.add_argument("-i", "--in", dest="infile", help="restore: snapshot file to replay")
    ap.add_argument("--token", default=os.environ.get("DEMO_ADMIN_TOKEN", ""))
    ap.add_argument("--case", action="append", default=[], help="reset: limit to these denial ids")
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

    if not args.token:
        return int(bool(sys.stderr.write(
            "no token: pass --token or set DEMO_ADMIN_TOKEN (it must match the deployment)\n")))

    if args.command == "restore":
        if not args.infile:
            return int(bool(sys.stderr.write("restore needs -i <snapshot.json>\n")))
        snap = json.loads(Path(args.infile).read_text())
        res = restore(args.base, snap, args.token)
        print(f"restored {res['restored']} case(s), cleared {res['cleared']} — {describe(snap)}")
    else:
        res = reset(args.base, args.token, args.case or None)
        print(f"reset: cleared {res['cleared']} case(s) (scope: {res['scope']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
