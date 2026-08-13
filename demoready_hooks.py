#!/usr/bin/env python3
"""What "safe to show a client" means for the denial-appeal board.

``tools/demoready.py`` already knows the demo is up and rendering. Three things it
cannot know: whether the queue still has live deadlines (the timeline is
anchored to build day and decays — see ``refresh_demo.py``), whether the last
person to poke at it left cases sitting in "submitted", and whether the files the
page hangs off itself actually deployed — ``/assets`` is a separate static mount
from the page, so the client's own logo can 404 into a broken-image icon at the
top of their board while every other check here says READY (#106).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import demo_state
from html_tags import A, IMG, LINK, fetched, tags_in
from refresh_demo import MAX_LAPSED

TIMEOUT = 30
READY, STALE, DIRTY, DOWN = "READY", "STALE", "DIRTY", "DOWN"

# What the board is wearing. The logo is the client's own brand on the client's own
# demo: showing it broken is the embarrassing case, so it is DOWN, while the
# download-all bundle is a button that fails when pressed — bad, not unshowable.
LOGO = "/assets/musc-logo-navy.png"

# The page builds its per-case links in JS, so `${...}` is a template, not a URL.
# Counted and named rather than dropped: a check nobody made reads like one that
# passed.
TEMPLATE = "${"


def _get(base: str, path: str):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def _status(base: str, path: str) -> int:
    """The status code for a URL, where a 404 is the answer rather than an error."""
    try:
        with urllib.request.urlopen(base.rstrip("/") + path, timeout=TIMEOUT) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def _page(base: str) -> str:
    with urllib.request.urlopen(base.rstrip("/") + "/", timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


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


def letters(base: str) -> dict:
    """Every denial must still have a letter to open.

    The PDFs are no longer committed: the deploy mirror renders them in its
    ``prepare`` step. Uptime opens one of them, which catches "none shipped" but
    not "half shipped", so compare the counts the deploy reports about itself.
    """
    h = _get(base, "/api/health")
    on_disk, denials = h.get("letters_on_disk"), h.get("denials")
    short = on_disk is not None and denials is not None and on_disk < denials
    return {"name": "letters", "state": DOWN if short else READY,
            "detail": (f"only {on_disk} of {denials} letters shipped — the mirror's prepare step "
                       f"(generate_letters.py --rerender) did not finish; re-run "
                       f"`python tools/mirror.py push musc-appeals`" if short
                       else f"{on_disk} letters on disk")}


def assets(base: str) -> dict:
    """Does everything the board's page hangs off itself actually deploy? (#106)

    ``/assets`` is mounted separately from the page, so the logo can go missing on
    its own: the board renders, every API answers, and the client's brand is a
    broken-image icon above their own cases. Nothing else here would notice.

    The per-case PDF links are built in JavaScript from a template, so there is no
    URL to ask for — those are counted as skipped rather than passed over, because
    a check nobody made reads exactly like one that passed.
    """
    page = _page(base)
    refs = ([t.get("href", "") for t in tags_in(page, LINK)]
            + [t.get("src", "") for t in tags_in(page, IMG)]
            + [t.get("href", "") for t in tags_in(page, A)])
    templated = sorted({r for r in refs if TEMPLATE in r})
    wanted = sorted({r for r in refs if fetched(r) and TEMPLATE not in r})
    missing = [r for r in wanted if _status(base, r) != 200]

    skipped = f"; {len(templated)} link(s) built in JS not checked" if templated else ""
    if LOGO in missing:
        return {"name": "assets", "state": DOWN,
                "detail": f"the MUSC logo is not deployed ({LOGO}) — the client's own brand "
                          f"renders as a broken image on their board; do not show it"}
    if missing:
        return {"name": "assets", "state": DIRTY,
                "detail": f"linked file(s) missing: {', '.join(missing)} — the board reads "
                          f"fine, the link fails when pressed"}
    return {"name": "assets", "state": READY,
            "detail": f"{len(wanted)} linked file(s) load{skipped}"}


def checks(base: str) -> list[dict]:
    return [freshness(base), letters(base), assets(base), cleanliness(base)]
