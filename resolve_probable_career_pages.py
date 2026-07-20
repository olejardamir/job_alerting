#!/usr/bin/env python3
"""
Stage 2A: Resolve probable employer career pages.

Input:
    probable_career_pages.csv

This pass does not process known ATS-review pages. It focuses on career landing
pages that were valid and career-related but did not expose job links during
Stage 1.

For each page it:
- scrolls the page to trigger lazy content;
- extracts links, iframes, button destinations, and ATS URLs embedded in HTML;
- scores likely job/career destinations;
- visits up to N strong candidates;
- determines a better monitor URL and page classification;
- saves every completed result incrementally to JSONL;
- exports a final CSV.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

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
    "jazzhr": ("jazz.co",),
    "rippling": ("ats.rippling.com",),
    "jobvite": ("jobvite.com",),
}

CAREER_TEXT = (
    "careers", "career opportunities", "job opportunities", "current openings",
    "open positions", "view jobs", "find jobs", "search jobs", "available jobs",
    "join our team", "work with us", "employment opportunities",
    "current vacancies", "apply now", "see openings",
    "emplois", "carrières", "offres d'emploi", "postes disponibles",
)

NO_OPENING_TEXT = (
    "no current openings", "no open positions", "no positions available",
    "no vacancies", "there are currently no", "we do not have any openings",
    "we don't have any openings", "no jobs found", "0 jobs",
    "aucun poste disponible", "aucune offre",
)

FALSE_POSITIVE_TEXT = (
    "career fair", "career services", "career centre", "career center",
    "career counselling", "career counseling", "career development award",
    "early career award", "career symposium", "student career",
    "alumni career", "scholarship", "conference", "congress",
)

BLOCK_TEXT = (
    "access denied", "verify you are human", "checking your browser",
    "attention required", "unusual traffic", "captcha", "request blocked",
)

BAD_LINK_TEXT = (
    "privacy", "terms", "cookie", "facebook", "instagram", "linkedin",
    "youtube", "twitter", "x.com", "contact us", "news", "blog",
    "accessibility", "sitemap",
)

JOB_TITLE_WORDS = (
    "engineer", "developer", "manager", "director", "analyst", "specialist",
    "coordinator", "administrator", "consultant", "technician", "designer",
    "scientist", "assistant", "associate", "representative", "supervisor",
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="probable_career_pages.csv")
    p.add_argument("--output", default="output/stage2_probable_resolved.csv")
    p.add_argument("--jsonl", default="output/stage2_probable_resolved.jsonl")
    p.add_argument("--limit", type=int, default=25, help="0 means all rows")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--tasks-per-minute", type=float, default=20)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--max-candidates", type=int, default=3)
    p.add_argument("--headful", action="store_true")
    return p.parse_args()


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def host(url: str) -> str:
    try:
        value = urlparse(url).netloc.lower()
        return value[4:] if value.startswith("www.") else value
    except Exception:
        return ""


def detect_ats(urls: list[str]) -> str:
    combined = "\n".join(urls).lower()
    for provider, patterns in ATS_PATTERNS.items():
        if any(pattern in combined for pattern in patterns):
            return provider
    return ""


def count_jobposting_objects(value: Any) -> int:
    if isinstance(value, dict):
        count = 1 if clean(value.get("@type")).lower() == "jobposting" else 0
        return count + sum(count_jobposting_objects(v) for v in value.values())
    if isinstance(value, list):
        return sum(count_jobposting_objects(v) for v in value)
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
            count += len(re.findall(r'"@type"\s*:\s*"JobPosting"', raw, re.I))
    return count


def contains_any(text: str, values: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(value in low for value in values)


def probable_job_link(text: str, href: str) -> bool:
    combined = f"{text} {href}".lower()
    path = urlparse(href).path.lower()
    if re.search(r"/(job|jobs|position|positions|vacancy|vacancies|requisition|posting)/", path):
        return True
    return contains_any(combined, JOB_TITLE_WORDS) or contains_any(
        combined,
        ("apply", "view job", "job details", "position details", "opening", "vacancy"),
    )


def candidate_score(text: str, url: str, source_url: str) -> int:
    low_text = text.lower()
    low_url = url.lower()
    score = 0

    if detect_ats([url]):
        score += 130

    strong = (
        "view jobs", "find jobs", "search jobs", "current openings",
        "open positions", "career opportunities", "job opportunities",
        "see openings", "available positions", "apply now",
    )
    if contains_any(low_text, strong):
        score += 90
    elif contains_any(low_text, CAREER_TEXT):
        score += 55

    if re.search(r"/(careers?|jobs?|employment|opportunities|vacancies)(/|$|\\?|#)", low_url):
        score += 60

    if host(url) != host(source_url):
        score += 10

    if contains_any(f"{low_text} {low_url}", BAD_LINK_TEXT):
        score -= 100

    if url.startswith(("mailto:", "tel:", "javascript:")):
        score -= 200

    return score


async def dismiss_common_cookie_banner(page) -> None:
    patterns = (
        re.compile(r"^(accept|accept all|allow all|agree|okay|ok)$", re.I),
        re.compile(r"accept.*cookies", re.I),
    )
    for pattern in patterns:
        try:
            locator = page.get_by_role("button", name=pattern)
            if await locator.count():
                await locator.first.click(timeout=1500)
                await page.wait_for_timeout(300)
                return
        except Exception:
            continue


async def scroll_page(page) -> None:
    for _ in range(4):
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(700)
        except Exception:
            break
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


async def extract_page(page) -> dict[str, Any]:
    await dismiss_common_cookie_banner(page)
    await scroll_page(page)

    try:
        title = clean(await page.title())
    except Exception:
        title = ""

    try:
        body = clean(await page.locator("body").inner_text(timeout=12_000))
    except Exception:
        body = ""

    try:
        anchors = await page.locator("a[href]").evaluate_all(
            """els => els.map(e => ({
                text: (e.innerText || e.textContent || '').trim(),
                url: e.href || ''
            }))"""
        )
    except Exception:
        anchors = []

    try:
        frames = await page.locator("iframe[src]").evaluate_all(
            """els => els.map(e => ({
                text: e.title || e.name || 'iframe',
                url: e.src || ''
            }))"""
        )
    except Exception:
        frames = []

    try:
        button_data = await page.locator("button, [role=button]").evaluate_all(
            """els => els.map(e => ({
                text: (e.innerText || e.textContent || '').trim(),
                url: e.getAttribute('data-href') ||
                     e.getAttribute('data-url') ||
                     e.getAttribute('formaction') ||
                     ''
            }))"""
        )
    except Exception:
        button_data = []

    try:
        html = await page.content()
    except Exception:
        html = ""

    embedded = []
    url_pattern = re.compile(
        r'https?://[^"\'<>\\s]+(?:careers?|jobs?|employment|'
        r'greenhouse|lever|workday|ashby|smartrecruiters|bamboohr|'
        r'dayforce|icims|taleo|oraclecloud|workable|recruitee|jobvite)[^"\'<>\\s]*',
        re.I,
    )
    for match in url_pattern.findall(html):
        embedded.append({"text": "embedded_url", "url": match.replace("&amp;", "&")})

    try:
        scripts = await page.locator('script[type="application/ld+json"]').all_text_contents()
    except Exception:
        scripts = []

    all_items = []
    seen = set()
    for item in [*anchors, *frames, *button_data, *embedded]:
        text = clean(item.get("text"))
        url = clean(item.get("url"))
        if not url:
            continue
        url = urljoin(page.url, url)
        if not url.startswith(("http://", "https://")):
            continue
        key = url.split("#", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        all_items.append({"text": text, "url": url})

    probable_jobs = [
        item for item in all_items if probable_job_link(item["text"], item["url"])
    ]

    return {
        "url": page.url,
        "title": title,
        "body": body,
        "items": all_items,
        "jsonld_jobs": count_jsonld_jobs(scripts),
        "job_links": probable_jobs,
        "ats": detect_ats([page.url, *[item["url"] for item in all_items]]),
    }


def classify_evidence(evidence: dict[str, Any]) -> tuple[str, str]:
    text = f'{evidence["title"]}\n{evidence["body"]}'.lower()
    ats = clean(evidence["ats"])
    jobs = int(evidence["jsonld_jobs"]) + len(evidence["job_links"])

    if contains_any(text, BLOCK_TEXT):
        return "blocked_or_challenge", "Access challenge detected"

    no_openings = contains_any(text, NO_OPENING_TEXT)
    career_signal = contains_any(text, CAREER_TEXT)
    false_signal = contains_any(text, FALSE_POSITIVE_TEXT)

    if ats and jobs > 0:
        return "confirmed_external_ats_active", f"{ats} with job records or job links"
    if ats and no_openings:
        return "confirmed_external_ats_no_openings", f"{ats} with explicit no-opening signal"
    if ats:
        return "external_ats_needs_platform_pass", f"{ats} detected but jobs need ATS-specific extraction"

    if evidence["jsonld_jobs"] > 0:
        return "confirmed_career_page_active", "JobPosting structured data detected"
    if career_signal and len(evidence["job_links"]) > 0:
        return "confirmed_career_page_active", "Career language and job links detected"
    if career_signal and no_openings:
        return "confirmed_career_page_no_openings", "Career page with explicit no-opening signal"
    if false_signal and jobs == 0:
        return "likely_false_positive", "Career-related service, event, award, or educational content"
    if career_signal:
        return "confirmed_career_landing_unresolved", "Valid career landing page; downstream job source not resolved"
    return "needs_manual_review", "No conclusive employer-career evidence"


def choose_candidates(items: list[dict[str, str]], source_url: str, max_candidates: int) -> list[dict[str, Any]]:
    scored = []
    for item in items:
        score = candidate_score(item["text"], item["url"], source_url)
        if score >= 45:
            scored.append({**item, "score": score})
    scored.sort(key=lambda item: (-item["score"], item["url"]))
    return scored[:max_candidates]


def load_rows(path: Path, offset: int, limit: int) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = [{k: clean(v) for k, v in row.items()} for row in csv.DictReader(f)]
    rows = rows[offset:]
    if limit > 0:
        rows = rows[:limit]
    return rows


def load_completed_ids(jsonl_path: Path) -> set[str]:
    completed = set()
    if not jsonl_path.exists():
        return completed
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
                completed.add(clean(item.get("record_id")))
            except Exception:
                continue
    return completed


def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
        f.flush()


def export_jsonl_to_csv(jsonl_path: Path, csv_path: Path) -> None:
    records = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    if not records:
        return

    fields = []
    seen = set()
    for record in records:
        for key in record:
            if key not in seen:
                seen.add(key)
                fields.append(key)

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


async def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    jsonl_path = Path(args.jsonl)

    rows = load_rows(input_path, args.offset, args.limit)
    completed_ids = load_completed_ids(jsonl_path)
    rows = [r for r in rows if clean(r.get("record_id")) not in completed_ids]

    if not rows:
        print("No unprocessed rows selected.")
        if jsonl_path.exists():
            export_jsonl_to_csv(jsonl_path, output_path)
        return

    requests = []
    for row in rows:
        start_url = clean(row.get("loaded_url")) or clean(row.get("career_url"))
        if not start_url.startswith(("http://", "https://")):
            continue
        record_id = clean(row.get("record_id"))
        requests.append(
            Request.from_url(
                start_url,
                unique_key=f"stage2-probable:{record_id}:{start_url}",
                user_data={"source": row},
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
    async def handler(context: PlaywrightCrawlingContext) -> None:
        source = dict(context.request.user_data["source"])
        page = context.page

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        await page.wait_for_timeout(1_500)

        initial = await extract_page(page)
        initial_result, initial_reason = classify_evidence(initial)
        candidates = choose_candidates(initial["items"], initial["url"], args.max_candidates)

        visited = []
        best = {
            "result": initial_result,
            "reason": initial_reason,
            "url": initial["url"],
            "ats": initial["ats"],
            "jsonld_jobs": initial["jsonld_jobs"],
            "job_link_count": len(initial["job_links"]),
            "sample_job_links": [item["url"] for item in initial["job_links"][:5]],
        }

        preferred_results = {
            "confirmed_external_ats_active": 100,
            "confirmed_career_page_active": 95,
            "confirmed_external_ats_no_openings": 90,
            "confirmed_career_page_no_openings": 85,
            "external_ats_needs_platform_pass": 70,
            "confirmed_career_landing_unresolved": 60,
            "likely_false_positive": 20,
            "blocked_or_challenge": 10,
            "needs_manual_review": 0,
        }

        for candidate in candidates:
            if candidate["url"].split("#", 1)[0] == initial["url"].split("#", 1)[0]:
                continue
            try:
                await page.goto(
                    candidate["url"],
                    wait_until="domcontentloaded",
                    timeout=args.timeout * 1000,
                )
                await page.wait_for_timeout(1_500)
                evidence = await extract_page(page)
                result, reason = classify_evidence(evidence)
                visited.append({
                    "url": evidence["url"],
                    "result": result,
                    "ats": evidence["ats"],
                    "score": candidate["score"],
                })
                if preferred_results.get(result, 0) > preferred_results.get(best["result"], 0):
                    best = {
                        "result": result,
                        "reason": reason,
                        "url": evidence["url"],
                        "ats": evidence["ats"],
                        "jsonld_jobs": evidence["jsonld_jobs"],
                        "job_link_count": len(evidence["job_links"]),
                        "sample_job_links": [item["url"] for item in evidence["job_links"][:5]],
                    }
                if preferred_results.get(best["result"], 0) >= 90:
                    break
            except Exception as exc:
                visited.append({
                    "url": candidate["url"],
                    "result": "candidate_visit_failed",
                    "ats": detect_ats([candidate["url"]]),
                    "score": candidate["score"],
                    "error": f"{type(exc).__name__}: {exc}",
                })

        output = {
            **source,
            "stage2_checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "stage2_result": best["result"],
            "stage2_reason": best["reason"],
            "resolved_monitor_url": best["url"],
            "resolved_ats": best["ats"],
            "stage2_jsonld_jobposting_count": best["jsonld_jobs"],
            "stage2_probable_job_link_count": best["job_link_count"],
            "stage2_sample_job_links": " | ".join(best["sample_job_links"]),
            "candidate_urls_found": " | ".join(
                f'{item["score"]}:{item["url"]}' for item in candidates
            ),
            "candidate_visits_json": json.dumps(visited, ensure_ascii=False),
            "stage2_error": "",
        }
        append_jsonl(jsonl_path, output)

    @crawler.failed_request_handler
    async def failed(context: BasicCrawlingContext, error: Exception) -> None:
        source = dict(context.request.user_data["source"])
        output = {
            **source,
            "stage2_checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "stage2_result": "crawl_failed",
            "stage2_reason": "Stage 2 page and retries failed",
            "resolved_monitor_url": clean(context.request.loaded_url) or context.request.url,
            "resolved_ats": "",
            "stage2_jsonld_jobposting_count": 0,
            "stage2_probable_job_link_count": 0,
            "stage2_sample_job_links": "",
            "candidate_urls_found": "",
            "candidate_visits_json": "[]",
            "stage2_error": f"{type(error).__name__}: {error}",
        }
        append_jsonl(jsonl_path, output)

    await crawler.run(requests)
    export_jsonl_to_csv(jsonl_path, output_path)
    print(f"Wrote {output_path}")
    print(f"Incremental recovery file: {jsonl_path}")


if __name__ == "__main__":
    asyncio.run(main())
