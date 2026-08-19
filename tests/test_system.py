#!/usr/bin/env python3
"""End-to-end checks for the MUSC appeal automation system.

Run:  python -m pytest tests -q     (from apps/musc-appeal-automation)
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pypdfium2 as pdfium
import pytest

APP_DIR = Path(__file__).resolve().parent.parent

# No `import app` here any more. The nine smoke tests below must not be able to
# reach the application object at all — reaching it is how a live run ends up
# answering itself. What is left of the module's local knowledge is `DB` and the
# PDFs, which are the *expectation* the deploy is measured against.

DB = APP_DIR / "data" / "musc_appeals.db"
LETTERS = APP_DIR / "letters"

# The HTTP tests below take `client` from conftest instead of a module-level
# `TestClient(webapp.app)`. The fixture is the in-process client with no
# `--base-url` and a real socket with one, and that is the whole point: a smoke
# test holding a module-level in-process client would go green against a deploy
# it never contacted (#368).
#
# `ALL_DENIALS` stays local on purpose. Pointed at a URL, every `== len(ALL_DENIALS)`
# below becomes "does the deploy hold the database this workspace is about to
# ship" — which is the question a shipped-the-old-DB failure answers no to.


def rows(sql, params=()):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


ALL_DENIALS = rows("SELECT d.*, p.mrn, p.full_name FROM denials d JOIN patients p USING(patient_id)")


# ----------------------------------------------------------------- database

def test_db_populated():
    counts = rows("""SELECT (SELECT COUNT(*) FROM patients) patients,
                            (SELECT COUNT(*) FROM claims) claims,
                            (SELECT COUNT(*) FROM denials) denials,
                            (SELECT COUNT(*) FROM payers) payers,
                            (SELECT COUNT(*) FROM appeals) appeals""")[0]
    assert counts["patients"] == 50
    assert counts["denials"] >= 50
    assert counts["claims"] == counts["denials"]
    assert counts["appeals"] == counts["denials"], "every denial needs a pre-generated appeal"
    assert counts["payers"] >= 8


def test_referential_integrity():
    assert not rows("SELECT 1 FROM denials d LEFT JOIN claims c USING(claim_id) WHERE c.claim_id IS NULL")
    assert not rows("SELECT 1 FROM denials d LEFT JOIN patients p USING(patient_id) WHERE p.patient_id IS NULL")
    assert not rows("SELECT 1 FROM denials d LEFT JOIN payers pay USING(payer_id) WHERE pay.payer_id IS NULL")
    assert not rows("SELECT 1 FROM coverage c LEFT JOIN payers p USING(payer_id) WHERE p.payer_id IS NULL")


def test_denial_fields_are_coded_and_costed():
    bad = rows("""SELECT denial_id FROM denials
                  WHERE carc_code IS NULL OR carc_description IS NULL
                     OR denied_amount IS NULL OR denied_amount <= 0
                     OR appeal_deadline IS NULL OR category IS NULL""")
    assert not bad, bad


def test_every_payer_has_an_appeal_portal_link():
    for p in rows("SELECT payer_id, name, portal_url, appeal_url FROM payers"):
        url = p["appeal_url"] or p["portal_url"]
        assert url and url.startswith("https://"), p


# ----------------------------------------------------------------- letters

@pytest.mark.parametrize("denial", ALL_DENIALS, ids=lambda d: d["denial_id"])
def test_letter_pdf_is_valid_and_case_specific(denial):
    """Every case has a real PDF naming that patient, claim and denial code."""
    path = LETTERS / f"{denial['denial_id']}.pdf"
    assert path.exists(), f"missing letter for {denial['denial_id']}"
    assert path.stat().st_size > 4000, "PDF suspiciously small"

    doc = pdfium.PdfDocument(path)
    assert len(doc) >= 1
    text = "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    squashed = re.sub(r"\s+", " ", text)

    assert denial["mrn"] in squashed
    assert denial["claim_id"] in squashed
    assert denial["full_name"].split()[-1] in squashed
    assert f"CARC {denial['carc_code']}" in squashed
    assert "MUSC" in squashed
    assert "169 Ashley Avenue" in squashed          # letterhead address block
    assert "synthetic" in squashed.lower()          # synthetic-data disclosure
    assert len(squashed.split()) > 250, "letter body too short"
    assert "[insert" not in squashed.lower() and "lorem ipsum" not in squashed.lower()


def test_letters_carry_the_musc_logo():
    """The letterhead image must actually be embedded on page 1."""
    for denial in ALL_DENIALS[:5]:
        doc = pdfium.PdfDocument(LETTERS / f"{denial['denial_id']}.pdf")
        kinds = {obj.type for obj in doc[0].get_objects()}
        assert 3 in kinds, "no image object (logo) on page 1"  # 3 == FPDF_PAGEOBJ_IMAGE


def test_letter_is_addressed_to_the_right_payer():
    for d in ALL_DENIALS[:15]:
        payer = rows("SELECT name FROM payers WHERE payer_id=?", (d["payer_id"],))[0]["name"]
        doc = pdfium.PdfDocument(LETTERS / f"{d['denial_id']}.pdf")
        text = re.sub(r"\s+", " ", doc[0].get_textpage().get_text_range())
        head = payer.split("(")[0].strip()
        assert head[:18] in text, f"{d['denial_id']} not addressed to {payer}"


def test_stored_draft_allows_offline_rerender():
    missing = rows("SELECT denial_id FROM appeals WHERE sections_json IS NULL OR sections_json = ''")
    assert not missing, "drafts must be persisted so PDFs re-render without LLM calls"
    for r in rows("SELECT sections_json FROM appeals LIMIT 5"):
        assert json.loads(r["sections_json"])["sections"]


# ----------------------------------------------------------------- api / ui

@pytest.mark.smoke
def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["letters_on_disk"] >= body["denials"]
    # >= let a PDF for a deleted case pad the count forever; the mismatch is the
    # thing worth reporting, so the count has to be exactly the live cases (#118).
    assert (body["orphan_letters"], body["orphan_letter_ids"]) == (0, []), (
        "letter PDFs on disk for cases that are no longer in denials — "
        "generate_letters.py --prune-orphans")


@pytest.mark.smoke
def test_health_says_how_old_the_shipped_data_is(client):
    """The deployed board has to be able to answer "how old are you?" itself —
    otherwise the only way to know is to rebuild it locally and diff."""
    body = client.get("/api/health").json()
    assert body["built_at"], "shipped DB has no meta.built_at — run build_db.py --stamp"
    assert body["built_days_ago"] >= 0


@pytest.mark.smoke
def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Denial Appeal Automation" in r.text
    assert "synthetic" in r.text.lower()


# --- keeping a synthetic-data demo out of search results (#461) --------------
# Until this issue the app had nothing to say on the subject: the live URL 404'd
# for robots.txt, and no response carried a header. Two facts, so two tests —
# the file is what a crawler asks for, the header is what reaches it when it did
# not ask, or asked and was handed the wrong content type, which is exactly how
# the game's `Disallow: /` sat there doing nothing for months (#369).

@pytest.mark.smoke
def test_robots_asks_crawlers_to_stay_out(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200, "no robots.txt is served at all"
    assert "Disallow: /" in r.text, f"robots.txt does not disallow anything: {r.text!r}"
    # Google treats a robots.txt of any other type as no robots.txt at all, so
    # this half is not decoration: it is the difference between the line above
    # being obeyed and being skipped.
    assert r.headers["content-type"].startswith("text/plain"), (
        f"robots.txt is served as {r.headers['content-type']!r}, and a crawler "
        f"handed anything but text/plain reads it as absent — crawl everything")


# Spelled out here rather than imported from app.py on purpose: a test that reads
# the value from the code it is checking agrees with any value that code grows
# next, including one that protects nobody. Changing the app's header should cost
# a deliberate second edit, here.
NOINDEX = "noindex, nofollow"

#: A segment no route matches, so the response is written by whoever writes this
#: app's 404s — asked at the root and once under each mount, because a
#: `StaticFiles` mount is a separate ASGI app and answers its own (#478).
PROBE = "/ninja-noindex-probe-no-such-path"

APP_SRC = (APP_DIR / "app.py").read_text()

#: The routing table, read out of the source rather than off the application
#: object. These paths are *subjects*, not expected values, and the declaration
#: they come from grows on its own — but importing `app` here would hand a live
#: run this box's application to answer with, which is the one thing this module
#: is careful not to have (#368). A regex over the decorators costs no import.
ROUTE = re.compile(r"^@app\.(?:get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']", re.M)
#: …and its oracle. A decorator this cannot read — a path built from a variable,
#: a router included from another module — would leave a route unasked while
#: saying nothing, which is the failure one level up from the one being fixed.
DECORATOR = re.compile(r"^@app\.(?:get|post|put|patch|delete)\(", re.M)
MOUNT = re.compile(r"^app\.mount\(\s*[\"']([^\"']+)[\"']", re.M)
#: FastAPI's own docs route, declared in the `FastAPI(...)` call and written by
#: somebody else entirely — exactly the kind of route that inherits a middleware
#: or does not.
DOCS = re.compile(r"docs_url\s*=\s*[\"']([^\"']+)[\"']")

#: The one path parameter these routes use, filled from this box's database —
#: the same stance the rest of the file takes, where local data is the
#: expectation the deploy is measured against. A route that grows a *new*
#: parameter is not silently dropped: `test_every_declared_route_can_be_asked`.
FILLABLE = {"denial_id": ALL_DENIALS[0]["denial_id"]}
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def declared_routes() -> list[str]:
    """Every path app.py declares a route for, in source order."""
    paths = ROUTE.findall(APP_SRC)
    assert len(paths) == len(DECORATOR.findall(APP_SRC)), (
        "a route decorator in app.py declares its path in a way this regex cannot read, so "
        "that route would drop out of the noindex subjects below with nothing saying so — "
        "teach the regex, or the list has quietly gone back to being hand-written")
    return paths


def unfillable_routes() -> list[str]:
    """Declared paths whose parameters nothing here knows a good value for."""
    return [p for p in declared_routes()
            if any(name not in FILLABLE for name in PLACEHOLDER.findall(p))]


def noindex_subjects() -> list[str]:
    """Every path worth asking for the header, derived rather than listed.

    Three sources, none of them typed out here:

    * the routing table, with `{denial_id}` filled from the DB — including the
      POST routes, which are asked with GET on purpose. That 405 is written by
      Starlette's router rather than by any handler in this app, and it is a
      response a crawler can be holding. GET is also the only safe way to ask:
      a POST here would reset a live demo board.
    * one real file and one unrouted path under each `app.mount` prefix, because
      a mount is a separate ASGI app. That the middleware wraps it is a fact
      about how Starlette composes them, and `/` cannot demonstrate it.
    * the root probe.

    What it replaces is a list of five paths somebody imagined (#479): the
    letters, the zip, the assets mount and every POST exit went unasked, and
    nothing made adding a route add it here.
    """
    paths = [PROBE]
    unfillable = unfillable_routes()
    for path in declared_routes():
        if path in unfillable:
            continue                    # its own test complains; this one cannot ask it
        paths.append(PLACEHOLDER.sub(lambda m: FILLABLE[m.group(1)], path))
    for prefix in MOUNT.findall(APP_SRC):
        served = APP_DIR / prefix.strip("/")
        files = sorted(f for f in served.rglob("*") if f.is_file()) if served.is_dir() else []
        assert files, (
            f"app.py mounts {prefix} but {served} holds no files, so the mount would be "
            f"asked nothing but its 404 — the convention read here is that a mount's "
            f"prefix names the directory beside app.py")
        paths.append(f"{prefix.rstrip('/')}/{files[0].relative_to(served).as_posix()}")
        paths.append(f"{prefix.rstrip('/')}{PROBE}")
    paths.extend(DOCS.findall(APP_SRC))
    return sorted(dict.fromkeys(paths))


#: Computed once at import, so `--collect-only` shows what is being asked — which
#: is how anyone checks that the route they added today is in the list.
NOINDEX_SUBJECTS = noindex_subjects()


@pytest.mark.smoke
@pytest.mark.parametrize("path", NOINDEX_SUBJECTS)
def test_every_response_carries_the_noindex_header(client, path):
    """Including the 404s, the 405s, the mounts and the payloads.

    Parametrised across the kinds of response the app makes rather than one
    happy path: the header is set by middleware, and middleware that stopped
    wrapping a mount or an error would still look right at `/`.

    The whole value, not `"noindex" in got`: X-Robots-Tag may be scoped to one
    crawler, so `googlebot: noindex` contains the word while telling every other
    crawler nothing at all. The gate stopped reading it as a substring in #472;
    this suite kept doing it until #474.

    Streamed and never read: `/letters.zip` is 5.8MB of PDFs and this asks only
    what the response says about itself. Still a GET — a HEAD is a different
    request, and a header that rides on one and not the other is exactly the
    gap not to introduce into the check that looks for it.
    """
    with client.stream("GET", path) as r:
        got = r.headers.get("x-robots-tag", "")
    assert got == NOINDEX, (
        f"{path} came back with X-Robots-Tag {got!r}, not {NOINDEX!r}, so anything that "
        f"reached it without reading robots.txt may be free to index a page of "
        f"patient-shaped data")


def test_every_declared_route_can_be_asked():
    """The other half of a derived list: what the derivation left out.

    A filter lets a case leave the world silently — a route that grows a path
    parameter nothing here can fill would simply stop being asked, and the suite
    would go green with fewer questions. So the leftovers are their own
    complaint, and the two sets partition the routing table.

    No `smoke`: it reads app.py off this box, and pointed at a URL it would
    still be reading app.py off this box.
    """
    unfillable = unfillable_routes()
    assert not unfillable, (
        f"{unfillable} declare a path parameter with no known-good value in FILLABLE, so "
        f"nothing asks them for the noindex header — add one, the way `denial_id` is "
        f"filled from the database")


def test_the_derived_subjects_cover_more_than_the_list_they_replaced():
    """A floor, because a derivation that quietly returns [] is a green suite.

    The paths below are the hand-written list this replaced (#479), spelled out
    one last time as the thing the derivation has to beat: if a regex stops
    matching or a mount is renamed, the subject list shrinks back towards them,
    and this says so instead of the parametrise silently getting shorter.
    """
    was = ["/", "/robots.txt", "/api/stats", "/static/index.html"]
    missing = [p for p in was if p not in NOINDEX_SUBJECTS]
    assert not missing, f"the derived subjects dropped {missing}, which the old list asked"
    assert len(NOINDEX_SUBJECTS) > len(was) + 1, (
        f"only {len(NOINDEX_SUBJECTS)} subjects were derived from app.py — no better than "
        f"the five that were typed out by hand, so the derivation has stopped working")
    mounted = [p for p in NOINDEX_SUBJECTS if p.endswith(PROBE) and p != PROBE]
    assert mounted, "no mount is asked what it does with a path it has no route for (#478)"


@pytest.mark.smoke
def test_stats_endpoint(client):
    s = client.get("/api/stats").json()
    assert s["totals"]["denials"] == len(ALL_DENIALS)
    assert s["totals"]["denied_total"] > 0
    assert len(s["by_payer"]) >= 8
    assert sum(p["denials"] for p in s["by_payer"]) == len(ALL_DENIALS)
    assert abs(sum(p["denied_amount"] for p in s["by_payer"]) - s["totals"]["denied_total"]) < 1


@pytest.mark.smoke
def test_cases_list_and_filters(client):
    all_cases = client.get("/api/cases").json()
    assert len(all_cases) == len(ALL_DENIALS)
    assert all(c["letter_path"] for c in all_cases), "every case must expose a letter"

    payer = all_cases[0]["payer_id"]
    filtered = client.get(f"/api/cases?payer={payer}").json()
    assert filtered and all(c["payer_id"] == payer for c in filtered)

    searched = client.get(f"/api/cases?search={all_cases[0]['mrn']}").json()
    assert any(c["mrn"] == all_cases[0]["mrn"] for c in searched)

    empty = client.get("/api/cases?search=zzzz-no-such-case").json()
    assert empty == []


@pytest.mark.smoke
def test_case_detail_has_clinical_context_and_portal(client):
    d = ALL_DENIALS[0]["denial_id"]
    c = client.get(f"/api/cases/{d}").json()
    assert c["denial_id"] == d
    assert c["letter_text"] and len(c["letter_text"]) > 500
    assert c["argument_summary"]
    assert (c["appeal_url"] or c["portal_url"]).startswith("https://")
    assert isinstance(c["conditions"], list)
    assert c["coverage"]["member_id"]
    assert client.get("/api/cases/DEN-does-not-exist").status_code == 404


@pytest.mark.smoke
def test_pdf_download_endpoint(client):
    d = ALL_DENIALS[0]["denial_id"]
    r = client.get(f"/letters/{d}.pdf?download=1")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"
    assert client.get("/letters/DEN-nope.pdf").status_code == 404


@pytest.mark.smoke
def test_bulk_zip_contains_every_letter(client):
    import io
    import zipfile
    r = client.get("/letters.zip")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert len(z.namelist()) == len(ALL_DENIALS)
        assert all(n.endswith(".pdf") for n in z.namelist())


@pytest.mark.smoke
def test_payers_endpoint_totals_match(client):
    payers = client.get("/api/payers").json()
    assert sum(p["denials"] for p in payers) == len(ALL_DENIALS)
    assert all(p["appeal_url"] or p["portal_url"] for p in payers)
