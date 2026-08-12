# MUSC Health — AI Denial Appeal Automation

Turns payer claim denials into ready-to-submit, MUSC-letterhead appeal letters, and puts
every case, dollar figure and payer appeal portal behind one dashboard.

> **All patient data here is synthetic** (Synthea via the SMART Health IT FHIR R4 sandbox,
> plus a synthetic revenue-cycle overlay). There is no PHI in this repository.
> Letters are AI-drafted and require revenue-cycle / clinician review before real submission.

## What it does

| Stage | Detail |
|---|---|
| **Ingest** | 50 synthetic patients pulled as full FHIR R4 bundles → `data/raw/*.json` |
| **Model** | Normalized SQLite: patients, conditions, encounters, procedures, observations, medications, coverage, payers, claims, denials, appeals |
| **Denials** | 90 denials across 12 payers with real X12 CARC/RARC code sets, payer remarks, appeal deadlines and denied amounts |
| **Draft** | One LLM call per case (Claude Sonnet via LiteLLM) grounded *only* in that patient's record — Summary / Clinical Background / Basis for Appeal / Requested Action |
| **Render** | ReportLab MUSC letterhead PDF: official logo, Charleston address block, claim metadata table, signature block, enclosures |
| **Serve** | FastAPI dashboard: KPIs, denied-$ per payer, denial reasons, per-case detail, PDF preview + download, bulk ZIP, payer appeal-portal links |

## Layout

```
ingest.py            pull FHIR bundles into data/raw/
build_db.py          raw JSON  →  data/musc_appeals.db   (deterministic, re-runnable)
generate_letters.py  DB → LLM draft → MUSC PDF → appeals table
letterhead.py        MUSC letterhead renderer (ReportLab)
app.py               FastAPI API + dashboard
static/index.html    dashboard UI (no build step, no CDN)
letters/             one pre-generated PDF per denial
docs/DATA_TAXONOMY.md  FHIR resources, code systems, denial→argument mapping
tests/test_system.py   end-to-end checks (DB, every PDF, API, UI)
```

## Run locally

```bash
pip install -r requirements.txt
python app.py                 # http://localhost:8080
```

Rebuild from scratch:

```bash
python ingest.py              # re-pull FHIR bundles (network)
python build_db.py            # rebuild SQLite from data/raw/
python generate_letters.py    # draft + render any missing letters
python generate_letters.py --rerender   # rebuild PDFs from stored drafts, no LLM calls
python -m pytest tests -q
```

`generate_letters.py` caches: it skips cases whose PDF already exists, and every draft is
persisted to `appeals.sections_json` so a redeploy or a letterhead tweak never re-bills the model.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | counts + letters on disk |
| `GET /api/stats` | KPIs, per-payer, per-reason, per-CARC, per-month aggregates |
| `GET /api/cases?payer=&category=&search=` | filtered case list |
| `GET /api/cases/{denial_id}` | full case: denial, claim, coverage, clinical context, letter text |
| `GET /api/payers` | payers with denial counts, $ and appeal-portal URLs |
| `GET /letters/{denial_id}.pdf?download=1` | one appeal letter |
| `GET /letters.zip?payer=` | bulk download |
| `POST /api/cases/{denial_id}/regenerate` | re-draft a single letter |

## Deploy

Railway, from this repo's subtree — `Procfile` runs uvicorn on `$PORT`, binding `0.0.0.0`.
The SQLite DB and all pre-generated PDFs ship in the repo, so a fresh deploy is fully
populated without any LLM calls at boot.

## Guardrails

- The prompt forbids invented diagnoses, dates, dollar amounts, policy numbers and guideline
  citations; the model only sees facts pulled from the DB for that one patient.
- Identifiers and money in the letter's metadata block are rendered from SQLite, not from
  model output, so they can't drift.
- Every PDF is verified by the test suite to contain the correct MRN, claim number, CARC code,
  patient surname, the MUSC letterhead and the synthetic-data disclosure.
