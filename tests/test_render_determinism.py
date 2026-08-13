#!/usr/bin/env python3
"""The same letter must render to the same bytes, anywhere.

The PDFs are no longer committed: they are built from the cached drafts by the
deploy mirror's `prepare` step, in a throwaway clone with a random path. If a
re-render is not byte-for-byte reproducible, every deploy looks like 67 changed
files and `demoready`'s "is the deploy behind the repo?" check cries wolf
forever. Two things broke that before, and both are guarded here: ReportLab's
per-run /CreationDate and document /ID, and an embedded image named after the
*path* it was loaded from.

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
