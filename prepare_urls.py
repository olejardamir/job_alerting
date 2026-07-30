#!/usr/bin/env python3
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

urls_text = sys.stdin.read()
urls = [line.strip() for line in urls_text.splitlines() if line.strip()]

rows: list[dict[str, str]] = []
for i, url in enumerate(urls, start=1):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    domain_parts = host.split(".")
    org = domain_parts[0].capitalize() if len(domain_parts) >= 2 else host
    rows.append({
        "record_id": str(i),
        "source_file": "new_urls",
        "source_row": str(i),
        "organization_name_original": org,
        "unresolved_q_identifier": "no",
        "input_website": url,
        "final_website": url,
        "canonical_website": url,
        "normalized_domain": host,
        "original_status": "FOUND",
        "career_url": url,
        "link_text": "",
        "score": "",
        "ats_provider_detected": "",
        "evidence": "",
        "candidate_count": "",
        "initial_classification": "",
        "classification_reason": "",
        "retry_category": "",
        "recommended_next_action": "validate_page_and_extract_jobs",
        "processing_queue": "career_validation",
        "error": "",
        "organization_name_resolved": "",
        "ottawa_relationship": "",
        "ottawa_evidence": "",
        "validation_status": "unreviewed",
        "validation_notes": "",
        "last_processed": "",
    })

out = Path("data/new_career_validation.csv")
out.parent.mkdir(exist_ok=True)
fieldnames = list(rows[0].keys()) if rows else []
with out.open("w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

print(f"Written {len(rows)} rows to {out}")
