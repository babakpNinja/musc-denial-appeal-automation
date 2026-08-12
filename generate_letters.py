#!/usr/bin/env python3
"""Generate an AI-drafted, MUSC-letterhead appeal letter PDF for every denial.

Grounding rules (enforced in the prompt and by construction):
  * every clinical fact handed to the model comes straight out of the SQLite DB
  * the model may not invent policy numbers, dates, dollar figures or diagnoses
  * identifiers (patient, MRN, claim, CARC/RARC, amounts) are rendered from the
    DB into the letter's metadata block, not from the model output

Usage:
    python generate_letters.py            # generate anything missing
    python generate_letters.py --force    # regenerate everything
    python generate_letters.py --limit 3  # smoke test
    python generate_letters.py --denial DEN-...  # one case (used by the UI)
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, "/workspace/ninja")

from clients.litellm_client import get_config, get_headers, litellm_request, resolve_model  # noqa: E402

from letterhead import render_letter  # noqa: E402

HERE = Path(__file__).parent
DB = HERE / "data" / "musc_appeals.db"
LETTERS = HERE / "letters"
MODEL = "claude-sonnet"

PAYER_ADDRESSES = {
    "bcbssc": ["Provider Appeals Department", "P.O. Box 100300", "Columbia, SC 29202"],
    "bluechoice": ["Provider Appeals Unit", "P.O. Box 6170", "Columbia, SC 29260"],
    "uhc": ["Provider Appeals", "P.O. Box 30432", "Salt Lake City, UT 84130"],
    "aetna": ["Provider Resolution Team", "P.O. Box 14020", "Lexington, KY 40512"],
    "cigna": ["Cigna Appeals Unit", "P.O. Box 188011", "Chattanooga, TN 37422"],
    "humana": ["Provider Correspondence", "P.O. Box 14601", "Lexington, KY 40512"],
    "medicare": ["Palmetto GBA — J M Part B Appeals", "P.O. Box 100190", "Columbia, SC 29202"],
    "medicaid": ["SC Healthy Connections Medicaid — Appeals", "P.O. Box 8206", "Columbia, SC 29202"],
    "molina": ["Molina Healthcare of SC — Provider Appeals", "P.O. Box 40309", "North Charleston, SC 29423"],
    "absolutetotal": ["Absolute Total Care — Appeals", "P.O. Box 3000", "Farmington, MO 63640"],
    "tricare": ["TRICARE East — Claims Appeals", "P.O. Box 7981", "Madison, WI 53707"],
    "selfpay": ["Patient Financial Services", "Charleston, SC 29425"],
}

SYSTEM = """You are a senior appeals specialist in the Revenue Cycle department of MUSC Health
(Medical University of South Carolina), an academic medical center in Charleston, SC.

You draft formal, fact-based letters appealing insurance claim denials. Rules:
- Use ONLY facts contained in the CASE RECORD provided. Never invent diagnoses,
  dates, dollar amounts, policy/contract numbers, guideline citations with
  specific section numbers, lab values, or clinical events.
- If a supporting fact is not present, argue from what IS present rather than
  inventing it. Do not use placeholders like [insert] or TBD.
- Professional, respectful, confident. No hyperbole, no threats, no emotion.
- Quote the payer's stated denial reason and its CARC/RARC code verbatim.
- Reference generally accepted standards (e.g. medical necessity, correct coding
  per CPT/ICD-10 guidance, prompt-pay and appeal-rights obligations) in general
  terms only, without fabricating a specific policy number.
- Write in prose paragraphs. Do NOT include a date, address block, salutation,
  signature block, or "RE:" line — those are added by the letterhead template.

Output format — plain text using these exact markers, nothing else (no JSON, no markdown):

## Summary of Appeal
<paragraphs>
## Clinical Background
<paragraphs>
## Basis for Appeal
<paragraphs>
## Requested Action
<paragraphs>
## Argument Summary
<one sentence, under 200 characters, stating the core argument>

Target 450-650 words total across the four body sections."""


# --------------------------------------------------------------------------- data


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def _fmt_money(v) -> str:
    return f"${float(v or 0):,.2f}"


def case_record(con: sqlite3.Connection, denial_id: str) -> dict:
    """Assemble everything known about one denial into a single dict."""
    d = con.execute("SELECT * FROM denials WHERE denial_id=?", (denial_id,)).fetchone()
    if d is None:
        raise KeyError(denial_id)
    d = dict(d)
    claim = dict(con.execute("SELECT * FROM claims WHERE claim_id=?", (d["claim_id"],)).fetchone())
    pat = dict(con.execute("SELECT * FROM patients WHERE patient_id=?", (d["patient_id"],)).fetchone())
    payer = dict(con.execute("SELECT * FROM payers WHERE payer_id=?", (d["payer_id"],)).fetchone())
    cov = con.execute("SELECT * FROM coverage WHERE patient_id=?", (d["patient_id"],)).fetchone()
    cov = dict(cov) if cov else {}
    enc = con.execute("SELECT * FROM encounters WHERE encounter_id=?", (claim["encounter_id"],)).fetchone()
    enc = dict(enc) if enc else {}
    conds = [dict(r) for r in con.execute(
        "SELECT display, icd10, clinical_status, onset_date FROM conditions "
        "WHERE patient_id=? AND display IS NOT NULL ORDER BY onset_date DESC LIMIT 12",
        (d["patient_id"],))]
    procs = [dict(r) for r in con.execute(
        "SELECT display, code, performed_date FROM procedures WHERE patient_id=? "
        "ORDER BY performed_date DESC LIMIT 10", (d["patient_id"],))]
    obs = [dict(r) for r in con.execute(
        "SELECT display, value, unit, effective_date FROM observations WHERE patient_id=? "
        "AND value IS NOT NULL ORDER BY effective_date DESC LIMIT 12", (d["patient_id"],))]
    meds = [dict(r) for r in con.execute(
        "SELECT display, status, authored_on FROM medications WHERE patient_id=? "
        "ORDER BY authored_on DESC LIMIT 8", (d["patient_id"],))]
    return {
        "denial": d, "claim": claim, "patient": pat, "payer": payer,
        "coverage": cov, "encounter": enc, "conditions": conds,
        "procedures": procs, "observations": obs, "medications": meds,
    }


def prompt_for(rec: dict) -> str:
    d, c, p, pay = rec["denial"], rec["claim"], rec["patient"], rec["payer"]
    cov, enc = rec["coverage"], rec["encounter"]

    def lines(items, fmt):
        return "\n".join("  - " + fmt(i) for i in items) or "  - (none recorded)"

    return f"""CASE RECORD (all data synthetic; use only these facts)

PATIENT
  Name: {p['full_name']}   MRN: {p['mrn']}   DOB: {p['birth_date']}   Age: {p['age']}   Sex: {p['gender']}
  Address: {p.get('address')}, {p.get('city')}, {p.get('state')} {p.get('postal_code')}

COVERAGE
  Payer: {pay['name']} ({pay['type']})
  Member ID: {cov.get('member_id')}   Group: {cov.get('group_number')}   Plan: {cov.get('plan_name')}
  Coverage period: {cov.get('period_start')} to {cov.get('period_end') or 'active'}

CLAIM
  Claim ID: {c['claim_id']}   Date of service: {c['service_date']}   Submitted: {c['submitted_date']}
  Place of service: {c['place_of_service']}   Facility: {c['facility']}
  Rendering provider: {c['rendering_provider']} (NPI {c['npi']})
  CPT/HCPCS: {c['cpt_code']} — {c['cpt_description']}   Units: {c['units']}   Rev code: {c['revenue_code']}
  Primary diagnosis: {c['icd10_primary']} — {c['icd10_primary_desc']}   Secondary: {c['icd10_secondary']}
  Billed: {_fmt_money(c['billed_amount'])}   Allowed: {_fmt_money(c['allowed_amount'])}   Paid: {_fmt_money(c['paid_amount'])}   Denied: {_fmt_money(c['denied_amount'])}

DENIAL
  Denial date: {d['denial_date']}   Category: {d['category']}
  CARC {d['carc_code']}: {d['carc_description']}
  RARC {d['rarc_code']}: {d['rarc_description']}
  Payer remark: "{d['payer_remark']}"
  Appeal deadline: {d['appeal_deadline']} ({pay.get('appeal_notes')})

ENCOUNTER
  Class: {enc.get('class')}   Type: {enc.get('type_display')}
  Dates: {enc.get('start_date')} to {enc.get('end_date')}   Reason: {enc.get('reason')}

ACTIVE / RECORDED CONDITIONS
{lines(rec['conditions'], lambda i: f"{i['display']} (ICD-10 {i['icd10'] or 'n/a'}, {i['clinical_status'] or 'unknown status'}, onset {i['onset_date'] or 'n/a'})")}

PROCEDURES
{lines(rec['procedures'], lambda i: f"{i['display']} ({i['performed_date'] or 'n/a'})")}

OBSERVATIONS / RESULTS
{lines(rec['observations'], lambda i: f"{i['display']}: {i['value']} {i['unit'] or ''} ({i['effective_date'] or 'n/a'})")}

MEDICATIONS
{lines(rec['medications'], lambda i: f"{i['display']} ({i['status']}, ordered {i['authored_on'] or 'n/a'})")}

Draft the appeal body for this case, tailored to {pay['name']}'s appeal process.
"""


# --------------------------------------------------------------------------- llm


def call_model(prompt: str, model: str = MODEL) -> dict:
    cfg = get_config()
    body = {
        "model": resolve_model(model),
        "max_tokens": 2500,
        "temperature": 0.3,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }
    r = litellm_request("POST", "/v1/messages", headers=get_headers(), json=body, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"LLM {r.status_code}: {r.text[:300]}")
    text = "".join(b.get("text", "") for b in r.json().get("content", []))
    return parse_sections(text), cfg


def parse_sections(text: str) -> dict:
    """Parse the ``## Heading`` marker format into sections + argument summary."""
    text = re.sub(r"^```\w*$", "", text.strip(), flags=re.M).strip()
    chunks = re.split(r"^\s*#{1,3}\s*(.+?)\s*$", text, flags=re.M)[1:]
    if not chunks:
        raise ValueError(f"no sections in model output: {text[:200]}")
    sections, summary = [], ""
    for heading, body in zip(chunks[::2], chunks[1::2]):
        body = "\n".join(l.strip() for l in body.strip().splitlines() if l.strip())
        if not body:
            continue
        if heading.strip().lower().startswith("argument summary"):
            summary = body.replace("\n", " ")
        else:
            sections.append({"heading": heading.strip(), "text": body})
    if not sections:
        raise ValueError("model returned no body sections")
    return {"sections": sections, "argument_summary": summary}


# --------------------------------------------------------------------------- render


def build_letter(rec: dict, drafted: dict) -> dict:
    d, c, p, pay = rec["denial"], rec["claim"], rec["patient"], rec["payer"]
    cov = rec["coverage"]
    sections = [(s.get("heading", ""), s.get("text", "")) for s in drafted.get("sections", [])]
    return {
        "payer_name": pay["name"],
        "payer_address": PAYER_ADDRESSES.get(pay["payer_id"], ["Provider Appeals Department"]),
        "letter_date": date.today().strftime("%B %d, %Y"),
        "subject": f"Formal Appeal of Claim Denial — {p['full_name']} (MRN {p['mrn']}), Claim {c['claim_id']}",
        "meta_rows": [
            ("Patient", f"{p['full_name']}  (DOB {p['birth_date']})"),
            ("MRN", p["mrn"]),
            ("Member ID / Group", f"{cov.get('member_id', 'n/a')} / {cov.get('group_number', 'n/a')}"),
            ("Claim number", c["claim_id"]),
            ("Date of service", c["service_date"]),
            ("Procedure billed", f"CPT {c['cpt_code']} — {c['cpt_description']}"),
            ("Primary diagnosis", f"{c['icd10_primary']} — {c['icd10_primary_desc']}"),
            ("Denied amount", _fmt_money(d["denied_amount"])),
            ("Denial code", f"CARC {d['carc_code']} / RARC {d['rarc_code']} — {d['carc_description']}"),
            ("Appeal deadline", d["appeal_deadline"]),
        ],
        "salutation": "Dear Appeals Review Committee:",
        "sections": sections,
        "closing_name": "Angela R. Whitfield, RN, BSN, CPC",
        "closing_title": "Senior Appeals Specialist, Revenue Cycle Management",
        "enclosures": [
            "Remittance advice / explanation of payment",
            "Itemized claim detail (UB-04 / CMS-1500)",
            "Relevant clinical documentation from the medical record",
        ],
    }


def letter_text(letter: dict) -> str:
    parts = [f"RE: {letter['subject']}", ""]
    parts += [f"{k}: {v}" for k, v in letter["meta_rows"]]
    parts.append("")
    parts.append(letter["salutation"])
    for h, t in letter["sections"]:
        parts += ["", h.upper(), t]
    return "\n".join(parts)


def generate_one(denial_id: str, *, force: bool = False, model: str = MODEL,
                 rerender_only: bool = False) -> dict:
    """Draft + render one appeal letter.

    ``rerender_only`` rebuilds the PDF from the stored draft (no LLM call), which is
    what a redeploy or a letterhead tweak needs.
    """
    con = connect()
    try:
        existing = con.execute("SELECT * FROM appeals WHERE denial_id=?", (denial_id,)).fetchone()
        if existing and not force and not rerender_only and (LETTERS / Path(existing["letter_path"]).name).exists():
            return {"denial_id": denial_id, "status": "cached"}

        rec = case_record(con, denial_id)
        if rerender_only:
            if not existing or not existing["sections_json"]:
                return {"denial_id": denial_id, "status": "skipped (no stored draft)"}
            drafted = json.loads(existing["sections_json"])
            model_used = existing["model"]
        else:
            drafted, _ = call_model(prompt_for(rec), model)
            model_used = resolve_model(model)
        letter = build_letter(rec, drafted)
        pdf = LETTERS / f"{denial_id}.pdf"
        render_letter(pdf, letter)
        if pdf.stat().st_size < 4000:
            raise RuntimeError(f"suspiciously small PDF for {denial_id}")

        con.execute("DELETE FROM appeals WHERE denial_id=?", (denial_id,))
        con.execute(
            "INSERT INTO appeals (appeal_id, denial_id, patient_id, payer_id, status, letter_path,"
            " letter_text, model, generated_at, argument_summary, sections_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"AP-{denial_id}", denial_id, rec["denial"]["patient_id"], rec["denial"]["payer_id"],
             "READY", f"letters/{pdf.name}", letter_text(letter), model_used,
             datetime.utcnow().isoformat(timespec="seconds") + "Z",
             (drafted.get("argument_summary") or "")[:300], json.dumps(drafted)),
        )
        con.commit()
        return {"denial_id": denial_id, "status": "generated", "bytes": pdf.stat().st_size}
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--rerender", action="store_true",
                    help="rebuild PDFs from the stored drafts (no LLM calls)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--denial")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()

    con = connect()
    if args.denial:
        ids = [args.denial]
    else:
        ids = [r[0] for r in con.execute("SELECT denial_id FROM denials ORDER BY denial_id")]
    con.close()
    if args.limit:
        ids = ids[: args.limit]

    ok = fail = 0
    errors = []

    def run(did):
        try:
            return generate_one(did, force=args.force, model=args.model, rerender_only=args.rerender)
        except Exception as e:  # noqa: BLE001
            return {"denial_id": did, "status": "error", "error": str(e)[:200]}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for res in pool.map(run, ids):
            if res["status"] == "error":
                fail += 1
                errors.append(res)
            else:
                ok += 1
            print(f"{res['status']:>9}  {res['denial_id']}", flush=True)

    print(f"\ndone: {ok} ok, {fail} failed")
    for e in errors:
        print("  !", e["denial_id"], e["error"])
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
