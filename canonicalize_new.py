#!/usr/bin/env python3
from __future__ import annotations
import csv, sys, sqlite3
from pathlib import Path
from collections import defaultdict, Counter
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))
from canonicalize_jobs import (
    clean, clean_title, parse_location, check_quality,
    generate_canonical_id, find_duplicates, create_database, populate_database,
)

CSV_IN = Path("output/new_stage2c/jobs_current.csv")
SOURCES_IN = Path("output/new_job_sources.csv")
CSV_OUT = Path("output/new_stage2c/jobs_canonical.csv")
DB_OUT = Path("output/new_job_monitor.db")

with CSV_IN.open(newline="", encoding="utf-8-sig") as f:
    raw_jobs = list(csv.DictReader(f))
print(f"Loaded {len(raw_jobs)} raw jobs")

canonical: list[dict] = []
rejected: list[dict] = []
quality_flags: dict[int, dict] = {}

for i, j in enumerate(raw_jobs):
    ok, reason, flags = check_quality(j)
    if not ok:
        j["rejection_reason"] = reason
        rejected.append(j)
    else:
        title_clean, title_norm = clean_title(j.get("title", ""))
        j["clean_title"] = title_clean
        j["normalized_title"] = title_norm
        location = j.get("location", "")
        city, region, country, nl = parse_location(location)
        j["city"] = city
        j["region"] = region
        j["country"] = country
        j["normalized_location"] = nl
        canonical.append(j)
        if flags:
            quality_flags[i] = flags

print(f"  Passed: {len(canonical)}")
print(f"  Rejected: {len(rejected)}")

# Dedup
duplicates = find_duplicates(canonical)
dup_ids = {d[1] for d in duplicates}
deduped = [j for j in canonical if j.get("canonical_job_id", "") not in dup_ids]
dup_records = [
    {
        "canonical_job_id": d[0],
        "record_id": "",
        "organization_name": "",
        "title": "",
        "normalized_title": "",
        "location": "",
        "source_type": "",
        "source_provider": "",
        "job_url": "",
    }
    for d in duplicates
]
print(f"  Found {len(duplicates)} duplicate pairs")
print(f"  After dedup: {len(deduped)} unique jobs ({len(canonical) - len(deduped)} removed)")

# Assign canonical_job_id if missing
for j in deduped:
    if not j.get("canonical_job_id"):
        j["canonical_job_id"] = generate_canonical_id(
            j.get("title", ""), j.get("organization_name", ""), j.get("job_url", "")
        )

# Write canonical CSV
fieldnames = [
    "canonical_job_id", "source_listing_url", "source_job_id", "source_type",
    "source_provider", "title", "normalized_title", "location",
    "normalized_location", "country", "region", "city",
    "work_arrangement", "employment_type", "salary_min", "salary_max",
    "currency", "posted_date", "closing_date", "description",
    "job_url", "application_url", "application_email", "organization_name",
    "status", "first_seen", "last_seen", "content_hash",
]
with CSV_OUT.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
    w.writeheader()
    for j in deduped:
        row = {
            "canonical_job_id": j.get("canonical_job_id", generate_canonical_id(j.get("title",""), j.get("organization_name",""), j.get("job_url",""))),
            "source_listing_url": j.get("source_listing_url", ""),
            "source_job_id": j.get("job_id", ""),
            "source_type": j.get("source_type", ""),
            "source_provider": j.get("source_provider", ""),
            "title": j.get("clean_title", j.get("title", "")),
            "normalized_title": j.get("normalized_title", ""),
            "location": j.get("location", ""),
            "normalized_location": j.get("normalized_location", ""),
            "country": j.get("country", ""),
            "region": j.get("region", ""),
            "city": j.get("city", ""),
            "work_arrangement": j.get("work_arrangement", ""),
            "employment_type": j.get("employment_type", ""),
            "salary_min": j.get("salary_min", ""),
            "salary_max": j.get("salary_max", ""),
            "currency": j.get("currency", ""),
            "posted_date": j.get("posted_date", ""),
            "closing_date": j.get("closing_date", ""),
            "description": j.get("description", ""),
            "job_url": j.get("job_url", ""),
            "application_url": j.get("application_url", ""),
            "application_email": j.get("application_email", ""),
            "organization_name": j.get("organization_name", ""),
            "status": j.get("status", "active"),
            "first_seen": j.get("first_seen", ""),
            "last_seen": j.get("last_seen", ""),
            "content_hash": j.get("content_hash", ""),
        }
        w.writerow(row)
print(f"Wrote {CSV_OUT} ({len(deduped)} rows)")

# Create DB
if DB_OUT.exists():
    DB_OUT.unlink()
conn = create_database(DB_OUT)

# Populate organizations/jobs from canonical CSV
with CSV_OUT.open(newline="", encoding="utf-8") as f:
    deduped_read = list(csv.DictReader(f))
populate_database(conn, deduped_read)
conn.close()
print(f"Wrote {DB_OUT}")

# Populate job_sources table from sources CSV
conn = sqlite3.connect(str(DB_OUT))
with SOURCES_IN.open(newline="", encoding="utf-8-sig") as f:
    sources = list(csv.DictReader(f))
now = datetime.now(timezone.utc).isoformat()
for s in sources:
    org = s.get("organization_name", "")
    cur = conn.execute("SELECT organization_id FROM organizations WHERE organization_name = ?", (org,))
    row = cur.fetchone()
    org_id = row[0] if row else None
    if org_id is None:
        cur = conn.execute(
            "INSERT INTO organizations (organization_name, first_seen, last_checked) VALUES (?, ?, ?)",
            (org, now, now),
        )
        org_id = cur.lastrowid

    conn.execute(
        "INSERT OR IGNORE INTO job_sources (organization_id, source_type, source_provider, listing_url, source_status) VALUES (?, ?, ?, ?, ?)",
        (org_id,
         s.get("source_type", ""),
         s.get("source_provider", ""),
         s.get("monitor_url", ""),
         "active",
         ),
    )
conn.commit()
conn.close()
print(f"Populated {len(sources)} sources in DB")

print(f"\n=== New Sources Summary ===")
print(f"Raw jobs:           {len(raw_jobs)}")
print(f"After QC:           {len(canonical)}")
print(f"After dedup:        {len(deduped)}")
print(f"Rejected:           {len(rejected)}")
print(f"Sources in DB:      {len(sources)}")

rej_reasons = Counter(j.get("rejection_reason", "") for j in rejected)
print(f"\nRejection reasons:")
for reason, count in rej_reasons.most_common():
    print(f"  {reason:35s}: {count}")
