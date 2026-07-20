# Stage 2B — Network and Interactive Resolution

Stage 2A left 681 records classified as:

```text
confirmed_career_landing_unresolved
```

They are valid career landing pages, but their downstream job source was not
found through ordinary links.

Stage 2B performs a deeper inspection. It listens to browser network traffic,
checks XHR and fetch responses, searches for embedded recruiting systems,
examines forms and iframes, and clicks a maximum of two high-value job buttons.

It does **not** submit forms, apply for jobs, log in, or bypass access controls.

## Files

Place these files in your existing `~/career-monitor` directory:

- `resolve_unresolved_landings.py`
- `stage2b_unresolved_career_landings.csv`

Your existing `.venv`, Crawlee installation, and Chromium installation can be
reused.

## 1. Activate the environment

```bash
cd ~/career-monitor
source .venv/bin/activate
```

## 2. Run five visible records

```bash
python resolve_unresolved_landings.py \
  --input stage2b_unresolved_career_landings.csv \
  --output output/stage2b_test_5.csv \
  --jsonl output/stage2b_test_5.jsonl \
  --limit 5 \
  --headful \
  --concurrency 1 \
  --tasks-per-minute 5
```

Inspect:

- `stage2b_result`
- `stage2b_reason`
- `stage2b_monitor_url`
- `stage2b_provider`
- `stage2b_job_api_url`
- `stage2b_network_candidates_json`
- `stage2b_static_urls`
- `stage2b_click_results_json`
- `stage2b_error`

## 3. Run a 25-record pilot

Use fresh output names:

```bash
python resolve_unresolved_landings.py \
  --input stage2b_unresolved_career_landings.csv \
  --output output/stage2b_pilot_25.csv \
  --jsonl output/stage2b_pilot_25.jsonl \
  --limit 25 \
  --concurrency 1 \
  --tasks-per-minute 8
```

Stage 2B performs extra reloads, response-body inspection, scrolling, and
optional clicks. Keep concurrency at 1 during the pilot.

## 4. Full run

```bash
python resolve_unresolved_landings.py \
  --input stage2b_unresolved_career_landings.csv \
  --output output/stage2b_resolved_all.csv \
  --jsonl output/stage2b_resolved_all.jsonl \
  --limit 0 \
  --concurrency 1 \
  --tasks-per-minute 10
```

The run is resumable. If interrupted, run the same command again. Completed
`record_id` values in the JSONL file are skipped.

## Important result values

- `resolved_external_ats` — a recruiting platform was found
- `resolved_job_api` — a job-like JSON or network endpoint was found
- `resolved_general_job_board` — a third-party board such as Indeed was found
- `confirmed_career_page_no_openings` — explicit no-opening message
- `passive_application_form_only` — general resume/application form only
- `passive_application_only` — accepts future resumes but has no current job list
- `career_page_still_unresolved` — valid page, but still no downstream source
- `likely_false_positive` — not an employer job page
- `blocked_or_challenge` — access challenge
- `needs_manual_review` — inconclusive
- `crawl_failed` — all retries failed

## Why this is separate from the ATS adapter stage

This script discovers the platform or API behind an unresolved landing page.
It does not yet normalize every job from Workday, Dayforce, iCIMS, ADP, and
other ATS systems. That extraction stage follows after this resolver.
