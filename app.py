#!/usr/bin/env python3
"""MUSC Health — AI Denial Appeal Automation dashboard (FastAPI + server-rendered UI)."""

from __future__ import annotations

import io
import os
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).parent
DB = HERE / "data" / "musc_appeals.db"
LETTERS = HERE / "letters"
ASSETS = HERE / "assets"

app = FastAPI(title="MUSC Appeal Automation", docs_url="/api/docs", redoc_url=None)
app.mount("/assets", StaticFiles(directory=str(ASSETS)), name="assets")


def q(sql: str, params: tuple = ()) -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def one(sql: str, params: tuple = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


# ------------------------------------------------------------- appeal workflow
# The shipped musc_appeals.db is read-only (it is rebuilt from FHIR + committed),
# so lifecycle changes live in their own small DB. If Railway mounts a volume we
# write there and the workflow is durable; otherwise it sits on the container's
# ephemeral disk and resets on redeploy — surfaced by /api/workflow and the UI.

STATUSES = ("ready", "submitted", "overturned", "upheld")
TRANSITIONS = {
    "ready": ("submitted",),
    "submitted": ("overturned", "upheld", "ready"),  # ready = withdrawn for correction
    "upheld": ("submitted",),                        # second-level appeal
    "overturned": (),
}
OUTCOMES = {"overturned": "overturned", "upheld": "upheld"}


def status_db_path() -> Path:
    """Resolved every call so tests (and a mounted volume) can redirect it."""
    if os.environ.get("APPEAL_STATUS_DB"):
        return Path(os.environ["APPEAL_STATUS_DB"])
    vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    return Path(vol) / "appeal_status.db" if vol else HERE / "data" / "appeal_status.db"


def is_durable() -> bool:
    return bool(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("APPEAL_STATUS_DURABLE"))


def status_con() -> sqlite3.Connection:
    path = status_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS appeal_status (
          denial_id TEXT PRIMARY KEY, status TEXT NOT NULL, submitted_at TEXT,
          outcome TEXT, note TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS appeal_status_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, denial_id TEXT NOT NULL,
          from_status TEXT, to_status TEXT NOT NULL, note TEXT, at TEXT NOT NULL);
    """)
    return con


def status_map() -> dict[str, dict]:
    con = status_con()
    try:
        return {r["denial_id"]: dict(r) for r in con.execute("SELECT * FROM appeal_status")}
    finally:
        con.close()


def apply_status(row: dict, smap: dict[str, dict] | None = None) -> dict:
    """Overlay workflow state onto a case row (defaults to 'ready')."""
    smap = status_map() if smap is None else smap
    s = smap.get(row["denial_id"], {})
    row["appeal_status"] = s.get("status", "ready")
    row["submitted_at"] = s.get("submitted_at")
    row["outcome"] = s.get("outcome")
    row["status_note"] = s.get("note")
    row["status_updated_at"] = s.get("updated_at")
    return row


CASE_SQL = """
SELECT d.denial_id, d.claim_id, d.patient_id, d.payer_id, d.denial_date, d.category,
       d.carc_code, d.carc_description, d.rarc_code, d.rarc_description,
       d.payer_remark, d.appeal_deadline, d.denied_amount, d.data_source,
       p.full_name, p.mrn, p.birth_date, p.age, p.gender,
       pay.name AS payer_name, pay.type AS payer_type, pay.portal_name,
       pay.portal_url, pay.appeal_url, pay.appeal_notes,
       c.service_date, c.submitted_date, c.cpt_code, c.cpt_description, c.units,
       c.icd10_primary, c.icd10_primary_desc, c.icd10_secondary, c.place_of_service,
       c.billed_amount, c.allowed_amount, c.paid_amount, c.rendering_provider, c.npi, c.facility,
       a.appeal_id, a.status AS appeal_status, a.letter_path, a.argument_summary,
       a.generated_at, a.model
FROM denials d
JOIN patients p ON p.patient_id = d.patient_id
JOIN payers   pay ON pay.payer_id = d.payer_id
JOIN claims   c ON c.claim_id = d.claim_id
LEFT JOIN appeals a ON a.denial_id = d.denial_id
"""


# --------------------------------------------------------------------------- api

@app.get("/api/health")
def health():
    n = one("SELECT (SELECT COUNT(*) FROM patients) patients, (SELECT COUNT(*) FROM denials) denials,"
            " (SELECT COUNT(*) FROM appeals) appeals")
    return {"status": "ok", **n, "letters_on_disk": len(list(LETTERS.glob("DEN-*.pdf")))}


@app.get("/api/stats")
def stats():
    smap = status_map()
    amounts = {r["denial_id"]: r["denied_amount"] for r in q("SELECT denial_id, denied_amount FROM denials")}
    wf = {s: {"denials": 0, "denied_amount": 0.0} for s in STATUSES}
    for did, amt in amounts.items():
        b = wf[smap.get(did, {}).get("status", "ready")]
        b["denials"] += 1
        b["denied_amount"] = round(b["denied_amount"] + (amt or 0), 2)
    return {
        "workflow": {
            **wf,
            "outstanding": wf["ready"]["denials"] + wf["submitted"]["denials"],
            "durable": is_durable(),
        },
        "totals": one(
            "SELECT COUNT(DISTINCT d.patient_id) patients, COUNT(*) denials,"
            " ROUND(SUM(d.denied_amount),2) denied_total,"
            " ROUND(AVG(d.denied_amount),2) denied_avg FROM denials d"),
        "by_payer": q("""SELECT pay.name payer, pay.payer_id, pay.type, COUNT(*) denials,
                                ROUND(SUM(d.denied_amount),2) denied_amount,
                                ROUND(AVG(d.denied_amount),2) avg_denial,
                                pay.appeal_url, pay.portal_name
                         FROM denials d JOIN payers pay ON pay.payer_id=d.payer_id
                         GROUP BY pay.payer_id ORDER BY denied_amount DESC"""),
        "by_reason": q("""SELECT category, COUNT(*) denials, ROUND(SUM(denied_amount),2) denied_amount
                          FROM denials GROUP BY category ORDER BY denied_amount DESC"""),
        "by_carc": q("""SELECT carc_code, carc_description, COUNT(*) denials,
                               ROUND(SUM(denied_amount),2) denied_amount
                        FROM denials GROUP BY carc_code ORDER BY denials DESC LIMIT 10"""),
        "by_month": q("""SELECT substr(denial_date,1,7) month, COUNT(*) denials,
                                ROUND(SUM(denied_amount),2) denied_amount
                         FROM denials GROUP BY month ORDER BY month"""),
    }


@app.get("/api/cases")
def cases(payer: str = "", category: str = "", search: str = "", status: str = "", limit: int = 500):
    where, params = [], []
    if payer:
        where.append("d.payer_id = ?"); params.append(payer)
    if category:
        where.append("d.category = ?"); params.append(category)
    if search:
        where.append("(p.full_name LIKE ? OR p.mrn LIKE ? OR d.claim_id LIKE ? OR c.cpt_code LIKE ?"
                     " OR d.carc_code LIKE ? OR c.icd10_primary LIKE ?)")
        params += [f"%{search}%"] * 6
    sql = CASE_SQL + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY d.denied_amount DESC LIMIT ?"
    params.append(limit)
    smap = status_map()
    rows = [apply_status(r, smap) for r in q(sql, tuple(params))]
    if status:
        rows = [r for r in rows if r["appeal_status"] == status]
    return rows


@app.get("/api/cases/{denial_id}")
def case_detail(denial_id: str):
    row = one(CASE_SQL + " WHERE d.denial_id = ?", (denial_id,))
    if not row:
        raise HTTPException(404, "case not found")
    apply_status(row)
    row["status_events"] = status_events(denial_id)
    row["next_statuses"] = list(TRANSITIONS[row["appeal_status"]])
    pid = row["patient_id"]
    row["conditions"] = q("SELECT display, icd10, clinical_status, onset_date FROM conditions"
                          " WHERE patient_id=? AND display IS NOT NULL ORDER BY onset_date DESC LIMIT 12", (pid,))
    row["procedures"] = q("SELECT display, code, performed_date FROM procedures WHERE patient_id=?"
                          " ORDER BY performed_date DESC LIMIT 8", (pid,))
    row["observations"] = q("SELECT display, value, unit, effective_date FROM observations WHERE patient_id=?"
                            " AND value IS NOT NULL ORDER BY effective_date DESC LIMIT 8", (pid,))
    row["coverage"] = one("SELECT member_id, group_number, plan_name, period_start, period_end"
                          " FROM coverage WHERE patient_id=?", (pid,))
    row["letter_text"] = (one("SELECT letter_text FROM appeals WHERE denial_id=?", (denial_id,)) or {}).get("letter_text")
    return row


@app.get("/api/payers")
def payers():
    return q("""SELECT pay.*, COUNT(d.denial_id) denials, ROUND(COALESCE(SUM(d.denied_amount),0),2) denied_amount
                FROM payers pay LEFT JOIN denials d ON d.payer_id=pay.payer_id
                GROUP BY pay.payer_id ORDER BY denied_amount DESC""")


@app.get("/letters/{denial_id}.pdf")
def letter(denial_id: str, download: int = 0):
    path = LETTERS / f"{denial_id}.pdf"
    if not path.exists():
        raise HTTPException(404, "letter not generated")
    return FileResponse(path, media_type="application/pdf",
                        filename=f"MUSC-Appeal-{denial_id}.pdf" if download else None,
                        headers={} if download else {"Content-Disposition": f'inline; filename="{denial_id}.pdf"'})


@app.get("/letters.zip")
def letters_zip(payer: str = ""):
    rows = q("SELECT denial_id FROM denials" + (" WHERE payer_id=?" if payer else ""), (payer,) if payer else ())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in rows:
            p = LETTERS / f"{r['denial_id']}.pdf"
            if p.exists():
                z.write(p, f"MUSC-Appeal-{r['denial_id']}.pdf")
    buf.seek(0)
    name = f"MUSC-appeal-letters{'-' + payer if payer else ''}.zip"
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


def status_events(denial_id: str) -> list[dict]:
    con = status_con()
    try:
        return [dict(r) for r in con.execute(
            "SELECT from_status, to_status, note, at FROM appeal_status_events"
            " WHERE denial_id=? ORDER BY id", (denial_id,))]
    finally:
        con.close()


@app.get("/api/workflow")
def workflow():
    """Where appeal status is stored and whether it survives a redeploy."""
    return {
        "statuses": list(STATUSES),
        "transitions": {k: list(v) for k, v in TRANSITIONS.items()},
        "durable": is_durable(),
        "store": str(status_db_path()),
        "note": ("Appeal status is stored on a mounted volume and persists across redeploys."
                 if is_durable() else
                 "Demo mode: appeal status is stored on the container's ephemeral disk and "
                 "resets when the app redeploys."),
    }


@app.get("/api/cases/{denial_id}/status")
def get_status(denial_id: str):
    if not one("SELECT 1 FROM denials WHERE denial_id=?", (denial_id,)):
        raise HTTPException(404, "case not found")
    row = apply_status({"denial_id": denial_id})
    return {**row, "events": status_events(denial_id), "next_statuses": list(TRANSITIONS[row["appeal_status"]])}


@app.post("/api/cases/{denial_id}/status")
def set_status(denial_id: str, payload: dict = Body(...)):
    if not one("SELECT 1 FROM denials WHERE denial_id=?", (denial_id,)):
        raise HTTPException(404, "case not found")
    new = (payload.get("status") or "").strip().lower()
    note = (payload.get("note") or "").strip()[:500] or None
    if new not in STATUSES:
        raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
    current = apply_status({"denial_id": denial_id})["appeal_status"]
    if new not in TRANSITIONS[current]:
        allowed = ", ".join(TRANSITIONS[current]) or "nothing (terminal state)"
        raise HTTPException(409, f"cannot move {current} -> {new}; allowed from {current}: {allowed}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prev = status_map().get(denial_id, {})
    submitted_at = now if new == "submitted" else (None if new == "ready" else prev.get("submitted_at"))
    outcome = OUTCOMES.get(new)
    con = status_con()
    try:
        con.execute("INSERT INTO appeal_status(denial_id,status,submitted_at,outcome,note,updated_at)"
                    " VALUES(?,?,?,?,?,?) ON CONFLICT(denial_id) DO UPDATE SET"
                    " status=excluded.status, submitted_at=excluded.submitted_at,"
                    " outcome=excluded.outcome, note=excluded.note, updated_at=excluded.updated_at",
                    (denial_id, new, submitted_at, outcome, note, now))
        con.execute("INSERT INTO appeal_status_events(denial_id,from_status,to_status,note,at)"
                    " VALUES(?,?,?,?,?)", (denial_id, current, new, note, now))
        con.commit()
    finally:
        con.close()
    return {**apply_status({"denial_id": denial_id}), "events": status_events(denial_id),
            "next_statuses": list(TRANSITIONS[new]), "durable": is_durable()}


@app.post("/api/cases/{denial_id}/regenerate")
def regenerate(denial_id: str):
    try:
        import generate_letters
        res = generate_letters.generate_one(denial_id, force=True)
        return JSONResponse(res)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"status": "error", "error": str(e)[:300]}, status_code=500)


# --------------------------------------------------------------------------- ui

@app.get("/", response_class=HTMLResponse)
def index():
    return (HERE / "static" / "index.html").read_text()


app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
