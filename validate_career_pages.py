#!/usr/bin/env python3
"""
Stage 1: Validate URLs from career_validation.csv.

This script:
- reads candidate career URLs from the CSV;
- opens each URL with Crawlee + Playwright;
- preserves the original CSV columns;
- records redirects, status codes, page signals, ATS detection, and classification;
- writes one output CSV.

It does NOT yet crawl every individual job description.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from crawlee import ConcurrencySettings, Request
from crawlee.crawlers import (
    BasicCrawlingContext,
    PlaywrightCrawler,
    PlaywrightCrawlingContext,
)


ATS_PATTERNS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
    "workday": ("myworkdayjobs.com", "workdayjobs.com"),
    "ashby": ("ashbyhq.com",),
    "smartrecruiters": ("smartrecruiters.com",),
    "bamboohr": ("bamboohr.com",),
    "dayforce": ("dayforcehcm.com", "dayforce.com"),
    "icims": ("icims.com",),
    "taleo": ("taleo.net",),
    "oracle": ("oraclecloud.com",),
    "adp": ("workforcenow.adp.com",),
    "successfactors": ("successfactors.com",),
    "workable": ("workable.com",),
    "recruitee": ("recruitee.com",),
    "ukg": ("ukg.com",),
    "applytojob": ("applytojob.com",),
    "jazzhr": ("applytojob.com", "jazz.co"),
    "rippling": ("ats.rippling.com",),
    "jobvite": ("jobvite.com",),
}

BLOCK_MARKERS = (
    "access denied",
    "verify you are human",
    "checking your browser",
    "attention required",
    "unusual traffic",
    "captcha",
    "temporarily blocked",
    "request blocked",
    "enable javascript and cookies to continue",
)

NO_OPENING_MARKERS = (
    "no current openings",
    "no open positions",
    "no positions available",
    "no vacancies",
    "there are currently no",
    "we do not have any openings",
    "we don't have any openings",
    "no jobs found",
    "0 jobs",
    "aucun poste disponible",
    "aucune offre",
)

FALSE_POSITIVE_MARKERS = (
    "career fair",
    "career services",
    "career centre",
    "career center",
    "career counselling",
    "career counseling",
    "career development award",
    "early career award",
    "career symposium",
    "student career",
    "alumni career",
    "conference",
    "congress",
    "scholarship",
    "fellowship award",
)

CAREER_MARKERS = (
    "careers",
    "career opportunities",
    "job opportunities",
    "open positions",
    "join our team",
    "work with us",
    "employment opportunities",
    "current vacancies",
    "emplois",
    "carrières",
    "offres d'emploi",
    "possibilités d'emploi",
)

JOB_URL_PATTERNS = (
    "/job/",
    "/jobs/",
    "/position/",
    "/positions/",
    "/vacancy/",
    "/vacancies/",
    "/requisition/",
    "/posting/",
    "/job-details/",
    "/jobdetail/",
)


def jsonl_append(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def jsonl_to_csv(jsonl_path: Path, csv_path: Path) -> int:
    records: list[dict[str, str]] = []
    with jsonl_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    if not records:
        return 0
    fieldnames = list(records[0].keys())
    seen = set(fieldnames)
    for rec in records:
        for k in rec:
            if k not in seen:
                fieldnames.append(k)
                seen.add(k)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
    return len(records)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        default="career_validation.csv",
        help="Input CSV produced in the previous stage.",
    )
    parser.add_argument(
        "--output",
        default="output/step1_validated.csv",
        help="Output CSV path.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Number of rows to process. Use 0 for all rows.",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Skip this many data rows before processing.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Maximum simultaneously open pages.",
    )
    parser.add_argument(
        "--tasks-per-minute",
        type=float,
        default=30,
        help="Global maximum number of pages processed per minute.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Navigation timeout in seconds.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Show the Chromium windows for debugging.",
    )
    return parser.parse_args()


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def detect_ats(urls: list[str]) -> str:
    joined = "\n".join(urls).lower()
    for provider, patterns in ATS_PATTERNS.items():
        if any(pattern in joined for pattern in patterns):
            return provider
    return ""


def count_jobposting_objects(value: Any) -> int:
    if isinstance(value, dict):
        count = 1 if clean(value.get("@type")).lower() == "jobposting" else 0
        return count + sum(count_jobposting_objects(item) for item in value.values())
    if isinstance(value, list):
        return sum(count_jobposting_objects(item) for item in value)
    return 0


def count_jsonld_jobs(raw_scripts: list[str]) -> int:
    count = 0
    for raw in raw_scripts:
        raw = raw.strip()
        if not raw:
            continue
        try:
            count += count_jobposting_objects(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            # Some websites place malformed or multiple JSON objects in one block.
            count += len(re.findall(r'"@type"\s*:\s*"JobPosting"', raw, re.I))
    return count


def is_probable_job_link(text: str, href: str) -> bool:
    combined = f"{text} {href}".lower()
    if any(pattern in href.lower() for pattern in JOB_URL_PATTERNS):
        return True
    return bool(
        re.search(
            r"\b(apply|view job|job details|position details|opening|vacancy|"
            r"software engineer|developer|manager|director|analyst|specialist|"
            r"coordinator|administrator|consultant|technician|designer)\b",
            combined,
            re.I,
        )
    )


def classify_page(
    *,
    body_text: str,
    title: str,
    final_url: str,
    ats_provider: str,
    jsonld_job_count: int,
    job_link_count: int,
) -> tuple[str, str]:
    text = f"{title}\n{body_text}".lower()
    path = urlparse(final_url).path.lower()
    path_segments = [segment for segment in path.split("/") if segment]

    if any(marker in text for marker in BLOCK_MARKERS):
        return "blocked_or_challenge", "Page contains an access challenge or bot-block marker"

    no_openings = any(marker in text for marker in NO_OPENING_MARKERS)
    false_positive = any(marker in text for marker in FALSE_POSITIVE_MARKERS)
    career_signal = any(marker in text for marker in CAREER_MARKERS)
    likely_single_job_url = (
        len(path_segments) >= 2
        and any(pattern in path for pattern in JOB_URL_PATTERNS)
        and not path.rstrip("/").endswith(("/jobs", "/careers", "/positions", "/vacancies"))
    )

    if jsonld_job_count > 0 and (jsonld_job_count == 1 or likely_single_job_url):
        return "single_job_or_job_detail", "JobPosting structured data or individual-job URL detected"

    if ats_provider:
        if no_openings:
            return "confirmed_external_ats_no_openings", f"{ats_provider} page with no-opening signal"
        if jsonld_job_count > 0 or job_link_count > 0:
            return "confirmed_external_ats_active", f"{ats_provider} page with job records or job links"
        return "external_ats_needs_review", f"{ats_provider} detected, but no jobs were confidently identified"

    if false_positive and jsonld_job_count == 0 and job_link_count == 0:
        return "likely_false_positive", "Career-related event, service, award, or educational content"

    if no_openings and career_signal:
        return "confirmed_career_page_no_openings", "Employer-career language and explicit no-opening signal"

    if career_signal and (jsonld_job_count > 0 or job_link_count > 0):
        return "confirmed_career_page_active", "Employer-career language plus job records or job links"

    if jsonld_job_count > 0:
        return "confirmed_job_content", "JobPosting structured data detected"

    if career_signal:
        return "probable_career_page_needs_review", "Career language detected, but job state is unclear"

    if job_link_count > 0:
        return "possible_job_listing_needs_review", "Job-like links detected without clear career-page language"

    return "needs_manual_review", "Insufficient evidence for automatic classification"


def load_rows(path: Path, offset: int, limit: int) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Input file does not exist: {path}")

    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    selected = rows[offset:]
    if limit > 0:
        selected = selected[:limit]

    usable: list[dict[str, str]] = []
    for row in selected:
        url = clean(row.get("career_url"))
        if not url.startswith(("http://", "https://")):
            continue
        usable.append({key: clean(value) for key, value in row.items()})
    return usable


async def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    jsonl_path = output_path.with_suffix(".jsonl")

    rows = load_rows(input_path, args.offset, args.limit)
    if not rows:
        raise RuntimeError("No usable career_url rows were selected.")

    requests: list[Request] = []
    for index, row in enumerate(rows, start=args.offset + 1):
        record_id = clean(row.get("record_id")) or str(index)
        url = clean(row["career_url"])
        requests.append(
            Request.from_url(
                url,
                # The record ID prevents Crawlee from collapsing duplicate URLs
                # that belong to different input rows.
                unique_key=f"career-validation:{record_id}:{url}",
                user_data={
                    "source_row_data": row,
                    "source_record_id": record_id,
                },
                max_retries=2,
            )
        )

    crawler = PlaywrightCrawler(
        headless=not args.headful,
        browser_type="chromium",
        browser_launch_options={"chromium_sandbox": False},
        max_request_retries=2,
        max_session_rotations=2,
        retry_on_blocked=True,
        navigation_timeout=timedelta(seconds=args.timeout),
        request_handler_timeout=timedelta(seconds=args.timeout),
        max_requests_per_crawl=len(requests),
        respect_robots_txt_file=False,
        concurrency_settings=ConcurrencySettings(
            min_concurrency=1,
            desired_concurrency=min(2, args.concurrency),
            max_concurrency=args.concurrency,
            max_tasks_per_minute=args.tasks_per_minute,
        ),
        browser_new_context_options={
            "locale": "en-CA",
            "timezone_id": "America/Toronto",
            "viewport": {"width": 1365, "height": 768},
        },
    )

    @crawler.router.default_handler
    async def validate(context: PlaywrightCrawlingContext) -> None:
        page = context.page
        source = dict(context.request.user_data["source_row_data"])

        # Give late JavaScript a brief opportunity to render jobs.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        await page.wait_for_timeout(1_500)

        final_url = page.url
        title = clean(await page.title())

        try:
            body_text = clean(await page.locator("body").inner_text(timeout=12_000))
        except Exception:
            body_text = ""

        try:
            raw_links = await page.locator("a[href]").evaluate_all(
                """elements => elements.map(a => ({
                    text: (a.innerText || a.textContent || '').trim(),
                    href: a.href || ''
                }))"""
            )
        except Exception:
            raw_links = []

        links: list[dict[str, str]] = [
            {"text": clean(link.get("text")), "href": clean(link.get("href"))}
            for link in raw_links
            if clean(link.get("href")).startswith(("http://", "https://"))
        ]

        try:
            jsonld_scripts = await page.locator(
                'script[type="application/ld+json"]'
            ).all_text_contents()
        except Exception:
            jsonld_scripts = []

        jsonld_job_count = count_jsonld_jobs(jsonld_scripts)
        job_links = [
            link for link in links if is_probable_job_link(link["text"], link["href"])
        ]
        ats_provider = detect_ats([final_url, *[link["href"] for link in links]])

        validation_result, validation_reason = classify_page(
            body_text=body_text,
            title=title,
            final_url=final_url,
            ats_provider=ats_provider,
            jsonld_job_count=jsonld_job_count,
            job_link_count=len(job_links),
        )

        status_code = context.response.status if context.response else None
        checked_at = datetime.now(timezone.utc).isoformat()

        record = {
            **source,
            "checked_at_utc": checked_at,
            "requested_url": context.request.url,
            "loaded_url": final_url,
            "http_status": status_code,
            "page_title": title[:500],
            "detected_ats": ats_provider,
            "jsonld_jobposting_count": jsonld_job_count,
            "probable_job_link_count": len(job_links),
            "sample_job_links": " | ".join(
                link["href"] for link in job_links[:5]
            ),
            "validation_result": validation_result,
            "validation_reason": validation_reason,
            "body_text_sample": body_text[:1500],
            "crawl_error": "",
        }
        jsonl_append(jsonl_path, record)
        await context.push_data(record)

    @crawler.failed_request_handler
    async def failed(
        context: BasicCrawlingContext, error: Exception
    ) -> None:
        source = dict(context.request.user_data["source_row_data"])
        record = {
            **source,
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "requested_url": context.request.url,
            "loaded_url": clean(context.request.loaded_url),
            "http_status": "",
            "page_title": "",
            "detected_ats": "",
            "jsonld_jobposting_count": 0,
            "probable_job_link_count": 0,
            "sample_job_links": "",
            "validation_result": "crawl_failed",
            "validation_reason": "All configured retries failed",
            "body_text_sample": "",
            "crawl_error": f"{type(error).__name__}: {error}",
        }
        jsonl_append(jsonl_path, record)
        await context.push_data(record)

    await crawler.run(requests)
    await crawler.export_data(str(output_path))
    count = jsonl_to_csv(jsonl_path, output_path)
    print(f"Wrote {output_path} with {count} validated rows from {len(rows)} input rows.")


if __name__ == "__main__":
    asyncio.run(main())
