from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .core import ComparisonResult, TRACKED_FIELDS, classify_source, clean, csv_rows, safe_int, source_is_successful


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {clean(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add(conn: sqlite3.Connection, table: str, name: str, declaration: str) -> None:
    if name not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def migrate_database(conn: sqlite3.Connection) -> None:
    """Upgrade a Stage 3A database for source runs and job state transitions."""
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS organizations (
            organization_id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_name TEXT NOT NULL, canonical_domain TEXT,
            first_seen TEXT, last_checked TEXT
        );
        CREATE TABLE IF NOT EXISTS job_sources (
            source_id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER, source_type TEXT, source_provider TEXT,
            listing_url TEXT, api_url TEXT, adapter_name TEXT,
            source_status TEXT, last_successful_check TEXT,
            consecutive_failures INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS jobs (
            canonical_job_id TEXT PRIMARY KEY, organization_id INTEGER,
            source_job_id TEXT, title TEXT, normalized_title TEXT,
            location TEXT, normalized_location TEXT, country TEXT, region TEXT,
            city TEXT, work_arrangement TEXT, employment_type TEXT,
            salary_min TEXT, salary_max TEXT, currency TEXT, posted_date TEXT,
            closing_date TEXT, description TEXT, job_url TEXT,
            application_url TEXT, application_email TEXT,
            status TEXT DEFAULT 'active', first_seen TEXT, last_seen TEXT,
            content_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS crawl_runs (
            crawl_run_id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER,
            started_at TEXT, completed_at TEXT, result TEXT,
            jobs_found INTEGER, error TEXT
        );
        CREATE TABLE IF NOT EXISTS job_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_job_id TEXT, event_type TEXT, event_time TEXT,
            old_value TEXT, new_value TEXT, crawl_run_id INTEGER
        );
    """)
    for name, decl in (
        ("record_id", "TEXT"), ("source_type", "TEXT"),
        ("source_provider", "TEXT"), ("source_listing_url", "TEXT"),
        ("missing_successful_runs", "INTEGER DEFAULT 0"),
        ("removed_at", "TEXT"), ("reopened_at", "TEXT"),
        ("last_changed_at", "TEXT"),
    ):
        _add(conn, "jobs", name, decl)
    for name, decl in (
        ("record_id", "TEXT"), ("monitor_url", "TEXT"),
        ("last_checked", "TEXT"), ("last_result", "TEXT"),
        ("last_error", "TEXT"),
    ):
        _add(conn, "job_sources", name, decl)
    for name, decl in (
        ("run_batch_id", "TEXT"), ("http_status", "TEXT"),
        ("error_type", "TEXT"), ("error_message", "TEXT"),
        ("snapshot_path", "TEXT"),
    ):
        _add(conn, "crawl_runs", name, decl)
    for name, decl in (
        ("source_id", "INTEGER"), ("changed_fields_json", "TEXT"),
        ("previous_values_json", "TEXT"), ("current_values_json", "TEXT"),
    ):
        _add(conn, "job_events", name, decl)
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_stage4_jobs_record ON jobs(record_id);
        CREATE INDEX IF NOT EXISTS idx_stage4_jobs_source_job ON jobs(source_job_id);
        CREATE INDEX IF NOT EXISTS idx_stage4_sources_record ON job_sources(record_id);
        CREATE INDEX IF NOT EXISTS idx_stage4_events_type ON job_events(event_type);
        CREATE INDEX IF NOT EXISTS idx_stage4_runs_batch ON crawl_runs(run_batch_id);
    """)


def load_baseline_jobs(conn: sqlite3.Connection, baseline_csv: Path | None = None):
    conn.row_factory = sqlite3.Row
    rows = {
        clean(row["canonical_job_id"]): dict(row)
        for row in conn.execute("""
            SELECT jobs.*, organizations.organization_name AS organization_name
              FROM jobs LEFT JOIN organizations
                ON organizations.organization_id=jobs.organization_id
        """)
    }
    if baseline_csv and baseline_csv.exists():
        for source in csv_rows(baseline_csv):
            cid = clean(source.get("canonical_job_id"))
            if cid not in rows:
                continue
            for field in ("record_id", "source_type", "source_provider", "source_listing_url"):
                if not clean(rows[cid].get(field)):
                    rows[cid][field] = clean(source.get(field))
    return rows


def read_source_states(conn: sqlite3.Connection):
    if "record_id" not in table_columns(conn, "job_sources"):
        return {}
    conn.row_factory = sqlite3.Row
    return {
        clean(row["record_id"]): dict(row)
        for row in conn.execute("SELECT * FROM job_sources WHERE record_id IS NOT NULL")
        if clean(row["record_id"])
    }


def backfill_baseline_metadata(conn: sqlite3.Connection, baseline_csv: Path | None) -> None:
    if not baseline_csv or not baseline_csv.exists():
        return
    for row in csv_rows(baseline_csv):
        conn.execute("""
            UPDATE jobs
               SET record_id=COALESCE(NULLIF(record_id,''), ?),
                   source_type=COALESCE(NULLIF(source_type,''), ?),
                   source_provider=COALESCE(NULLIF(source_provider,''), ?),
                   source_listing_url=COALESCE(NULLIF(source_listing_url,''), ?),
                   missing_successful_runs=COALESCE(missing_successful_runs,0)
             WHERE canonical_job_id=?
        """, (
            clean(row.get("record_id")), clean(row.get("source_type")),
            clean(row.get("source_provider")), clean(row.get("source_listing_url")),
            clean(row.get("canonical_job_id")),
        ))


def _organization(conn: sqlite3.Connection, name: str, domain: str, now: str):
    if not clean(name):
        return None
    row = conn.execute(
        "SELECT organization_id FROM organizations WHERE organization_name=? ORDER BY organization_id LIMIT 1",
        (clean(name),),
    ).fetchone()
    if row:
        conn.execute("UPDATE organizations SET last_checked=? WHERE organization_id=?", (now, row[0]))
        return int(row[0])
    cursor = conn.execute(
        "INSERT INTO organizations (organization_name,canonical_domain,first_seen,last_checked) VALUES (?,?,?,?)",
        (clean(name), clean(domain), now, now),
    )
    return int(cursor.lastrowid)


def _upsert_source(conn: sqlite3.Connection, row: Mapping[str, Any], now: str) -> int:
    rid = clean(row.get("record_id"))
    listing = clean(row.get("source_listing_url") or row.get("monitor_url"))
    provider = clean(row.get("source_provider"))
    org_id = _organization(conn, clean(row.get("organization_name")),
                           urlparse(listing).netloc if listing else provider, now)
    existing = conn.execute(
        "SELECT source_id FROM job_sources WHERE record_id=? ORDER BY source_id LIMIT 1", (rid,)
    ).fetchone()
    values = (
        org_id, clean(row.get("source_type")), provider, listing,
        clean(row.get("monitor_url")) or listing, clean(row.get("source_api_url")), now,
    )
    if existing:
        source_id = int(existing[0])
        conn.execute("""
            UPDATE job_sources SET organization_id=?,source_type=?,source_provider=?,
                listing_url=?,monitor_url=?,api_url=?,last_checked=? WHERE source_id=?
        """, (*values, source_id))
        return source_id
    cursor = conn.execute("""
        INSERT INTO job_sources
        (organization_id,record_id,source_type,source_provider,listing_url,
         monitor_url,api_url,source_status,last_checked,consecutive_failures)
        VALUES (?,?,?,?,?,?,?,'unreviewed',?,0)
    """, (org_id, rid, *values[1:]))
    return int(cursor.lastrowid)


def apply_commit(conn: sqlite3.Connection, comparison: ComparisonResult,
                 status_rows: Sequence[Mapping[str, Any]], *,
                 run_batch_id: str, snapshot_path: Path, now: str) -> None:
    """Apply one monitoring batch. Caller owns the surrounding transaction."""
    source_ids: dict[str, int] = {}
    crawl_ids: dict[str, int] = {}
    for row in status_rows:
        rid = clean(row.get("record_id"))
        if not rid:
            continue
        source_id = _upsert_source(conn, row, now)
        source_ids[rid] = source_id
        classification = classify_source(row)
        if classification == "success":
            conn.execute("""
                UPDATE job_sources SET source_status='active',last_successful_check=?,
                    last_checked=?,last_result=?,last_error='',consecutive_failures=0
                 WHERE source_id=?
            """, (now, now, clean(row.get("extraction_result")), source_id))
        elif classification == "failure":
            conn.execute("""
                UPDATE job_sources SET source_status='failed',last_checked=?,last_result=?,
                    last_error=?,consecutive_failures=COALESCE(consecutive_failures,0)+1
                 WHERE source_id=?
            """, (now, clean(row.get("extraction_result")),
                   clean(row.get("extraction_error") or row.get("extraction_reason")), source_id))
        else:
            conn.execute("""
                UPDATE job_sources SET source_status='indeterminate',last_checked=?,
                    last_result=?,last_error=''
                 WHERE source_id=?
            """, (now, clean(row.get("extraction_result")), source_id))
        cursor = conn.execute("""
            INSERT INTO crawl_runs
            (source_id,run_batch_id,started_at,completed_at,result,jobs_found,error,
             error_type,error_message,snapshot_path)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            source_id, run_batch_id, clean(row.get("extraction_checked_at_utc")) or now,
            now, clean(row.get("extraction_result")), safe_int(row.get("jobs_found")),
            clean(row.get("extraction_error")),
            "" if classification == "success" else classification,
            clean(row.get("extraction_error") or row.get("extraction_reason")), str(snapshot_path),
        ))
        crawl_ids[rid] = int(cursor.lastrowid)

    events_by_job: dict[str, list[dict[str, Any]]] = {}
    for event in comparison.events:
        events_by_job.setdefault(clean(event.get("canonical_job_id")), []).append(event)

    for cid, current in comparison.current_jobs.items():
        old_row = conn.execute("SELECT * FROM jobs WHERE canonical_job_id=?", (cid,)).fetchone()
        old = dict(old_row) if old_row else None
        rid = clean(current.get("record_id"))
        org_id = _organization(conn, clean(current.get("organization_name")),
                               clean(current.get("source_provider")), now)
        sid = clean(current.get("source_job_id") or current.get("job_id"))
        if old is None:
            _insert_job(conn, cid, org_id, rid, sid, current, now)
        else:
            _update_job(conn, cid, org_id, rid, sid, old, current,
                        events_by_job.get(cid, []), now)

    for event in comparison.events:
        event_type = clean(event.get("event_type"))
        cid, rid = clean(event.get("canonical_job_id")), clean(event.get("record_id"))
        if event_type in {"POSSIBLY_REMOVED", "REMOVED"}:
            missing = safe_int(event.get("missing_successful_runs"))
            conn.execute("""
                UPDATE jobs SET status=?,missing_successful_runs=?,removed_at=?
                 WHERE canonical_job_id=?
            """, ("removed" if event_type == "REMOVED" else "possibly_removed",
                   missing, now if event_type == "REMOVED" else "", cid))
        _insert_event(conn, event, source_ids.get(rid), crawl_ids.get(rid))

    for event in [*comparison.source_failures, *comparison.source_recoveries]:
        rid = clean(event.get("record_id"))
        _insert_event(conn, event, source_ids.get(rid), crawl_ids.get(rid), source_event=True)


def _insert_job(conn, cid, org_id, rid, sid, job, now):
    conn.execute("""
        INSERT INTO jobs
        (canonical_job_id,organization_id,record_id,source_job_id,source_type,
         source_provider,source_listing_url,title,normalized_title,location,
         normalized_location,country,region,city,work_arrangement,employment_type,
         salary_min,salary_max,currency,posted_date,closing_date,description,job_url,
         application_url,application_email,status,first_seen,last_seen,content_hash,
         missing_successful_runs,removed_at,reopened_at,last_changed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?)
    """, (
        cid, org_id, rid, sid, clean(job.get("source_type")), clean(job.get("source_provider")),
        clean(job.get("source_listing_url")), clean(job.get("title")),
        clean(job.get("normalized_title")), clean(job.get("location")),
        clean(job.get("normalized_location")), clean(job.get("country")),
        clean(job.get("region")), clean(job.get("city")), clean(job.get("work_arrangement")),
        clean(job.get("employment_type")), clean(job.get("salary_min")),
        clean(job.get("salary_max")), clean(job.get("currency")),
        clean(job.get("posted_date")), clean(job.get("closing_date")),
        clean(job.get("description")), clean(job.get("job_url")),
        clean(job.get("application_url")), clean(job.get("application_email")),
        clean(job.get("first_seen")) or now, now, clean(job.get("content_hash")),
        0, "", "", now,
    ))


def _update_job(conn, cid, org_id, rid, sid, old, job, events, now):
    merged = {field: clean(job.get(field)) or clean(old.get(field)) for field in TRACKED_FIELDS}
    reopened = now if clean(old.get("status")) == "removed" else clean(old.get("reopened_at"))
    changed = now if any(e.get("event_type") == "CHANGED" for e in events) else clean(old.get("last_changed_at"))
    conn.execute("""
        UPDATE jobs SET organization_id=?,record_id=COALESCE(NULLIF(?,''),record_id),
            source_job_id=COALESCE(NULLIF(?,''),source_job_id),
            source_type=COALESCE(NULLIF(?,''),source_type),
            source_provider=COALESCE(NULLIF(?,''),source_provider),
            source_listing_url=COALESCE(NULLIF(?,''),source_listing_url),
            title=?,normalized_title=?,location=?,normalized_location=?,country=?,region=?,
            city=?,work_arrangement=?,employment_type=?,salary_min=?,salary_max=?,currency=?,
            posted_date=?,closing_date=?,description=?,job_url=?,application_url=?,
            application_email=?,status='active',last_seen=?,content_hash=?,
            missing_successful_runs=0,removed_at='',reopened_at=?,last_changed_at=?
         WHERE canonical_job_id=?
    """, (
        org_id, rid, sid, clean(job.get("source_type")), clean(job.get("source_provider")),
        clean(job.get("source_listing_url")), *[merged[field] for field in TRACKED_FIELDS],
        now, clean(job.get("content_hash")) or clean(old.get("content_hash")),
        reopened, changed, cid,
    ))


def _insert_event(conn, event, source_id, crawl_id, source_event=False):
    payload = json.dumps(dict(event), ensure_ascii=False, sort_keys=True)
    conn.execute("""
        INSERT INTO job_events
        (canonical_job_id,source_id,event_type,event_time,old_value,new_value,crawl_run_id,
         changed_fields_json,previous_values_json,current_values_json)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        None if source_event else clean(event.get("canonical_job_id")) or None,
        source_id, clean(event.get("event_type")), clean(event.get("event_time")),
        clean(event.get("previous_values_json")), clean(event.get("current_values_json")),
        crawl_id, clean(event.get("changed_fields_json")) or "{}",
        clean(event.get("previous_values_json")) or "{}",
        payload if source_event else clean(event.get("current_values_json")) or "{}",
    ))
