#!/usr/bin/env python3
"""MUSC Health — AI Denial Appeal Automation dashboard (FastAPI + server-rendered UI)."""

from __future__ import annotations

import io
import os
import sqlite3
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
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
    return {
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
def cases(payer: str = "", category: str = "", search: str = "", limit: int = 500):
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
    return q(sql, tuple(params))


@app.get("/api/cases/{denial_id}")
def case_detail(denial_id: str):
    row = one(CASE_SQL + " WHERE d.denial_id = ?", (denial_id,))
    if not row:
        raise HTTPException(404, "case not found")
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
