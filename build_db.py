#!/usr/bin/env python3
"""Build the local SQLite database from the raw FHIR bundles in data/raw/.

Two layers:

1. **Real FHIR clinical data** (synthetic patients from the SMART Health IT R4
   sandbox, Synthea-generated): patients, conditions, encounters, procedures,
   observations, medications.
2. **Synthetic revenue-cycle overlay** — coverage, claims and denials. The
   sandbox exposes no Claim/ExplanationOfBenefit adjudication data, so claims and
   denial reasons (CARC/RARC) are generated *deterministically* (seeded by the
   patient id) and are always grounded in that patient's real conditions,
   procedures and encounter dates. Clearly labelled SYNTHETIC everywhere.

Rebuild at any time:  python build_db.py
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
RAW = HERE / "data" / "raw"
DB_PATH = HERE / "data" / "musc_appeals.db"
PAYERS = json.loads((HERE / "data" / "payers.json").read_text())

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE patients (
  patient_id TEXT PRIMARY KEY,
  mrn TEXT, first_name TEXT, last_name TEXT, full_name TEXT,
  gender TEXT, birth_date TEXT, age INTEGER,
  address TEXT, city TEXT, state TEXT, postal_code TEXT, phone TEXT,
  marital_status TEXT, language TEXT, race TEXT, ethnicity TEXT
);
CREATE TABLE conditions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT, code TEXT, system TEXT,
  display TEXT, icd10 TEXT, clinical_status TEXT, onset_date TEXT,
  FOREIGN KEY(patient_id) REFERENCES patients(patient_id)
);
CREATE TABLE encounters (
  encounter_id TEXT PRIMARY KEY, patient_id TEXT, class TEXT, type_display TEXT,
  start_date TEXT, end_date TEXT, reason TEXT, service_provider TEXT
);
CREATE TABLE procedures (
  id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT, code TEXT, display TEXT,
  performed_date TEXT, encounter_id TEXT
);
CREATE TABLE observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT, code TEXT, display TEXT,
  value TEXT, unit TEXT, effective_date TEXT
);
CREATE TABLE medications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT, display TEXT,
  status TEXT, authored_on TEXT
);
CREATE TABLE payers (
  payer_id TEXT PRIMARY KEY, name TEXT, type TEXT, portal_name TEXT,
  portal_url TEXT, appeal_url TEXT, appeal_window_days INTEGER, appeal_notes TEXT
);
CREATE TABLE coverage (
  coverage_id TEXT PRIMARY KEY, patient_id TEXT, payer_id TEXT,
  member_id TEXT, group_number TEXT, plan_name TEXT, relationship TEXT,
  period_start TEXT, period_end TEXT
);
CREATE TABLE claims (
  claim_id TEXT PRIMARY KEY, patient_id TEXT, payer_id TEXT, encounter_id TEXT,
  service_date TEXT, source_encounter_date TEXT, submitted_date TEXT, place_of_service TEXT,
  cpt_code TEXT, cpt_description TEXT, revenue_code TEXT, units INTEGER,
  icd10_primary TEXT, icd10_primary_desc TEXT, icd10_secondary TEXT,
  billed_amount REAL, allowed_amount REAL, paid_amount REAL, denied_amount REAL,
  rendering_provider TEXT, npi TEXT, facility TEXT, status TEXT
);
CREATE TABLE denials (
  denial_id TEXT PRIMARY KEY, claim_id TEXT, patient_id TEXT, payer_id TEXT,
  denial_date TEXT, carc_code TEXT, carc_description TEXT,
  rarc_code TEXT, rarc_description TEXT, category TEXT,
  payer_remark TEXT, appeal_deadline TEXT, denied_amount REAL,
  appealable INTEGER, data_source TEXT
);
CREATE TABLE appeals (
  appeal_id TEXT PRIMARY KEY, denial_id TEXT, patient_id TEXT, payer_id TEXT,
  status TEXT, letter_path TEXT, letter_text TEXT, model TEXT,
  generated_at TEXT, argument_summary TEXT, submitted_at TEXT, outcome TEXT,
  sections_json TEXT
);

CREATE INDEX idx_cond_patient ON conditions(patient_id);
CREATE INDEX idx_enc_patient ON encounters(patient_id);
CREATE INDEX idx_proc_patient ON procedures(patient_id);
CREATE INDEX idx_obs_patient ON observations(patient_id);
CREATE INDEX idx_med_patient ON medications(patient_id);
CREATE INDEX idx_claims_patient ON claims(patient_id);
CREATE INDEX idx_claims_payer ON claims(payer_id);
CREATE INDEX idx_denials_claim ON denials(claim_id);
CREATE INDEX idx_denials_payer ON denials(payer_id);
CREATE INDEX idx_denials_category ON denials(category);
CREATE INDEX idx_appeals_denial ON appeals(denial_id);
"""

# --------------------------------------------------------------------------
# Reference data for the synthetic revenue-cycle overlay
# --------------------------------------------------------------------------

# CARC (Claim Adjustment Reason Code) + typical paired RARC, X12 835 standard.
DENIAL_REASONS = [
    {
        "carc_code": "50",
        "carc_description": "These are non-covered services because this is not deemed a 'medical necessity' by the payer.",
        "rarc_code": "N115",
        "rarc_description": "This decision was based on a Local Coverage Determination (LCD).",
        "category": "Medical necessity",
        "weight": 22,
    },
    {
        "carc_code": "197",
        "carc_description": "Precertification/authorization/notification/pre-treatment absent.",
        "rarc_code": "N54",
        "rarc_description": "Claim information is inconsistent with pre-certified/authorized services.",
        "category": "Prior authorization",
        "weight": 18,
    },
    {
        "carc_code": "16",
        "carc_description": "Claim/service lacks information or has submission/billing error(s).",
        "rarc_code": "N290",
        "rarc_description": "Missing/incomplete/invalid rendering provider primary identifier.",
        "category": "Missing information",
        "weight": 12,
    },
    {
        "carc_code": "11",
        "carc_description": "The diagnosis is inconsistent with the procedure.",
        "rarc_code": "M64",
        "rarc_description": "Missing/incomplete/invalid other diagnosis.",
        "category": "Coding / clinical validation",
        "weight": 10,
    },
    {
        "carc_code": "96",
        "carc_description": "Non-covered charge(s).",
        "rarc_code": "N130",
        "rarc_description": "Consult plan benefit documents/guidelines for information about restrictions for this service.",
        "category": "Benefit / non-covered",
        "weight": 9,
    },
    {
        "carc_code": "151",
        "carc_description": "Payer deems the information submitted does not support this many/frequency of services.",
        "rarc_code": "N362",
        "rarc_description": "The number of days or units of service exceeds our acceptable maximum.",
        "category": "Frequency / utilization",
        "weight": 8,
    },
    {
        "carc_code": "204",
        "carc_description": "This service/equipment/drug is not covered under the patient's current benefit plan.",
        "rarc_code": "N130",
        "rarc_description": "Consult plan benefit documents/guidelines for information about restrictions for this service.",
        "category": "Benefit / non-covered",
        "weight": 7,
    },
    {
        "carc_code": "29",
        "carc_description": "The time limit for filing has expired.",
        "rarc_code": "N211",
        "rarc_description": "You may not appeal this decision.",
        "category": "Timely filing",
        "weight": 5,
    },
    {
        "carc_code": "18",
        "carc_description": "Exact duplicate claim/service.",
        "rarc_code": "N522",
        "rarc_description": "Duplicate of a claim processed or in process as a crossover/coordination of benefits claim.",
        "category": "Duplicate",
        "weight": 5,
    },
    {
        "carc_code": "27",
        "carc_description": "Expenses incurred after coverage terminated.",
        "rarc_code": "N30",
        "rarc_description": "Patient ineligible for this service.",
        "category": "Eligibility",
        "weight": 4,
    },
]

# keyword in procedure/encounter text -> (CPT, description, typical billed charge)
CPT_MAP = [
    (("colonoscopy",), ("45378", "Colonoscopy, flexible; diagnostic", 2450)),
    (("appendectomy",), ("44970", "Laparoscopy, surgical, appendectomy", 12800)),
    (("cholecystectomy", "gallbladder"), ("47562", "Laparoscopic cholecystectomy", 15400)),
    (("mri", "magnetic resonance"), ("70553", "MRI brain without and with contrast", 3200)),
    (("ct ", "computed tomography", "tomography"), ("74177", "CT abdomen and pelvis with contrast", 2100)),
    (("echocardiog",), ("93306", "Transthoracic echocardiography, complete", 1450)),
    (("catheter", "angio", "coronary"), ("93458", "Left heart catheterization with coronary angiography", 9800)),
    (("physical therapy", "rehab"), ("97110", "Therapeutic exercise, each 15 minutes", 260)),
    (("dialysis", "renal replacement"), ("90935", "Hemodialysis procedure with single evaluation", 1750)),
    (("chemotherap", "infusion"), ("96413", "Chemotherapy administration, IV infusion, up to 1 hour", 3400)),
    (("biopsy",), ("11106", "Incisional biopsy of skin, single lesion", 720)),
    (("x-ray", "radiograph", "chest xray"), ("71046", "Radiologic examination, chest; 2 views", 320)),
    (("ultrasound", "sonograph", "echography"), ("76700", "Ultrasound, abdominal, complete", 640)),
    (("sleep", "polysomn"), ("95810", "Polysomnography; sleep staging, attended", 3100)),
    (("psychiatr", "mental health", "counsel", "depression"), ("90837", "Psychotherapy, 60 minutes with patient", 285)),
    (("substance", "alcohol", "opioid"), ("H0015", "Intensive outpatient substance use treatment, per diem", 890)),
    (("delivery", "cesarean", "obstetric", "prenatal"), ("59400", "Routine obstetric care including antepartum, vaginal delivery", 8600)),
    (("immuniz", "vaccin"), ("90471", "Immunization administration", 145)),
    (("spirometr", "pulmonary", "asthma", "copd"), ("94060", "Bronchodilator responsiveness, spirometry", 410)),
    (("wound", "suture", "laceration"), ("12002", "Simple repair of superficial wounds, 2.6-7.5 cm", 560)),
    (("fracture", "cast", "orthop", "knee", "hip"), ("27447", "Total knee arthroplasty", 34500)),
    (("cataract", "ophthalm", "eye"), ("66984", "Extracapsular cataract removal with IOL insertion", 4200)),
    (("stress test", "treadmill"), ("93015", "Cardiovascular stress test with supervision", 980)),
    (("emergency", "urgent"), ("99284", "Emergency department visit, moderate-to-high severity", 2650)),
    (("inpatient", "hospital admission", "admission"), ("99223", "Initial hospital care, high complexity", 1850)),
]
DEFAULT_CPT = ("99214", "Office/outpatient visit, established patient, moderate complexity", 385)

# SNOMED condition text -> plausible ICD-10 code
ICD10_MAP = [
    (("diabetes",), ("E11.9", "Type 2 diabetes mellitus without complications")),
    (("hypertension", "blood pressure"), ("I10", "Essential (primary) hypertension")),
    (("asthma",), ("J45.909", "Unspecified asthma, uncomplicated")),
    (("copd", "pulmonary disease"), ("J44.9", "Chronic obstructive pulmonary disease, unspecified")),
    (("coronary", "myocardial", "heart"), ("I25.10", "Atherosclerotic heart disease of native coronary artery")),
    (("stroke", "cerebrovascular"), ("I63.9", "Cerebral infarction, unspecified")),
    (("chronic kidney", "renal"), ("N18.9", "Chronic kidney disease, unspecified")),
    (("obesity", "body mass"), ("E66.9", "Obesity, unspecified")),
    (("depress",), ("F32.9", "Major depressive disorder, single episode, unspecified")),
    (("anxiety",), ("F41.9", "Anxiety disorder, unspecified")),
    (("cancer", "carcinoma", "neoplasm", "malignant"), ("C80.1", "Malignant (primary) neoplasm, unspecified")),
    (("pregnan",), ("Z34.90", "Encounter for supervision of normal pregnancy, unspecified")),
    (("fracture",), ("S72.001A", "Fracture of unspecified part of neck of femur, initial encounter")),
    (("arthritis", "osteoarthritis"), ("M17.9", "Osteoarthritis of knee, unspecified")),
    (("sinusitis", "bronchitis", "infection", "pharyngitis"), ("J06.9", "Acute upper respiratory infection, unspecified")),
    (("anemia",), ("D64.9", "Anemia, unspecified")),
    (("hyperlipid", "cholesterol"), ("E78.5", "Hyperlipidemia, unspecified")),
    (("covid",), ("U07.1", "COVID-19")),
    (("alcohol", "opioid", "substance", "drug"), ("F19.20", "Other psychoactive substance dependence, uncomplicated")),
    (("seizure", "epilep"), ("G40.909", "Epilepsy, unspecified, not intractable")),
]
DEFAULT_ICD = ("R69", "Illness, unspecified")

PROVIDERS = [
    ("Sarah E. Whitmore, MD", "1447382910", "Internal Medicine"),
    ("Daniel R. Okafor, MD", "1558493021", "Cardiology"),
    ("Priya N. Raghavan, MD", "1669504132", "Hematology/Oncology"),
    ("Marcus T. Bledsoe, DO", "1770615243", "Emergency Medicine"),
    ("Elena V. Castellanos, MD", "1881726354", "General Surgery"),
    ("James H. Ferrell, MD", "1992837465", "Orthopaedic Surgery"),
    ("Amara J. Boone, NP", "1203948576", "Primary Care"),
    ("Robert K. Lindqvist, MD", "1314059687", "Pulmonology"),
]
FACILITIES = [
    "MUSC Health University Medical Center, Charleston, SC",
    "MUSC Health Ashley River Tower, Charleston, SC",
    "MUSC Health East Cooper Medical Center, Mount Pleasant, SC",
    "MUSC Health Florence Medical Center, Florence, SC",
    "MUSC Health Rutledge Tower Ambulatory Care, Charleston, SC",
]
PLACE_OF_SERVICE = {
    "IMP": "21 - Inpatient Hospital",
    "EMER": "23 - Emergency Room Hospital",
    "AMB": "22 - On Campus Outpatient Hospital",
    "OBSENC": "22 - On Campus Outpatient Hospital",
    "HH": "12 - Home",
    "VR": "02 - Telehealth",
}


def _seeded(patient_id: str) -> random.Random:
    return random.Random(int(hashlib.sha256(patient_id.encode()).hexdigest()[:12], 16))


def _lookup(text: str, table, default):
    t = (text or "").lower()
    for keys, val in table:
        if any(k in t for k in keys):
            return val
    return default


def _best_condition(conditions: list[dict], service_text: str) -> dict | None:
    """Pick the condition most textually related to the billed service."""
    if not conditions:
        return None
    words = {w for w in (service_text or "").lower().split() if len(w) > 4}
    best, best_score = None, 0
    for c in conditions:
        cw = {w for w in (c["display"] or "").lower().split() if len(w) > 4}
        score = len(words & cw)
        if score > best_score:
            best, best_score = c, score
    if best:
        return best
    active = [c for c in conditions if "resolved" not in (c.get("display") or "").lower()]
    return sorted(active or conditions, key=lambda c: c.get("onset") or "", reverse=True)[0]


def _text(cc: dict | None) -> str:
    if not cc:
        return ""
    if cc.get("text"):
        return cc["text"]
    for c in cc.get("coding", []) or []:
        if c.get("display"):
            return c["display"]
    return ""


def _code(cc: dict | None) -> tuple[str, str]:
    for c in (cc or {}).get("coding", []) or []:
        return c.get("code", ""), c.get("system", "")
    return "", ""


def _dt(val: str | None) -> str:
    return (val or "")[:10]


def _age(birth: str) -> int | None:
    try:
        b = datetime.strptime(birth[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    t = date.today()
    return t.year - b.year - ((t.month, t.day) < (b.month, b.day))


def _ext(patient: dict, url_frag: str) -> str:
    for e in patient.get("extension", []) or []:
        if url_frag in e.get("url", ""):
            for sub in e.get("extension", []) or []:
                if sub.get("url") == "text":
                    return sub.get("valueString", "")
            if e.get("valueString"):
                return e["valueString"]
    return ""


def load_bundles() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(RAW.glob("*.json"))]


def build(db_path: Path = DB_PATH) -> dict:
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix)
        if p.exists():
            p.unlink()
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)

    for p in PAYERS:
        con.execute(
            "INSERT INTO payers VALUES (?,?,?,?,?,?,?,?)",
            (
                p["payer_id"], p["name"], p["type"], p["portal_name"],
                p["portal_url"], p["appeal_url"], p["appeal_window_days"],
                p["appeal_notes"],
            ),
        )

    stats = {"patients": 0, "claims": 0, "denials": 0}
    weights = [d["weight"] for d in DENIAL_REASONS]

    for bundle in load_bundles():
        pat = bundle["Patient"]
        pid = pat["id"]
        rnd = _seeded(pid)
        name = (pat.get("name") or [{}])[0]
        first = " ".join(name.get("given", []) or [])
        last = name.get("family", "")
        addr = (pat.get("address") or [{}])[0]
        phone = next(
            (t.get("value") for t in pat.get("telecom", []) if t.get("system") == "phone"),
            "",
        )
        mrn = "MUSC" + str(abs(hash(pid)) % 10_000_000).zfill(7)
        con.execute(
            "INSERT INTO patients VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pid, mrn, first, last, f"{first} {last}".strip(),
                pat.get("gender", ""), _dt(pat.get("birthDate")), _age(pat.get("birthDate") or ""),
                " ".join(addr.get("line", []) or []), addr.get("city", ""),
                addr.get("state", ""), addr.get("postalCode", ""), phone,
                _text(pat.get("maritalStatus")),
                ((pat.get("communication") or [{}])[0].get("language", {}) or {}).get("text", ""),
                _ext(pat, "us-core-race"), _ext(pat, "us-core-ethnicity"),
            ),
        )
        stats["patients"] += 1

        conditions = []
        for c in bundle.get("Condition", []):
            disp = _text(c.get("code"))
            code, system = _code(c.get("code"))
            icd = _lookup(disp, ICD10_MAP, DEFAULT_ICD)
            onset = _dt(c.get("onsetDateTime"))
            status = _text(c.get("clinicalStatus")) or (
                (c.get("clinicalStatus", {}).get("coding") or [{}])[0].get("code", "")
            )
            con.execute(
                "INSERT INTO conditions (patient_id, code, system, display, icd10, clinical_status, onset_date)"
                " VALUES (?,?,?,?,?,?,?)",
                (pid, code, system, disp, icd[0], status, onset),
            )
            conditions.append({"display": disp, "icd10": icd, "onset": onset})

        encounters = []
        for e in bundle.get("Encounter", []):
            cls = (e.get("class") or {}).get("code", "AMB")
            period = e.get("period") or {}
            enc = {
                "id": e["id"],
                "class": cls,
                "type": _text((e.get("type") or [{}])[0]),
                "start": _dt(period.get("start")),
                "end": _dt(period.get("end")),
                "reason": _text((e.get("reasonCode") or [{}])[0]),
                "provider": (e.get("serviceProvider") or {}).get("display", ""),
            }
            con.execute(
                "INSERT OR REPLACE INTO encounters VALUES (?,?,?,?,?,?,?,?)",
                (enc["id"], pid, cls, enc["type"], enc["start"], enc["end"],
                 enc["reason"], enc["provider"]),
            )
            encounters.append(enc)

        procedures = []
        for pr in bundle.get("Procedure", [])[:40]:
            disp = _text(pr.get("code"))
            code, _ = _code(pr.get("code"))
            performed = _dt(
                pr.get("performedDateTime") or (pr.get("performedPeriod") or {}).get("start")
            )
            con.execute(
                "INSERT INTO procedures (patient_id, code, display, performed_date, encounter_id)"
                " VALUES (?,?,?,?,?)",
                (pid, code, disp, performed, (pr.get("encounter") or {}).get("reference", "").split("/")[-1]),
            )
            procedures.append({"display": disp, "date": performed})

        for o in bundle.get("Observation", [])[:25]:
            vq = o.get("valueQuantity") or {}
            val = vq.get("value")
            if val is None:
                val = _text(o.get("valueCodeableConcept")) or o.get("valueString")
            con.execute(
                "INSERT INTO observations (patient_id, code, display, value, unit, effective_date)"
                " VALUES (?,?,?,?,?,?)",
                (pid, _code(o.get("code"))[0], _text(o.get("code")), str(val) if val is not None else "",
                 vq.get("unit", ""), _dt(o.get("effectiveDateTime"))),
            )

        for m in bundle.get("MedicationRequest", [])[:15]:
            con.execute(
                "INSERT INTO medications (patient_id, display, status, authored_on) VALUES (?,?,?,?)",
                (pid, _text(m.get("medicationCodeableConcept")), m.get("status", ""),
                 _dt(m.get("authoredOn"))),
            )

        # ---- synthetic revenue-cycle overlay -------------------------------
        payer = PAYERS[rnd.randrange(len(PAYERS))]
        cov_id = f"COV-{mrn[-6:]}"
        member_id = f"{payer['payer_id'].upper()[:3]}{rnd.randrange(10**8, 10**9)}"
        con.execute(
            "INSERT INTO coverage VALUES (?,?,?,?,?,?,?,?,?)",
            (cov_id, pid, payer["payer_id"], member_id,
             f"GRP-{rnd.randrange(10000, 99999)}",
             f"{payer['name']} {rnd.choice(['PPO', 'HMO', 'Choice Plus', 'Open Access', 'Advantage'])}",
             "self", "2024-01-01", "2026-12-31"),
        )

        # pick 1-3 recent, substantive encounters to bill + deny
        billable = [e for e in encounters if e["start"]]
        billable.sort(key=lambda e: e["start"], reverse=True)
        billable = billable[:6]
        n_claims = min(len(billable), rnd.choice([1, 1, 2, 2, 3]))
        for i, enc in enumerate(billable[:n_claims]):
            proc_near = next(
                (p for p in procedures if p["date"] and p["date"][:7] == enc["start"][:7]), None
            )
            src_text = " ".join(
                filter(None, [proc_near["display"] if proc_near else "", enc["type"], enc["reason"], enc["class"]])
            )
            cpt, cpt_desc, base = _lookup(src_text, CPT_MAP, DEFAULT_CPT)
            if enc["class"] == "IMP" and base < 5000:
                cpt, cpt_desc, base = ("99223", "Initial hospital care, high complexity", 1850)
            units = rnd.choice([1, 1, 1, 2])
            billed = round(base * units * rnd.uniform(0.85, 1.6), 2)
            # primary dx: the recorded condition whose text best overlaps the
            # billed service, else the most recent active condition
            primary_cond = _best_condition(conditions, src_text)
            primary = primary_cond["icd10"] if primary_cond else DEFAULT_ICD
            others = [c for c in conditions if c is not primary_cond]
            secondary = others[0]["icd10"][0] if others else ""
            provider, npi, _spec = PROVIDERS[rnd.randrange(len(PROVIDERS))]
            # Synthea encounters span decades; the billing overlay is shifted into
            # the current revenue-cycle window so appeal deadlines are meaningful.
            service_date = (date.today() - timedelta(days=rnd.randrange(45, 420))).isoformat()
            claim_id = f"MUSC-{service_date.replace('-', '')}-{pid[:4].upper()}-{i + 1}"
            submitted = (
                datetime.strptime(service_date, "%Y-%m-%d") + timedelta(days=rnd.randrange(2, 20))
            ).strftime("%Y-%m-%d")
            con.execute(
                "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (claim_id, pid, payer["payer_id"], enc["id"], service_date, enc["start"], submitted,
                 PLACE_OF_SERVICE.get(enc["class"], "11 - Office"), cpt, cpt_desc,
                 "0450" if enc["class"] == "EMER" else "0510", units,
                 primary[0], primary[1], secondary,
                 billed, round(billed * rnd.uniform(0.45, 0.75), 2), 0.0, billed,
                 provider, npi, rnd.choice(FACILITIES), "DENIED"),
            )
            stats["claims"] += 1

            reason = rnd.choices(DENIAL_REASONS, weights=weights, k=1)[0]
            denial_date = (
                datetime.strptime(submitted, "%Y-%m-%d") + timedelta(days=rnd.randrange(10, 45))
            ).strftime("%Y-%m-%d")
            deadline = (
                datetime.strptime(denial_date, "%Y-%m-%d")
                + timedelta(days=payer["appeal_window_days"])
            ).strftime("%Y-%m-%d")
            remark = {
                "Medical necessity": "Documentation submitted does not establish that the service was reasonable and necessary for the diagnosis reported.",
                "Prior authorization": "No prior authorization on file for the date of service billed.",
                "Missing information": "Claim cannot be adjudicated; required data element(s) are missing or invalid.",
                "Coding / clinical validation": "The diagnosis code reported does not support the procedure billed.",
                "Benefit / non-covered": "Service is excluded under the member's plan benefit document.",
                "Frequency / utilization": "Units billed exceed the plan's allowable frequency for this service.",
                "Timely filing": "Claim was received after the contractual filing deadline.",
                "Duplicate": "Claim appears to duplicate a previously adjudicated claim.",
                "Eligibility": "Member was not eligible for benefits on the date of service billed.",
            }[reason["category"]]
            con.execute(
                "INSERT INTO denials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"DEN-{claim_id}", claim_id, pid, payer["payer_id"], denial_date,
                 reason["carc_code"], reason["carc_description"], reason["rarc_code"],
                 reason["rarc_description"], reason["category"], remark, deadline, billed,
                 0 if reason["carc_code"] == "29" else 1, "SYNTHETIC"),
            )
            stats["denials"] += 1

    con.commit()
    con.close()
    return stats


if __name__ == "__main__":
    s = build()
    print(f"built {DB_PATH}: {s}")
