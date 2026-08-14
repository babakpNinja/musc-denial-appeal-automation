#!/usr/bin/env python3
"""The same letter must render to the same bytes, anywhere, on any day.

The PDFs are no longer committed: they are built from the cached drafts by the
deploy mirror's `prepare` step, in a throwaway clone with a random path. If a
re-render is not byte-for-byte reproducible, every deploy looks like 86 changed
files and `demoready`'s "is the deploy behind the repo?" check cries wolf
forever. Three things have broken that, and all three are guarded here:
ReportLab's per-run /CreationDate and document /ID, an embedded image named
after the *path* it was loaded from, and the header date.

The last one is why this file now tests `build_letter` and not only
`render_letter`. Every test below feeds `render_letter` a `letter_date` from a
constant, so the suite was green for four months while the *caller* stamped
`date.today()` on all 86 letters — the bytes changed at midnight, the mirror
reported 86 undeployed files every day, and no push could clear it for longer
than a day (#188). A reproducibility suite that supplies the varying input by
hand is testing its own fixture.

Run:  python -m pytest tests/test_render_determinism.py -q
"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import letterhead  # noqa: E402

SAMPLE = {
    "payer_name": "UnitedHealthcare",
    "payer_address": ["Provider Appeals Department", "P.O. Box 30432", "Salt Lake City, UT 84130"],
    "subject": "Formal Appeal of Claim Denial — Jane Q. Sample (MRN MUSC0000001)",
    "meta_rows": [("Patient", "Jane Q. Sample"), ("Claim ID", "MUSC-20260101-AAAA-1")],
    "sections": [("Summary", "This is a sample paragraph.\nSecond paragraph.")],
    "letter_date": "January 01, 2026",
    "enclosures": ["Remittance advice"],
}

RENDER_ELSEWHERE = """
import sys
sys.path.insert(0, %r)
from pathlib import Path
import letterhead
letterhead.render_letter(Path(sys.argv[1]), %r)
"""


def test_two_renders_are_identical(tmp_path):
    a = letterhead.render_letter(tmp_path / "a.pdf", SAMPLE).read_bytes()
    b = letterhead.render_letter(tmp_path / "b.pdf", SAMPLE).read_bytes()
    assert a == b, "a re-render changed the bytes — check rl_config.invariant"


def test_parallel_renders_match_the_serial_one(tmp_path):
    """generate_letters renders six at a time; shared render state must not leak."""
    want = letterhead.render_letter(tmp_path / "serial.pdf", SAMPLE).read_bytes()
    with ThreadPoolExecutor(max_workers=6) as pool:
        got = list(pool.map(lambda i: letterhead.render_letter(tmp_path / f"p{i}.pdf", SAMPLE).read_bytes(),
                            range(12)))
    assert all(b == want for b in got), "a concurrent render differs — check the logo cache"


def test_render_does_not_depend_on_where_it_runs(tmp_path):
    """A different working directory and output path must not change a byte."""
    here = letterhead.render_letter(tmp_path / "here.pdf", SAMPLE).read_bytes()

    elsewhere = tmp_path / "some" / "other" / "place"
    elsewhere.mkdir(parents=True)
    out = elsewhere / "there.pdf"
    proc = subprocess.run([sys.executable, "-c", RENDER_ELSEWHERE % (str(APP_DIR), SAMPLE), str(out)],
                          cwd=elsewhere, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert out.read_bytes() == here, "the render depends on its path — the deploy mirror will churn"


# ------------------------------------------------- the date is data, not a clock

import json  # noqa: E402
import sqlite3  # noqa: E402
from datetime import date  # noqa: E402

import generate_letters as gl  # noqa: E402


class _Clock:
    """A stand-in for ``datetime.date`` whose ``today()`` is whatever I say."""
    def __init__(self, day):
        self._day = day

    def today(self):
        return self._day

    def fromisoformat(self, s):
        return date.fromisoformat(s)


def _meta_db(tmp_path, built_at):
    db = tmp_path / "m.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    if built_at is not None:
        con.execute("INSERT INTO meta VALUES ('built_at', ?)", (built_at,))
    con.commit()
    return con


def test_the_letter_date_is_the_day_the_board_was_built(tmp_path, monkeypatch):
    con = _meta_db(tmp_path, "2026-08-13")
    monkeypatch.setattr(gl, "date", _Clock(date(2031, 12, 25)))
    assert gl.letter_date_for(con) == "August 13, 2026"


def test_a_board_with_no_build_date_falls_back_to_today_and_says_so(tmp_path, monkeypatch, capsys):
    """The pre-``meta`` DB app.py:144 also handles. Falling back reinstates the
    churn this whole file exists to prevent, so it must not be silent."""
    con = _meta_db(tmp_path, None)
    monkeypatch.setattr(gl, "date", _Clock(date(2031, 12, 25)))

    assert gl.letter_date_for(con) == "December 25, 2031"
    assert "depend on the clock" in capsys.readouterr().err


def test_a_real_letter_renders_the_same_bytes_on_two_different_days(tmp_path, monkeypatch):
    """The end-to-end property, and the one the tests above only approximate: the
    same case, rendered on two days a year apart, is the same file. This is what
    keeps `demoready`'s deployed check answerable — see the module docstring."""
    con = gl.connect()
    try:
        denial_id = con.execute("SELECT denial_id FROM denials ORDER BY denial_id").fetchone()[0]
        rec = gl.case_record(con, denial_id)
        row = con.execute("SELECT sections_json FROM appeals WHERE denial_id=?", (denial_id,)).fetchone()
        assert row and row["sections_json"], f"no cached draft for {denial_id} to render"
        drafted = json.loads(row["sections_json"])

        out = []
        for day in (date(2026, 8, 13), date(2027, 3, 1)):
            monkeypatch.setattr(gl, "date", _Clock(day))
            letter = gl.build_letter(rec, drafted, gl.letter_date_for(con))
            out.append(letterhead.render_letter(tmp_path / f"{day}.pdf", letter).read_bytes())
    finally:
        con.close()

    assert out[0] == out[1], (
        "the same case rendered differently on a different day — the deploy mirror "
        "will report every letter as undeployed, every day, forever (#188)")
