# Job Alerting

A multi-stage system for discovering employer career pages, identifying how
jobs are published, extracting normalized vacancies, deduplicating them, and
monitoring job-level changes.

## Current pipeline

1. **Stage 1:** validate candidate career URLs.
2. **Stage 2A:** resolve probable career pages.
3. **Stage 2B:** inspect network traffic and interactive job destinations.
4. **Stage 2C:** detect ATS, APIs, static HTML, iframes, job boards, email
   applications, and unknown platforms; extract normalized jobs.
5. **Stage 3A:** quality control, canonicalization, deduplication, and SQLite
   baseline creation.
6. **Stage 4:** incremental comparison and job-event tracking.

## Current baseline

- 7,909 raw extracted jobs
- 6,484 jobs after quality control
- 5,063 canonical jobs after deduplication
- SQLite baseline: `output/job_monitor.db`

## Stage 4

The incremental monitor is safe by default:

```bash
python incremental_monitor.py --limit 10
```

The command above is a dry run. To perform a fresh extraction first:

```bash
python incremental_monitor.py --run-extractor --limit 10
```

To update SQLite after reviewing dry-run reports:

```bash
python incremental_monitor.py --run-extractor --limit 10 --commit
```

See [`docs/STAGE_4.md`](docs/STAGE_4.md) for event semantics, removal
protection, output files, tests, and rollout instructions.

## Tests

```bash
python -m unittest discover -s tests -v
```
