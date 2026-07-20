# Stage 2A — Resolve Probable Career Pages

Stage 1 produced 1,373 rows classified as:

```text
probable_career_page_needs_review
```

These are valid pages with strong career language, but Stage 1 did not see job
links or an explicit no-openings message.

This pass:

1. scrolls the page;
2. finds job/career buttons, links, iframes, and embedded ATS URLs;
3. visits up to three likely destinations;
4. chooses a better monitor URL;
5. identifies pages that require a later ATS-specific pass;
6. saves each completed record immediately to JSONL.

## Files to place in your existing career-monitor folder

- `resolve_probable_career_pages.py`
- `probable_career_pages.csv`

Your existing `.venv` and installed dependencies can be reused.

## First: a visible five-record test

```bash
source .venv/bin/activate

python resolve_probable_career_pages.py \
  --input probable_career_pages.csv \
  --output output/stage2_test_5.csv \
  --jsonl output/stage2_test_5.jsonl \
  --limit 5 \
  --headful \
  --concurrency 1 \
  --tasks-per-minute 10
```

Review these fields:

- `stage2_result`
- `stage2_reason`
- `resolved_monitor_url`
- `resolved_ats`
- `candidate_urls_found`
- `candidate_visits_json`
- `stage2_error`

## Then: a 25-record pilot

Use separate output names so the five-record test is not mixed into the pilot:

```bash
python resolve_probable_career_pages.py \
  --input probable_career_pages.csv \
  --output output/stage2_pilot_25.csv \
  --jsonl output/stage2_pilot_25.jsonl \
  --limit 25 \
  --concurrency 2 \
  --tasks-per-minute 20
```

## Full run

```bash
python resolve_probable_career_pages.py \
  --input probable_career_pages.csv \
  --output output/stage2_probable_resolved_all.csv \
  --jsonl output/stage2_probable_resolved_all.jsonl \
  --limit 0 \
  --concurrency 2 \
  --tasks-per-minute 20
```

`--limit 0` means all 1,373 rows.

## Restarting after interruption

Run the exact same full-run command. The script reads the JSONL file and skips
record IDs that were already completed.

Do not delete:

```text
output/stage2_probable_resolved_all.jsonl
```

until the run has finished and the CSV has been verified.

## Meaning of important Stage 2 results

- `confirmed_career_page_active` — current jobs detected
- `confirmed_career_page_no_openings` — valid career page, no current jobs
- `confirmed_external_ats_active` — an external ATS with jobs was resolved
- `confirmed_external_ats_no_openings` — ATS resolved, no current jobs
- `external_ats_needs_platform_pass` — ATS was found, but requires its dedicated adapter
- `confirmed_career_landing_unresolved` — definitely a career page, but downstream listing still unresolved
- `likely_false_positive` — career-related content that should not be monitored
- `blocked_or_challenge` — access challenge
- `crawl_failed` — retries failed
- `needs_manual_review` — still inconclusive
