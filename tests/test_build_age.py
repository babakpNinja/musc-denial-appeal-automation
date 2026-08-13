#!/usr/bin/env python3
"""How old is the data this app is serving?

Every deadline on the board is anchored to build day, so the build date is the
demo's shelf life. It is recorded at build time and served on /api/health so
nobody has to rebuild the DB locally just to find out how stale the deploy is.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import app as webapp  # noqa: E402
import build_db  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A throwaway DB the app reads instead of the shipped one."""
    path = tmp_path / "t.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr(webapp, "DB", path)
    return path


def stamp(path: Path, when: date | None = None) -> None:
    con = sqlite3.connect(path)
    build_db.stamp(con, when)
    con.close()


def test_a_db_built_today_is_zero_days_old(db):
    stamp(db)
    assert webapp.build_info() == {"built_at": date.today().isoformat(), "built_days_ago": 0}


def test_the_age_counts_up_from_build_day(db):
    stamp(db, date.today() - timedelta(days=34))
    assert webapp.build_info()["built_days_ago"] == 34


def test_a_db_from_before_the_meta_table_still_serves(db):
    """Old data is worth serving; it just cannot say how old it is."""
    assert webapp.build_info() == {"built_at": None, "built_days_ago": None}


def test_a_rebuild_restamps_rather_than_piling_up_rows(db):
    stamp(db, date(2026, 1, 1))
    stamp(db)
    rows = list(sqlite3.connect(db).execute("SELECT key, value FROM meta"))
    assert rows == [("built_at", date.today().isoformat())]


def test_the_schema_a_real_build_creates_carries_meta():
    assert "CREATE TABLE meta" in build_db.SCHEMA
