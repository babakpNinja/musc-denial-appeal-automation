#!/usr/bin/env python3
"""Where the app under test lives, and the letters it needs to exist.

The same tests run in-process and against the deployed site — the only
difference is where the ``client`` fixture is pointed:

    python -m pytest tests -q                                # the app in-process
    python -m pytest tests -q -m smoke --base-url=https://…  # the deployed site

With no ``--base-url`` the fixture is FastAPI's ``TestClient``, which never
opens a socket, and the suite is a pre-push gate. Pointed at a URL it is the
post-deploy check ``ship.py`` runs after the container has swapped (#368).

The 67 PDFs are deploy artifacts, not source: they are derived from the drafts
in ``data/letter_drafts.json`` and are no longer committed (the repo was gaining
5.8 MB every time the timeline was rebuilt). A fresh clone therefore has none, so
render them once per session — ~6 s, no model calls — and the whole suite still
checks every letter exactly as before.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

APP_DIR = Path(__file__).resolve().parent.parent
LETTERS = APP_DIR / "letters"

#: What `-m smoke` means here, written down because a marker with a vague meaning
#: grows until it is the suite again (#332). This suite is 251 tests and most of
#: them re-ask questions no deploy can answer differently — what a PDF renders to,
#: what `days_left` returns, whether the DB's foreign keys line up.
#:
#: A test earns `smoke` if a **deploy** can be the thing that makes it fail:
#:
#:   * data that is in the repo and not in the deploy, or is the wrong vintage
#:     over there — the DB, the letters (which are `keep`, not committed), the
#:     static index, the logo;
#:   * the app not booting at all: a route that 500s, an import that is missing
#:     from requirements.txt, a worker that never came up;
#:   * the server in front of it: the wrong content type, a redirect that is not
#:     followed, a stale container still serving the old build.
#:
#: A test does *not* earn it for being fast, and a test that reads the app's own
#: disk — `LETTERS.glob`, `sqlite3.connect(DB)`, `webapp.something` — can never
#: earn it, because pointed at a URL it would still be reading this box.
SMOKE = ("smoke: this can fail because of how the build was deployed, not just "
         "because of what the code does — the subset `ship.py` re-runs live")

#: `/letters.zip` streams 67 PDFs. httpx's default is 5s, which is generous
#: in-process and not obviously enough over the wire from a cold container.
LIVE_TIMEOUT = 60


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--base-url", action="store", default=None,
                     help="the deployed site to ask; omit to drive the app in-process")


def _missing() -> bool:
    import sqlite3

    con = sqlite3.connect(f"file:{APP_DIR / 'data' / 'musc_appeals.db'}?mode=ro", uri=True)
    try:
        want = con.execute("SELECT COUNT(*) FROM denials").fetchone()[0]
    finally:
        con.close()
    return len(list(LETTERS.glob("DEN-*.pdf"))) < want


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", SMOKE)
    if config.getoption("--base-url"):
        # The letters being asked for are the deploy's, and rendering this box's
        # copy of them would spend ~6 s to make no difference to a single
        # assertion. It would also hide the finding: a live run whose PDFs are
        # missing over there should go red, not quietly regenerate them here.
        return
    if not _missing():
        return
    proc = subprocess.run([sys.executable, "generate_letters.py", "--rerender"],
                          cwd=APP_DIR, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0 or _missing():
        raise pytest.UsageError(
            "letters are missing and could not be rendered from the stored drafts:\n"
            + (proc.stderr or proc.stdout)[-800:])


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Under ``--base-url``, a test that is not `smoke` must not quietly pass.

    `-m smoke` already deselects them, so this only fires when someone points the
    *whole* suite at a URL. Without it those tests read this box's database and
    this box's PDFs and go green, and the run reports 251 passed against a deploy
    that answered nine of them — the false-reassurance shape the marker exists to
    prevent. Skipped rather than failed: they are not broken, they are simply not
    about the thing being asked. `ship.verify_step` counts passed/failed/xfailed/
    xpassed only, so a skip can never pad the "N test(s) live" it prints.
    """
    if not config.getoption("--base-url"):
        return
    why = pytest.mark.skip(reason="--base-url is set and this test is not `smoke`: it reads "
                                  "this box's database and letters, so running it here would "
                                  "say nothing about the deploy")
    for item in items:
        if "smoke" not in item.keywords:
            item.add_marker(why)


@pytest.fixture(scope="session")
def client(request: pytest.FixtureRequest):
    """The app, in-process or over HTTP, with one call signature either way.

    ``TestClient`` is an ``httpx.Client`` subclass, so `.get("/api/health")` and
    everything it returns are identical — but two of its defaults are its own and
    both matter here. It follows redirects and a bare ``httpx.Client`` does not,
    so a route that started 307-ing would fail live and pass in-process for a
    reason that has nothing to do with the deploy. And its timeout is untimed
    where the network's is 5 s.
    """
    base = request.config.getoption("--base-url")
    if not base:
        from fastapi.testclient import TestClient

        sys.path.insert(0, str(APP_DIR))
        import app as webapp

        with TestClient(webapp.app) as c:
            yield c
        return
    with httpx.Client(base_url=base.rstrip("/"), follow_redirects=True,
                      timeout=LIVE_TIMEOUT) as c:
        yield c
