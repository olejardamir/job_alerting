# Job Alerting Monitor

## Quick Start

### First run (establish baseline)

```bash
python incremental_monitor.py --run-extractor --commit
```

This visits all 3,334 sources in `output/job_sources.csv`, extracts current jobs, and saves them to the database. All found jobs are treated as the baseline — nothing is reported as "new" on this run.

### Every subsequent run (find new jobs)

```bash
python incremental_monitor.py --run-extractor --commit
```

Same command. It compares current extractions against the previous baseline and outputs only **new** job openings.

### Output

- **Terminal**: `12 new job openings found` (or `No new job openings since the previous check.`)
- **CSV**: `output/runs/<timestamp>/new_jobs.csv`
- **Summary**: `output/runs/<timestamp>/run_summary.json`

### View all job URLs

```bash
# All 3,469+ job URLs in one file
output/all_job_urls.csv
```

### View latest new jobs only

```bash
# List the most recent run directory
ls output/runs/ | tail -1

# Then view the new jobs
cat output/runs/<latest_run>/new_jobs.csv
```

## Options

| Flag | Effect |
|------|--------|
| `--run-extractor` | Run the extraction pipeline (without this, uses cached extraction) |
| `--commit` | Save results to the database (dry-run by default) |
| `--limit N` | Process only N sources (for testing) |
| `--source-type TYPE` | Filter to one source type (e.g. `ats`, `static_html_listing`) |
| `--provider NAME` | Filter to one provider (e.g. `greenhouse`, `lever`) |
| `--source-id ID` | Process a single source by record ID |

### Examples

```bash
# Quick test: 10 sources, no database commit
python incremental_monitor.py --run-extractor --limit 10

# Only ATS sources
python incremental_monitor.py --run-extractor --commit --source-type ats

# Only Greenhouse
python incremental_monitor.py --run-extractor --commit --provider greenhouse
```

## Database

- SQLite: `output/job_monitor.db`
- Backup created automatically before each committed run
- Backup location: `output/job_monitor.db.backup-<timestamp>`
