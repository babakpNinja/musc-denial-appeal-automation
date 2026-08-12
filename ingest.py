#!/usr/bin/env python3
"""Pull synthetic patient records (FHIR R4) into data/raw/.

Primary source: the public SMART Health IT R4 sandbox (https://r4.smarthealthit.org),
which serves Synthea-generated synthetic patients including Claim /
ExplanationOfBenefit resources with payer + line-item cost data.

Epic's public sandbox (fhir.epic.com) requires OAuth client registration and does
not expose claim adjudication/denial payloads, so it is not usable as the claims
source here. See docs/DATA_TAXONOMY.md for the full rationale.

No PHI: every record is synthetic.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

BASE = "https://r4.smarthealthit.org"
RAW = Path(__file__).parent / "data" / "raw"
N_PATIENTS = 50

# Resources fetched per patient; value = search param pointing at the patient
PER_PATIENT = {
    "Condition": "patient",
    "Encounter": "patient",
    "Procedure": "patient",
    "Observation": "patient",
    "MedicationRequest": "patient",
    "Immunization": "patient",
    "CarePlan": "patient",
    "Coverage": "patient",
    "Claim": "patient",
    "ExplanationOfBenefit": "patient",
}
# cap on entries stored per resource type (Observations are enormous in Synthea)
CAPS = {"Observation": 40, "Encounter": 25, "CarePlan": 5, "Immunization": 10}


def get(path: str, params: dict | None = None) -> dict:
    for attempt in range(4):
        try:
            r = requests.get(f"{BASE}/{path}", params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
    return {}


def entries(bundle: dict) -> list[dict]:
    return [e["resource"] for e in bundle.get("entry", []) if "resource" in e]


def fetch_patient(pid: str) -> dict:
    out: dict[str, list[dict]] = {}
    for rtype, param in PER_PATIENT.items():
        cap = CAPS.get(rtype, 100)
        bundle = get(rtype, {param: pid, "_count": cap})
        out[rtype] = entries(bundle)[:cap]
    return out


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    print("searching for patients ...")

    patients: list[dict] = []
    url = f"{BASE}/Patient?_count=100"
    while url and len(patients) < N_PATIENTS * 2:
        r = requests.get(url, timeout=90)
        if r.status_code != 200:
            break
        b = r.json()
        patients.extend(p for p in entries(b) if p.get("resourceType") == "Patient")
        url = next(
            (l["url"] for l in b.get("link", []) if l.get("relation") == "next"), None
        )
    print(f"  {len(patients)} candidate patients")

    def work(pat: dict):
        pid = pat["id"]
        rec = {"Patient": pat, **fetch_patient(pid)}
        # keep only patients with real clinical substance to ground a letter in
        if len(rec.get("Condition", [])) < 1 or len(rec.get("Encounter", [])) < 1:
            return None
        (RAW / f"{pid}.json").write_text(json.dumps(rec))
        counts = {k: len(v) for k, v in rec.items() if isinstance(v, list) and v}
        print(f"  {pid}: {counts}")
        return pid

    with ThreadPoolExecutor(max_workers=8) as pool:
        done = [p for p in pool.map(work, patients) if p][:N_PATIENTS]

    (RAW.parent / "patient_ids.json").write_text(json.dumps(done, indent=1))
    print(f"\nwrote {len(done)} patient bundles to {RAW}")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
