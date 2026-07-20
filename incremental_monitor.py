#!/usr/bin/env python3
"""Stage 4 — safe incremental job monitoring."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from monitoring.canonicalize import canonicalize_snapshot
from monitoring.core import (
    MonitorError, clean, compare_jobs, csv_rows, filter_sources, write_csv,
)
from monitoring.database import (
    apply_commit, backfill_baseline_metadata, load_baseline_jobs,
    migrate_database, read_source_states,
)

REPORTS = {
    "NEW": "new_jobs.csv", "CHANGED": "changed_jobs.csv",
    "POSSIBLY_REMOVED": "possibly_removed_jobs.csv",
    "REMOVED": "removed_jobs.csv", "REOPENED": "reopened_jobs.csv",
}


def parse_args():
    p = argparse.ArgumentParser(description="Stage 4 incremental job monitor")
    p.add_argument("--db", type=Path, default=Path("output/job_monitor.db"))
    p.add_argument("--baseline-csv", type=Path, default=Path("output/jobs_canonical.csv"))
    p.add_argument("--sources", type=Path, default=Path("output/job_sources.csv"))
    p.add_argument("--snapshot", type=Path, default=Path("output/jobs_current.csv"))
    p.add_argument("--status-file", type=Path, default=Path("output/extraction_status.csv"))
    p.add_argument("--output-root", type=Path, default=Path("output/runs"))
    p.add_argument("--extractor-script", type=Path, default=Path("extract_jobs_unified.py"))
    p.add_argument("--run-extractor", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--source-id", default="")
    p.add_argument("--source-type", default="")
    p.add_argument("--provider", default="")
    p.add_argument("--confirm-removal-after", type=int, default=2)
    p.add_argument("--timeout", type=int, default=25)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--commit", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    return p.parse_args()


def run_extractor(args, run_dir: Path):
    jsonl = run_dir / "stage2c_extraction.jsonl"
    command = [
        sys.executable, str(args.extractor_script), "--jsonl", str(jsonl),
        "--output-dir", str(run_dir), "--limit", str(args.limit),
        "--timeout", str(args.timeout), "--concurrency", str(args.concurrency),
    ]
    if args.source_id:
        command.extend(["--source-id", args.source_id])
    if args.source_type:
        command.extend(["--source-type", args.source_type])
    if args.provider:
        command.extend(["--provider", args.provider])
    if args.sources.exists():
        command.extend(["--sources-file", str(args.sources)])
    print("Running extractor:", " ".join(command))
    completed = subprocess.run(command, check=False)
    if completed.returncode:
        raise MonitorError(f"Extractor exited with code {completed.returncode}")
    return run_dir / "jobs_current.csv", run_dir / "extraction_status.csv"


def main() -> int:
    args = parse_args()
    if args.confirm_removal_after < 2:
        raise MonitorError("--confirm-removal-after must be at least 2")
    if not args.db.exists():
        raise MonitorError(f"Database does not exist: {args.db}")

    now = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    batch_id = str(uuid.uuid4())
    run_dir = args.output_root / f"{stamp}-{batch_id[:8]}"
    run_dir.mkdir(parents=True, exist_ok=False)

    if args.run_extractor:
        snapshot_path, status_path = run_extractor(args, run_dir)
    else:
        snapshot_path = args.snapshot
        status_path = args.status_file if args.status_file.exists() else None

    if status_path:
        statuses = csv_rows(status_path)
    else:
        statuses = csv_rows(args.sources)
        for row in statuses:
            row["extraction_result"] = "status_unavailable"
            row["extraction_error"] = "Fresh extraction status was not provided"

    statuses = filter_sources(
        statuses, limit=args.limit, source_id=args.source_id,
        source_type=args.source_type, provider=args.provider,
    )
    selected = {clean(row.get("record_id")) for row in statuses}
    if not selected:
        raise MonitorError("No sources matched the requested filters")

    raw_jobs = [row for row in csv_rows(snapshot_path)
                if clean(row.get("record_id")) in selected]
    canonical, rejected = canonicalize_snapshot(raw_jobs)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    existing = load_baseline_jobs(conn, args.baseline_csv)
    try:
        prior_states = read_source_states(conn)
    except sqlite3.DatabaseError:
        prior_states = {}
    comparison = compare_jobs(
        existing, canonical, statuses,
        confirm_removal_after=args.confirm_removal_after,
        prior_source_states=prior_states, now=now,
    )

    write_csv(run_dir / "jobs_snapshot_canonical.csv", list(comparison.current_jobs.values()))
    write_csv(run_dir / "jobs_snapshot_rejected.csv", rejected)
    for event_type, filename in REPORTS.items():
        write_csv(run_dir / filename,
                  [e for e in comparison.events if e.get("event_type") == event_type])
    write_csv(run_dir / "source_failures.csv", comparison.source_failures)
    write_csv(run_dir / "source_recoveries.csv", comparison.source_recoveries)
    write_csv(run_dir / "source_indeterminate.csv", comparison.source_indeterminate)

    counts = Counter(clean(e.get("event_type")) for e in comparison.events)
    summary = {
        "run_batch_id": batch_id, "generated_at_utc": now,
        "mode": "commit" if args.commit else "dry-run",
        "run_directory": str(run_dir), "snapshot_path": str(snapshot_path),
        "status_path": str(status_path) if status_path else "",
        "selected_sources": len(statuses),
        "successful_sources": len(comparison.successful_source_ids),
        "failed_sources": len(comparison.failed_source_ids),
        "indeterminate_sources": len(comparison.indeterminate_source_ids),
        "raw_snapshot_jobs": len(raw_jobs),
        "canonical_snapshot_jobs": len(canonical),
        "rejected_snapshot_jobs": len(rejected),
        "events": dict(counts),
        "source_failures": len(comparison.source_failures),
        "source_recoveries": len(comparison.source_recoveries),
        "source_indeterminate": len(comparison.source_indeterminate),
    }

    if args.commit:
        conn.close()
        if not args.no_backup:
            backup = args.db.with_name(f"{args.db.name}.backup-{stamp}")
            shutil.copy2(args.db, backup)
            summary["database_backup"] = str(backup)
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        try:
            migrate_database(conn)
            conn.commit()  # migration first; monitoring state below is atomic
            conn.execute("BEGIN IMMEDIATE")
            backfill_baseline_metadata(conn, args.baseline_csv)
            apply_commit(
                conn, comparison, statuses, run_batch_id=batch_id,
                snapshot_path=snapshot_path, now=now,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn.close()

    (run_dir / "run_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"Reports written to {run_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MonitorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
