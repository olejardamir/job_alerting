#!/usr/bin/env python3
"""
Stage 2C — Unified Job-Source Detection and Job Extraction

Inspects every confirmed career target and extracts jobs using the
appropriate adapter based on how the company publishes vacancies.

Source types:
  ats                    Workday, Greenhouse, Dayforce, iCIMS, etc.
  public_job_api         JSON, GraphQL or AJAX endpoint
  static_html_listing    Jobs in ordinary HTML
  javascript_listing     Dynamic JS rendering without recognized ATS
  iframe_listing         Embedded from another domain
  general_job_board      Indeed, Jobillico, LinkedIn company jobs
  individual_job_pages   One page per job
  downloadable_document  PDF or document with vacancies
  email_application      Email-based application method
  passive_application    General résumé submission
  no_openings            Explicit no-vacancy message
  unknown                Source not yet identified

Produces:
  job_sources.csv         — one row per target with source classification
  jobs_current.csv        — one row per job
  extraction_status.csv   — per-target extraction outcome
  platform_fingerprints.jsonl — discovered platform patterns
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs, urljoin

import httpx
from bs4 import BeautifulSoup, Tag


# ---------------------------------------------------------------------------
# Known ATS domains
# ---------------------------------------------------------------------------

ATS_DOMAINS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("greenhouse.io",),
    "lever": ("lever.co",),
    "ashby": ("ashbyhq.com",),
    "smartrecruiters": ("smartrecruiters.com",),
    "workable": ("workable.com",),
    "bamboohr": ("bamboohr.com",),
    "workday": ("myworkdayjobs.com", "workdayjobs.com"),
    "dayforce": ("dayforcehcm.com", "dayforce.com"),
    "icims": ("icims.com",),
    "adp": ("workforcenow.adp.com",),
    "oracle": ("oraclecloud.com",),
    "successfactors": ("successfactors.com",),
    "taleo": ("taleo.net",),
    "rippling": ("rippling.com",),
    "jobvite": ("jobvite.com",),
    "applytojob": ("applytojob.com",),
    "recruitee": ("recruitee.com",),
    "ukg_pro": ("ultipro.com", "ultipro.ca", "apply.ukg.com"),
    "paycor": ("recruitingbypaycor.com",),
    "njoyn": ("njoyn.com",),
    "phenom": ("phenompeople.com",),
    "workland": ("workland.com",),
    "breezy_hr": ("breezy.hr",),
    "darwinbox": ("darwinbox.com", "darwinbox.in"),
    "careerplug": ("careerplug.com",),
    "jazzhr": ("jazzhr.com",),
    "push_operations": ("pushoperations.com",),
    "teamworkonline": ("teamworkonline.com",),
    "sutihr": ("sutihr.com",),
}

JOB_BOARD_DOMAINS: dict[str, tuple[str, ...]] = {
    "indeed": ("indeed.com", "indeed.ca"),
    "jobillico": ("jobillico.com",),
    "linkedin": ("linkedin.com/jobs",),
    "ziprecruiter": ("ziprecruiter.com",),
    "glassdoor": ("glassdoor.com", "glassdoor.ca"),
}

PLATFORM_PATTERNS = {**ATS_DOMAINS, **JOB_BOARD_DOMAINS}

NOISE_URL_PATTERNS = (
    "googleapis.com/$rpc", "googleapis.com/maps", "google.com/maps",
    "parastorage.com", "enzuzo.com", "cookie", "analytics", "gtm.js",
    "gtag", "doubleclick.net", "googletagmanager", "facebook.net/tr",
    "hotjar.com", "clarity.ms", "sentry.io", "newrelic.com",
    "segment.io", "segment.com", "mixpanel.com", "amplitude.com",
    "stats.wp.com", "bat.bing.com",
)

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

JOB_URL_WORDS = (
    "/jobs", "/job", "/careers", "/career", "/positions", "/position",
    "/requisitions", "/requisition", "/vacancies", "/vacancy",
    "jobsearch", "searchjobs", "job-list", "job_list", "openings",
)

JOB_POSTING_ID_PATTERNS = (
    r"/jobs?/(\d+)", r"/job-detail/(\d+)", r"/requisitions?/(\d+)",
    r"/openings?/(\d+)", r"/apply/(\d+)", r"/posting/(\d+)",
)

JOB_TITLE_INDICATORS = re.compile(
    r"(engineer|developer|manager|analyst|coordinator|specialist|director|"
    r"administrator|technician|advisor|consultant|officer|lead|supervisor|"
    r"architect|designer|scientist|accountant|clerk|assistant|representative|"
    r"therapist|nurse|technologist|planner|controller|executive|associate)",
    re.I,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def host(url: str) -> str:
    try:
        value = urlparse(url).netloc.lower()
        return value[4:] if value.startswith("www.") else value
    except Exception:
        return ""


def contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in markers)


def detect_named(urls: list[str], patterns: dict[str, tuple[str, ...]]) -> str:
    combined = "\n".join(urls).lower()
    for name, values in patterns.items():
        if any(value in combined for value in values):
            return name
    return ""


def unique_urls(values: list[str]) -> list[str]:
    seen = set()
    output = []
    for value in values:
        value = clean(value).replace("&amp;", "&")
        if not value or not value.startswith(("http://", "https://")):
            continue
        key = value.split("#", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)


def _deduplicate_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    unique = []
    for j in jobs:
        key = (j.get("title", "").lower(), j.get("job_url", "").lower())
        if key not in seen:
            seen.add(key)
            unique.append(j)
    return unique


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 2C: Unified Job-Source Extraction")
    p.add_argument("--jsonl", default="output/stage2c_extraction_all.jsonl")
    p.add_argument("--output-dir", default="output")
    p.add_argument("--limit", type=int, default=0, help="0 = all records")
    p.add_argument("--timeout", type=int, default=25)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--source-id", default="", help="Filter to specific record_id")
    p.add_argument("--source-type", default="", help="Filter by source type (ats, static_html_listing, etc.)")
    p.add_argument("--provider", default="", help="Filter by ATS provider (greenhouse, lever, etc.)")
    p.add_argument("--sources-file", default="output/job_sources.csv",
                   help="Authoritative source inventory for pre-extraction filtering")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Input collection
# ---------------------------------------------------------------------------

def collect_targets() -> list[dict[str, str]]:
    """Collect every confirmed career target from Stages 1, 2A, and 2B."""
    seen: set[str] = set()
    targets: list[dict[str, str]] = []

    def _add(rid: str, org: str, monitor_url: str, source_stage: str,
             previous_result: str, detected_ats: str = "",
             detected_ats_provider: str = "", body_sample: str = "",
             jsonld_count: str = "", job_link_count: str = "",
             sample_job_links: str = ""):
        if rid in seen or not monitor_url.startswith(("http://", "https://")):
            return
        seen.add(rid)
        targets.append({
            "record_id": rid,
            "organization_name": org,
            "monitor_url": monitor_url,
            "source_stage": source_stage,
            "previous_result": previous_result,
            "detected_ats": detected_ats,
            "detected_ats_provider": detected_ats_provider,
            "body_sample": body_sample,
            "jsonld_jobposting_count": jsonld_count,
            "probable_job_link_count": job_link_count,
            "sample_job_links": sample_job_links,
        })

    # Stage 1: confirmed targets
    path1 = Path("output/career_pages_validated_all.csv")
    if path1.exists():
        with path1.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                vr = clean(row.get("validation_result"))
                if vr not in ("confirmed_career_page_active", "confirmed_external_ats_active",
                              "confirmed_career_page_no_openings", "confirmed_external_ats_no_openings"):
                    continue
                rid = clean(row.get("record_id"))
                url = clean(row.get("loaded_url")) or clean(row.get("career_url"))
                _add(rid, clean(row.get("organization_name_original")), url, "stage1", vr,
                     clean(row.get("detected_ats")), clean(row.get("ats_provider_detected")),
                     clean(row.get("body_text_sample")),
                     clean(row.get("jsonld_jobposting_count")),
                     clean(row.get("probable_job_link_count")),
                     clean(row.get("sample_job_links")))

    # Stage 2A: confirmed targets
    path2 = Path("output/stage2_probable_resolved_all.csv")
    if path2.exists():
        with path2.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                s2r = clean(row.get("stage2_result"))
                if s2r not in ("confirmed_career_page_active", "confirmed_external_ats_active",
                               "confirmed_career_page_no_openings", "confirmed_external_ats_no_openings"):
                    continue
                rid = clean(row.get("record_id"))
                url = clean(row.get("resolved_monitor_url")) or clean(row.get("career_url"))
                _add(rid, clean(row.get("organization_name_original")), url, "stage2a", s2r,
                     "", clean(row.get("resolved_ats")),
                     clean(row.get("body_text_sample")),
                     clean(row.get("stage2_jsonld_jobposting_count")),
                     clean(row.get("stage2_probable_job_link_count")),
                     clean(row.get("stage2_sample_job_links")))

    # Stage 2B: resolved targets
    path3 = Path("output/stage2b_resolved_all.csv")
    if path3.exists():
        with path3.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                s2br = clean(row.get("stage2b_result"))
                if s2br not in ("resolved_job_api", "resolved_external_ats",
                                "resolved_general_job_board", "confirmed_career_page_no_openings"):
                    continue
                rid = clean(row.get("record_id"))
                url = (clean(row.get("stage2b_job_api_url"))
                       or clean(row.get("stage2b_monitor_url"))
                       or clean(row.get("resolved_monitor_url"))
                       or clean(row.get("career_url")))
                _add(rid, clean(row.get("organization_name_original")), url, "stage2b", s2br,
                     "", clean(row.get("stage2b_provider")),
                     clean(row.get("body_text_sample")))

    # Stage 1: unprocessed ATS review
    if path1.exists():
        with path1.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                vr = clean(row.get("validation_result"))
                if vr != "external_ats_needs_review":
                    continue
                rid = clean(row.get("record_id"))
                url = clean(row.get("loaded_url")) or clean(row.get("career_url"))
                _add(rid, clean(row.get("organization_name_original")), url, "stage1_ats_review", vr,
                     clean(row.get("detected_ats")), clean(row.get("ats_provider_detected")),
                     clean(row.get("body_text_sample")))

    return targets


# ---------------------------------------------------------------------------
# Page fetcher (shared across classifiers and extractors)
# ---------------------------------------------------------------------------

async def fetch_page(client: httpx.AsyncClient, url: str, timeout: int = 25) -> tuple[int, str, str]:
    """Fetch a page and return (status, final_url, html_text)."""
    try:
        resp = await client.get(url, timeout=timeout, follow_redirects=True)
        return resp.status_code, str(resp.url), resp.text
    except Exception:
        return 0, url, ""


# ---------------------------------------------------------------------------
# Source-type classifier
# ---------------------------------------------------------------------------

async def classify_source(
    client: httpx.AsyncClient,
    target: dict[str, str],
    timeout: int,
) -> dict[str, str]:
    """Determine source_type, source_provider, listing_url, api_url for a target."""
    url = target["monitor_url"]
    detected_ats = target.get("detected_ats", "")
    detected_provider = target.get("detected_ats_provider", "")
    previous = target.get("previous_result", "")

    # 1. Check if previous stages already identified an ATS or API
    if detected_provider or detected_ats:
        provider = detected_provider or detected_ats.split(",")[0].strip()
        return {
            "source_type": "ats",
            "source_provider": provider,
            "listing_url": url,
            "api_url": "",
        }

    if previous == "resolved_job_api":
        return {
            "source_type": "public_job_api",
            "source_provider": "unknown",
            "listing_url": url,
            "api_url": url,
        }

    if previous == "resolved_general_job_board":
        provider = detect_named([url], JOB_BOARD_DOMAINS)
        return {
            "source_type": "general_job_board",
            "source_provider": provider or "unknown",
            "listing_url": url,
            "api_url": "",
        }

    if previous in ("confirmed_career_page_no_openings", "confirmed_external_ats_no_openings"):
        return {
            "source_type": "no_openings",
            "source_provider": "",
            "listing_url": url,
            "api_url": "",
        }

    # 2. Fetch the page and inspect
    status, final_url, html = await fetch_page(client, url, timeout)
    if status == 0 or not html:
        return {
            "source_type": "unknown",
            "source_provider": "",
            "listing_url": url,
            "api_url": "",
        }

    lower_html = html.lower()
    body = _html_to_text(html)
    parsed_final = urlparse(final_url)

    # Check for blocks
    if contains_any(body, BLOCK_MARKERS):
        return {
            "source_type": "unknown",
            "source_provider": "blocked",
            "listing_url": final_url,
            "api_url": "",
        }

    # 3. Check for known ATS domains in the final URL or page content
    ats = detect_named([final_url], ATS_DOMAINS)
    if ats:
        return {
            "source_type": "ats",
            "source_provider": ats,
            "listing_url": final_url,
            "api_url": "",
        }

    # Check for ATS domains in HTML content
    ats_in_html = detect_named([html], ATS_DOMAINS)
    if ats_in_html:
        # Find the iframe or embed URL
        iframe_url = ""
        for pattern in [
            rf'((?:https?:)?//[^"\'<>\s]*{re.escape(ATS_DOMAINS.get(ats_in_html, ("",))[0])}[^"\'<>\s]*)',
        ]:
            m = re.search(pattern, html, re.I)
            if m:
                iframe_url = m.group(1)
                if iframe_url.startswith("//"):
                    iframe_url = "https:" + iframe_url
                break
        return {
            "source_type": "ats",
            "source_provider": ats_in_html,
            "listing_url": iframe_url or final_url,
            "api_url": "",
        }

    # 4. Check for job board domains
    board = detect_named([final_url] + [html[:5000]], JOB_BOARD_DOMAINS)
    if board:
        return {
            "source_type": "general_job_board",
            "source_provider": board,
            "listing_url": final_url,
            "api_url": "",
        }

    # 5. Check for job board references in links
    soup = BeautifulSoup(html, "html.parser")
    all_urls = [a.get("href", "") for a in soup.find_all("a", href=True)]
    board = detect_named(all_urls, JOB_BOARD_DOMAINS)
    if board:
        board_url = next((u for u in all_urls if detect_named([u], JOB_BOARD_DOMAINS) == board), "")
        return {
            "source_type": "general_job_board",
            "source_provider": board,
            "listing_url": board_url or final_url,
            "api_url": "",
        }

    # 6. Check for JSON-LD JobPosting
    jsonld_match = re.search(r'"@type"\s*:\s*"JobPosting"', html)
    if jsonld_match:
        return {
            "source_type": "static_html_listing",
            "source_provider": "json-ld",
            "listing_url": final_url,
            "api_url": "",
        }

    # 7. Check for iframes pointing to external domains
    iframes = soup.find_all("iframe", src=True)
    for iframe in iframes:
        src = iframe.get("src", "")
        if src.startswith("//"):
            src = "https:" + src
        iframe_host = host(src)
        if iframe_host and iframe_host != host(final_url):
            # Check if this is a known platform
            iframe_ats = detect_named([src], ATS_DOMAINS)
            return {
                "source_type": "iframe_listing",
                "source_provider": iframe_ats or "unknown",
                "listing_url": src,
                "api_url": "",
            }

    # 8. Check for structured job-like content in HTML
    job_links = _find_job_links(soup, final_url)
    if len(job_links) >= 3:
        return {
            "source_type": "static_html_listing",
            "source_provider": "",
            "listing_url": final_url,
            "api_url": "",
        }

    # 9. Check for email-based applications
    email_match = re.search(r'[\w.-]+@[\w.-]+\.\w+', body)
    if email_match and contains_any(body, ("apply", "send", "resume", "cv", "submit")):
        return {
            "source_type": "email_application",
            "source_provider": "",
            "listing_url": final_url,
            "api_url": "",
        }

    # 10. Check for passive application markers
    if contains_any(body, PASSIVE_APPLICATION_MARKERS):
        return {
            "source_type": "passive_application",
            "source_provider": "",
            "listing_url": final_url,
            "api_url": "",
        }

    # 11. Check for no-openings
    if contains_any(body, NO_OPENING_MARKERS):
        return {
            "source_type": "no_openings",
            "source_provider": "",
            "listing_url": final_url,
            "api_url": "",
        }

    # 12. Check for career page with some content
    if contains_any(body, CAREER_MARKERS):
        return {
            "source_type": "static_html_listing",
            "source_provider": "",
            "listing_url": final_url,
            "api_url": "",
        }

    return {
        "source_type": "unknown",
        "source_provider": "",
        "listing_url": final_url,
        "api_url": "",
    }


def _find_job_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Find links that look like individual job postings."""
    job_links = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        if not href or not text:
            continue
        full = urljoin(base_url, href) if not href.startswith("http") else href
        lower_href = href.lower()
        if any(p.search(lower_href) for p in [re.compile(pat) for pat in JOB_POSTING_ID_PATTERNS]):
            job_links.append(full)
        elif any(word in lower_href for word in ("/job/", "/jobs/", "/position/", "/opening/", "/requisition/")):
            job_links.append(full)
        elif JOB_TITLE_INDICATORS.search(text) and len(text) > 5:
            job_links.append(full)
    return list(dict.fromkeys(job_links))[:50]


# ---------------------------------------------------------------------------
# Extraction adapters (each returns list of normalized jobs)
# ---------------------------------------------------------------------------

async def _extract_jsonld(soup: BeautifulSoup, base_url: str) -> list[dict[str, Any]]:
    """Extract jobs from JSON-LD JobPosting structured data."""
    jobs = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("@graph", [data]) if "@graph" in data else [data]
            else:
                continue
            for item in items:
                if clean(item.get("@type")).lower() != "jobposting":
                    continue
                title = clean(item.get("title", ""))
                if not title:
                    continue
                loc = item.get("jobLocation", {})
                if isinstance(loc, list):
                    loc = loc[0] if loc else {}
                addr = loc.get("address", {}) if isinstance(loc, dict) else {}
                location = clean(addr.get("addressLocality", "")) + (
                    ", " + clean(addr.get("addressRegion", "")) if addr.get("addressRegion") else ""
                )
                jobs.append({
                    "job_id": clean(item.get("identifier", {}).get("value", "")) if isinstance(item.get("identifier"), dict) else "",
                    "title": title,
                    "location": location,
                    "department": "",
                    "employment_type": clean(item.get("employmentType", "")),
                    "work_arrangement": "",
                    "salary_min": "",
                    "salary_max": "",
                    "currency": "",
                    "posted_date": clean(item.get("datePosted", "")),
                    "closing_date": clean(item.get("validThrough", "")),
                    "description": _html_to_text(item.get("description", ""))[:2000],
                    "job_url": clean(item.get("url", "")) or clean(item.get("hiringOrganization", {}).get("url", "")) if isinstance(item.get("hiringOrganization"), dict) else "",
                    "application_url": "",
                    "application_email": "",
                    "application_method": "",
                })
        except Exception:
            continue
    return jobs


async def _extract_static_html(soup: BeautifulSoup, base_url: str) -> list[dict[str, Any]]:
    """Extract jobs from ordinary HTML structures (cards, lists, tables)."""
    jobs = []
    # Strategy: find repeated structures with job-like content
    # Look for containers that have multiple child elements with titles + links
    for container in soup.find_all(["section", "div", "ul", "ol", "table", "main"]):
        candidates = []
        for child in container.find_all(["a", "h2", "h3", "h4", "li", "tr", "article"]):
            text = child.get_text(strip=True)
            if not text or len(text) < 5 or len(text) > 200:
                continue
            # Check if this looks like a job title
            if JOB_TITLE_INDICATORS.search(text):
                # Find associated link
                link = ""
                if child.name == "a":
                    link = child.get("href", "")
                else:
                    a = child.find("a", href=True) or child.find_parent("a", href=True)
                    if a:
                        link = a.get("href", "")
                # Find associated location (sibling or nearby)
                location = ""
                for sib in [child.find_next_sibling(), child.parent]:
                    if sib:
                        sib_text = sib.get_text(strip=True) if sib != child.parent else ""
                        loc_match = re.search(r'(?:in|at|–|-)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)', sib_text)
                        if loc_match:
                            location = loc_match.group(1)
                            break

                full_url = urljoin(base_url, link) if link and not link.startswith("http") else link
                candidates.append({
                    "job_id": "",
                    "title": text,
                    "location": location,
                    "department": "",
                    "employment_type": "",
                    "work_arrangement": "",
                    "salary_min": "",
                    "salary_max": "",
                    "currency": "",
                    "posted_date": "",
                    "closing_date": "",
                    "description": "",
                    "job_url": full_url,
                    "application_url": "",
                    "application_email": "",
                    "application_method": "",
                })
        if len(candidates) >= 2:
            jobs.extend(candidates)
            break  # Take the first successful container
    return _deduplicate_jobs(jobs)


async def _extract_iframe(client: httpx.AsyncClient, iframe_url: str, timeout: int) -> list[dict[str, Any]]:
    """Fetch and extract jobs from an iframe source URL."""
    status, final_url, html = await fetch_page(client, iframe_url, timeout)
    if status == 0 or not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    # Try JSON-LD first
    jobs = await _extract_jsonld(soup, final_url)
    if jobs:
        return jobs
    # Try static HTML extraction
    return await _extract_static_html(soup, final_url)


async def _extract_greenhouse(client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
    """Greenhouse public jobs API."""
    token = await _find_greenhouse_token(client, url)
    if not token:
        return []
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    try:
        resp = await client.get(api_url, timeout=20)
        if resp.status_code != 200:
            return []
        data = resp.json()
        jobs = []
        for j in data.get("jobs", []):
            loc = j.get("location", {})
            jobs.append({
                "job_id": str(j.get("id", "")),
                "title": j.get("title", ""),
                "location": loc.get("name", ""),
                "department": (j.get("departments") or [{}])[0].get("name", "") if j.get("departments") else "",
                "employment_type": "",
                "work_arrangement": "",
                "salary_min": "",
                "salary_max": "",
                "currency": "",
                "posted_date": j.get("updated_at", ""),
                "closing_date": "",
                "description": _html_to_text(j.get("content", ""))[:2000],
                "job_url": j.get("absolute_url", ""),
                "application_url": "",
                "application_email": "",
                "application_method": "",
            })
        return jobs
    except Exception:
        return []


async def _find_greenhouse_token(client: httpx.AsyncClient, url: str) -> str:
    """Extract Greenhouse board token from URL or page content."""
    m = re.search(r"boards\.greenhouse\.io/([a-zA-Z0-9_-]+)", url.lower())
    if m and m.group(1) not in ("careers", "jobs"):
        return m.group(1)
    m = re.search(r"greenhouse\.io/([a-zA-Z0-9_-]+)", url.lower())
    if m and m.group(1) not in ("careers", "jobs"):
        return m.group(1)
    # Scrape the page
    try:
        resp = await client.get(url, timeout=15, follow_redirects=True)
        if resp.status_code == 200:
            for pat in [r"boards\.greenhouse\.io/([a-zA-Z0-9_-]+)",
                        r"job-boards/([a-zA-Z0-9_-]+)\?embed=true"]:
                m = re.search(pat, resp.text)
                if m:
                    return m.group(1)
            soup = BeautifulSoup(resp.text, "html.parser")
            for iframe in soup.find_all("iframe", src=True):
                if "greenhouse" in iframe["src"].lower():
                    m = re.search(r"greenhouse\.io/([a-zA-Z0-9_-]+)", iframe["src"])
                    if m:
                        return m.group(1)
    except Exception:
        pass
    return ""


async def _extract_lever(client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
    """Lever public postings API."""
    company = await _find_lever_company(client, url)
    if not company:
        return []
    api_url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    try:
        resp = await client.get(api_url, timeout=20)
        if resp.status_code != 200:
            return []
        postings = resp.json()
        jobs = []
        for p in postings:
            cat = p.get("categories", {}) or {}
            jobs.append({
                "job_id": p.get("id", ""),
                "title": p.get("text", ""),
                "location": cat.get("location", ""),
                "department": cat.get("team", ""),
                "employment_type": cat.get("commitment", ""),
                "work_arrangement": "",
                "salary_min": "",
                "salary_max": "",
                "currency": "",
                "posted_date": p.get("createdAt", ""),
                "closing_date": "",
                "description": _html_to_text(p.get("descriptionPlain", "") or p.get("description", ""))[:2000],
                "job_url": p.get("hostedUrl", ""),
                "application_url": "",
                "application_email": "",
                "application_method": "",
            })
        return jobs
    except Exception:
        return []


async def _find_lever_company(client: httpx.AsyncClient, url: str) -> str:
    m = re.search(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)", url.lower())
    if m:
        return m.group(1)
    host_val = urlparse(url).netloc.lower().replace("www.", "")
    if host_val.endswith(".lever.co"):
        return host_val.split(".lever.co")[0]
    # Scrape page
    try:
        resp = await client.get(url, timeout=15, follow_redirects=True)
        if resp.status_code == 200:
            m = re.search(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)", resp.text)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


async def _extract_workday(client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
    """Workday career site — HTTP API with Playwright fallback."""
    tenant, site = "", ""
    m = re.search(r"([a-z0-9-]+)\.(?:myworkdayjobs|workdayjobs)\.com", url.lower())
    if m:
        tenant = m.group(1)
        parsed = urlparse(url)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        site = parts[-1] if parts else ""
    if not tenant or not site:
        # Try to scrape
        try:
            resp = await client.get(url, timeout=15, follow_redirects=True)
            if resp.status_code == 200:
                m = re.search(r"([a-z0-9-]+)\.(?:myworkdayjobs|workdayjobs)\.com/([^\s\"'<>]+)", resp.text, re.I)
                if m:
                    tenant = m.group(1)
                    site = m.group(2).strip("/").split("?")[0]
        except Exception:
            pass
    if not tenant or not site:
        return []

    api_url = f"https://{tenant}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    try:
        resp = await client.post(api_url, json={"appliedFacets": {}, "limit": 50, "offset": 0, "searchText": ""},
                                 headers={"Content-Type": "application/json", "Accept": "application/json"}, timeout=20)
        if resp.status_code != 200:
            return []
        data = resp.json()
        jobs = []
        for j in data.get("jobPostings", []):
            ext_path = j.get("externalPath", "")
            jobs.append({
                "job_id": j.get("bulletFields", [""])[0] if j.get("bulletFields") else "",
                "title": j.get("title", ""),
                "location": j.get("locationsText", ""),
                "department": "",
                "employment_type": "",
                "work_arrangement": "",
                "salary_min": "",
                "salary_max": "",
                "currency": "",
                "posted_date": "",
                "closing_date": "",
                "description": "",
                "job_url": f"https://{tenant}.myworkdayjobs.com{ext_path}" if ext_path else "",
                "application_url": "",
                "application_email": "",
                "application_method": "",
            })
        return jobs
    except Exception:
        return []


ASYNC_EXTRACTORS: dict[str, Any] = {
    "greenhouse": _extract_greenhouse,
    "lever": _extract_lever,
    "workday": _extract_workday,
}


# ---------------------------------------------------------------------------
# Main extraction pipeline
# ---------------------------------------------------------------------------

async def extract_jobs_for_target(
    client: httpx.AsyncClient,
    target: dict[str, str],
    classification: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    """Run the appropriate extraction method and return the result."""
    source_type = classification["source_type"]
    source_provider = classification["source_provider"]
    listing_url = classification["listing_url"]
    api_url = classification.get("api_url", "")

    now_iso = datetime.now(timezone.utc).isoformat()
    base_result = {
        "record_id": target["record_id"],
        "organization_name": target["organization_name"],
        "monitor_url": target["monitor_url"],
        "source_type": source_type,
        "source_provider": source_provider,
        "source_listing_url": listing_url,
        "source_api_url": api_url,
        "source_stage": target["source_stage"],
        "extraction_checked_at_utc": now_iso,
        "extraction_result": "",
        "extraction_reason": "",
        "jobs_found": 0,
        "extraction_error": "",
    }

    # Route to the right extractor
    jobs: list[dict[str, Any]] = []
    reason = ""

    try:
        if source_type == "no_openings":
            base_result["extraction_result"] = "confirmed_no_openings"
            base_result["extraction_reason"] = "Career page explicitly shows no openings"
            return base_result

        if source_type == "passive_application":
            base_result["extraction_result"] = "passive_application_only"
            base_result["extraction_reason"] = "Page accepts general résumés, no current vacancies"
            return base_result

        if source_type == "email_application":
            # Try to find email
            email = ""
            status, _, html = await fetch_page(client, listing_url, timeout)
            if html:
                m = re.search(r'[\w.-]+@[\w.-]+\.\w+', _html_to_text(html))
                if m:
                    email = m.group(0)
            base_result["extraction_result"] = "email_application_found"
            base_result["extraction_reason"] = f"Application email: {email}" if email else "Email-based application detected"
            base_result["application_email"] = email
            return base_result

        if source_type == "ats":
            extractor = ASYNC_EXTRACTORS.get(source_provider)
            if extractor:
                jobs = await extractor(client, listing_url)
            else:
                # Unknown ATS — try generic extraction
                status, final_url, html = await fetch_page(client, listing_url, timeout)
                if html:
                    soup = BeautifulSoup(html, "html.parser")
                    jobs = await _extract_jsonld(soup, final_url)
                    if not jobs:
                        jobs = await _extract_static_html(soup, final_url)
                    if not jobs:
                        # Check for iframe
                        iframes = soup.find_all("iframe", src=True)
                        for iframe in iframes:
                            src = iframe.get("src", "")
                            if src.startswith("//"):
                                src = "https:" + src
                            jobs = await _extract_iframe(client, src, timeout)
                            if jobs:
                                break
                reason = f"Unknown ATS: {source_provider}"

        elif source_type == "public_job_api":
            if api_url:
                try:
                    resp = await client.get(api_url, timeout=timeout)
                    if resp.status_code == 200:
                        data = resp.json()
                        # Try to extract jobs from JSON response
                        jobs = _extract_jobs_from_json(data, api_url)
                        reason = f"API: {api_url}"
                except Exception as exc:
                    reason = f"API error: {exc}"

        elif source_type == "static_html_listing":
            status, final_url, html = await fetch_page(client, listing_url, timeout)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                # Try JSON-LD first
                jobs = await _extract_jsonld(soup, final_url)
                if not jobs:
                    jobs = await _extract_static_html(soup, final_url)

        elif source_type == "iframe_listing":
            if listing_url:
                jobs = await _extract_iframe(client, listing_url, timeout)

        elif source_type == "general_job_board":
            base_result["extraction_result"] = "general_job_board_detected"
            base_result["extraction_reason"] = f"Job board: {source_provider}"
            return base_result

        elif source_type == "unknown":
            # Generic extraction attempt
            status, final_url, html = await fetch_page(client, listing_url, timeout)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                jobs = await _extract_jsonld(soup, final_url)
                if not jobs:
                    jobs = await _extract_static_html(soup, final_url)
                if not jobs:
                    iframes = soup.find_all("iframe", src=True)
                    for iframe in iframes:
                        src = iframe.get("src", "")
                        if src.startswith("//"):
                            src = "https:" + src
                        jobs = await _extract_iframe(client, src, timeout)
                        if jobs:
                            break
                if not jobs:
                    base_result["extraction_result"] = "needs_manual_review"
                    base_result["extraction_reason"] = "No jobs found via generic extraction"
                    return base_result

    except Exception as exc:
        base_result["extraction_result"] = "crawl_failed"
        base_result["extraction_error"] = f"{type(exc).__name__}: {exc}"
        return base_result

    # Populate result
    if jobs:
        base_result["extraction_result"] = "jobs_extracted"
        base_result["extraction_reason"] = reason or f"{len(jobs)} jobs found"
        base_result["jobs_found"] = len(jobs)
    else:
        base_result["extraction_result"] = "no_jobs_found"
        base_result["extraction_reason"] = reason or "No jobs extracted"

    # Attach jobs as JSON
    base_result["jobs_json"] = json.dumps(jobs, ensure_ascii=False)
    return base_result


def _extract_jobs_from_json(data: Any, source_url: str) -> list[dict[str, Any]]:
    """Recursively search a JSON response for job-like objects."""
    jobs: list[dict[str, Any]] = []
    _search_json_for_jobs(data, jobs, depth=0)
    return _deduplicate_jobs(jobs[:200])


def _search_json_for_jobs(obj: Any, jobs: list, depth: int = 0) -> None:
    if depth > 10 or len(jobs) >= 200:
        return
    if isinstance(obj, dict):
        keys = {str(k).lower().replace("-", "").replace("_", "") for k in obj.keys()}
        job_key_count = len(keys & {"jobid", "jobtitle", "title", "position", "posting",
                                      "requisition", "vacancy", "opening"})
        has_location = "location" in keys or "city" in keys
        has_title = "title" in keys or "jobtitle" in keys or "name" in keys
        if job_key_count >= 2 or (has_title and has_location and depth > 0):
            title = clean(obj.get("title", "") or obj.get("jobtitle", "") or obj.get("name", ""))
            if title:
                jobs.append({
                    "job_id": clean(obj.get("jobid", "") or obj.get("id", "") or obj.get("requisitionid", "")),
                    "title": title,
                    "location": clean(obj.get("location", "") or obj.get("city", "") or obj.get("locationsText", "")),
                    "department": clean(obj.get("department", "") or obj.get("team", "")),
                    "employment_type": clean(obj.get("employmenttype", "") or obj.get("type", "")),
                    "work_arrangement": "",
                    "salary_min": "",
                    "salary_max": "",
                    "currency": "",
                    "posted_date": clean(obj.get("posteddate", "") or obj.get("createdat", "")),
                    "closing_date": clean(obj.get("closingdate", "") or obj.get("validthrough", "")),
                    "description": _html_to_text(obj.get("description", ""))[:2000],
                    "job_url": clean(obj.get("url", "") or obj.get("applyurl", "") or obj.get("joburl", "")),
                    "application_url": clean(obj.get("applyurl", "") or obj.get("applicationurl", "")),
                    "application_email": "",
                    "application_method": "",
                })
            return
        for v in obj.values():
            _search_json_for_jobs(v, jobs, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:50]:
            _search_json_for_jobs(item, jobs, depth + 1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_extraction(targets: list[dict[str, str]], jsonl_path: Path, timeout: int) -> None:
    completed = load_completed(jsonl_path)
    pending = [t for t in targets if t["record_id"] not in completed]
    print(f"Total targets: {len(targets)}, completed: {len(completed)}, pending: {len(pending)}")

    if not pending:
        return

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout + 5,
        headers={"User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0; +https://example.com/bot)"},
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    ) as client:
        sem = asyncio.Semaphore(5)

        async def process_one(target: dict[str, str]) -> None:
            async with sem:
                rid = target["record_id"]
                try:
                    classification = await classify_source(client, target, timeout)
                    result = await extract_jobs_for_target(client, target, classification, timeout)
                    result["source_type"] = classification["source_type"]
                    result["source_provider"] = classification["source_provider"]
                    result["source_listing_url"] = classification["listing_url"]
                    result["source_api_url"] = classification.get("api_url", "")
                except Exception as exc:
                    result = {
                        **target,
                        "source_type": "unknown",
                        "source_provider": "",
                        "source_listing_url": target["monitor_url"],
                        "source_api_url": "",
                        "extraction_checked_at_utc": datetime.now(timezone.utc).isoformat(),
                        "extraction_result": "crawl_failed",
                        "extraction_reason": "",
                        "jobs_found": 0,
                        "extraction_error": f"{type(exc).__name__}: {exc}",
                        "jobs_json": "[]",
                    }
                append_jsonl(jsonl_path, result)

        # Process in batches for progress reporting
        batch_size = 50
        for i in range(0, len(pending), batch_size):
            batch = pending[i:i + batch_size]
            tasks = [process_one(t) for t in batch]
            await asyncio.gather(*tasks)
            print(f"  Processed {min(i + batch_size, len(pending))}/{len(pending)} targets")


def export_outputs(records: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. job_sources.csv
    source_fields = [
        "record_id", "organization_name", "monitor_url", "source_type",
        "source_provider", "source_listing_url", "source_api_url",
        "source_stage", "previous_result", "extraction_result",
        "extraction_reason", "jobs_found", "extraction_error",
        "extraction_checked_at_utc",
    ]
    with (out_dir / "job_sources.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=source_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)
    print(f"Wrote job_sources.csv ({len(records)} rows)")

    # 2. extraction_status.csv
    with (out_dir / "extraction_status.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=source_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)
    print(f"Wrote extraction_status.csv ({len(records)} rows)")

    # 3. jobs_current.csv
    job_fields = [
        "record_id", "organization_name", "source_type", "source_provider",
        "source_listing_url", "job_id", "title", "location", "department",
        "employment_type", "work_arrangement", "salary_min", "salary_max",
        "currency", "posted_date", "closing_date", "description",
        "job_url", "application_url", "application_email", "application_method",
        "first_seen", "last_seen", "status", "content_hash",
    ]
    job_rows: list[dict] = []
    for rec in records:
        jobs_json = rec.get("jobs_json", "[]")
        try:
            jobs = json.loads(jobs_json) if jobs_json else []
        except Exception:
            jobs = []
        for job in jobs:
            desc = job.get("description", "")
            job_rows.append({
                "record_id": rec.get("record_id", ""),
                "organization_name": rec.get("organization_name", ""),
                "source_type": rec.get("source_type", ""),
                "source_provider": rec.get("source_provider", ""),
                "source_listing_url": rec.get("source_listing_url", ""),
                "job_id": job.get("job_id", ""),
                "title": job.get("title", ""),
                "location": job.get("location", ""),
                "department": job.get("department", ""),
                "employment_type": job.get("employment_type", ""),
                "work_arrangement": job.get("work_arrangement", ""),
                "salary_min": job.get("salary_min", ""),
                "salary_max": job.get("salary_max", ""),
                "currency": job.get("currency", ""),
                "posted_date": job.get("posted_date", ""),
                "closing_date": job.get("closing_date", ""),
                "description": desc,
                "job_url": job.get("job_url", ""),
                "application_url": job.get("application_url", ""),
                "application_email": job.get("application_email", ""),
                "application_method": job.get("application_method", ""),
                "first_seen": now_iso,
                "last_seen": now_iso,
                "status": "active",
                "content_hash": _content_hash(desc) if desc else "",
            })
    with (out_dir / "jobs_current.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=job_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(job_rows)
    print(f"Wrote jobs_current.csv ({len(job_rows)} jobs)")


async def main() -> None:
    args = parse_args()
    jsonl_path = Path(args.jsonl)
    out_dir = Path(args.output_dir)

    print("Collecting targets from all stages...")
    targets = collect_targets()
    print(f"Collected {len(targets)} unique targets")

    # Source stage breakdown
    from collections import Counter
    stages = Counter(t["source_stage"] for t in targets)
    for s, c in stages.most_common():
        print(f"  {s:20s}: {c}")

    # Detected ATS breakdown
    ats_targets = [t for t in targets if t.get("detected_ats") or t.get("detected_ats_provider")]
    print(f"  Targets with ATS detection: {len(ats_targets)}")

    # Use job_sources.csv as authoritative inventory when available
    sources_path = Path(args.sources_file)
    if sources_path.exists():
        import csv as csv_mod
        with sources_path.open(newline="", encoding="utf-8-sig") as f:
            source_rows = {clean(r.get("record_id")): r
                           for r in csv_mod.DictReader(f) if clean(r.get("record_id"))}
        print(f"Loaded {len(source_rows)} sources from {sources_path}")

        # Filter targets by authoritative inventory
        target_ids = {t.get("record_id") for t in targets}
        matched = [source_rows[rid] for rid in target_ids if rid in source_rows]
        print(f"  Matched {len(matched)} targets in source inventory")

    # Apply filters before limiting
    if args.source_id:
        targets = [t for t in targets if t.get("record_id") == args.source_id]
        print(f"Filtered to record_id={args.source_id}: {len(targets)} targets")
    if args.source_type:
        targets = [t for t in targets if args.source_type.lower() in
                   (t.get("detected_ats", "") or "").lower()
                   or args.source_type.lower() in (t.get("source_type", "") or "").lower()]
        print(f"Filtered to source_type={args.source_type}: {len(targets)} targets")
    if args.provider:
        targets = [t for t in targets
                   if args.provider.lower() in (t.get("detected_ats_provider", "") or "").lower()
                   or args.provider.lower() in (t.get("detected_ats", "") or "").lower()]
        print(f"Filtered to provider={args.provider}: {len(targets)} targets")
    if args.limit > 0:
        targets = targets[:args.limit]
        print(f"Limited to {len(targets)} records")

    await run_extraction(targets, jsonl_path, args.timeout)

    # Read all results for export
    all_results: list[dict] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                all_results.append(json.loads(line))

    export_outputs(all_results, out_dir)

    # Summary
    result_counts: Counter = Counter()
    type_counts: Counter = Counter()
    total_jobs = 0
    for r in all_results:
        result_counts[r.get("extraction_result", "unknown")] += 1
        type_counts[r.get("source_type", "unknown")] += 1
        total_jobs += r.get("jobs_found", 0)

    print(f"\nExtraction summary ({len(all_results)} targets):")
    print("  By result:")
    for er, c in result_counts.most_common():
        print(f"    {er:40s}: {c:4d}")
    print("  By source type:")
    for st, c in type_counts.most_common():
        print(f"    {st:40s}: {c:4d}")
    print(f"  Total jobs extracted: {total_jobs}")


if __name__ == "__main__":
    asyncio.run(main())
