#!/usr/bin/env python3
"""What "safe to show a client" means for the denial-appeal board.

``tools/demoready.py`` already knows the demo is up and rendering. Two things it
cannot know: whether the queue still has live deadlines (the timeline is
anchored to build day and decays — see ``refresh_demo.py``), and whether the
last person to poke at it left cases sitting in "submitted".
"""

from __future__ import annotations

import json
import urllib.request

import demo_state
from refresh_demo import MAX_LAPSED

TIMEOUT = 30
READY, STALE, DIRTY = "READY", "STALE", "DIRTY"


def _get(base: str, path: str):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def build_age(base: str) -> int | None:
    """How many days ago the *deployed* data was built (None if it predates ``meta``)."""
    return _get(base, "/api/health").get("built_days_ago")


def freshness(base: str) -> dict:
    """Read the *live* board's deadlines — the local DB may not be what is deployed."""
    cases = _get(base, "/api/cases?limit=1000")
    left = [c.get("days_to_deadline") for c in cases if c.get("days_to_deadline") is not None]
    lapsed = sum(1 for d in left if d <= 0)
    appealable = sum(1 for d in left if d > 0)
    stale = lapsed > MAX_LAPSED
    age = build_age(base)
    # the age is what a human actually asks ("how old is this thing?"); the
    # lapsed count is what makes it embarrassing, so that still sets the verdict
    built = f"data built {age}d ago" if age is not None else "build date unknown"
    return {"name": "timeline", "state": STALE if stale else READY,
            "detail": (f"{built}, {lapsed} of {len(cases)} cases past their deadline — run "
                       f"apps/musc-appeal-automation/refresh_demo.py" if stale
                       else f"{built}, {appealable}/{len(cases)} appealable, {lapsed} lapsed")}


def cleanliness(base: str) -> dict:
    """Anything not at ``ready`` is someone else's work in progress, on the client's board."""
    snap = demo_state.snapshot(base)
    return {"name": "board state", "state": DIRTY if snap["cases"] else READY,
            "detail": (f"{demo_state.describe(snap)} — snapshot then "
                       f"`python demo_state.py reset --base {base}` if that was a smoke test"
                       if snap["cases"] else "every case at ready")}


def checks(base: str) -> list[dict]:
    return [freshness(base), cleanliness(base)]
