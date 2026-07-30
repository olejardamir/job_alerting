#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
from pathlib import Path
from urllib.parse import urlparse

VALIDATED = Path("output/new_career_pages_validated_full.csv")
SOURCES_OUT = Path("output/new_job_sources.csv")
JSONL = Path("output/new_career_pages_validated_all.jsonl")

# Load JSONL for enriched fields
jsonl_records: dict[str, dict] = {}
if JSONL.exists():
    with JSONL.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                jsonl_records[str(r.get("record_id"))] = r

INCLUDE_RESULTS = {
    "confirmed_career_page_active",
    "confirmed_external_ats_active",
    "confirmed_career_page_no_openings",
    "confirmed_external_ats_no_openings",
}

CONFIRMED_ACTIVE = {
    "confirmed_career_page_active",
    "confirmed_external_ats_active",
}

# Map detected ATS to source type
ATS_TYPES = {
    "greenhouse": "ats", "lever": "ats", "ashby": "ats",
    "smartrecruiters": "ats", "workable": "ats", "bamboohr": "ats",
    "workday": "ats", "dayforce": "ats", "icims": "ats",
    "adp": "ats", "oracle": "ats", "successfactors": "ats",
    "taleo": "ats", "rippling": "ats", "jobvite": "ats",
    "applytojob": "ats", "recruitee": "ats", "ukg_pro": "ats",
    "njoyn": "ats", "phenom": "ats",
}

def classify_source_type(row: dict) -> str:
    ats = row.get("detected_ats", "").strip().lower()
    result = row.get("validation_result", "")
    if ats in ATS_TYPES or row.get("ats_provider_detected", "").strip().lower() in ATS_TYPES:
        return "ats"
    if result == "confirmed_external_ats_active":
        return "ats"
    if result == "confirmed_external_ats_no_openings":
        return "no_openings"
    if result == "confirmed_career_page_no_openings":
        return "no_openings"
    return "static_html_listing"

rows = []
with VALIDATED.open(newline="", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        vr = row.get("validation_result", "").strip()
        rid = row.get("record_id", "").strip()
        if not rid:
            continue
        if vr not in INCLUDE_RESULTS:
            continue

        org = row.get("organization_name_original", row.get("organization_name", "")).strip()
        url = (row.get("loaded_url", "") or row.get("career_url", "")).strip()
        if not url.startswith(("http://", "https://")):
            continue

        ats = jsonl_records.get(rid, {}).get("detected_ats", "") or row.get("detected_ats", "")
        provider = ats.strip().lower() if ats else ""
        source_type = classify_source_type(row)

        rows.append({
            "record_id": rid,
            "organization_name": org,
            "monitor_url": url,
            "source_type": source_type,
            "source_provider": provider,
            "source_listing_url": url,
            "source_api_url": "",
            "source_stage": "stage1",
            "previous_result": vr,
            "extraction_result": "",
            "extraction_reason": "",
            "jobs_found": "0",
            "extraction_error": "",
            "extraction_checked_at_utc": "",
        })

fieldnames = [
    "record_id", "organization_name", "monitor_url", "source_type",
    "source_provider", "source_listing_url", "source_api_url",
    "source_stage", "previous_result", "extraction_result",
    "extraction_reason", "jobs_found", "extraction_error",
    "extraction_checked_at_utc",
]

with SOURCES_OUT.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(rows)

active = [r for r in rows if r["previous_result"] in CONFIRMED_ACTIVE]
print(f"Written {len(rows)} sources to {SOURCES_OUT}")
print(f"  Active with jobs: {len(active)}")
print(f"  No openings: {len(rows) - len(active)}")
print()

type_counts = {}
for r in rows:
    t = r["source_type"]
    type_counts[t] = type_counts.get(t, 0) + 1
for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
    print(f"  {t}: {c}")
