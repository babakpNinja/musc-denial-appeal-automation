#!/usr/bin/env python3
"""Keep cached letter drafts in step with the rebuilt claim timeline.

``build_db.py`` anchors every claim to the *current* date, so a rebuild on a
later day moves each case's service / submitted / denial / deadline dates. The
LLM prose is cached (``data/letter_drafts.json``, ``appeals.sections_json``) and
cites those dates verbatim, so without this step a rebuilt demo ships letters
arguing about dates the claim no longer has.

Each cached draft records the dates it was written against under ``draft_dates``;
this rewrites the prose whenever the database disagrees, in every format the
letters use. No LLM calls -- it is a textual re-point, not a re-draft.

    python retime_drafts.py            # report what would change
    python retime_drafts.py --apply    # rewrite the cache + the appeals table
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "data" / "musc_appeals.db"
DRAFTS_PATH = APP_DIR / "data" / "letter_drafts.json"

# claim facts a draft quotes that a rebuild can move
DATE_KEYS = ("service_date", "submitted_date", "denial_date", "appeal_deadline")
TEXT_FIELDS = ("letter_text", "argument_summary", "sections_json")


def date_forms(iso: str) -> list[str]:
    """Every rendering of ``iso`` that appears in a letter, in a fixed order.

    The order is what pairs an old date with its replacement, so it must not
    depend on the date itself -- no sorting, no de-duplication here.
    """
    try:
        d = datetime.strptime(str(iso)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return []
    return [
        d.isoformat(),                        # 2026-09-04
        f"{d:%B} {d.day}, {d.year}",          # September 4, 2026
        f"{d:%B} {d.day:02d}, {d.year}",      # September 04, 2026
        f"{d.month}/{d.day}/{d.year}",        # 9/4/2026
        f"{d:%m/%d/%Y}",                      # 09/04/2026
        f"{d:%B} {d.year}",                   # September 2026
    ]


def substitutions(was: dict, now: dict) -> list[tuple[str, str]]:
    """Old -> new literals for one case, longest match first."""
    pairs: list[tuple[str, str]] = []
    for key in ("denial_id", "claim_id"):
        if was.get(key) and now.get(key) and was[key] != now[key]:
            pairs.append((was[key], now[key]))
    for key in DATE_KEYS:
        old, new = was.get(key), now.get(key)
        if not old or not new or old == new:
            continue
        pairs += list(zip(date_forms(old), date_forms(new)))
    # ids and full dates overlap ("MUSC-ABCD-1" inside "DEN-MUSC-ABCD-1"), so the
    # longest literal has to win the match
    return sorted(dict.fromkeys(pairs), key=lambda p: len(p[0]), reverse=True)


def rewrite(text: str, pairs: list[tuple[str, str]]) -> str:
    """Apply every substitution in one pass.

    Sequential ``str.replace`` calls chain: one case had a submitted date equal to
    another date's *new* value, so the second pass rewrote what the first had just
    written. Matching everything at once makes each character rewritten only once.
    """
    if not text or not pairs:
        return text
    table: dict[str, str] = {}
    for old, new in pairs:          # first pair wins if two literals coincide
        table.setdefault(old, new)
    pattern = re.compile("|".join(re.escape(o) for o in table))
    return pattern.sub(lambda m: table[m.group(0)], text)


def current_facts(db_path: Path = DB_PATH) -> dict[str, dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT d.denial_id, d.claim_id, c.service_date, c.submitted_date,"
        "       d.denial_date, d.appeal_deadline"
        "  FROM denials d JOIN claims c ON c.claim_id = d.claim_id"
    ).fetchall()
    con.close()
    return {r["denial_id"]: dict(r) for r in rows}


def retime(cache: dict, facts: dict[str, dict]) -> tuple[dict, list[str]]:
    """Rewrite every draft whose recorded dates no longer match the database."""
    changed: list[str] = []
    for did, row in cache.items():
        now = facts.get(did)
        was = row.get("draft_dates")
        if not now:
            continue
        if was:
            pairs = substitutions(was, now)
            if pairs:
                for field in TEXT_FIELDS:
                    if row.get(field):
                        row[field] = rewrite(row[field], pairs)
                changed.append(did)
        row["draft_dates"] = {k: now[k] for k in ("claim_id", "denial_id") + DATE_KEYS}
    return cache, changed


def write_back(cache: dict, db_path: Path = DB_PATH) -> int:
    """Push the retimed prose into the appeals table the renderer reads."""
    con = sqlite3.connect(db_path)
    n = 0
    for did, row in cache.items():
        cur = con.execute(
            "UPDATE appeals SET letter_text = ?, argument_summary = ?, sections_json = ?"
            " WHERE denial_id = ?",
            (row.get("letter_text"), row.get("argument_summary"),
             row.get("sections_json"), did),
        )
        n += cur.rowcount
    con.commit()
    con.close()
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: report only)")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--cache", type=Path, default=DRAFTS_PATH)
    args = ap.parse_args()

    cache = json.loads(args.cache.read_text())
    facts = current_facts(args.db)
    orphans = [d for d in cache if d not in facts]
    cache, changed = retime(cache, facts)
    undrafted = [d for d in facts if d not in cache]

    print(f"drafts {len(cache)} | cases {len(facts)} | retimed {len(changed)} "
          f"| orphaned drafts {len(orphans)} | cases without a draft {len(undrafted)}")
    if args.apply:
        args.cache.write_text(json.dumps(cache, indent=1, sort_keys=True))
        print(f"updated appeals rows: {write_back(cache, args.db)}")
    else:
        print("dry run; pass --apply to write")


if __name__ == "__main__":
    main()
