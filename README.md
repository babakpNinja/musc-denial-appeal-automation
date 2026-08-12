# MUSC Health — AI Denial Appeal Automation

**Live:** https://musc-appeals-production.up.railway.app  ·  deploy repo: `babakpNinja/musc-denial-appeal-automation`

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
| **Track** | Appeal lifecycle `ready → submitted → overturned / upheld` with notes, an event timeline, status chips + filter and a submitted/outstanding KPI |

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
tests/test_status.py   appeal lifecycle: transitions, persistence, filters, KPIs
tests/shots.py         responsive screenshots at phone / tablet / desktop widths
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
| `GET /api/workflow` | statuses, legal transitions, where status is stored and whether it is durable |
| `GET /api/cases/{denial_id}/status` | current status + event history |
| `POST /api/cases/{denial_id}/status` | `{"status": "submitted", "note": "faxed, ref 8821"}` — 409 on an illegal transition |

## Appeal lifecycle

```
ready ──▶ submitted ──▶ overturned   (terminal)
  ▲           │
  └───────────┴──▶ upheld ──▶ submitted   (second-level appeal)
```

`ready → submitted` stamps `submitted_at`; withdrawing back to `ready` clears it; the outcome
states set `outcome`. Every change is appended to an event log with an optional note and shown
as a timeline in the case modal.

**Where it is stored.** `musc_appeals.db` is a build artifact (rebuilt from FHIR and committed),
so lifecycle writes go to a separate `appeal_status.db`:

| Env | Store | Durable? |
|---|---|---|
| `RAILWAY_VOLUME_MOUNT_PATH` set | `$RAILWAY_VOLUME_MOUNT_PATH/appeal_status.db` | yes — survives redeploys |
| otherwise | `data/appeal_status.db` (container disk) | no — resets on redeploy |
| `APPEAL_STATUS_DB` | explicit path (used by tests) | depends |

Railway's filesystem is ephemeral, so without a mounted volume the dashboard shows a **Demo mode**
banner and `/api/workflow` reports `durable: false` — the workflow is deliberately explicit about
this rather than silently losing status. The production service has a volume mounted at `/data`
(`volumeCreate` via `tools/railway.py`'s GraphQL helper), so live status **is** durable:
`GET /api/workflow` returns `durable: true`.

## Responsive UI

One stylesheet, three breakpoints: ≤900px collapses both tables into stacked cards (labels come
from `data-label`) and shrinks the header, ≤760px puts KPIs 2-up, makes filters full-width and the
case modal full-screen. Verified at 390×844 (iPhone) and 820×1180 (iPad):

```bash
python tests/shots.py http://localhost:8123 _local    # phone / tablet / desktop screenshots
```

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
