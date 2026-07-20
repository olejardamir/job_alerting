#!/usr/bin/env python3
"""
LLM-based job extraction for uncertain sources.

Uses the local OpenCode gateway (http://127.0.0.1:14096) with the
big-pickle model to extract job postings from HTML pages that the
standard extractors couldn't handle.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.request
import urllib.error
from typing import Any

from bs4 import BeautifulSoup


# OpenCode gateway configuration
OPENCODE_BASE_URL = "http://127.0.0.1:14096"
OPENCODE_MODEL = "big-pickle"
OPENCODE_PROVIDER = "opencode"


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text, removing scripts and styles."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "nav", "footer", "header"]):
        element.decompose()
    return soup.get_text(separator=" ", strip=True)[:8000]


def _create_session() -> str:
    """Create an OpenCode session and return the session ID."""
    req = urllib.request.Request(
        f"{OPENCODE_BASE_URL}/session",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
        return data["id"]


def _send_message(session_id: str, prompt: str, system: str = "") -> str:
    """Send a message to the LLM and return the response text."""
    body: dict[str, Any] = {
        "model": {"providerID": OPENCODE_PROVIDER, "modelID": OPENCODE_MODEL},
        "parts": [{"type": "text", "text": prompt}],
    }
    if system:
        body["system"] = system

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{OPENCODE_BASE_URL}/session/{session_id}/message",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())

    # Extract text from response parts
    text_parts = []
    for part in result.get("parts", []):
        if part.get("type") == "text":
            text_parts.append(part.get("text", ""))
    return "\n".join(text_parts)


EXTRACTION_PROMPT = """Extract all job openings from this career page. For each job, extract:
- title: Job title
- location: Job location (city, province/state, country)
- department: Department or team (if available)
- employment_type: Full-time, Part-time, Contract, etc. (if available)
- job_url: URL to apply or view the job (if available)

Return ONLY a JSON array of job objects. If no jobs are found, return an empty array [].
Do not include any explanation or markdown formatting, just the JSON.

Example format:
[
  {
    "title": "Software Developer",
    "location": "Toronto, ON, Canada",
    "department": "Engineering",
    "employment_type": "Full-time",
    "job_url": "https://example.com/jobs/123"
  }
]

If the page contains no job openings, return: []"""


def extract_jobs_with_llm(html: str, url: str) -> list[dict[str, Any]]:
    """Extract jobs from HTML using the LLM.
    
    Args:
        html: Raw HTML content from the career page
        url: URL of the career page
        
    Returns:
        List of job dictionaries with standardized fields
    """
    if not html or len(html) < 100:
        return []

    # Convert HTML to text for the LLM
    text = _html_to_text(html)
    if not text or len(text) < 50:
        return []

    # Create session and send prompt
    try:
        session_id = _create_session()
        prompt = f"URL: {url}\n\nPage content:\n{text[:6000]}"
        response = _send_message(session_id, prompt, EXTRACTION_PROMPT)

        # Parse JSON response
        # Try to extract JSON from the response (might be wrapped in markdown)
        json_match = re.search(r'\[[\s\S]*?\]', response)
        if json_match:
            jobs = json.loads(json_match.group())
        else:
            jobs = json.loads(response)

        # Standardize job fields
        standardized = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            title = job.get("title", "").strip()
            if not title:
                continue

            standardized.append({
                "job_id": job.get("job_id", "") or job.get("id", ""),
                "title": title,
                "location": job.get("location", ""),
                "department": job.get("department", ""),
                "employment_type": job.get("employment_type", ""),
                "work_arrangement": job.get("work_arrangement", ""),
                "salary_min": "",
                "salary_max": "",
                "currency": "",
                "posted_date": job.get("posted_date", ""),
                "closing_date": job.get("closing_date", ""),
                "description": job.get("description", "")[:2000],
                "job_url": job.get("job_url", "") or job.get("url", ""),
                "application_url": job.get("application_url", ""),
                "application_email": "",
                "application_method": "",
            })

        return standardized

    except (json.JSONDecodeError, urllib.error.URLError, Exception) as e:
        # Log error but don't fail - return empty list
        print(f"  LLM extraction failed for {url}: {e}")
        return []


async def extract_jobs_with_llm_async(
    html: str,
    url: str,
    semaphore: asyncio.Semaphore | None = None,
) -> list[dict[str, Any]]:
    """Async wrapper for LLM extraction."""
    if semaphore:
        async with semaphore:
            return await asyncio.to_thread(extract_jobs_with_llm, html, url)
    return await asyncio.to_thread(extract_jobs_with_llm, html, url)


def is_llm_worthy(extraction_result: str, jobs_found: int, source_type: str) -> bool:
    """Determine if a source should be retried with LLM extraction.

    Only needs_manual_review triggers LLM — these are pages where the
    standard extractors couldn't determine the source type or extract jobs.
    Everything else (static_html_listing, iframe_listing, ats) already ran
    its specialized extractor and the result is reliable.
    """
    return extraction_result == "needs_manual_review"
