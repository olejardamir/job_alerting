#!/usr/bin/env python3
"""
Stage 2B — Network and interactive resolver for unresolved career landing pages.

Input:
    stage2b_unresolved_career_landings.csv

This pass:
- reloads each landing page while listening to XHR/fetch/document responses;
- identifies JSON responses that appear to contain jobs;
- detects a broader set of ATS and recruiting platforms;
- inspects forms, iframes, script sources, links, buttons, onclick attributes,
  and embedded URLs;
- clicks a small number of high-value career/job buttons without submitting forms;
- classifies passive application forms separately from job listings;
- writes every completed row immediately to JSONL;
- exports a final CSV.

It never submits an application, sends a form, logs in, or bypasses a challenge.
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


PLATFORM_PATTERNS: dict[str, tuple[str, ...]] = {
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
    "ukg_pro": ("ultipro.com", "ultipro.ca"),
    "paycor": ("recruitingbypaycor.com",),
    "njoyn": ("njoyn.com",),
    "phenom": ("phenompeople.com",),
    "workland": ("workland.com",),
    "darwinbox": ("darwinbox.com", "darwinbox.in"),
    "breezy_hr": ("breezy.hr",),
    "hiringplatform": ("hiringplatform.ca",),
    "sutihr": ("sutihr.com",),
    "push_operations": ("pushoperations.com",),
    "jobvite": ("jobvite.com",),
    "rippling": ("rippling.com",),
    "applytojob": ("applytojob.com",),
    "careerplug": ("careerplug.com",),
    "paylocity": ("recruiting.paylocity.com",),
    "teamworkonline": ("teamworkonline.com",),
}

JOB_BOARD_PATTERNS: dict[str, tuple[str, ...]] = {
    "indeed": ("indeed.com", "indeed.ca"),
    "jobillico": ("jobillico.com",),
    "linkedin": ("linkedin.com/jobs",),
    "ziprecruiter": ("ziprecruiter.com",),
    "glassdoor": ("glassdoor.com", "glassdoor.ca"),
}

FORM_PATTERNS: dict[str, tuple[str, ...]] = {
    "jotform": ("jotform.com",),
    "typeform": ("typeform.com",),
    "formstack": ("formstack.com",),
    "wufoo": ("wufoo.com",),
    "google_forms": ("docs.google.com/forms",),
    "microsoft_forms": ("forms.office.com",),
}

CAREER_MARKERS = (
    "careers", "career opportunities", "job opportunities", "current openings",
    "open positions", "view jobs", "find jobs", "search jobs", "available jobs",
    "join our team", "work with us", "employment opportunities",
    "current vacancies", "apply now", "see openings", "browse jobs",
    "emplois", "carrières", "offres d'emploi", "postes disponibles",
)

NO_OPENING_MARKERS = (
    "no current openings", "no open positions", "no positions available",
    "no vacancies", "there are currently no", "we do not have any openings",
    "we don't have any openings", "no jobs found", "0 jobs",
    "aucun poste disponible", "aucune offre",
)

PASSIVE_APPLICATION_MARKERS = (
    "submit your resume", "send us your resume", "general application",
    "future opportunities", "future consideration", "talent community",
    "join our talent network", "expression of interest", "spontaneous application",
    "candidature spontanée", "déposer votre cv", "submit a resume",
)

BLOCK_MARKERS = (
    "access denied", "verify you are human", "checking your browser",
    "attention required", "unusual traffic", "captcha", "request blocked",
)

FALSE_POSITIVE_MARKERS = (
    "career fair", "career services", "career centre", "career center",
    "career counselling", "career counseling", "career development award",
    "early career award", "career symposium", "student career",
    "alumni career", "scholarship", "conference", "congress",
)

JOB_KEYS = {
    "job", "jobs", "jobid", "job_id", "jobtitle", "job_title",
    "position", "positions", "positiontitle", "posting", "postings",
    "requisition", "requisitions", "requisitionid", "requisition_id",
    "vacancy", "vacancies", "opening", "openings",
    "location", "locations", "department", "employmenttype",
}

JOB_URL_WORDS = (
    "/jobs", "/job", "/careers", "/career", "/positions", "/position",
    "/requisitions", "/requisition", "/vacancies", "/vacancy",
    "jobsearch", "searchjobs", "job-list", "job_list", "openings",
)

NOISE_URL_PATTERNS = (
    "googleapis.com/$rpc",
    "googleapis.com/maps",
    "google.com/maps",
    "parastorage.com",
    "enzuzo.com",
    "cookie",
    "analytics",
    "gtm.js",
    "gtag",
    "doubleclick.net",
    "googletagmanager",
    "facebook.net/tr",
    "hotjar.com",
    "clarity.ms",
    "sentry.io",
    "newrelic.com",
    "segment.io",
    "segment.com",
    "mixpanel.com",
    "amplitude.com",
    "stats.wp.com",
    "bat.bing.com",
)

BUTTON_TEXT = re.compile(
    r"(view|search|find|browse|see|current|open|available).{0,25}"
    r"(jobs?|positions?|openings?|vacancies)|"
    r"(jobs?|positions?|openings?|vacancies).{0,25}"
    r"(view|search|find|browse|see|current|open|available)",
    re.I,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="stage2b_unresolved_career_landings.csv")
    parser.add_argument("--output", default="output/stage2b_resolved_all.csv")
    parser.add_argument("--jsonl", default="output/stage2b_resolved_all.jsonl")
    parser.add_argument("--limit", type=int, default=10, help="0 means all rows")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--tasks-per-minute", type=float, default=10)
    parser.add_argument("--timeout", type=int, default=75)
    parser.add_argument("--max-clicks", type=int, default=2)
    parser.add_argument("--headful", action="store_true")
    return parser.parse_args()


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def host(url: str) -> str:
    try:
        value = urlparse(url).netloc.lower()
        return value[4:] if value.startswith("www.") else value
    except Exception:
        return ""


def detect_named(urls: list[str], patterns: dict[str, tuple[str, ...]]) -> str:
    combined = "\n".join(urls).lower()
    for name, values in patterns.items():
        if any(value in combined for value in values):
            return name
    return ""


def contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in markers)


def recursive_job_score(value: Any, depth: int = 0) -> int:
    if depth > 8:
        return 0
    score = 0
    if isinstance(value, dict):
        lower_keys = {str(key).lower().replace("-", "").replace(" ", "") for key in value.keys()}
        normalized_job_keys = {key.replace("_", "") for key in JOB_KEYS}
        matched = len(lower_keys & normalized_job_keys)
        score += matched * 8
        type_value = clean(value.get("@type")).lower()
        if type_value == "jobposting":
            score += 80
        score += sum(recursive_job_score(item, depth + 1) for item in value.values())
    elif isinstance(value, list):
        if len(value) >= 2:
            score += min(len(value), 20)
        score += sum(recursive_job_score(item, depth + 1) for item in value[:50])
    return score


def response_score(url: str, content_type: str, text: str) -> tuple[int, int]:
    score = 0
    structured_score = 0
    lower_url = url.lower()
    lower_text = text.lower()

    if any(pattern in lower_url for pattern in NOISE_URL_PATTERNS):
        return 0, 0

    if "json" in content_type.lower():
        score += 20

    if any(word in lower_url for word in JOB_URL_WORDS):
        score += 35

    platform = detect_named([url], PLATFORM_PATTERNS)
    if platform:
        score += 80

    if '"jobposting"' in lower_text or '"@type":"jobposting"' in lower_text.replace(" ", ""):
        score += 80

    try:
        parsed = json.loads(text)
        structured_score = recursive_job_score(parsed)
        score += min(structured_score, 120)
    except Exception:
        job_terms = len(re.findall(
            r"\b(job|position|requisition|vacancy|opening|location|department)\b",
            lower_text,
        ))
        score += min(job_terms, 30)

    return score, structured_score


def unique_urls(values: list[str]) -> list[str]:
    seen = set()
    output = []
    for value in values:
        value = clean(value).replace("&amp;", "&")
        if not value:
            continue
        if not value.startswith(("http://", "https://")):
            continue
        key = value.split("#", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        handle.flush()


def load_completed(path: Path) -> set[str]:
    completed = set()
    if not path.exists():
        return completed
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                completed.add(clean(json.loads(line).get("record_id")))
            except Exception:
                continue
    return completed


def export_jsonl(jsonl_path: Path, csv_path: Path) -> None:
    records = []
    with jsonl_path.open(encoding="utf-8") as handle:
        for line in handle:
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
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def load_rows(path: Path, offset: int, limit: int) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = [{key: clean(value) for key, value in row.items()} for row in csv.DictReader(handle)]
    rows = rows[offset:]
    if limit > 0:
        rows = rows[:limit]
    return rows


async def dismiss_cookie_banner(page) -> None:
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
    for _ in range(5):
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(650)
        except Exception:
            break
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


async def extract_static_evidence(page) -> dict[str, Any]:
    await dismiss_cookie_banner(page)
    await scroll_page(page)

    try:
        title = clean(await page.title())
    except Exception:
        title = ""

    try:
        body = clean(await page.locator("body").inner_text(timeout=12_000))
    except Exception:
        body = ""

    js = """els => els.map(e => ({
        text: (e.innerText || e.textContent || e.value || '').trim(),
        href: e.href || '',
        src: e.src || '',
        action: e.action || '',
        dataHref: e.getAttribute('data-href') || '',
        dataUrl: e.getAttribute('data-url') || '',
        formAction: e.getAttribute('formaction') || '',
        onclick: e.getAttribute('onclick') || ''
    }))"""

    try:
        elements = await page.locator(
            "a[href], iframe[src], form[action], script[src], "
            "button, [role=button], [data-href], [data-url], [onclick]"
        ).evaluate_all(js)
    except Exception:
        elements = []

    try:
        html = await page.content()
    except Exception:
        html = ""

    raw_urls = [page.url]
    button_texts = []
    for item in elements:
        button_texts.append(clean(item.get("text")))
        for key in ("href", "src", "action", "dataHref", "dataUrl", "formAction"):
            value = clean(item.get(key))
            if value:
                raw_urls.append(urljoin(page.url, value))
        onclick = clean(item.get("onclick"))
        raw_urls.extend(
            urljoin(page.url, match)
            for match in re.findall(r"""https?://[^"'\\s<>]+|/[^"'\\s<>]+""", onclick)
        )

    raw_urls.extend(
        match.replace("\\/", "/")
        for match in re.findall(r'https?:(?:\\\\/\\\\/|//)[^"\'<>\\s]+', html)
    )
    urls = unique_urls(raw_urls)

    return {
        "url": page.url,
        "title": title,
        "body": body,
        "urls": urls,
        "button_texts": [text for text in button_texts if text],
        "platform": detect_named(urls, PLATFORM_PATTERNS),
        "job_board": detect_named(urls, JOB_BOARD_PATTERNS),
        "form_provider": detect_named(urls, FORM_PATTERNS),
    }


async def click_high_value_buttons(page, max_clicks: int) -> list[dict[str, str]]:
    results = []
    try:
        locator = page.locator("button, [role=button], a")
        count = min(await locator.count(), 250)
    except Exception:
        return results

    candidates = []
    for index in range(count):
        element = locator.nth(index)
        try:
            text = clean(await element.inner_text(timeout=400))
        except Exception:
            continue
        if not text or not BUTTON_TEXT.search(text):
            continue
        candidates.append((index, text))

    for index, text in candidates[:max_clicks]:
        before = page.url
        try:
            element = locator.nth(index)
            await element.scroll_into_view_if_needed(timeout=1500)
            await element.click(timeout=3000)
            await page.wait_for_timeout(1800)
            after = page.url
            results.append({"button_text": text, "before_url": before, "after_url": after})
            if after != before:
                break
        except Exception as exc:
            results.append({
                "button_text": text,
                "before_url": before,
                "after_url": page.url,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return results


def decide_result(
    static: dict[str, Any],
    response_candidates: list[dict[str, Any]],
    click_results: list[dict[str, str]],
) -> tuple[str, str, str, str, str]:
    combined_text = f'{static["title"]}\n{static["body"]}'.lower()
    all_urls = list(static["urls"])
    all_urls.extend(item["url"] for item in response_candidates)
    all_urls.extend(item.get("after_url", "") for item in click_results)

    platform = detect_named(all_urls, PLATFORM_PATTERNS)
    job_board = detect_named(all_urls, JOB_BOARD_PATTERNS)
    form_provider = detect_named(all_urls, FORM_PATTERNS)

    strong_responses = [item for item in response_candidates if item["score"] >= 100]
    best_api_url = strong_responses[0]["url"] if strong_responses else ""

    if contains_any(combined_text, BLOCK_MARKERS):
        return "blocked_or_challenge", "Access challenge detected", static["url"], platform, best_api_url

    if platform:
        return "resolved_external_ats", f"Recruiting platform detected: {platform}", (
            next((url for url in all_urls if detect_named([url], PLATFORM_PATTERNS) == platform), static["url"])
        ), platform, best_api_url

    if strong_responses:
        return "resolved_job_api", "Job-like JSON or network response detected", static["url"], "", best_api_url

    if contains_any(combined_text, NO_OPENING_MARKERS):
        return "confirmed_career_page_no_openings", "Explicit no-opening message", static["url"], "", ""

    passive_signal = contains_any(combined_text, PASSIVE_APPLICATION_MARKERS)
    if form_provider and passive_signal:
        return "passive_application_form_only", f"Passive application form detected: {form_provider}", (
            next((url for url in all_urls if detect_named([url], FORM_PATTERNS) == form_provider), static["url"])
        ), form_provider, ""

    if job_board:
        return "resolved_general_job_board", f"Third-party job board detected: {job_board}", (
            next((url for url in all_urls if detect_named([url], JOB_BOARD_PATTERNS) == job_board), static["url"])
        ), job_board, best_api_url

    if contains_any(combined_text, FALSE_POSITIVE_MARKERS):
        return "likely_false_positive", "Career-related service, event, or educational content", static["url"], "", ""

    if passive_signal:
        return "passive_application_only", "Page accepts general resumes but does not expose current jobs", static["url"], "", ""

    if contains_any(combined_text, CAREER_MARKERS):
        return "career_page_still_unresolved", "Career page confirmed, but no downstream listing or job API was found", static["url"], "", ""

    return "needs_manual_review", "Insufficient evidence after network and interactive inspection", static["url"], "", ""


async def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    jsonl_path = Path(args.jsonl)

    rows = load_rows(input_path, args.offset, args.limit)
    completed = load_completed(jsonl_path)
    rows = [row for row in rows if clean(row.get("record_id")) not in completed]

    if not rows:
        print("No unprocessed rows selected.")
        if jsonl_path.exists():
            export_jsonl(jsonl_path, output_path)
        return

    requests = []
    for row in rows:
        start_url = (
            clean(row.get("resolved_monitor_url"))
            or clean(row.get("loaded_url"))
            or clean(row.get("career_url"))
        )
        if not start_url.startswith(("http://", "https://")):
            continue
        record_id = clean(row.get("record_id"))
        requests.append(
            Request.from_url(
                start_url,
                unique_key=f"stage2b:{record_id}:{start_url}",
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
            desired_concurrency=1,
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

        response_candidates: list[dict[str, Any]] = []
        response_tasks: set[asyncio.Task] = set()

        async def inspect_response(response) -> None:
            try:
                request = response.request
                resource_type = clean(request.resource_type)
                content_type = clean(await response.header_value("content-type"))
                url = response.url

                if (
                    resource_type not in {"xhr", "fetch", "document"}
                    and "json" not in content_type.lower()
                ):
                    return

                text = await response.text()
                if not text:
                    return
                text = text[:1_500_000]
                score, structured_score = response_score(url, content_type, text)
                if score < 35:
                    return

                response_candidates.append({
                    "url": url,
                    "status": response.status,
                    "resource_type": resource_type,
                    "content_type": content_type,
                    "score": score,
                    "structured_job_score": structured_score,
                    "platform": detect_named([url], PLATFORM_PATTERNS),
                    "body_sample": text[:1200],
                })
            except Exception:
                return

        def on_response(response) -> None:
            task = asyncio.create_task(inspect_response(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        page.on("response", on_response)

        try:
            # Reload after the listener is attached so the initial network traffic is captured.
            await page.reload(wait_until="domcontentloaded", timeout=args.timeout * 1000)
        except Exception:
            pass

        await page.wait_for_timeout(2500)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        static = await extract_static_evidence(page)
        click_results = await click_high_value_buttons(page, args.max_clicks)
        await page.wait_for_timeout(1800)

        if response_tasks:
            await asyncio.gather(*list(response_tasks), return_exceptions=True)

        # Sort and deduplicate network candidates.
        unique = {}
        for item in response_candidates:
            key = item["url"]
            if key not in unique or item["score"] > unique[key]["score"]:
                unique[key] = item
        response_candidates = sorted(
            unique.values(),
            key=lambda item: (-item["score"], item["url"]),
        )[:25]

        result, reason, monitor_url, provider, api_url = decide_result(
            static,
            response_candidates,
            click_results,
        )

        output = {
            **source,
            "stage2b_checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "stage2b_result": result,
            "stage2b_reason": reason,
            "stage2b_monitor_url": monitor_url,
            "stage2b_provider": provider,
            "stage2b_job_api_url": api_url,
            "stage2b_network_candidate_count": len(response_candidates),
            "stage2b_network_candidates_json": json.dumps(response_candidates, ensure_ascii=False),
            "stage2b_static_urls": " | ".join(static["urls"][:40]),
            "stage2b_click_results_json": json.dumps(click_results, ensure_ascii=False),
            "stage2b_page_title": static["title"],
            "stage2b_error": "",
        }
        append_jsonl(jsonl_path, output)

    @crawler.failed_request_handler
    async def failed(context: BasicCrawlingContext, error: Exception) -> None:
        source = dict(context.request.user_data["source"])
        output = {
            **source,
            "stage2b_checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "stage2b_result": "crawl_failed",
            "stage2b_reason": "Stage 2B navigation and retries failed",
            "stage2b_monitor_url": clean(context.request.loaded_url) or context.request.url,
            "stage2b_provider": "",
            "stage2b_job_api_url": "",
            "stage2b_network_candidate_count": 0,
            "stage2b_network_candidates_json": "[]",
            "stage2b_static_urls": "",
            "stage2b_click_results_json": "[]",
            "stage2b_page_title": "",
            "stage2b_error": f"{type(error).__name__}: {error}",
        }
        append_jsonl(jsonl_path, output)

    await crawler.run(requests)
    export_jsonl(jsonl_path, output_path)
    print(f"Wrote {output_path}")
    print(f"Recovery file: {jsonl_path}")


if __name__ == "__main__":
    asyncio.run(main())
