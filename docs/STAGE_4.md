# Stage 4 — Incremental Job Monitor

Stage 4 compares a fresh Stage 2C extraction with the 5,063-job Stage 3A
baseline in `output/job_monitor.db`.

## Safety model

- Dry-run is the default. SQLite changes require `--commit`.
- A failed, blocked, unsupported, or indeterminate source never removes jobs.
- A missing job is first marked `POSSIBLY_REMOVED`.
- Removal requires two consecutive successful source snapshots by default.
- Empty fields from a sparse extraction do not erase richer baseline values.
- A timestamped database backup is created before each committed run.
- Job identity falls back from canonical ID to source job ID and canonical URL,
  reducing false new/removal pairs when a title or location changes.

## Events

- `NEW`
- `CHANGED`
- `POSSIBLY_REMOVED`
- `REMOVED`
- `REOPENED`
- `SOURCE_FAILED`
- `SOURCE_RECOVERED`

## First dry run using existing files

This checks the monitor against the currently committed Stage 2C snapshot. It
does not crawl and does not alter SQLite.

```bash
python incremental_monitor.py \
  --db output/job_monitor.db \
  --snapshot output/jobs_current.csv \
  --status-file output/extraction_status.csv \
  --limit 10
```

Every run writes to a timestamped directory under `output/runs/`:

```text
jobs_snapshot_canonical.csv
jobs_snapshot_rejected.csv
new_jobs.csv
changed_jobs.csv
possibly_removed_jobs.csv
removed_jobs.csv
reopened_jobs.csv
source_failures.csv
source_recoveries.csv
run_summary.json
```

## Fresh extraction plus comparison

The monitor can invoke the existing Stage 2C extractor first:

```bash
python incremental_monitor.py \
  --run-extractor \
  --limit 10
```

This writes the fresh Stage 2C files inside the timestamped run directory and
then performs a dry-run comparison.

The extractor currently applies `--limit` before extraction. `--source-id`,
`--source-type`, and `--provider` limit the comparison scope; they do not yet
make Stage 2C selectively crawl a single source.

## Controlled commit

Back up the repository-level database yourself if desired; the script also
creates a timestamped backup automatically.

```bash
python incremental_monitor.py \
  --run-extractor \
  --limit 10 \
  --commit
```

A committed run:

1. migrates the Stage 3A schema for monitoring state;
2. backfills source metadata from `output/jobs_canonical.csv`;
3. records one `crawl_runs` row per checked source;
4. inserts job and source events;
5. updates current jobs and missing counters in one immediate transaction.

## Filtering

```bash
python incremental_monitor.py --provider greenhouse --limit 20
python incremental_monitor.py --source-type static_html_listing --limit 20
python incremental_monitor.py --source-id 123
```

## Removal confirmation

The default requires two consecutive successful snapshots:

```bash
python incremental_monitor.py --confirm-removal-after 2
```

Values below 2 are rejected.

## Tests

```bash
python -m unittest discover -s tests -v
```

The test suite covers:

- new jobs;
- changed jobs matched by URL;
- first and second successful absences;
- failure-safe removal protection;
- reopened jobs;
- sparse snapshot field preservation;
- conservative zero-job status handling;
- migration and transactional insertion against a Stage 3A-style database.

## Recommended rollout

1. Existing-snapshot dry run, 10 sources.
2. Fresh extraction dry run, 10 sources.
3. Fresh extraction committed run, 10 sources.
4. Dry run, 100 sources.
5. Full dry run.
6. Full committed run only after reports are reviewed.
