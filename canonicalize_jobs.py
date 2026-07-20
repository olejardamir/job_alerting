#!/usr/bin/env python3
"""
Stage 3A — Job Canonicalization and Baseline Builder

Reads jobs_current.csv, cleans and normalizes every field, assigns stable
identities, deduplicates across sources, and stores the canonical baseline
in SQLite.

Outputs:
  output/jobs_canonical.csv    — cleaned, deduplicated job records
  output/jobs_rejected.csv     — jobs that failed quality checks
  output/jobs_duplicates.csv   — merged duplicate pairs
  output/quality_report.csv    — per-source quality metrics
  output/job_monitor.db        — SQLite baseline database
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalized(value: str) -> str:
    """Lowercase, collapse whitespace, strip accents and special chars."""
    v = value.lower().strip()
    v = re.sub(r'\s+', ' ', v)
    v = re.sub(r'[^\w\s]', '', v)
    return v.strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# Canadian province/territory mapping
CA_PROVINCES = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB",
    "new brunswick": "NB", "newfoundland and labrador": "NL",
    "nova scotia": "NS", "northwest territories": "NT",
    "nunavut": "NU", "ontario": "ON", "prince edward island": "PE",
    "quebec": "QC", "saskatchewan": "SK", "yukon": "YT",
    "alberta": "AB", "bc": "BC", "mb": "MB", "nb": "NB",
    "nl": "NL", "ns": "NS", "nt": "NT", "nu": "NU",
    "on": "ON", "pe": "PE", "qc": "QC", "sk": "SK", "yt": "YT",
}

CA_PROVINCE_ABBREVS = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}

US_STATES_ABBREVS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

REMOTE_MARKERS = (
    "remote", "telecommute", "telework", "work from home", "wfh",
    "virtual", "anywhere", "home office", "distributed",
    "travail à distance", "télétravail",
)

HYBRID_MARKERS = (
    "hybrid", "flexible", "mixte", "flex",
)

ONSITE_MARKERS = (
    "on-site", "onsite", "in-office", "in office", "sur site",
)

SOURCE_PREFERENCE = {
    "ats": 1,
    "public_job_api": 2,
    "static_html_listing": 3,
    "iframe_listing": 4,
    "general_job_board": 5,
    "email_application": 6,
    "unknown": 7,
}


# ---------------------------------------------------------------------------
# Title cleaning
# ---------------------------------------------------------------------------

def clean_title(title: str) -> tuple[str, str]:
    """Clean a messy title and extract embedded location/salary/type info.

    Returns (cleaned_title, extracted_location_hint).
    """
    t = title.strip()
    if not t:
        return "", ""

    # Remove HTML tags
    t = re.sub(r'<[^>]+>', '', t)

    # Remove URL-encoded labels
    t = re.sub(r'%[A-Z_]+%', ' ', t)

    # Remove salary patterns embedded in title
    salary_pattern = re.compile(
        r'(?:\$[\d,.]+\s*(?:–|-)\s*\$[\d,.]+|'
        r'\$[\d,.]+\s*(?:per|/)\s*(?:hour|hr|year|annum|month|mo|week|wk)|'
        r'[\d,.]+\s*(?:–|-)\s*[\d,.]+\s*(?:per|/)\s*(?:hour|hr|year)|'
        r'(?:CAD|USD|EUR)\s*[\d,.]+)',
        re.I,
    )
    salary_match = salary_pattern.search(t)
    salary_hint = salary_match.group(0) if salary_match else ""
    t = salary_pattern.sub('', t).strip()

    # Extract embedded location from title
    location_hint = ""
    loc_pattern = re.compile(
        r'(?:^|\s)((?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,?\s*(?:'
        + '|'.join(re.escape(p) + r'\b' for p in ["ON", "QC", "AB", "BC", "MB", "NB", "NS", "NL", "SK", "PE", "YT", "NT", "NU"])
        + r')(?:\s+\w+)?)'
        r'|(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,\s*(?:Canada|USA|United States|Remote)))',
    )
    loc_match = loc_pattern.search(t)
    if loc_match:
        location_hint = loc_match.group(0).strip()
        t = t[:loc_match.start()] + t[loc_match.end():]
        t = t.strip()

    # Remove trailing location-like patterns: "City, ST"
    t = re.sub(r'\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,\s*[A-Z]{2}\s*$', '', t).strip()

    # Remove employment type tags embedded in title
    type_pattern = re.compile(
        r'(?:^|\s)(Full[- ]?[Tt]ime|Part[- ]?[Tt]ime|Temps plein|Temps partiel|Contract|Permanent|Temporary|Intern|Stage)\s*$',
        re.I,
    )
    t = type_pattern.sub('', t).strip()

    # Remove leading/trailing pipe, dash, bullet
    t = re.sub(r'^[\s|–\-•·]+|[\s|–\-•·]+$', '', t).strip()

    # Collapse multiple spaces
    t = re.sub(r'\s+', ' ', t).strip()

    return t, location_hint


# ---------------------------------------------------------------------------
# Location parsing
# ---------------------------------------------------------------------------

def parse_location(raw: str) -> tuple[str, str, str, str]:
    """Parse a raw location string into (country, region, city, normalized_location).

    Returns ("", "", "", "") if unparseable.
    """
    if not raw:
        return "", "", "", ""

    loc = raw.strip()
    country = ""
    region = ""
    city = ""

    # Normalize country names
    country_map = {
        "canada": "Canada", "ca": "Canada",
        "united states": "United States", "usa": "United States", "us": "United States",
        "u.s.a.": "United States", "u.s.": "United States",
        "united kingdom": "United Kingdom", "uk": "United Kingdom",
    }

    # Check for country mentions
    loc_lower = loc.lower()
    for pattern, name in country_map.items():
        if re.search(r'\b' + re.escape(pattern) + r'\b', loc_lower):
            country = name
            break

    if "remote" in loc_lower or "virtual" in loc_lower or "wfh" in loc_lower:
        country = "Remote"

    # Try "City, Province" pattern (Canadian)
    m = re.match(r'([A-Za-z\s.\'-]+),\s*([A-Z]{2})', loc)
    if m:
        city = m.group(1).strip().rstrip(',.')
        abbrev = m.group(2).upper()
        if abbrev in CA_PROVINCE_ABBREVS:
            region = abbrev
            if not country:
                country = "Canada"
        elif abbrev in US_STATES_ABBREVS:
            region = abbrev
            if not country:
                country = "United States"

    # Try "City, State, Country" pattern
    if not city:
        m = re.match(r'([A-Za-z\s.\'-]+),\s*([A-Za-z\s.\'-]+),\s*([A-Za-z\s.\'-]+)', loc)
        if m:
            city = m.group(1).strip().rstrip(',.')
            region = m.group(2).strip().rstrip(',.')
            c = m.group(3).strip().rstrip(',.')
            if c.lower() in country_map:
                country = country_map[c.lower()]

    # If no comma, treat as city — but only if it's not a province abbreviation alone
    if not city and loc:
        # Check if it's just a province/state abbreviation
        if loc.upper() in CA_PROVINCE_ABBREVS:
            region = loc.upper()
            if not country:
                country = "Canada"
        elif loc.upper() in US_STATES_ABBREVS:
            region = loc.upper()
            if not country:
                country = "United States"
        else:
            city = loc

    normalized_loc = ", ".join(filter(None, [city, region, country]))
    return country, region, city, normalized_loc


def infer_country_from_url(url: str) -> str:
    """Infer country from domain TLD or URL patterns."""
    lower = url.lower()
    if ".ca" in lower or "canada" in lower:
        return "Canada"
    if ".us" in lower or ".gov" in lower:
        return "United States"
    if ".co.uk" in lower or ".org.uk" in lower:
        return "United Kingdom"
    return ""


# ---------------------------------------------------------------------------
# Work arrangement detection
# ---------------------------------------------------------------------------

def detect_work_arrangement(text: str) -> str:
    lower = text.lower()
    if any(m in lower for m in REMOTE_MARKERS):
        return "remote"
    if any(m in lower for m in HYBRID_MARKERS):
        return "hybrid"
    if any(m in lower for m in ONSITE_MARKERS):
        return "onsite"
    return ""


# ---------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------

REJECTED_REASONS: dict[str, str] = {}

def check_quality(job: dict) -> tuple[bool, str, dict]:
    """Run quality checks on a job. Returns (passed, reason, flags)."""
    flags: dict[str, str] = {}
    title = clean(job.get("title", ""))
    url = clean(job.get("job_url", ""))
    desc = clean(job.get("description", ""))
    org = clean(job.get("organization_name", ""))

    # Hard reject: no title
    if not title:
        return False, "empty_title", flags

    # Hard reject: title is just a URL
    if title.startswith("http"):
        return False, "title_is_url", flags

    # Hard reject: title is a page title / navigation element
    nav_patterns = (
        "executive team", "our team", "leadership", "about us",
        "contact us", "home", "menu", "skip to", "loading",
        "cookie", "privacy", "terms", "login", "sign in", "register",
    )
    if any(p in title.lower() for p in nav_patterns):
        return False, "title_is_navigation", flags

    # Hard reject: title too short to be a real job
    if len(title) < 3:
        return False, "title_too_short", flags

    # Hard reject: single letter or number
    if len(title) <= 2:
        return False, "title_too_short", flags

    # Hard reject: title is a single generic word
    if title.lower() in ("jobs", "careers", "employment", "opportunities", "vacancies", "positions", "openings", "apply", "search"):
        return False, "title_is_category_heading", flags

    # Hard reject: title is a sentence (contains common sentence words and is long)
    sentence_indicators = (
        "we are", "we have", "we're", "our team", "join us", "we grow",
        "we offer", "we provide", "you will", "you'll", "your role",
        "click here", "learn more", "find out", "see all",
    )
    if any(s in title.lower() for s in sentence_indicators):
        return False, "title_is_sentence", flags

    # Hard reject: title ends with common non-title patterns
    if re.search(r'(?:general application|apply now|learn more|click here|view all|see more)\s*$', title, re.I):
        return False, "title_is_cta", flags

    # Hard reject: job_url points to main career page (not a specific job)
    if url:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/").lower()
        if path in ("", "/careers", "/career", "/jobs", "/employment", "/about"):
            if not any(kw in path for kw in ("/job/", "/position/", "/requisition/", "/posting/")):
                flags["url_is_career_page"] = "true"

    # Warning: no job URL
    if not url:
        flags["missing_job_url"] = "true"

    # Warning: no location
    if not clean(job.get("location", "")):
        flags["missing_location"] = "true"

    # Warning: no description
    if not desc or len(desc) < 20:
        flags["short_description"] = "true"

    # Warning: title > 80 chars (likely concatenated)
    if len(title) > 80:
        flags["title_too_long"] = "true"

    # Warning: title contains HTML
    if "<" in title or ">" in title:
        flags["title_has_html"] = "true"

    return True, "", flags


# ---------------------------------------------------------------------------
# Canonical ID generation
# ---------------------------------------------------------------------------

def generate_canonical_id(
    org: str,
    title: str,
    location: str,
    domain: str,
    source_job_id: str = "",
    job_url: str = "",
) -> str:
    """Generate a stable canonical job ID using priority order."""
    # 1. ATS/Source job ID
    if source_job_id:
        return f"src:{normalized(org)}:{source_job_id}"

    # 2. Canonical job URL (path-based)
    if job_url:
        parsed = urlparse(job_url)
        path = parsed.path.rstrip("/")
        if path and path != "/":
            return f"url:{normalized(org)}:{normalized(path)}"

    # 3. Company + title + location + domain
    parts = [normalized(org), normalized(title), normalized(location), normalized(domain)]
    combined = "|".join(p for p in parts if p)
    return f"hash:{hashlib.sha256(combined.encode()).hexdigest()[:12]}"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def find_duplicates(jobs: list[dict]) -> list[tuple[str, str, str]]:
    """Find duplicate job pairs. Returns list of (canonical_id_a, canonical_id_b, reason)."""
    duplicates = []

    # Group by canonical_job_id — these are exact duplicates
    by_canon: dict[str, list[int]] = defaultdict(list)
    for i, j in enumerate(jobs):
        cid = j.get("canonical_job_id", "")
        if cid:
            by_canon[cid].append(i)

    for cid, indices in by_canon.items():
        if len(indices) < 2:
            continue
        # All pairs within same canonical ID are duplicates
        for a_idx in range(len(indices)):
            for b_idx in range(a_idx + 1, len(indices)):
                duplicates.append((cid, cid, "same_canonical_id"))

    # Group by org + normalized title for fuzzy matching
    by_key: dict[str, list[int]] = defaultdict(list)
    for i, j in enumerate(jobs):
        key = f"{normalized(j.get('organization_name', ''))}|{normalized(j.get('normalized_title', j.get('title', '')))}"
        by_key[key].append(i)

    for key, indices in by_key.items():
        if len(indices) < 2:
            continue

        for a_idx in range(len(indices)):
            for b_idx in range(a_idx + 1, len(indices)):
                i, j = indices[a_idx], indices[b_idx]
                job_a, job_b = jobs[i], jobs[j]
                cid_a = job_a.get("canonical_job_id", "")
                cid_b = job_b.get("canonical_job_id", "")
                if cid_a == cid_b:
                    continue  # Already caught above

                # Same ATS job ID
                if (job_a.get("job_id") and job_b.get("job_id")
                        and job_a["job_id"] == job_b["job_id"]
                        and job_a.get("source_provider") == job_b.get("source_provider")):
                    duplicates.append((cid_a, cid_b, "same_ats_job_id"))
                    continue

                # Same URL
                url_a = clean(job_a.get("job_url", "")).rstrip("/")
                url_b = clean(job_b.get("job_url", "")).rstrip("/")
                if url_a and url_b and url_a == url_b:
                    duplicates.append((cid_a, cid_b, "same_url"))
                    continue

    return duplicates


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def create_database(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS organizations (
            organization_id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_name TEXT NOT NULL,
            canonical_domain TEXT,
            first_seen TEXT,
            last_checked TEXT
        );

        CREATE TABLE IF NOT EXISTS job_sources (
            source_id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER,
            source_type TEXT,
            source_provider TEXT,
            listing_url TEXT,
            api_url TEXT,
            adapter_name TEXT,
            source_status TEXT,
            last_successful_check TEXT,
            consecutive_failures INTEGER DEFAULT 0,
            FOREIGN KEY (organization_id) REFERENCES organizations(organization_id)
        );

        CREATE TABLE IF NOT EXISTS jobs (
            canonical_job_id TEXT PRIMARY KEY,
            organization_id INTEGER,
            source_job_id TEXT,
            title TEXT,
            normalized_title TEXT,
            location TEXT,
            normalized_location TEXT,
            country TEXT,
            region TEXT,
            city TEXT,
            work_arrangement TEXT,
            employment_type TEXT,
            salary_min TEXT,
            salary_max TEXT,
            currency TEXT,
            posted_date TEXT,
            closing_date TEXT,
            description TEXT,
            job_url TEXT,
            application_url TEXT,
            application_email TEXT,
            status TEXT DEFAULT 'active',
            first_seen TEXT,
            last_seen TEXT,
            content_hash TEXT,
            FOREIGN KEY (organization_id) REFERENCES organizations(organization_id)
        );

        CREATE TABLE IF NOT EXISTS job_source_links (
            link_id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_job_id TEXT,
            source_id INTEGER,
            source_url TEXT,
            source_job_id TEXT,
            first_seen TEXT,
            last_seen TEXT,
            FOREIGN KEY (canonical_job_id) REFERENCES jobs(canonical_job_id),
            FOREIGN KEY (source_id) REFERENCES job_sources(source_id)
        );

        CREATE TABLE IF NOT EXISTS crawl_runs (
            crawl_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER,
            started_at TEXT,
            completed_at TEXT,
            result TEXT,
            jobs_found INTEGER,
            error TEXT,
            FOREIGN KEY (source_id) REFERENCES job_sources(source_id)
        );

        CREATE TABLE IF NOT EXISTS job_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_job_id TEXT,
            event_type TEXT,
            event_time TEXT,
            old_value TEXT,
            new_value TEXT,
            crawl_run_id INTEGER,
            FOREIGN KEY (canonical_job_id) REFERENCES jobs(canonical_job_id),
            FOREIGN KEY (crawl_run_id) REFERENCES crawl_runs(crawl_run_id)
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_org ON jobs(organization_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(normalized_title);
        CREATE INDEX IF NOT EXISTS idx_job_sources_org ON job_sources(organization_id);
        CREATE INDEX IF NOT EXISTS idx_job_source_links_job ON job_source_links(canonical_job_id);
        CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(canonical_job_id);
    """)
    return conn


def populate_database(conn: sqlite3.Connection, jobs: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()

    # Build org map
    org_map: dict[str, int] = {}
    for j in jobs:
        org = clean(j.get("organization_name", ""))
        if org and org not in org_map:
            cur = conn.execute(
                "INSERT INTO organizations (organization_name, canonical_domain, first_seen, last_checked) VALUES (?, ?, ?, ?)",
                (org, clean(j.get("source_provider", "")), now, now),
            )
            org_map[org] = cur.lastrowid

    # Insert jobs
    for j in jobs:
        org = clean(j.get("organization_name", ""))
        org_id = org_map.get(org)
        conn.execute("""
            INSERT OR REPLACE INTO jobs
            (canonical_job_id, organization_id, source_job_id, title, normalized_title,
             location, normalized_location, country, region, city,
             work_arrangement, employment_type, salary_min, salary_max, currency,
             posted_date, closing_date, description, job_url, application_url,
             application_email, status, first_seen, last_seen, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            j.get("canonical_job_id", ""),
            org_id,
            j.get("job_id", ""),
            j.get("title", ""),
            j.get("normalized_title", ""),
            j.get("location", ""),
            j.get("normalized_location", ""),
            j.get("country", ""),
            j.get("region", ""),
            j.get("city", ""),
            j.get("work_arrangement", ""),
            j.get("employment_type", ""),
            j.get("salary_min", ""),
            j.get("salary_max", ""),
            j.get("currency", ""),
            j.get("posted_date", ""),
            j.get("closing_date", ""),
            j.get("description", ""),
            j.get("job_url", ""),
            j.get("application_url", ""),
            j.get("application_email", ""),
            j.get("status", "active"),
            j.get("first_seen", now),
            j.get("last_seen", now),
            j.get("content_hash", ""),
        ))

    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    input_path = Path("output/jobs_current.csv")
    out_dir = Path("output")

    print("Loading jobs_current.csv...")
    with input_path.open(newline="", encoding="utf-8-sig") as f:
        raw_jobs = list(csv.DictReader(f))
    print(f"Loaded {len(raw_jobs)} raw jobs")

    canonical: list[dict] = []
    rejected: list[dict] = []
    quality_flags: dict[str, dict] = {}

    print("Running quality checks and normalization...")
    for job in raw_jobs:
        passed, reason, flags = check_quality(job)

        if not passed:
            job["rejection_reason"] = reason
            rejected.append(job)
            continue

        # Clean title
        cleaned_title, location_hint = clean_title(clean(job.get("title", "")))

        # Combine location from title hint and original location
        raw_location = clean(job.get("location", "")) or location_hint

        # Parse location
        country, region, city, normalized_loc = parse_location(raw_location)

        # If no country from location, try URL
        if not country:
            country = infer_country_from_url(clean(job.get("job_url", "")))

        # Detect work arrangement
        work_arr = detect_work_arrangement(
            f"{clean(job.get('title', ''))} {raw_location} {clean(job.get('description', ''))}"
        )

        # Generate canonical ID
        domain = clean(job.get("source_provider", ""))
        if not domain:
            try:
                domain = urlparse(clean(job.get("job_url", ""))).netloc
            except Exception:
                domain = ""
        canonical_id = generate_canonical_id(
            clean(job.get("organization_name", "")),
            cleaned_title,
            normalized_loc,
            domain,
            clean(job.get("job_id", "")),
            clean(job.get("job_url", "")),
        )

        # Update job record
        job["title"] = cleaned_title
        job["normalized_title"] = normalized(cleaned_title)
        job["location"] = raw_location
        job["normalized_location"] = normalized_loc
        job["country"] = country
        job["region"] = region
        job["city"] = city
        job["work_arrangement"] = work_arr
        job["canonical_job_id"] = canonical_id
        job["salary_status"] = "not_disclosed"
        job["location_status"] = "not_disclosed" if not raw_location else "available"
        job["posted_date_status"] = "unavailable" if not clean(job.get("posted_date")) else "available"
        job["content_hash"] = content_hash(clean(job.get("description", "")))

        if flags:
            quality_flags[canonical_id] = flags

        canonical.append(job)

    print(f"  Passed: {len(canonical)}")
    print(f"  Rejected: {len(rejected)}")

    # Deduplication
    print("Finding duplicates...")
    duplicates = find_duplicates(canonical)
    print(f"  Found {len(duplicates)} duplicate pairs")

    # Deduplicate: keep the best source for each group
    dup_to_remove: set[str] = set()
    for id_a, id_b, reason in duplicates:
        if id_a in dup_to_remove or id_b in dup_to_remove:
            continue
        if id_a == id_b:
            # Same canonical ID — keep only the first occurrence
            indices = [i for i, j in enumerate(canonical) if j.get("canonical_job_id") == id_a]
            for idx in indices[1:]:
                dup_to_remove.add(f"{id_a}_{idx}")
            continue
        # Mark the lower-preference one for removal
        job_a = next((j for j in canonical if j.get("canonical_job_id") == id_a), None)
        job_b = next((j for j in canonical if j.get("canonical_job_id") == id_b), None)
        if job_a and job_b:
            pref_a = SOURCE_PREFERENCE.get(job_a.get("source_type", ""), 99)
            pref_b = SOURCE_PREFERENCE.get(job_b.get("source_type", ""), 99)
            if pref_a <= pref_b:
                dup_to_remove.add(id_b)
            else:
                dup_to_remove.add(id_a)

    # Remove duplicates from canonical
    deduped = []
    dup_records = []
    seen_canon: set[str] = set()
    for j in canonical:
        cid = j.get("canonical_job_id", "")
        idx_key = f"{cid}_{canonical.index(j)}"
        if idx_key in dup_to_remove:
            dup_records.append(j)
            continue
        if cid in seen_canon:
            # Already kept one with this canonical ID
            dup_records.append(j)
            continue
        seen_canon.add(cid)
        deduped.append(j)
    print(f"  After dedup: {len(deduped)} unique jobs ({len(dup_records)} removed as duplicates)")

    # Write outputs
    now_iso = datetime.now(timezone.utc).isoformat()

    # jobs_canonical.csv
    canonical_fields = [
        "canonical_job_id", "record_id", "organization_name", "source_type",
        "source_provider", "source_listing_url", "job_id", "title",
        "normalized_title", "location", "normalized_location", "country",
        "region", "city", "work_arrangement", "employment_type",
        "salary_min", "salary_max", "currency", "salary_status",
        "posted_date", "closing_date", "posted_date_status",
        "location_status", "description", "job_url", "application_url",
        "application_email", "application_method", "first_seen", "last_seen",
        "status", "content_hash",
    ]
    canonical_path = out_dir / "jobs_canonical.csv"
    with canonical_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=canonical_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(deduped)
    print(f"Wrote {canonical_path} ({len(deduped)} rows)")

    # jobs_rejected.csv
    rejected_fields = [
        "record_id", "organization_name", "title", "job_url", "source_type",
        "source_provider", "rejection_reason",
    ]
    rejected_path = out_dir / "jobs_rejected.csv"
    with rejected_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rejected_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rejected)
    print(f"Wrote {rejected_path} ({len(rejected)} rows)")

    # jobs_duplicates.csv
    dup_fields = [
        "canonical_job_id", "record_id", "organization_name", "title",
        "normalized_title", "location", "source_type", "source_provider",
        "job_url",
    ]
    dup_path = out_dir / "jobs_duplicates.csv"
    with dup_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=dup_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(dup_records)
    print(f"Wrote {dup_path} ({len(dup_records)} rows)")

    # quality_report.csv
    qr_rows = []
    source_groups: dict[str, list[dict]] = defaultdict(list)
    for j in canonical:
        key = f"{j.get('source_type', '')}|{j.get('source_provider', '')}"
        source_groups[key].append(j)
    for key, group in sorted(source_groups.items(), key=lambda x: -len(x[1])):
        total = len(group)
        has_loc = sum(1 for j in group if j.get("location"))
        has_url = sum(1 for j in group if j.get("job_url"))
        has_desc = sum(1 for j in group if len(j.get("description", "")) >= 20)
        long_title = sum(1 for j in group if len(j.get("title", "")) > 80)
        parts = key.split("|")
        qr_rows.append({
            "source_type": parts[0],
            "source_provider": parts[1] if len(parts) > 1 else "",
            "total_jobs": total,
            "pct_with_location": f"{has_loc/total*100:.1f}" if total else "0",
            "pct_with_url": f"{has_url/total*100:.1f}" if total else "0",
            "pct_with_description": f"{has_desc/total*100:.1f}" if total else "0",
            "pct_long_titles": f"{long_title/total*100:.1f}" if total else "0",
        })
    qr_path = out_dir / "quality_report.csv"
    with qr_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["source_type", "source_provider", "total_jobs",
                                           "pct_with_location", "pct_with_url",
                                           "pct_with_description", "pct_long_titles"])
        w.writeheader()
        w.writerows(qr_rows)
    print(f"Wrote {qr_path} ({len(qr_rows)} rows)")

    # SQLite database
    db_path = out_dir / "job_monitor.db"
    if db_path.exists():
        db_path.unlink()
    conn = create_database(db_path)
    populate_database(conn, deduped)
    conn.close()
    print(f"Wrote {db_path}")

    # Summary
    print(f"\n=== Stage 3A Summary ===")
    print(f"Raw jobs:           {len(raw_jobs)}")
    print(f"After QC:           {len(canonical)}")
    print(f"After dedup:        {len(deduped)}")
    print(f"Rejected:           {len(rejected)}")
    print(f"Removed as dup:     {len(dup_records)}")
    print(f"Duplicate pairs:    {len(duplicates)}")

    # Rejection breakdown
    rej_reasons = Counter(j.get("rejection_reason", "") for j in rejected)
    print(f"\nRejection reasons:")
    for reason, count in rej_reasons.most_common():
        print(f"  {reason:35s}: {count}")

    # Quality flags summary
    flag_counts: Counter = Counter()
    for flags in quality_flags.values():
        for k in flags:
            flag_counts[k] += 1
    print(f"\nQuality flags:")
    for flag, count in flag_counts.most_common():
        print(f"  {flag:35s}: {count}")


if __name__ == "__main__":
    main()
