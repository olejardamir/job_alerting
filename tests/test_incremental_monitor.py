from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from monitoring.core import changed_fields, classify_source, compare_jobs, source_is_successful
from monitoring.database import apply_commit, migrate_database, table_columns


class SourceStatusTests(unittest.TestCase):
    def test_unknown_ats_zero_is_not_success(self):
        self.assertFalse(source_is_successful({
            "source_type": "ats", "extraction_result": "no_jobs_found",
            "extraction_reason": "Unknown ATS: example", "jobs_found": "0",
            "extraction_error": "",
        }))

    def test_explicit_no_openings_is_success(self):
        self.assertTrue(source_is_successful({
            "source_type": "no_openings",
            "extraction_result": "confirmed_no_openings",
            "jobs_found": "0", "extraction_error": "",
        }))

    def test_generic_no_jobs_found_is_indeterminate(self):
        """Generic no_jobs_found must not advance removal counters."""
        self.assertFalse(source_is_successful({
            "source_type": "ats", "extraction_result": "no_jobs_found",
            "extraction_reason": "", "jobs_found": "0", "extraction_error": "",
        }))

    def test_static_html_no_jobs_found_is_indeterminate(self):
        """Even static HTML no_jobs_found is indeterminate without confirmation."""
        self.assertFalse(source_is_successful({
            "source_type": "static_html_listing", "extraction_result": "no_jobs_found",
            "extraction_reason": "", "jobs_found": "0", "extraction_error": "",
        }))

    def test_http_error_no_jobs_found_is_failure(self):
        """HTTP errors with no_jobs_found are failures, not indeterminate."""
        self.assertFalse(source_is_successful({
            "source_type": "ats", "extraction_result": "no_jobs_found",
            "extraction_reason": "", "jobs_found": "0",
            "extraction_error": "TimeoutError",
        }))

    def test_confirmed_career_page_no_openings_is_success(self):
        self.assertTrue(source_is_successful({
            "source_type": "ats",
            "extraction_result": "confirmed_career_page_no_openings",
            "jobs_found": "0", "extraction_error": "",
        }))


class ClassifySourceTests(unittest.TestCase):
    def test_jobs_extracted_zero_jobs_is_indeterminate(self):
        """jobs_extracted with 0 jobs is indeterminate, not success."""
        self.assertEqual(classify_source({
            "extraction_result": "jobs_extracted", "jobs_found": "0",
            "extraction_error": "",
        }), "indeterminate")

    def test_active_jobs_extracted_zero_jobs_is_indeterminate(self):
        self.assertEqual(classify_source({
            "extraction_result": "active_jobs_extracted", "jobs_found": "0",
            "extraction_error": "",
        }), "indeterminate")

    def test_confirmed_career_page_active_zero_jobs_is_indeterminate(self):
        self.assertEqual(classify_source({
            "extraction_result": "confirmed_career_page_active", "jobs_found": "0",
            "extraction_error": "",
        }), "indeterminate")

    def test_confirmed_external_ats_active_zero_jobs_is_indeterminate(self):
        self.assertEqual(classify_source({
            "extraction_result": "confirmed_external_ats_active", "jobs_found": "0",
            "extraction_error": "",
        }), "indeterminate")

    def test_jobs_extracted_with_jobs_is_success(self):
        self.assertEqual(classify_source({
            "extraction_result": "jobs_extracted", "jobs_found": "5",
            "extraction_error": "",
        }), "success")

    def test_confirmed_no_openings_is_success(self):
        self.assertEqual(classify_source({
            "extraction_result": "confirmed_no_openings", "jobs_found": "0",
            "extraction_error": "",
        }), "success")

    def test_confirmed_career_page_no_openings_is_success(self):
        self.assertEqual(classify_source({
            "extraction_result": "confirmed_career_page_no_openings", "jobs_found": "0",
            "extraction_error": "",
        }), "success")

    def test_confirmed_external_ats_no_openings_is_success(self):
        self.assertEqual(classify_source({
            "extraction_result": "confirmed_external_ats_no_openings", "jobs_found": "0",
            "extraction_error": "",
        }), "success")

    def test_crawl_failed_is_failure(self):
        self.assertEqual(classify_source({
            "extraction_result": "crawl_failed", "jobs_found": "0",
            "extraction_error": "TimeoutError",
        }), "failure")

    def test_no_jobs_found_is_indeterminate(self):
        self.assertEqual(classify_source({
            "extraction_result": "no_jobs_found", "jobs_found": "0",
            "extraction_error": "",
        }), "indeterminate")

    def test_unknown_result_is_indeterminate(self):
        """Unknown results default to indeterminate."""
        self.assertEqual(classify_source({
            "extraction_result": "something_new", "jobs_found": "0",
            "extraction_error": "",
        }), "indeterminate")


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.now = "2026-07-19T20:00:00+00:00"
        self.job = {
            "canonical_job_id": "url:acme:/jobs/1", "record_id": "10",
            "organization_name": "Acme", "source_job_id": "1",
            "title": "Software Engineer", "normalized_title": "software engineer",
            "location": "Ottawa, ON", "normalized_location": "Ottawa, ON, Canada",
            "job_url": "https://acme.example/jobs/1", "application_url": "",
            "description": "Build software.", "status": "active",
            "missing_successful_runs": 0,
        }
        self.success = {
            "record_id": "10", "organization_name": "Acme", "source_type": "ats",
            "source_provider": "greenhouse", "extraction_result": "jobs_extracted",
            "jobs_found": "1", "extraction_error": "",
        }

    def test_new_job(self):
        job = {**self.job, "canonical_job_id": "url:acme:/jobs/2",
               "source_job_id": "2", "job_url": "https://acme.example/jobs/2"}
        result = compare_jobs({}, [job], [self.success], now=self.now)
        self.assertEqual([e["event_type"] for e in result.events], ["NEW"])

    def test_changed_job_matches_by_url(self):
        current = {**self.job, "canonical_job_id": "hash:different",
                   "title": "Senior Software Engineer"}
        result = compare_jobs({self.job["canonical_job_id"]: self.job},
                              [current], [self.success], now=self.now)
        changed = [e for e in result.events if e["event_type"] == "CHANGED"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["canonical_job_id"], self.job["canonical_job_id"])

    def test_first_absence_is_possible_removal(self):
        status = {**self.success, "jobs_found": "0",
                  "extraction_result": "confirmed_no_openings"}
        result = compare_jobs({self.job["canonical_job_id"]: self.job}, [], [status],
                              confirm_removal_after=2, now=self.now)
        self.assertEqual(result.events[0]["event_type"], "POSSIBLY_REMOVED")
        self.assertEqual(result.events[0]["missing_successful_runs"], 1)

    def test_second_absence_removes(self):
        old = {**self.job, "status": "possibly_removed", "missing_successful_runs": 1}
        status = {**self.success, "jobs_found": "0",
                  "extraction_result": "confirmed_no_openings"}
        result = compare_jobs({old["canonical_job_id"]: old}, [], [status],
                              confirm_removal_after=2, now=self.now)
        self.assertEqual(result.events[0]["event_type"], "REMOVED")

    def test_failed_source_never_removes(self):
        failure = {**self.success, "extraction_result": "crawl_failed",
                   "jobs_found": "0", "extraction_error": "TimeoutError"}
        result = compare_jobs({self.job["canonical_job_id"]: self.job}, [], [failure], now=self.now)
        self.assertEqual(result.events, [])
        self.assertEqual(len(result.source_failures), 1)

    def test_indeterminate_source_never_removes(self):
        """Generic no_jobs_found must not advance removal counters."""
        indeterminate = {**self.success, "extraction_result": "no_jobs_found",
                         "jobs_found": "0", "extraction_error": ""}
        result = compare_jobs({self.job["canonical_job_id"]: self.job}, [], [indeterminate], now=self.now)
        # No removal events — indeterminate sources don't advance removal
        self.assertEqual(result.events, [])
        # Indeterminate sources are NOT in successful_source_ids
        self.assertNotIn("10", result.successful_source_ids)
        # Indeterminate sources are NOT in failed_source_ids
        self.assertNotIn("10", result.failed_source_ids)
        # Indeterminate sources ARE in indeterminate_source_ids
        self.assertIn("10", result.indeterminate_source_ids)
        # No source failures emitted
        self.assertEqual(len(result.source_failures), 0)
        # Indeterminate recorded
        self.assertEqual(len(result.source_indeterminate), 1)

    def test_parser_empty_with_error_never_removes(self):
        """Parser failure resulting in no jobs + error must not remove."""
        failure = {**self.success, "extraction_result": "no_jobs_found",
                   "jobs_found": "0", "extraction_error": "ParseError"}
        result = compare_jobs({self.job["canonical_job_id"]: self.job}, [], [failure], now=self.now)
        self.assertEqual(result.events, [])
        self.assertEqual(len(result.failed_source_ids), 1)

    def test_removed_job_reopens(self):
        old = {**self.job, "status": "removed", "missing_successful_runs": 2}
        result = compare_jobs({old["canonical_job_id"]: old}, [dict(self.job)],
                              [self.success], now=self.now)
        self.assertIn("REOPENED", [e["event_type"] for e in result.events])

    def test_sparse_snapshot_does_not_clear_fields(self):
        self.assertNotIn("description", changed_fields(self.job, {**self.job, "description": ""}))


class DatabaseTests(unittest.TestCase):
    def _stage3(self, path: Path):
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE organizations (organization_id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_name TEXT NOT NULL, canonical_domain TEXT, first_seen TEXT, last_checked TEXT);
            CREATE TABLE job_sources (source_id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER, source_type TEXT, source_provider TEXT,
                listing_url TEXT, api_url TEXT, adapter_name TEXT, source_status TEXT,
                last_successful_check TEXT, consecutive_failures INTEGER DEFAULT 0);
            CREATE TABLE jobs (canonical_job_id TEXT PRIMARY KEY, organization_id INTEGER,
                source_job_id TEXT, title TEXT, normalized_title TEXT, location TEXT,
                normalized_location TEXT, country TEXT, region TEXT, city TEXT,
                work_arrangement TEXT, employment_type TEXT, salary_min TEXT, salary_max TEXT,
                currency TEXT, posted_date TEXT, closing_date TEXT, description TEXT,
                job_url TEXT, application_url TEXT, application_email TEXT,
                status TEXT DEFAULT 'active', first_seen TEXT, last_seen TEXT, content_hash TEXT);
            CREATE TABLE crawl_runs (crawl_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER, started_at TEXT, completed_at TEXT, result TEXT,
                jobs_found INTEGER, error TEXT);
            CREATE TABLE job_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_job_id TEXT, event_type TEXT, event_time TEXT,
                old_value TEXT, new_value TEXT, crawl_run_id INTEGER);
        """)
        return conn

    def test_migration_and_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = self._stage3(Path(directory) / "monitor.db")
            migrate_database(conn)
            self.assertIn("missing_successful_runs", table_columns(conn, "jobs"))
            current = {
                "canonical_job_id": "url:acme:/jobs/2", "record_id": "10",
                "organization_name": "Acme", "source_job_id": "2",
                "source_type": "ats", "source_provider": "greenhouse",
                "source_listing_url": "https://acme.example/jobs",
                "title": "Data Engineer", "normalized_title": "data engineer",
                "description": "Build data systems.",
                "job_url": "https://acme.example/jobs/2", "first_seen": "2026-07-19",
                "content_hash": "abc",
            }
            status = {
                "record_id": "10", "organization_name": "Acme", "source_type": "ats",
                "source_provider": "greenhouse", "source_listing_url": "https://acme.example/jobs",
                "extraction_result": "jobs_extracted", "jobs_found": "1", "extraction_error": "",
            }
            comparison = compare_jobs({}, [current], [status], now="2026-07-19T20:00:00+00:00")
            apply_commit(conn, comparison, [status], run_batch_id="test",
                         snapshot_path=Path("snapshot.csv"), now="2026-07-19T20:00:00+00:00")
            conn.commit()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_events WHERE event_type='NEW'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM crawl_runs").fetchone()[0], 1)

    def test_indeterminate_does_not_increment_failures(self):
        """Indeterminate sources must not increment consecutive_failures."""
        with tempfile.TemporaryDirectory() as directory:
            conn = self._stage3(Path(directory) / "monitor.db")
            migrate_database(conn)
            status = {
                "record_id": "20", "organization_name": "Beta", "source_type": "ats",
                "source_provider": "greenhouse", "source_listing_url": "https://beta.example/jobs",
                "extraction_result": "no_jobs_found", "jobs_found": "0", "extraction_error": "",
            }
            comparison = compare_jobs({}, [], [status], now="2026-07-19T20:00:00+00:00")
            apply_commit(conn, comparison, [status], run_batch_id="test",
                         snapshot_path=Path("snapshot.csv"), now="2026-07-19T20:00:00+00:00")
            conn.commit()
            row = conn.execute(
                "SELECT consecutive_failures, source_status FROM job_sources WHERE record_id='20'"
            ).fetchone()
            self.assertEqual(row[0], 0)  # No failure increment
            self.assertEqual(row[1], "indeterminate")


class FilterTests(unittest.TestCase):
    def test_provider_filter_selects_before_extraction(self):
        """--provider greenhouse should filter targets before extraction."""
        from monitoring.core import filter_sources
        rows = [
            {"record_id": "1", "source_type": "ats", "source_provider": "greenhouse"},
            {"record_id": "2", "source_type": "ats", "source_provider": "lever"},
            {"record_id": "3", "source_type": "static_html_listing", "source_provider": ""},
            {"record_id": "4", "source_type": "ats", "source_provider": "greenhouse"},
        ]
        filtered = filter_sources(rows, provider="greenhouse")
        self.assertEqual(len(filtered), 2)
        self.assertTrue(all(r["source_provider"] == "greenhouse" for r in filtered))

    def test_source_type_filter(self):
        from monitoring.core import filter_sources
        rows = [
            {"record_id": "1", "source_type": "ats", "source_provider": "greenhouse"},
            {"record_id": "2", "source_type": "static_html_listing", "source_provider": ""},
        ]
        filtered = filter_sources(rows, source_type="ats")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["source_type"], "ats")

    def test_combined_filters(self):
        from monitoring.core import filter_sources
        rows = [
            {"record_id": "1", "source_type": "ats", "source_provider": "greenhouse"},
            {"record_id": "2", "source_type": "ats", "source_provider": "lever"},
        ]
        filtered = filter_sources(rows, source_type="ats", provider="greenhouse")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["source_provider"], "greenhouse")

    def test_limit_applied_after_filtering(self):
        from monitoring.core import filter_sources
        rows = [
            {"record_id": str(i), "source_type": "ats", "source_provider": "greenhouse"}
            for i in range(10)
        ]
        filtered = filter_sources(rows, provider="greenhouse", limit=3)
        self.assertEqual(len(filtered), 3)

    def test_no_filter_returns_all(self):
        from monitoring.core import filter_sources
        rows = [
            {"record_id": "1", "source_type": "ats"},
            {"record_id": "2", "source_type": "static_html_listing"},
        ]
        filtered = filter_sources(rows)
        self.assertEqual(len(filtered), 2)


class InventoryTests(unittest.TestCase):
    def _write_sources_csv(self, path: Path, rows: list[dict]):
        import csv
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def test_load_targets_from_inventory(self):
        """A three-row job_sources.csv produces exactly three extraction targets."""
        from extract_jobs_unified import load_targets_from_inventory
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "job_sources.csv"
            rows = [
                {"record_id": "1", "organization_name": "Acme",
                 "monitor_url": "https://acme.example/careers",
                 "source_type": "ats", "source_provider": "greenhouse",
                 "source_listing_url": "https://acme.example/careers",
                 "source_api_url": "", "source_stage": "stage1",
                 "previous_result": "jobs_extracted"},
                {"record_id": "2", "organization_name": "Beta",
                 "monitor_url": "https://beta.example/jobs",
                 "source_type": "static_html_listing", "source_provider": "",
                 "source_listing_url": "https://beta.example/jobs",
                 "source_api_url": "", "source_stage": "stage2a",
                 "previous_result": "no_jobs_found"},
                {"record_id": "3", "organization_name": "Gamma",
                 "monitor_url": "https://gamma.example/openings",
                 "source_type": "ats", "source_provider": "lever",
                 "source_listing_url": "https://gamma.example/openings",
                 "source_api_url": "", "source_stage": "stage2b",
                 "previous_result": "jobs_extracted"},
            ]
            self._write_sources_csv(csv_path, rows)
            targets = load_targets_from_inventory(csv_path)
            self.assertEqual(len(targets), 3)
            self.assertEqual(targets[0]["source_type"], "ats")
            self.assertEqual(targets[0]["source_provider"], "greenhouse")
            self.assertEqual(targets[1]["source_type"], "static_html_listing")
            self.assertEqual(targets[2]["source_provider"], "lever")

    def test_inventory_only_source_included(self):
        """An inventory-only source absent from Stage 1/2 is still included."""
        from extract_jobs_unified import load_targets_from_inventory
        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "job_sources.csv"
            rows = [
                {"record_id": "99", "organization_name": "NewCo",
                 "monitor_url": "https://newco.example/careers",
                 "source_type": "public_job_api", "source_provider": "",
                 "source_listing_url": "https://newco.example/careers",
                 "source_api_url": "https://newco.example/api/jobs",
                 "source_stage": "stage1_ats_review",
                 "previous_result": ""},
            ]
            self._write_sources_csv(csv_path, rows)
            targets = load_targets_from_inventory(csv_path)
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0]["source_type"], "public_job_api")
            self.assertEqual(targets[0]["source_api_url"], "https://newco.example/api/jobs")

    def test_inventory_source_type_filter(self):
        """source_type=static_html_listing selects correct inventory rows."""
        from monitoring.core import filter_sources
        rows = [
            {"record_id": "1", "source_type": "ats", "source_provider": "greenhouse"},
            {"record_id": "2", "source_type": "static_html_listing", "source_provider": ""},
            {"record_id": "3", "source_type": "static_html_listing", "source_provider": ""},
            {"record_id": "4", "source_type": "ats", "source_provider": "lever"},
        ]
        filtered = filter_sources(rows, source_type="static_html_listing")
        self.assertEqual(len(filtered), 2)
        self.assertTrue(all(r["source_type"] == "static_html_listing" for r in filtered))

    def test_inventory_provider_filter(self):
        """Provider filter works on inventory rows."""
        from monitoring.core import filter_sources
        rows = [
            {"record_id": "1", "source_type": "ats", "source_provider": "greenhouse"},
            {"record_id": "2", "source_type": "ats", "source_provider": "greenhouse"},
            {"record_id": "3", "source_type": "ats", "source_provider": "lever"},
        ]
        filtered = filter_sources(rows, provider="greenhouse")
        self.assertEqual(len(filtered), 2)


class ClassificationPreservationTests(unittest.TestCase):
    """Prove that classify_source() trusts inventory and does not rediscover."""

    def _make_target(self, **overrides):
        base = {
            "record_id": "1", "organization_name": "Acme",
            "monitor_url": "https://acme.example/careers",
            "source_type": "", "source_provider": "",
            "source_listing_url": "", "source_api_url": "",
            "detected_ats": "", "detected_ats_provider": "",
            "previous_result": "",
        }
        base.update(overrides)
        return base

    def test_public_job_api_remains_public_job_api(self):
        """Inventory public_job_api is not reclassified as unknown."""
        from extract_jobs_unified import classify_source
        target = self._make_target(
            source_type="public_job_api",
            source_api_url="https://acme.example/api/jobs",
        )
        import asyncio
        result = asyncio.run(classify_source(None, target, 25))
        self.assertEqual(result["source_type"], "public_job_api")
        self.assertEqual(result["api_url"], "https://acme.example/api/jobs")

    def test_static_html_remains_static_html(self):
        from extract_jobs_unified import classify_source
        target = self._make_target(source_type="static_html_listing")
        import asyncio
        result = asyncio.run(classify_source(None, target, 25))
        self.assertEqual(result["source_type"], "static_html_listing")

    def test_no_openings_remains_no_openings(self):
        from extract_jobs_unified import classify_source
        target = self._make_target(source_type="no_openings")
        import asyncio
        result = asyncio.run(classify_source(None, target, 25))
        self.assertEqual(result["source_type"], "no_openings")

    def test_ats_retains_provider(self):
        from extract_jobs_unified import classify_source
        target = self._make_target(
            source_type="ats", source_provider="greenhouse",
            source_listing_url="https://boards.greenhouse.io/acme",
        )
        import asyncio
        result = asyncio.run(classify_source(None, target, 25))
        self.assertEqual(result["source_type"], "ats")
        self.assertEqual(result["source_provider"], "greenhouse")
        self.assertEqual(result["listing_url"], "https://boards.greenhouse.io/acme")

    def test_no_http_when_inventory_classified(self):
        """classify_source should not make HTTP request when source_type is set."""
        from extract_jobs_unified import classify_source
        target = self._make_target(source_type="ats", source_provider="lever")
        import asyncio
        # Passing None as client proves no HTTP is attempted
        result = asyncio.run(classify_source(None, target, 25))
        self.assertEqual(result["source_type"], "ats")
        self.assertEqual(result["source_provider"], "lever")


class IndeterminatePersistenceTests(unittest.TestCase):
    def _stage3(self, path: Path):
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE organizations (organization_id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_name TEXT NOT NULL, canonical_domain TEXT, first_seen TEXT, last_checked TEXT);
            CREATE TABLE job_sources (source_id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER, source_type TEXT, source_provider TEXT,
                listing_url TEXT, api_url TEXT, adapter_name TEXT, source_status TEXT,
                last_successful_check TEXT, consecutive_failures INTEGER DEFAULT 0);
            CREATE TABLE jobs (canonical_job_id TEXT PRIMARY KEY, organization_id INTEGER,
                source_job_id TEXT, title TEXT, normalized_title TEXT, location TEXT,
                normalized_location TEXT, country TEXT, region TEXT, city TEXT,
                work_arrangement TEXT, employment_type TEXT, salary_min TEXT, salary_max TEXT,
                currency TEXT, posted_date TEXT, closing_date TEXT, description TEXT,
                job_url TEXT, application_url TEXT, application_email TEXT,
                status TEXT DEFAULT 'active', first_seen TEXT, last_seen TEXT, content_hash TEXT);
            CREATE TABLE crawl_runs (crawl_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER, started_at TEXT, completed_at TEXT, result TEXT,
                jobs_found INTEGER, error TEXT);
            CREATE TABLE job_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_job_id TEXT, event_type TEXT, event_time TEXT,
                old_value TEXT, new_value TEXT, crawl_run_id INTEGER);
        """)
        return conn

    def test_indeterminate_committed_crawl_inserts_event(self):
        """SOURCE_INDETERMINATE events are persisted to job_events."""
        with tempfile.TemporaryDirectory() as directory:
            conn = self._stage3(Path(directory) / "monitor.db")
            migrate_database(conn)
            status = {
                "record_id": "30", "organization_name": "Gamma",
                "source_type": "ats", "source_provider": "greenhouse",
                "source_listing_url": "https://gamma.example/careers",
                "extraction_result": "no_jobs_found", "jobs_found": "0",
                "extraction_error": "",
            }
            comparison = compare_jobs({}, [], [status], now="2026-07-20T10:00:00+00:00")
            self.assertEqual(len(comparison.source_indeterminate), 1)
            self.assertEqual(comparison.source_indeterminate[0]["event_type"], "SOURCE_INDETERMINATE")
            apply_commit(conn, comparison, [status], run_batch_id="test",
                         snapshot_path=Path("snapshot.csv"), now="2026-07-20T10:00:00+00:00")
            conn.commit()
            events = conn.execute(
                "SELECT event_type FROM job_events WHERE event_type='SOURCE_INDETERMINATE'"
            ).fetchall()
            self.assertEqual(len(events), 1)

    def test_indeterminate_crawl_run_error_type(self):
        """Crawl run for indeterminate has error_type='indeterminate'."""
        with tempfile.TemporaryDirectory() as directory:
            conn = self._stage3(Path(directory) / "monitor.db")
            migrate_database(conn)
            status = {
                "record_id": "31", "organization_name": "Delta",
                "source_type": "ats", "source_provider": "lever",
                "source_listing_url": "https://delta.example/careers",
                "extraction_result": "no_jobs_found", "jobs_found": "0",
                "extraction_error": "",
            }
            comparison = compare_jobs({}, [], [status], now="2026-07-20T10:00:00+00:00")
            apply_commit(conn, comparison, [status], run_batch_id="test",
                         snapshot_path=Path("snapshot.csv"), now="2026-07-20T10:00:00+00:00")
            conn.commit()
            row = conn.execute("SELECT error_type FROM crawl_runs").fetchone()
            self.assertEqual(row[0], "indeterminate")


if __name__ == "__main__":
    unittest.main()
