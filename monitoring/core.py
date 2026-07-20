from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse, urlunparse

TRACKED_FIELDS = (
    "title", "normalized_title", "location", "normalized_location",
    "country", "region", "city", "work_arrangement", "employment_type",
    "salary_min", "salary_max", "currency", "posted_date", "closing_date",
    "description", "job_url", "application_url", "application_email",
)

SUCCESS_RESULTS = {
    "jobs_extracted", "active_jobs_extracted", "confirmed_no_openings",
    "confirmed_career_page_no_openings", "confirmed_external_ats_no_openings",
    "confirmed_career_page_active", "confirmed_external_ats_active",
}
FAILURE_TOKENS = (
    "crawl_failed", "blocked", "challenge", "needs_manual_review",
    "unsupported", "error", "failed", "unknown_platform",
)
ZERO_SAFE_TYPES = {
    "ats", "public_job_api", "static_html_listing", "javascript_listing",
    "iframe_listing", "individual_job_pages", "downloadable_document",
    "email_application", "no_openings",
}


class MonitorError(RuntimeError):
    pass


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(clean(value) or default)
    except (TypeError, ValueError):
        return default


def normalized_url(value: str) -> str:
    value = clean(value)
    if not value:
        return ""
    try:
        parsed = urlparse(value)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return urlunparse((parsed.scheme.lower() or "https", host,
                           parsed.path.rstrip("/") or "/", "", parsed.query, ""))
    except Exception:
        return value.rstrip("/").lower()


def csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise MonitorError(f"Required file does not exist: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [{k: clean(v) for k, v in row.items()} for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    if not fields:
        fields = ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def source_is_successful(row: Mapping[str, Any]) -> bool:
    """Only return True when an empty result can safely imply job absence."""
    result = clean(row.get("extraction_result") or row.get("result")).lower()
    reason = clean(row.get("extraction_reason") or row.get("reason")).lower()
    error = clean(row.get("extraction_error") or row.get("error"))
    source_type = clean(row.get("source_type")).lower()
    jobs_found = safe_int(row.get("jobs_found"))
    if error or any(token in result for token in FAILURE_TOKENS):
        return False
    if jobs_found > 0 or result in SUCCESS_RESULTS:
        return True
    if result == "no_jobs_found":
        return (source_type in ZERO_SAFE_TYPES
                and "unknown ats" not in reason
                and "requires browser" not in reason)
    return False


def filter_sources(rows: list[dict[str, str]], *, limit: int = 0,
                   source_id: str = "", source_type: str = "",
                   provider: str = "") -> list[dict[str, str]]:
    result = rows
    if source_id:
        result = [r for r in result if clean(r.get("record_id")) == source_id]
    if source_type:
        result = [r for r in result if clean(r.get("source_type")).lower() == source_type.lower()]
    if provider:
        result = [r for r in result if clean(r.get("source_provider")).lower() == provider.lower()]
    return result[:limit] if limit > 0 else result


def changed_fields(old: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    changes: dict[str, dict[str, str]] = {}
    for name in TRACKED_FIELDS:
        before, after = clean(old.get(name)), clean(current.get(name))
        if after and after != before:  # sparse snapshots never erase good data
            changes[name] = {"old": before, "new": after}
    return changes


def identity_indexes(existing: Mapping[str, Mapping[str, Any]]):
    by_source_id: dict[tuple[str, str], str] = {}
    by_url: dict[str, str] = {}
    for cid, job in existing.items():
        org = clean(job.get("organization_name"))
        sid = clean(job.get("source_job_id"))
        if sid:
            by_source_id[(org, sid)] = cid
        url = normalized_url(clean(job.get("job_url")))
        if url:
            by_url[url] = cid
    return by_source_id, by_url


def bind_identity(current: Mapping[str, Any], existing: Mapping[str, Mapping[str, Any]],
                  by_source_id: Mapping[tuple[str, str], str],
                  by_url: Mapping[str, str]) -> str:
    cid = clean(current.get("canonical_job_id"))
    if cid in existing:
        return cid
    sid = clean(current.get("source_job_id") or current.get("job_id"))
    org = clean(current.get("organization_name"))
    if sid and (org, sid) in by_source_id:
        return by_source_id[(org, sid)]
    url = normalized_url(clean(current.get("job_url")))
    return by_url.get(url, cid) if url else cid


@dataclass
class ComparisonResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    current_jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_failures: list[dict[str, Any]] = field(default_factory=list)
    source_recoveries: list[dict[str, Any]] = field(default_factory=list)
    successful_source_ids: set[str] = field(default_factory=set)
    failed_source_ids: set[str] = field(default_factory=set)


def compare_jobs(existing: Mapping[str, Mapping[str, Any]],
                 current_jobs: Sequence[dict[str, Any]],
                 status_rows: Sequence[Mapping[str, Any]], *,
                 confirm_removal_after: int = 2,
                 prior_source_states: Mapping[str, Mapping[str, Any]] | None = None,
                 now: str | None = None) -> ComparisonResult:
    now = now or utc_now()
    prior_source_states = prior_source_states or {}
    result = ComparisonResult()

    for row in status_rows:
        rid = clean(row.get("record_id"))
        if not rid:
            continue
        if source_is_successful(row):
            result.successful_source_ids.add(rid)
            failures = safe_int(prior_source_states.get(rid, {}).get("consecutive_failures"))
            if failures:
                result.source_recoveries.append({
                    "record_id": rid, "event_type": "SOURCE_RECOVERED",
                    "event_time": now, "previous_failures": failures,
                    "result": clean(row.get("extraction_result")),
                })
        else:
            result.failed_source_ids.add(rid)
            result.source_failures.append({
                "record_id": rid,
                "organization_name": clean(row.get("organization_name")),
                "source_type": clean(row.get("source_type")),
                "source_provider": clean(row.get("source_provider")),
                "monitor_url": clean(row.get("source_listing_url") or row.get("monitor_url")),
                "event_type": "SOURCE_FAILED", "event_time": now,
                "result": clean(row.get("extraction_result")),
                "error": clean(row.get("extraction_error") or row.get("error")),
                "reason": clean(row.get("extraction_reason") or row.get("reason")),
            })

    by_sid, by_url = identity_indexes(existing)
    for raw in current_jobs:
        job = dict(raw)
        cid = bind_identity(job, existing, by_sid, by_url)
        job["canonical_job_id"] = cid
        result.current_jobs[cid] = job
        old = existing.get(cid)
        if old is None:
            result.events.append(_event("NEW", job, {}, now))
            continue
        if clean(old.get("status")) == "removed":
            result.events.append(_event("REOPENED", job, old, now))
        changes = changed_fields(old, job)
        if changes:
            event = _event("CHANGED", job, old, now)
            event["changed_fields_json"] = json.dumps(changes, ensure_ascii=False, sort_keys=True)
            event["previous_values_json"] = json.dumps(
                {field: clean(old.get(field)) for field in changes}, sort_keys=True)
            event["current_values_json"] = json.dumps(
                {field: clean(job.get(field)) for field in changes}, sort_keys=True)
            result.events.append(event)

    present = set(result.current_jobs)
    for cid, old in existing.items():
        rid = clean(old.get("record_id"))
        if cid in present or not rid or rid not in result.successful_source_ids:
            continue
        missing = safe_int(old.get("missing_successful_runs")) + 1
        event_type = "REMOVED" if missing >= confirm_removal_after else "POSSIBLY_REMOVED"
        event = _event(event_type, dict(old), old, now)
        event["missing_successful_runs"] = missing
        result.events.append(event)
    return result


def _event(event_type: str, current: Mapping[str, Any], old: Mapping[str, Any], now: str):
    return {
        **dict(current), "event_type": event_type, "event_time": now,
        "changed_fields_json": "{}",
        "previous_values_json": json.dumps(dict(old), ensure_ascii=False, sort_keys=True),
        "current_values_json": json.dumps(dict(current), ensure_ascii=False, sort_keys=True),
    }
