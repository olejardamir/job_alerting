from __future__ import annotations

import json
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

from .core import TRACKED_FIELDS, MonitorError, clean, utc_now


def canonicalize_snapshot(raw_jobs: list[dict[str, str]]):
    """Apply the same normalization helpers used by Stage 3A."""
    try:
        from canonicalize_jobs import (
            SOURCE_PREFERENCE, check_quality, clean_title, content_hash,
            detect_work_arrangement, generate_canonical_id,
            infer_country_from_url, normalized, parse_location,
        )
    except Exception as exc:  # pragma: no cover
        raise MonitorError(f"Could not import canonicalize_jobs.py: {exc}") from exc

    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    now = utc_now()
    for source in raw_jobs:
        job: dict[str, Any] = dict(source)
        passed, reason, flags = check_quality(job)
        if not passed:
            job["rejection_reason"] = reason
            rejected.append(job)
            continue

        title, location_hint = clean_title(clean(job.get("title")))
        raw_location = clean(job.get("location")) or location_hint
        country, region, city, normalized_location = parse_location(raw_location)
        if not country:
            country = infer_country_from_url(clean(job.get("job_url")))
        arrangement = clean(job.get("work_arrangement")) or detect_work_arrangement(
            f"{title} {raw_location} {clean(job.get('description'))}"
        )
        provider = clean(job.get("source_provider"))
        if not provider:
            provider = urlparse(clean(job.get("job_url"))).netloc
        canonical_id = generate_canonical_id(
            clean(job.get("organization_name")), title, normalized_location,
            provider, clean(job.get("job_id")), clean(job.get("job_url")),
        )
        job.update({
            "canonical_job_id": canonical_id,
            "source_job_id": clean(job.get("job_id")),
            "title": title,
            "normalized_title": normalized(title),
            "location": raw_location,
            "normalized_location": normalized_location,
            "country": country,
            "region": region,
            "city": city,
            "work_arrangement": arrangement,
            "status": "active",
            "first_seen": clean(job.get("first_seen")) or now,
            "last_seen": now,
            "content_hash": content_hash(clean(job.get("description"))),
            "quality_flags_json": json.dumps(flags, sort_keys=True),
        })
        candidates.append(job)

    def rank(job: dict[str, Any]):
        preference = SOURCE_PREFERENCE.get(clean(job.get("source_type")), 99)
        completeness = sum(bool(clean(job.get(field))) for field in TRACKED_FIELDS)
        return (preference, -completeness, -len(clean(job.get("description"))))

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in candidates:
        groups[clean(job.get("canonical_job_id"))].append(job)
    return [sorted(group, key=rank)[0] for group in groups.values()], rejected
