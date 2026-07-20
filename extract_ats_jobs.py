#!/usr/bin/env python3
"""
Stage 2C — Unified ATS inventory, platform-specific job extraction,
and normalized current-job output.

Collects every ATS-related URL from Stages 1, 2A, and 2B,
normalizes and deduplicates, groups by provider, runs provider-specific
extraction adapters, and produces:

  ats_targets_normalized.csv  — one deduplicated row per employer ATS board
  jobs_current.csv            — one row per job
  ats_extraction_status.csv   — one row per ATS target

It never submits an application, sends a form, logs in, or bypasses a challenge.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs, urljoin

import httpx
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Provider detection (consistent with resolve_unresolved_landings.py)
# ---------------------------------------------------------------------------

PROVIDER_DOMAINS: dict[str, tuple[str, ...]] = {
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

PROVIDER_NAME_ALIASES: dict[str, str] = {
    "ukg": "ukg_pro",
    "breezy_hr": "breezy_hr",
}


def detect_provider_from_url(url: str) -> str:
    lower = url.lower()
    for name, domains in PROVIDER_DOMAINS.items():
        if any(domain in lower for domain in domains):
            return name
    return ""


def normalize_provider(name: str) -> str:
    return PROVIDER_NAME_ALIASES.get(name.lower().strip(), name.lower().strip())


# ---------------------------------------------------------------------------
# API-based extractors (no browser needed)
# ---------------------------------------------------------------------------

async def extract_greenhouse(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """Greenhouse public jobs API: boards-api.greenhouse.io/v1/boards/{token}/jobs"""
    board_token = _extract_greenhouse_token(url)
    if not board_token:
        # Try to find the token by scraping the career page
        board_token = await _scrape_greenhouse_token(client, url)
    if not board_token:
        return {"result": "needs_manual_review", "reason": "Cannot extract Greenhouse board token", "jobs": []}
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    try:
        resp = await client.get(api_url, timeout=20)
        if resp.status_code != 200:
            return {"result": "crawl_failed", "reason": f"HTTP {resp.status_code}", "jobs": []}
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
                "posted_date": j.get("updated_at", ""),
                "job_url": j.get("absolute_url", ""),
                "application_url": "",
                "description": _html_to_text(j.get("content", ""))[:2000],
            })
        return {"result": "active_jobs_extracted" if jobs else "confirmed_no_openings", "reason": f"board={board_token}", "jobs": jobs}
    except Exception as exc:
        return {"result": "crawl_failed", "reason": f"{type(exc).__name__}: {exc}", "jobs": []}


def _extract_greenhouse_token(url: str) -> str:
    """Try to extract Greenhouse board token from various URL shapes."""
    lower = url.lower()
    for pattern in [
        r"boards\.greenhouse\.io/([a-zA-Z0-9_-]+)",
        r"greenhouse\.io/([a-zA-Z0-9_-]+)",
        r"job-boards/([a-zA-Z0-9_-]+)",
    ]:
        m = re.search(pattern, lower)
        if m:
            token = m.group(1)
            if token and token not in ("careers", "jobs", "career", "job"):
                return token
    return ""


async def _scrape_greenhouse_token(client: httpx.AsyncClient, url: str) -> str:
    """Scrape a career page to find embedded Greenhouse board token."""
    try:
        resp = await client.get(url, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            return ""
        text = resp.text
        # Look for greenhouse.io board embeds in HTML
        for pattern in [
            r"boards\.greenhouse\.io/([a-zA-Z0-9_-]+)",
            r"job-boards/([a-zA-Z0-9_-]+)\?embed=true",
            r"boards-api\.greenhouse\.io/v1/boards/([a-zA-Z0-9_-]+)",
        ]:
            m = re.search(pattern, text)
            if m:
                return m.group(1)
        # Check iframe sources
        soup = BeautifulSoup(text, "html.parser")
        for iframe in soup.find_all("iframe", src=True):
            src = iframe["src"]
            if "greenhouse" in src.lower():
                m = re.search(r"greenhouse\.io/([a-zA-Z0-9_-]+)", src)
                if m:
                    return m.group(1)
        return ""
    except Exception:
        return ""


async def extract_lever(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """Lever public postings API: api.lever.co/v0/postings/{company}"""
    company = _extract_lever_company(url)
    if not company:
        company = await _scrape_lever_company(client, url)
    if not company:
        return {"result": "needs_manual_review", "reason": "Cannot extract Lever company slug", "jobs": []}
    api_url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    try:
        resp = await client.get(api_url, timeout=20)
        if resp.status_code != 200:
            return {"result": "crawl_failed", "reason": f"HTTP {resp.status_code}", "jobs": []}
        postings = resp.json()
        jobs = []
        for p in postings:
            jobs.append({
                "job_id": p.get("id", ""),
                "title": p.get("text", ""),
                "location": (p.get("categories", {}) or {}).get("location", ""),
                "department": (p.get("categories", {}) or {}).get("team", ""),
                "employment_type": (p.get("categories", {}) or {}).get("commitment", ""),
                "posted_date": p.get("createdAt", ""),
                "job_url": p.get("hostedUrl", ""),
                "application_url": "",
                "description": _html_to_text(p.get("descriptionPlain", "") or p.get("description", ""))[:2000],
            })
        return {"result": "active_jobs_extracted" if jobs else "confirmed_no_openings", "reason": f"company={company}", "jobs": jobs}
    except Exception as exc:
        return {"result": "crawl_failed", "reason": f"{type(exc).__name__}: {exc}", "jobs": []}


def _extract_lever_company(url: str) -> str:
    lower = url.lower()
    m = re.search(r"jobs\.lever\.co/([^/\?#]+)", lower)
    if m:
        return m.group(1)
    m = re.search(r"lever\.co/([^/\?#]+)", lower)
    if m:
        return m.group(1)
    parsed = urlparse(url)
    host = parsed.netloc.lower().replace("www.", "")
    if host.endswith(".lever.co"):
        return host.split(".lever.co")[0]
    return ""


async def _scrape_lever_company(client: httpx.AsyncClient, url: str) -> str:
    """Scrape a career page to find embedded Lever company slug."""
    try:
        resp = await client.get(url, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            return ""
        text = resp.text
        for pattern in [
            r"jobs\.lever\.co/([a-zA-Z0-9_-]+)",
            r"lever\.co/([a-zA-Z0-9_-]+)",
        ]:
            m = re.search(pattern, text)
            if m:
                return m.group(1)
        soup = BeautifulSoup(text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "lever.co" in href.lower():
                m = re.search(r"lever\.co/([a-zA-Z0-9_-]+)", href)
                if m:
                    return m.group(1)
        return ""
    except Exception:
        return ""


async def extract_ashby(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """Ashby job board API (non-user GraphQL endpoint) with fallback to page scraping."""
    board_slug = _extract_ashby_slug(url)
    if not board_slug:
        return {"result": "needs_manual_review", "reason": "Cannot extract Ashby board slug from URL", "jobs": []}
    # Try the GraphQL API first
    api_url = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams"
    payload = {
        "operationName": "ApiJobBoardWithTeams",
        "variables": {"organizationHostedJobsPageName": board_slug},
        "query": "query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) { jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) { teams { name jobs { id title locationName employmentType ... on JobListing { updatedAt } } } } }",
    }
    try:
        resp = await client.post(api_url, json=payload, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            board = data.get("data", {}).get("jobBoard", {})
            teams = board.get("teams", [])
            jobs = []
            for team in teams:
                for j in team.get("jobs", []):
                    jobs.append({
                        "job_id": j.get("id", ""),
                        "title": j.get("title", ""),
                        "location": j.get("locationName", ""),
                        "department": team.get("name", ""),
                        "employment_type": j.get("employmentType", ""),
                        "posted_date": j.get("updatedAt", ""),
                        "job_url": f"https://jobs.ashbyhq.com/{board_slug}/{j.get('id', '')}",
                        "application_url": "",
                        "description": "",
                    })
            if jobs:
                return {"result": "active_jobs_extracted", "reason": f"slug={board_slug}", "jobs": jobs}
    except Exception:
        pass

    # Fallback: scrape the page for job listings
    page_url = f"https://jobs.ashbyhq.com/{board_slug}"
    try:
        resp = await client.get(page_url, timeout=20, follow_redirects=True)
        if resp.status_code != 200:
            return {"result": "crawl_failed", "reason": f"HTTP {resp.status_code}", "jobs": []}
        text = resp.text
        # Look for job data embedded in the page
        jobs = []
        # Try to find job links in the HTML
        soup = BeautifulSoup(text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            link_text = a.get_text(strip=True)
            if f"/{board_slug}/" in href and link_text and len(link_text) > 3:
                # This looks like a job listing link
                job_id = href.rstrip("/").split("/")[-1]
                if job_id and job_id != board_slug:
                    jobs.append({
                        "job_id": job_id,
                        "title": link_text,
                        "location": "",
                        "department": "",
                        "employment_type": "",
                        "posted_date": "",
                        "job_url": urljoin(resp.url, href) if not href.startswith("http") else href,
                        "application_url": "",
                        "description": "",
                    })
        jobs = _deduplicate_jobs(jobs)
        if jobs:
            return {"result": "active_jobs_extracted", "reason": f"scraped ({len(jobs)} jobs)", "jobs": jobs}

        # Check for no-openings message
        lower_text = text.lower()
        if any(marker in lower_text for marker in ("no current openings", "no open positions", "no positions available")):
            return {"result": "confirmed_no_openings", "reason": "No openings message found", "jobs": []}

        return {"result": "unsupported_platform_variant", "reason": f"Ashby page rendered client-side (slug={board_slug})", "jobs": []}
    except Exception as exc:
        return {"result": "crawl_failed", "reason": f"{type(exc).__name__}: {exc}", "jobs": []}


def _extract_ashby_slug(url: str) -> str:
    lower = url.lower()
    m = re.search(r"jobs\.ashbyhq\.com/([^/\?#]+)", lower)
    if m:
        return m.group(1)
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if parts:
        return parts[0]
    return ""


async def extract_smartrecruiters(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """SmartRecruiters public API: api.smartrecruiters.com/v1/companies/{companyId}/postings"""
    company_id = _extract_smartrecruiters_id(url)
    if not company_id:
        company_id = await _scrape_smartrecruiters_id(client, url)
    if not company_id:
        return {"result": "needs_manual_review", "reason": "Cannot extract SmartRecruiters company ID", "jobs": []}
    api_url = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings?limit=100&offset=0"
    try:
        resp = await client.get(api_url, timeout=20)
        if resp.status_code != 200:
            return {"result": "crawl_failed", "reason": f"HTTP {resp.status_code}", "jobs": []}
        data = resp.json()
        content = data.get("content", [])
        jobs = []
        for p in content:
            loc = p.get("location", {})
            apply_url = p.get("applyUrl", "")
            jobs.append({
                "job_id": str(p.get("id", "")),
                "title": p.get("name", ""),
                "location": loc.get("city", "") + (", " + loc.get("region", "") if loc.get("region") else ""),
                "department": "",
                "employment_type": "",
                "posted_date": p.get("releasedDate", ""),
                "job_url": apply_url if apply_url else "",
                "application_url": apply_url,
                "description": _html_to_text(p.get("jobAd", {}).get("sections", {}).get("jobDescription", ""))[:2000],
            })
        return {"result": "active_jobs_extracted" if jobs else "confirmed_no_openings", "reason": f"company={company_id}", "jobs": jobs}
    except Exception as exc:
        return {"result": "crawl_failed", "reason": f"{type(exc).__name__}: {exc}", "jobs": []}


def _extract_smartrecruiters_id(url: str) -> str:
    lower = url.lower()
    m = re.search(r"careers\.smartrecruiters\.com/([^/\?#]+)", lower)
    if m:
        candidate = m.group(1)
        if candidate and candidate not in ("careers", "jobs", "career"):
            return candidate
    m = re.search(r"smartrecruiters\.com/([^/\?#]+)", lower)
    if m:
        candidate = m.group(1)
        if candidate and candidate not in ("careers", "jobs", "career"):
            return candidate
    return ""


async def _scrape_smartrecruiters_id(client: httpx.AsyncClient, url: str) -> str:
    """Scrape career page to find SmartRecruiters company ID."""
    try:
        resp = await client.get(url, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            return ""
        text = resp.text
        for pattern in [
            r"careers\.smartrecruiters\.com/([a-zA-Z0-9_-]+)",
        ]:
            m = re.search(pattern, text)
            if m:
                candidate = m.group(1)
                if candidate and candidate not in ("careers", "jobs", "career"):
                    return candidate
        soup = BeautifulSoup(text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "careers.smartrecruiters.com" in href.lower():
                m = re.search(r"careers\.smartrecruiters\.com/([a-zA-Z0-9_-]+)", href)
                if m:
                    candidate = m.group(1)
                    if candidate and candidate not in ("careers", "jobs", "career"):
                        return candidate
        return ""
    except Exception:
        return ""


async def extract_workable(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """Workable job board: apply.workable.com/{slug}/jobs"""
    slug = _extract_workable_slug(url)
    if not slug:
        return {"result": "needs_manual_review", "reason": "Cannot extract Workable slug from URL", "jobs": []}
    board_url = f"https://apply.workable.com/{slug}/jobs"
    try:
        resp = await client.get(board_url, timeout=20, follow_redirects=True)
        if resp.status_code != 200:
            # Fallback to spider endpoint
            spider_url = f"https://{slug}.workable.com/spider"
            resp = await client.get(spider_url, timeout=20, follow_redirects=True)
            if resp.status_code != 200:
                return {"result": "crawl_failed", "reason": f"HTTP {resp.status_code}", "jobs": []}
        soup = BeautifulSoup(resp.text, "html.parser")
        jobs = []
        # Try parsing as XML (RSS/spider format)
        try:
            xml_soup = BeautifulSoup(resp.text, "lxml-xml")
            for item in xml_soup.find_all("item"):
                title = item.find("title")
                link = item.find("link")
                desc = item.find("description")
                loc = item.find("location")
                jobs.append({
                    "job_id": "",
                    "title": title.get_text(strip=True) if title else "",
                    "location": loc.get_text(strip=True) if loc else "",
                    "department": "",
                    "employment_type": "",
                    "posted_date": "",
                    "job_url": link.get_text(strip=True) if link else "",
                    "application_url": "",
                    "description": _html_to_text(desc.get_text(strip=True) if desc else "")[:2000],
                })
        except Exception:
            pass
        # If no XML jobs, try HTML parsing
        if not jobs:
            for link in soup.select("a[href]"):
                href = link.get("href", "")
                text = link.get_text(strip=True)
                if "/jobs/" in href and text and len(text) > 3:
                    job_url = urljoin(resp.url, href) if not href.startswith("http") else href
                    jobs.append({
                        "job_id": "",
                        "title": text,
                        "location": "",
                        "department": "",
                        "employment_type": "",
                        "posted_date": "",
                        "job_url": job_url,
                        "application_url": "",
                        "description": "",
                    })
        jobs = _deduplicate_jobs(jobs)
        return {"result": "active_jobs_extracted" if jobs else "confirmed_no_openings", "reason": "", "jobs": jobs}
    except Exception as exc:
        return {"result": "crawl_failed", "reason": f"{type(exc).__name__}: {exc}", "jobs": []}


def _extract_workable_slug(url: str) -> str:
    lower = url.lower()
    m = re.search(r"([a-z0-9-]+)\.workable\.com", lower)
    if m:
        return m.group(1)
    m = re.search(r"apply\.workable\.com/([^/\?#]+)", lower)
    if m:
        return m.group(1)
    return ""


async def extract_recruitee(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """Recruitee public API: api.recruitee.com/v3/careers/{company_id}/jobs"""
    company = _extract_recruitee_company(url)
    if not company:
        return {"result": "needs_manual_review", "reason": "Cannot extract company slug from URL", "jobs": []}
    api_url = f"https://api.recruitee.com/v3/careers/{company}/jobs/?page=1&limit=100"
    try:
        resp = await client.get(api_url, timeout=20)
        if resp.status_code != 200:
            return {"result": "crawl_failed", "reason": f"HTTP {resp.status_code}", "jobs": []}
        data = resp.json()
        jobs_data = data.get("jobs", [])
        jobs = []
        for j in jobs_data:
            jobs.append({
                "job_id": str(j.get("id", "")),
                "title": j.get("title", ""),
                "location": j.get("location", ""),
                "department": "",
                "employment_type": j.get("employment_type", ""),
                "posted_date": j.get("created_at", ""),
                "job_url": f"https://careers.recruitee.com/o/{company}/{j.get('slug', '')}",
                "application_url": "",
                "description": _html_to_text(j.get("description", ""))[:2000],
            })
        return {"result": "active_jobs_extracted" if jobs else "confirmed_no_openings", "reason": "", "jobs": jobs}
    except Exception as exc:
        return {"result": "crawl_failed", "reason": f"{type(exc).__name__}: {exc}", "jobs": []}


def _extract_recruitee_company(url: str) -> str:
    lower = url.lower()
    m = re.search(r"careers\.recruitee\.com/([^/\?#]+)", lower)
    if m:
        return m.group(1)
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) >= 2 and parts[0] == "o":
        return parts[1]
    if parts:
        return parts[0]
    return ""


async def extract_workday(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """Workday external career sites. First try HTTP API, fall back to Playwright browser."""
    tenant, site = _extract_workday_params(url)
    if not tenant or not site:
        # Try to scrape the page for Workday URLs
        tenant, site = await _scrape_workday_params(client, url)
    if not tenant or not site:
        return {"result": "unsupported_platform_variant", "reason": f"Cannot extract Workday tenant/site from URL: {url}", "jobs": []}

    # Try HTTP API first
    api_url = f"https://{tenant}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    payload = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        resp = await client.post(api_url, json=payload, headers=headers, timeout=20)
        if resp.status_code == 200:
            return _parse_workday_api_response(resp.json(), tenant)
    except Exception:
        pass

    # Fall back to Playwright browser rendering
    return await _extract_workday_browser(url, tenant, site)


def _extract_workday_params(url: str) -> tuple[str, str]:
    """Extract tenant and site from a Workday career URL."""
    lower = url.lower()
    m = re.search(r"([a-z0-9-]+)\.(?:myworkdayjobs|workdayjobs)\.com", lower)
    tenant = m.group(1) if m else ""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    site = ""
    if len(parts) >= 2:
        site = parts[-1]
    elif len(parts) == 1:
        site = parts[0]
    if not site and tenant:
        site = "external"
    return tenant, site


async def _scrape_workday_params(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """Scrape a career page to find embedded Workday tenant/site."""
    try:
        resp = await client.get(url, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            return "", ""
        text = resp.text
        m = re.search(r"([a-z0-9-]+)\.(?:myworkdayjobs|workdayjobs)\.com/([^\s\"'<>]+)", text, re.I)
        if m:
            return m.group(1), m.group(2).strip("/").split("?")[0]
        return "", ""
    except Exception:
        return "", ""


def _parse_workday_api_response(data: dict, tenant: str) -> dict[str, Any]:
    """Parse Workday API JSON response into standard job format."""
    total = data.get("total", 0)
    jobs = []
    for j in data.get("jobPostings", []):
        ext_path = j.get("externalPath", "")
        jobs.append({
            "job_id": j.get("bulletFields", [""])[0] if j.get("bulletFields") else "",
            "title": j.get("title", ""),
            "location": j.get("locationsText", ""),
            "department": "",
            "employment_type": "",
            "posted_date": "",
            "job_url": f"https://{tenant}.myworkdayjobs.com{ext_path}" if ext_path else "",
            "application_url": "",
            "description": "",
        })
    result = "active_jobs_extracted" if jobs else "confirmed_no_openings"
    return {"result": result, "reason": f"total={total}", "jobs": jobs}


async def _extract_workday_browser(url: str, tenant: str, site: str) -> dict[str, Any]:
    """Use Playwright to render a Workday career page and extract jobs."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"result": "unsupported_platform_variant", "reason": "Playwright not installed", "jobs": []}

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = await browser.new_page()
            try:
                await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                await page.wait_for_timeout(5000)
                content = await page.content()
            finally:
                await browser.close()

        # Try to find job listings in the rendered HTML
        soup = BeautifulSoup(content, "html.parser")
        jobs = []
        # Workday job listings typically have specific data-* attributes or classes
        for item in soup.select("[data-automation-id='jobTitle'], [class*='job-title'], [class*='JobTitle']"):
            text = item.get_text(strip=True)
            link = item.get("href") or ""
            if not link:
                parent = item.find_parent("a")
                if parent:
                    link = parent.get("href", "")
            if text:
                jobs.append({
                    "job_id": "",
                    "title": text,
                    "location": "",
                    "department": "",
                    "employment_type": "",
                    "posted_date": "",
                    "job_url": urljoin(url, link) if link else "",
                    "application_url": "",
                    "description": "",
                })

        # Fallback: look for any links that look like job postings
        if not jobs:
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                text = a.get_text(strip=True)
                if text and len(text) > 5 and any(kw in href.lower() for kw in ["job", "requisition", "posting"]):
                    jobs.append({
                        "job_id": "",
                        "title": text,
                        "location": "",
                        "department": "",
                        "employment_type": "",
                        "posted_date": "",
                        "job_url": urljoin(url, href) if not href.startswith("http") else href,
                        "application_url": "",
                        "description": "",
                    })

        jobs = _deduplicate_jobs(jobs)
        result = "active_jobs_extracted" if jobs else "confirmed_no_openings"
        return {"result": result, "reason": f"browser-rendered ({len(jobs)} jobs found)", "jobs": jobs}
    except Exception as exc:
        return {"result": "crawl_failed", "reason": f"Playwright: {type(exc).__name__}: {exc}", "jobs": []}


async def extract_bamboohr(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """BambooHR job board: {company}.bamboohr.com/careers/list"""
    slug = _extract_bamboohr_slug(url)
    if not slug:
        slug = await _scrape_bamboohr_slug(client, url)
    if not slug:
        # Try common slug patterns from the domain
        parsed = urlparse(url)
        host = parsed.netloc.lower().replace("www.", "")
        domain_slug = host.split(".")[0]
        if domain_slug and domain_slug not in ("careers", "jobs", "www"):
            test_url = f"https://{domain_slug}.bamboohr.com/careers/list"
            try:
                resp = await client.get(test_url, timeout=10, follow_redirects=True)
                if resp.status_code == 200 and "bamboohr" in resp.url.lower():
                    slug = domain_slug
            except Exception:
                pass
    if not slug:
        return {"result": "needs_manual_review", "reason": "Cannot extract BambooHR slug from URL", "jobs": []}
    list_url = f"https://{slug}.bamboohr.com/careers/list"
    try:
        resp = await client.get(list_url, timeout=20, follow_redirects=True)
        if resp.status_code != 200:
            return {"result": "crawl_failed", "reason": f"HTTP {resp.status_code}", "jobs": []}
        soup = BeautifulSoup(resp.text, "html.parser")
        jobs = []
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            text = link.get_text(strip=True)
            if "/careers/" in href and text and len(text) > 3 and text.lower() not in ("careers", "jobs"):
                job_url = urljoin(resp.url, href) if not href.startswith("http") else href
                jobs.append({
                    "job_id": "",
                    "title": text,
                    "location": "",
                    "department": "",
                    "employment_type": "",
                    "posted_date": "",
                    "job_url": job_url,
                    "application_url": "",
                    "description": "",
                })
        jobs = _deduplicate_jobs(jobs)
        return {"result": "active_jobs_extracted" if jobs else "confirmed_no_openings", "reason": f"slug={slug}", "jobs": jobs}
    except Exception as exc:
        return {"result": "crawl_failed", "reason": f"{type(exc).__name__}: {exc}", "jobs": []}


def _extract_bamboohr_slug(url: str) -> str:
    lower = url.lower()
    m = re.search(r"([a-z0-9-]+)\.bamboohr\.com", lower)
    if m:
        return m.group(1)
    return ""


async def _scrape_bamboohr_slug(client: httpx.AsyncClient, url: str) -> str:
    """Scrape career page to find BambooHR embed URL."""
    try:
        resp = await client.get(url, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            return ""
        text = resp.text
        m = re.search(r"([a-z0-9-]+)\.bamboohr\.com", text)
        if m:
            return m.group(1)
        soup = BeautifulSoup(text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "bamboohr" in href.lower():
                m = re.search(r"([a-z0-9-]+)\.bamboohr\.com", href)
                if m:
                    return m.group(1)
        return ""
    except Exception:
        return ""


async def extract_adp(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """ADP Workforce Now: extract from the recruitment iframe URL."""
    lower = url.lower()
    if "workforcenow.adp.com" not in lower:
        return {"result": "unsupported_platform_variant", "reason": "Not an ADP Workforce Now URL", "jobs": []}
    # ADP recruitment pages are SPA-rendered; we can try to fetch the job listing JSON
    # The cid parameter identifies the employer
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    cid = qs.get("cid", [""])[0]
    if not cid:
        # Try to find it in the path
        m = re.search(r"cid[=:]\s*([a-f0-9-]+)", url, re.I)
        if m:
            cid = m.group(1)
    if not cid:
        return {"result": "unsupported_platform_variant", "reason": "Cannot extract ADP client ID", "jobs": []}
    # ADP doesn't have a public JSON API for job listings; needs browser rendering
    return {"result": "unsupported_platform_variant", "reason": f"ADP WFN requires browser rendering (cid={cid[:16]}...)", "jobs": []}


async def extract_dayforce(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """Dayforce career site — typically SPA-rendered."""
    return {"result": "unsupported_platform_variant", "reason": "Dayforce career sites require browser rendering", "jobs": []}


async def extract_icims(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """iCIMS career site — often embedded iframe or SPA."""
    lower = url.lower()
    # Some iCIMS sites expose a job listing at /jobs/search
    try:
        resp = await client.get(url, timeout=20, follow_redirects=True)
        if resp.status_code != 200:
            return {"result": "crawl_failed", "reason": f"HTTP {resp.status_code}", "jobs": []}
        soup = BeautifulSoup(resp.text, "html.parser")
        jobs = []
        # Look for job listing links
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            text = link.get_text(strip=True)
            if ("icims.com/jobs/" in href or "/jobs/" in href) and text and len(text) > 3:
                job_url = urljoin(resp.url, href) if not href.startswith("http") else href
                jobs.append({
                    "job_id": "",
                    "title": text,
                    "location": "",
                    "department": "",
                    "employment_type": "",
                    "posted_date": "",
                    "job_url": job_url,
                    "application_url": "",
                    "description": "",
                })
        jobs = _deduplicate_jobs(jobs)
        if jobs:
            return {"result": "active_jobs_extracted", "reason": "", "jobs": jobs}
        return {"result": "unsupported_platform_variant", "reason": "iCIMS page has no visible job listings (may be iframe/SPA)", "jobs": []}
    except Exception as exc:
        return {"result": "crawl_failed", "reason": f"{type(exc).__name__}: {exc}", "jobs": []}


async def extract_oracle(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """Oracle HCM Cloud candidate experience — SPA-rendered."""
    return {"result": "unsupported_platform_variant", "reason": "Oracle HCM Cloud requires browser rendering", "jobs": []}


async def extract_taleo(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """Oracle Taleo career site — typically SPA-rendered."""
    return {"result": "unsupported_platform_variant", "reason": "Taleo career sites require browser rendering", "jobs": []}


async def extract_successfactors(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """SAP SuccessFactors — SPA-rendered."""
    return {"result": "unsupported_platform_variant", "reason": "SuccessFactors requires browser rendering", "jobs": []}


async def extract_rippling(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """Rippling career sites — SPA-rendered."""
    return {"result": "unsupported_platform_variant", "reason": "Rippling career sites require browser rendering", "jobs": []}


async def extract_jobvite(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """Jobvite career sites — often SPA-rendered."""
    return {"result": "unsupported_platform_variant", "reason": "Jobvite career sites typically require browser rendering", "jobs": []}


async def extract_ukg_pro(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """UKG Pro (UltiPro) career sites — SPA-rendered."""
    return {"result": "unsupported_platform_variant", "reason": "UKG Pro career sites require browser rendering", "jobs": []}


async def extract_applytojob(
    client: httpx.AsyncClient, url: str, org_name: str
) -> dict[str, Any]:
    """ApplyToJob — iframe-embedded career pages."""
    return {"result": "unsupported_platform_variant", "reason": "ApplyToJob pages are iframe-embedded", "jobs": []}


# ---------------------------------------------------------------------------
# Browser-rendered extractors (Playwright, run separately)
# ---------------------------------------------------------------------------

ASYNC_EXTRACTORS: dict[str, Any] = {
    "greenhouse": extract_greenhouse,
    "lever": extract_lever,
    "ashby": extract_ashby,
    "smartrecruiters": extract_smartrecruiters,
    "workable": extract_workable,
    "recruitee": extract_recruitee,
    "workday": extract_workday,
    "bamboohr": extract_bamboohr,
    "adp": extract_adp,
    "dayforce": extract_dayforce,
    "icims": extract_icims,
    "oracle": extract_oracle,
    "taleo": extract_taleo,
    "successfactors": extract_successfactors,
    "rippling": extract_rippling,
    "jobvite": extract_jobvite,
    "ukg_pro": extract_ukg_pro,
    "applytojob": extract_applytojob,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator=" ", strip=True)


def _deduplicate_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    unique = []
    for j in jobs:
        key = (j.get("title", "").lower(), j.get("job_url", "").lower())
        if key not in seen:
            seen.add(key)
            unique.append(j)
    return unique


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


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


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def collect_ats_records() -> list[dict[str, str]]:
    """Collect all ATS-related records from Stages 1, 2A, and 2B."""
    seen_ids: set[str] = set()
    records: list[dict[str, str]] = []

    def _add(record_id: str, source_stage: str, org_name: str, ats_provider: str,
             career_url: str, monitor_url: str, previous_result: str):
        key = record_id
        if key in seen_ids:
            return
        seen_ids.add(key)
        records.append({
            "record_id": record_id,
            "organization_name": org_name,
            "ats_provider": normalize_provider(ats_provider),
            "career_url": career_url,
            "resolved_monitor_url": monitor_url,
            "source_stage": source_stage,
            "previous_result": previous_result,
        })

    # Stage 1: ATS-confirmed and ATS-needs-review records
    stage1_path = Path("output/career_pages_validated_all.csv")
    if stage1_path.exists():
        with stage1_path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                vr = clean(row.get("validation_result"))
                if vr not in ("confirmed_external_ats_active", "external_ats_needs_review", "confirmed_external_ats_no_openings"):
                    continue
                rid = clean(row.get("record_id"))
                provider = clean(row.get("ats_provider_detected")) or clean(row.get("detected_ats"))
                if not provider:
                    continue
                career = clean(row.get("career_url")) or clean(row.get("loaded_url")) or clean(row.get("requested_url"))
                _add(rid, "stage1", clean(row.get("organization_name_original")), provider, career, career, vr)

    # Stage 2A: ATS-resolved and platform-pass records
    stage2a_path = Path("output/stage2_probable_resolved_all.csv")
    if stage2a_path.exists():
        with stage2a_path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                s2r = clean(row.get("stage2_result"))
                if s2r not in ("confirmed_external_ats_active", "confirmed_external_ats_no_openings", "external_ats_needs_platform_pass"):
                    continue
                rid = clean(row.get("record_id"))
                provider = clean(row.get("resolved_ats"))
                if not provider:
                    continue
                career = clean(row.get("career_url"))
                monitor = clean(row.get("resolved_monitor_url")) or career
                _add(rid, "stage2a", clean(row.get("organization_name_original")), provider, career, monitor, s2r)

    # Stage 2B: resolved_external_ats records
    stage2b_path = Path("output/stage2b_resolved_all.csv")
    if stage2b_path.exists():
        with stage2b_path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                s2br = clean(row.get("stage2b_result"))
                if s2br != "resolved_external_ats":
                    continue
                rid = clean(row.get("record_id"))
                provider = clean(row.get("stage2b_provider"))
                if not provider:
                    continue
                career = clean(row.get("career_url"))
                monitor = clean(row.get("stage2b_monitor_url")) or clean(row.get("resolved_monitor_url")) or career
                _add(rid, "stage2b", clean(row.get("organization_name_original")), provider, career, monitor, s2br)

    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_extraction(records: list[dict[str, str]], jsonl_path: Path) -> None:
    completed = load_completed(jsonl_path)
    pending = [r for r in records if r["record_id"] not in completed]
    print(f"Total ATS targets: {len(records)}, already completed: {len(completed)}, pending: {len(pending)}")

    if not pending:
        return

    provider_counts: dict[str, int] = {}
    for r in pending:
        p = r["ats_provider"]
        provider_counts[p] = provider_counts.get(p, 0) + 1
    print("Pending by provider:")
    for p in sorted(provider_counts, key=lambda x: -provider_counts[x]):
        adapter = "API" if p in ASYNC_EXTRACTORS else "NO_ADAPTER"
        print(f"  {p:25s}: {provider_counts[p]:4d}  [{adapter}]")

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=25,
        headers={"User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0; +https://example.com/bot)"},
    ) as client:
        for i, record in enumerate(pending):
            provider = record["ats_provider"]
            url = record["resolved_monitor_url"] or record["career_url"]
            org = record["organization_name"]
            rid = record["record_id"]

            extractor = ASYNC_EXTRACTORS.get(provider)
            if not extractor:
                result = {
                    **record,
                    "ats_extraction_checked_at_utc": datetime.now(timezone.utc).isoformat(),
                    "ats_extraction_result": "unsupported_platform_variant",
                    "ats_extraction_reason": f"No adapter implemented for provider: {provider}",
                    "ats_extraction_jobs_found": 0,
                    "ats_extraction_jobs_json": "[]",
                    "ats_extraction_error": "",
                }
                append_jsonl(jsonl_path, result)
                if (i + 1) % 50 == 0:
                    print(f"  [{i+1}/{len(pending)}] {provider} — no adapter, skipped")
                continue

            try:
                outcome = await extractor(client, url, org)
            except Exception as exc:
                outcome = {"result": "crawl_failed", "reason": f"{type(exc).__name__}: {exc}", "jobs": []}

            result = {
                **record,
                "ats_extraction_checked_at_utc": datetime.now(timezone.utc).isoformat(),
                "ats_extraction_result": outcome["result"],
                "ats_extraction_reason": outcome.get("reason", ""),
                "ats_extraction_jobs_found": len(outcome.get("jobs", [])),
                "ats_extraction_jobs_json": json.dumps(outcome.get("jobs", []), ensure_ascii=False),
                "ats_extraction_error": "",
            }
            append_jsonl(jsonl_path, result)

            if (i + 1) % 25 == 0:
                print(f"  [{i+1}/{len(pending)}] Latest: {provider} → {outcome['result']} ({len(outcome.get('jobs', []))} jobs)")

            # Small delay to avoid hammering APIs
            await asyncio.sleep(0.5)


def export_outputs(records: list[dict], jsonl_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. ats_targets_normalized.csv
    target_fields = [
        "record_id", "organization_name", "ats_provider", "career_url",
        "resolved_monitor_url", "source_stage", "previous_result",
    ]
    target_path = out_dir / "ats_targets_normalized.csv"
    with target_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=target_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    print(f"Wrote {target_path} ({len(records)} rows)")

    # 2. ats_extraction_status.csv
    status_fields = [
        "record_id", "organization_name", "ats_provider", "resolved_monitor_url",
        "ats_extraction_result", "ats_extraction_reason", "ats_extraction_jobs_found",
        "ats_extraction_error", "ats_extraction_checked_at_utc",
        "source_stage", "previous_result",
    ]
    status_path = out_dir / "ats_extraction_status.csv"
    with status_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=status_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    print(f"Wrote {status_path} ({len(records)} rows)")

    # 3. jobs_current.csv
    job_rows: list[dict[str, str]] = []
    job_fields = [
        "record_id", "organization_name", "ats_provider",
        "job_id", "title", "location", "department", "employment_type",
        "posted_date", "job_url", "application_url", "description",
        "first_seen", "last_seen",
    ]
    now_iso = datetime.now(timezone.utc).isoformat()
    for rec in records:
        jobs_json = rec.get("ats_extraction_jobs_json", "[]")
        try:
            jobs = json.loads(jobs_json) if jobs_json else []
        except Exception:
            jobs = []
        for job in jobs:
            job_rows.append({
                "record_id": rec.get("record_id", ""),
                "organization_name": rec.get("organization_name", ""),
                "ats_provider": rec.get("ats_provider", ""),
                "job_id": job.get("job_id", ""),
                "title": job.get("title", ""),
                "location": job.get("location", ""),
                "department": job.get("department", ""),
                "employment_type": job.get("employment_type", ""),
                "posted_date": job.get("posted_date", ""),
                "job_url": job.get("job_url", ""),
                "application_url": job.get("application_url", ""),
                "description": job.get("description", ""),
                "first_seen": now_iso,
                "last_seen": now_iso,
            })
    jobs_path = out_dir / "jobs_current.csv"
    with jobs_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=job_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(job_rows)
    print(f"Wrote {jobs_path} ({len(job_rows)} jobs from {len(records)} targets)")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 2C: Unified ATS job extraction")
    parser.add_argument("--jsonl", default="output/ats_extraction_all.jsonl")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--limit", type=int, default=0, help="0 = all records")
    args = parser.parse_args()

    jsonl_path = Path(args.jsonl)
    out_dir = Path(args.output_dir)

    print("Collecting ATS records from all stages...")
    records = collect_ats_records()
    print(f"Collected {len(records)} unique ATS targets")

    # Provider breakdown
    provider_counts: dict[str, int] = {}
    for r in records:
        p = r["ats_provider"]
        provider_counts[p] = provider_counts.get(p, 0) + 1
    print("By provider:")
    for p in sorted(provider_counts, key=lambda x: -provider_counts[x]):
        print(f"  {p:25s}: {provider_counts[p]:4d}")

    # Source stage breakdown
    stage_counts: dict[str, int] = {}
    for r in records:
        s = r["source_stage"]
        stage_counts[s] = stage_counts.get(s, 0) + 1
    print("By source stage:")
    for s in sorted(stage_counts, key=lambda x: -stage_counts[x]):
        print(f"  {s:10s}: {stage_counts[s]:4d}")

    if args.limit > 0:
        records = records[:args.limit]
        print(f"Limited to {len(records)} records")

    await run_extraction(records, jsonl_path)

    # Re-read all from JSONL for export
    all_results: list[dict] = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                all_results.append(json.loads(line))

    export_outputs(all_results, jsonl_path, out_dir)

    # Summary
    result_counts: dict[str, int] = {}
    total_jobs = 0
    for r in all_results:
        er = r.get("ats_extraction_result", "unknown")
        result_counts[er] = result_counts.get(er, 0) + 1
        total_jobs += r.get("ats_extraction_jobs_found", 0)
    print(f"\nExtraction summary:")
    for er in sorted(result_counts, key=lambda x: -result_counts[x]):
        print(f"  {er:40s}: {result_counts[er]:4d}")
    print(f"  Total jobs extracted: {total_jobs}")


if __name__ == "__main__":
    asyncio.run(main())
