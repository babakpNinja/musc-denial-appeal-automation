#!/usr/bin/env python3
"""Make sure the letters exist before anything asserts about them.

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

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
LETTERS = APP_DIR / "letters"


def _missing() -> bool:
    import sqlite3

    con = sqlite3.connect(f"file:{APP_DIR / 'data' / 'musc_appeals.db'}?mode=ro", uri=True)
    try:
        want = con.execute("SELECT COUNT(*) FROM denials").fetchone()[0]
    finally:
        con.close()
    return len(list(LETTERS.glob("DEN-*.pdf"))) < want


def pytest_configure(config: pytest.Config) -> None:
    if not _missing():
        return
    proc = subprocess.run([sys.executable, "generate_letters.py", "--rerender"],
                          cwd=APP_DIR, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0 or _missing():
        raise pytest.UsageError(
            "letters are missing and could not be rendered from the stored drafts:\n"
            + (proc.stderr or proc.stdout)[-800:])
