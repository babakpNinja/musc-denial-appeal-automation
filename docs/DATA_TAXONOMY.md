# Data taxonomy — FHIR → appeal argument

Everything below is **synthetic**. No PHI is present anywhere in this project.

## 1. Where the data comes from

| Source | Used for | Why |
|---|---|---|
| **SMART Health IT R4 sandbox** (`https://r4.smarthealthit.org`) | 50 Synthea patients + their full clinical record | Open, no OAuth registration, serves complete FHIR R4 bundles including `Claim` / `ExplanationOfBenefit` |
| **Epic on FHIR sandbox** (`https://fhir.epic.com`) | Reference for resource shape / field naming | Epic's public sandbox requires per-app OAuth client registration and its test patients do **not** expose payer adjudication or denial payloads, so it cannot be the claims source |
| **Synthetic denial generator** (`build_db.py`) | Denial rows (CARC/RARC, remarks, deadlines, denied $) | No public FHIR sandbox publishes realistic payer *denials*; these are generated with real-world code sets and clearly flagged `data_source = 'SYNTHETIC'` |
| **Tavily research** | Payer appeal portals, appeal windows, submission rules | Real, current provider-facing URLs |

Raw bundles are kept per patient in `data/raw/<patient-uuid>.json` so the whole
database can be rebuilt deterministically: `python build_db.py`.

## 2. FHIR resources consumed

| Resource | Key fields pulled | Lands in |
|---|---|---|
| `Patient` | `id`, `identifier` (MRN), `name`, `birthDate`, `gender`, `address`, `telecom`, `communication`, US-core race/ethnicity extensions | `patients` |
| `Coverage` | `payor.display`, `subscriberId`, `class` (group), `period`, `relationship` | `coverage`, `payers` |
| `Condition` | `code.coding` (SNOMED + ICD-10 where present), `clinicalStatus`, `onsetDateTime` | `conditions` |
| `Encounter` | `class`, `type`, `period`, `reasonCode`, `serviceProvider` | `encounters` |
| `Procedure` | `code.coding` (SNOMED/CPT), `performedPeriod` | `procedures` |
| `Observation` | `code`, `valueQuantity`, `effectiveDateTime` (vitals, labs) | `observations` |
| `MedicationRequest` | `medicationCodeableConcept`, `status`, `authoredOn` | `medications` |
| `Claim` / `ExplanationOfBenefit` | `item` (CPT/HCPCS, units, service date), `total`, `insurer`, `adjudication` | `claims` |

## 3. Code systems in play

| System | Example | Role in the appeal |
|---|---|---|
| **ICD-10-CM** | `I48.91 — Unspecified atrial fibrillation` | Establishes the diagnosis that makes the service medically necessary |
| **CPT / HCPCS** | `93458 — Left heart cath with coronary angiography` | The service actually billed and denied |
| **SNOMED CT** | `49436004 — Atrial fibrillation` | Native Synthea coding; mapped to ICD-10 for claim-facing use |
| **Revenue codes (UB-04)** | `0510 — Clinic` | Facility-side line identification |
| **Place of service** | `21 — Inpatient hospital` | Supports site-of-service arguments |
| **CARC** (Claim Adjustment Reason Code, X12 835 CAS segment) | `50 — Not deemed a medical necessity by the payer` | The payer's *stated reason*; the letter quotes it verbatim and rebuts it |
| **RARC** (Remittance Advice Remark Code, 835 MOA/LQ) | `N130 — Consult plan benefit documents` | Secondary explanation; narrows what evidence the payer wants |
| **X12 837** | claim submission | Conceptual origin of `claims` rows |
| **X12 835** | remittance / denial | Conceptual origin of `denials` rows |

## 4. Denial categories → argument strategy

The generator assigns each denial a `category`, which drives what the LLM is asked to argue:

| Category | Typical CARC | Argument the letter makes |
|---|---|---|
| Medical necessity | 50, 55, 197 | Ties the billed CPT to the documented ICD-10 diagnoses, comorbidities, prior failed therapy and objective results in the record |
| Prior authorization | 197, 15 | Retro-authorization request: urgency/clinical circumstance + the fact that services were rendered and documented |
| Missing information | 16, 226 | Identifies exactly what is enclosed and requests reprocessing rather than re-submission |
| Timely filing | 29 | Submission chronology from `submitted_date` vs `service_date`; requests good-cause review |
| Eligibility | 27, 31 | Coverage period from `coverage.period_start/end` vs the date of service |
| Benefit / non-covered | 96, 204 | Challenges the exclusion's application to this diagnosis/service pairing |
| Coding / clinical validation | 11, 4 | Defends the CPT–ICD-10 linkage and modifier use per coding guidance |
| Duplicate | 18 | Distinguishes the encounter/date/units from the alleged duplicate |
| Frequency / utilization | 119, 151 | Documents why the frequency was clinically indicated for this patient |

## 5. Database schema (SQLite, `data/musc_appeals.db`)

```
patients(patient_id PK, mrn, full_name, birth_date, age, gender, address…)
conditions(id PK, patient_id→patients, code, icd10, display, clinical_status, onset_date)
encounters(encounter_id PK, patient_id, class, type_display, start_date, end_date, reason)
procedures(id PK, patient_id, code, display, performed_date, encounter_id)
observations(id PK, patient_id, code, display, value, unit, effective_date)
medications(id PK, patient_id, display, status, authored_on)
payers(payer_id PK, name, type, portal_name, portal_url, appeal_url, appeal_window_days, appeal_notes)
coverage(coverage_id PK, patient_id, payer_id, member_id, group_number, plan_name, period_start, period_end)
claims(claim_id PK, patient_id, payer_id, encounter_id, service_date, cpt_code, cpt_description,
       icd10_primary, icd10_secondary, billed_amount, allowed_amount, paid_amount, denied_amount,
       rendering_provider, npi, facility, status)
denials(denial_id PK, claim_id→claims, patient_id, payer_id, denial_date, carc_code, carc_description,
        rarc_code, rarc_description, category, payer_remark, appeal_deadline, denied_amount,
        appealable, data_source)
appeals(appeal_id PK, denial_id→denials, patient_id, payer_id, status, letter_path, letter_text,
        sections_json, model, generated_at, argument_summary, submitted_at, outcome)
```

Indices exist on the foreign keys plus `denials.payer_id`, `denials.category` and
`claims.patient_id`, which are what the dashboard filters on.

## 6. How a row becomes a letter

```
denials ⟶ claims ⟶ patients / coverage / payers ⟶ conditions + procedures + observations + medications
   │
   └─▶ prompt (generate_letters.py: every fact interpolated from the DB, nothing invented)
          └─▶ LLM draft: Summary / Clinical Background / Basis for Appeal / Requested Action
                 └─▶ sections_json stored (so PDFs re-render with no LLM call)
                        └─▶ MUSC letterhead PDF (letterhead.py) → letters/<denial_id>.pdf
```

Guardrails enforced in the prompt: use only supplied facts, never invent policy numbers,
guideline section numbers, dates, amounts or clinical events; identifiers and dollar figures
in the letter's metadata block are rendered straight from SQLite rather than from model output.
