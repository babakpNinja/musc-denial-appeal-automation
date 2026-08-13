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
| **Denials** | 67 denials across 12 payers with real X12 CARC/RARC code sets, payer remarks, appeal deadlines and denied amounts |
| **Draft** | One LLM call per case (Claude Sonnet via LiteLLM) grounded *only* in that patient's record — Summary / Clinical Background / Basis for Appeal / Requested Action |
| **Render** | ReportLab MUSC letterhead PDF: official logo, Charleston address block, claim metadata table, signature block, enclosures |
| **Serve** | FastAPI dashboard: KPIs, denied-$ per payer, denial reasons, per-case detail, PDF preview + download, bulk ZIP, payer appeal-portal links |
| **Track** | Appeal lifecycle `ready → submitted → overturned / upheld` with notes, an event timeline, status chips + filter and a submitted/outstanding KPI |
| **Work it** | Per-payer submission batches (soonest deadline first, portal link, payer ZIP), multi-select bulk status changes with a shared note, and a days-to-deadline column that flags anything due inside 14 days |

## Layout

```
ingest.py            pull FHIR bundles into data/raw/
build_db.py          raw JSON  →  data/musc_appeals.db   (deterministic, re-runnable)
generate_letters.py  DB → LLM draft → MUSC PDF → appeals table
retime_drafts.py     re-point cached drafts at the rebuilt timeline (no LLM calls)
letterhead.py        MUSC letterhead renderer (ReportLab)
app.py               FastAPI API + dashboard
demo_state.py        snapshot / restore / reset the live board's appeal status
refresh_demo.py      scheduled re-anchor of the timeline: rebuild → test → push → verify
static/index.html    dashboard UI (no build step, no CDN)
letters/             one pre-generated PDF per denial
docs/DATA_TAXONOMY.md  FHIR resources, code systems, denial→argument mapping
tests/test_system.py   end-to-end checks (DB, every PDF, API, UI)
tests/test_status.py   appeal lifecycle: transitions, persistence, filters, KPIs
tests/test_plausibility.py  ages, deceased patients, claim timeline, draft cache
tests/test_batch.py    bulk status changes (incl. partial failure) + payer batches
tests/test_retime.py   date re-pointing when a rebuild moves the timeline
tests/test_demo_state.py  the admin reset door and snapshot/restore round-trip
tests/test_refresh.py     when the scheduled refresh ships and when it stays quiet
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
`build_db.py` drops the database when it rebuilds, so it exports those drafts to
`data/letter_drafts.json` first and restores them afterwards — rebuilding is free.

### Why the timeline moves but the ids don't

A denial queue is only interesting while the deadlines are live, so `build_db.py` anchors each
case to **today**: it draws how far into the payer's contractual appeal window the denial sits
(`_window_fraction`, 3 %–112 %) and works backwards to the denial, submission and service dates.
That keeps ~80 % of the queue still appealable, a handful due inside 14 days and a few recently
lapsed, on whatever day the database was last built — rather than the whole board rotting into
red as the demo ages.

Claim ids therefore carry no date (`MUSC-<patient>-<n>` / `DEN-MUSC-<patient>-<n>`) so the letter
cache still matches after a rebuild, and `retime_drafts.py` rewrites the dates the cached prose
quotes — in every format the letters use — so a rebuilt demo never argues about a date the claim
no longer has. `build_db.py` runs it automatically; run it by hand with `--apply` after any
manual date surgery. It is pure text substitution: no model calls, no re-drafting.

Because the whole board hangs off build day, `build_db.py` records that day in a `meta` table and
`/api/health` serves it as `built_at` / `built_days_ago`. That is how `tools/demoready.py` can say
"data built 34d ago" and how `refresh_demo.py` decides whether a rebuild is worth running at all —
without rebuilding to find out. A DB built before this existed reports nulls; stamp it with
`python build_db.py --stamp YYYY-MM-DD`.

### Who gets billed

The 50 FHIR patients are all kept as clinical records, but only the 35 who could plausibly
receive a claim today get the revenue-cycle overlay: Synthea's cohort includes patients who
died decades ago and several over 100, and a 113-year-old with an elective outpatient
procedure discredits an otherwise credible demo. `build_db.is_billable()` requires the patient
to be alive and aged 0–95 **on the date of service**; for a deceased patient, `patients.age`
is age at death rather than age since birth. `tests/test_plausibility.py` enforces this along
with the claim timeline (service ≤ submitted ≤ denial < appeal deadline, inside coverage).

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | counts, letters on disk, and `built_at` / `built_days_ago` — how old the deployed board is |
| `GET /api/stats` | KPIs, per-payer, per-reason, per-CARC, per-month aggregates |
| `GET /api/cases?payer=&category=&search=` | filtered case list |
| `GET /api/cases/{denial_id}` | full case: denial, claim, coverage, clinical context, letter text |
| `GET /api/payers` | payers with denial counts, $ and appeal-portal URLs |
| `GET /api/batches` | outstanding appeals grouped per payer: counts, $, soonest deadline, due-soon/overdue, portal + ZIP links |
| `GET /letters/{denial_id}.pdf?download=1` | one appeal letter |
| `GET /letters.zip?payer=` | bulk download |
| `POST /api/cases/{denial_id}/regenerate` | re-draft a single letter |
| `GET /api/workflow` | statuses, legal transitions, where status is stored and whether it is durable |
| `GET /api/cases/{denial_id}/status` | current status + event history |
| `POST /api/cases/{denial_id}/status` | `{"status": "submitted", "note": "faxed, ref 8821"}` — 409 on an illegal transition |
| `POST /api/cases/status/bulk` | `{"denial_ids": [...], "status": "submitted", "note": "..."}` — per-case `ok`/`error`, max 500 |
| `POST /api/workflow/reset` | admin-only: wipe status and optionally replay a snapshot (404 unless `DEMO_ADMIN_TOKEN` is set) |

## Appeal lifecycle

```
ready ──▶ submitted ──▶ overturned   (terminal)
  ▲           │
  └───────────┴──▶ upheld ──▶ submitted   (second-level appeal)
```

`ready → submitted` stamps `submitted_at`; withdrawing back to `ready` clears it; the outcome
states set `outcome`. Every change is appended to an event log with an optional note and shown
as a timeline in the case modal.

**Working a batch.** Staff do not appeal one claim at a time — they open one payer portal, upload
that payer's letters and mark them all submitted. The dashboard mirrors that: *Submission batches
by payer* lists outstanding appeals grouped by payer, ordered by the soonest deadline in the batch,
each with the portal link, that payer's letter ZIP and a one-click **Mark N submitted**. In the case
table, tick any set of rows (or select all matching the current filter) and apply a status with one
shared note. Bulk moves are validated case by case: a case that cannot legally move is reported and
left ticked while the rest go through, so a batch never half-fails silently. Every case still gets
its own event row.

**Where it is stored.** `musc_appeals.db` is a build artifact (rebuilt from FHIR and committed),
so lifecycle writes go to a separate `appeal_status.db`:

| Env | Store | Durable? |
|---|---|---|
| `RAILWAY_VOLUME_MOUNT_PATH` set | `$RAILWAY_VOLUME_MOUNT_PATH/appeal_status.db` | yes — survives redeploys |
| otherwise | `data/appeal_status.db` (container disk) | no — resets on redeploy |
| `APPEAL_STATUS_DB` | explicit path (used by tests) | depends |

**Putting the board back.** Because live status is durable, smoke-testing a deploy moves *real*
cases, and the public API cannot undo it — `overturned` is terminal on purpose. `demo_state.py`
is the safe way in and out:

```bash
python demo_state.py show     --base https://…                      # what is non-ready right now
python demo_state.py snapshot --base https://… -o data/demo_state.json
python demo_state.py restore  --base https://… -i data/demo_state.json   # replay it exactly
python demo_state.py reset    --base https://…                      # everything back to ready
```

`snapshot`/`show` are read-only; `restore`/`reset` go through `POST /api/workflow/reset`, which
**does not exist** unless the deployment sets `DEMO_ADMIN_TOKEN` (an unset token means 404, a wrong
one means 403). Pass the same value with `--token` or in the environment. A snapshot only carries
the cases that moved — it is a diff against a fresh board — and restore replays `submitted_at`,
the note and the whole event timeline verbatim, so the board ends up as found rather than with a
stray "withdrawn" entry in a case's history.

Railway's filesystem is ephemeral, so without a mounted volume the dashboard shows a **Demo mode**
banner and `/api/workflow` reports `durable: false` — the workflow is deliberately explicit about
this rather than silently losing status. The production service has a volume mounted at `/data`
(`volumeCreate` via `tools/railway.py`'s GraphQL helper), so live status **is** durable:
`GET /api/workflow` returns `durable: true`.

## Responsive UI

One stylesheet, three breakpoints: ≤900px stacks the payer batches and collapses the case table into cards (labels come
from `data-label`) and shrinks the header, ≤760px puts KPIs 2-up, makes filters full-width and the
case modal full-screen. Verified at 390×844 (iPhone) and 820×1180 (iPad):

```bash
python tests/shots.py http://localhost:8123 _local    # phone / tablet / desktop screenshots
```

## Deploy

Railway, from this repo's subtree — `Procfile` runs uvicorn on `$PORT`, binding `0.0.0.0`.
The SQLite DB and all pre-generated PDFs ship in the repo, so a fresh deploy is fully
populated without any LLM calls at boot.

**Keeping it fresh.** The shipped timeline is anchored to the day it was built, so the live
board slowly rots — deadlines stand still while today moves. `refresh_demo.py` re-anchors it,
and a monthly cron (`musc-demo-refresh`, see [`agent-docs/CRON.md`](../../agent-docs/CRON.md))
runs it unattended:

```bash
python refresh_demo.py --dry-run   # rebuild, test, report the drift, revert — touches nothing live
python refresh_demo.py             # ship it, but only if the board has actually aged
python refresh_demo.py --force     # …because it has to be now
```

It rebuilds, re-renders the letters from cache (no model spend), runs the suite, and **aborts
without pushing if anything fails**, reverting the local rebuild. A rebuild rewrites all 67
letters every time (they quote dates), so "the files changed" is not the signal to deploy:
it ships only once the timeline has drifted 21 days or more than 12 cases have lapsed.
After pushing it waits for the *new* container to actually serve the new deadlines before
touching workflow state — and only replays the snapshot if the deploy lost it. Finally it
runs `tools/uptime.py musc-appeals` (Chromium render check included) and **fails the run if
the live board is broken**, quoting the `git revert` + `mirror.py push` that undoes it — an
unattended push never reports success it did not verify.

## Guardrails

- The prompt forbids invented diagnoses, dates, dollar amounts, policy numbers and guideline
  citations; the model only sees facts pulled from the DB for that one patient.
- Identifiers and money in the letter's metadata block are rendered from SQLite, not from
  model output, so they can't drift.
- Every PDF is verified by the test suite to contain the correct MRN, claim number, CARC code,
  patient surname, the MUSC letterhead and the synthetic-data disclosure.
