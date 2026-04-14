"""Tests for tracker/database.py — CRUD, status transitions, followup logic, stats."""
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, UTC
from unittest import mock

import pytest

# Patch DB_PATH before importing database module
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

with mock.patch.dict(os.environ, {"DB_PATH": _tmp.name}):
    # Force settings to reload with test DB path
    import importlib
    import config.settings as settings
    settings.DB_PATH = type(settings.DB_PATH)(_tmp.name)

    from tracker.database import (
        init_db,
        insert_job,
        get_pending_jobs,
        get_approved_jobs,
        update_job_status,
        approve_job,
        reject_job,
        mark_applied,
        get_stats,
        get_job_by_id,
        get_jobs_needing_followup,
        record_followup,
        set_cover_letter,
        job_url_exists,
        get_all_applied_jobs,
        log_action,
        get_connection,
    )

# Initialize test DB
init_db()


@pytest.fixture(autouse=True)
def fresh_db():
    """Reset DB tables before each test."""
    conn = get_connection()
    conn.execute("DELETE FROM application_log")
    conn.execute("DELETE FROM jobs")
    conn.commit()
    conn.close()
    yield


class TestInsertJob:
    def test_insert_returns_id(self):
        job_id = insert_job("PM", "Acme", "Remote", "$100k", "https://example.com/1", "linkedin")
        assert job_id is not None
        assert isinstance(job_id, int)
        assert job_id > 0

    def test_insert_duplicate_url_returns_none(self):
        id1 = insert_job("PM", "Acme", "Remote", "$100k", "https://example.com/dup", "linkedin")
        id2 = insert_job("PM2", "Acme2", "NYC", "$120k", "https://example.com/dup", "indeed")
        # Same URL → second insert returns None (duplicate ignored)
        assert id1 is not None
        assert id2 is None

    def test_insert_truncates_long_description(self):
        long_desc = "x" * 10000
        job_id = insert_job("PM", "Co", "Remote", "", "https://example.com/long", "linkedin", long_desc)
        job = get_job_by_id(job_id)
        assert len(job["description"]) == 5000

    def test_insert_logs_scraped_action(self):
        job_id = insert_job("PM", "Co", "Remote", "", "https://example.com/log", "linkedin")
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT action FROM application_log WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert row["action"] == "scraped"
        finally:
            conn.close()


class TestUpdateJobStatus:
    def test_approve(self):
        job_id = insert_job("PM", "Co", "NY", "", "https://example.com/a1", "linkedin")
        approve_job(job_id)
        job = get_job_by_id(job_id)
        assert job["status"] == "approved"

    def test_reject(self):
        job_id = insert_job("PM", "Co", "NY", "", "https://example.com/r1", "linkedin")
        reject_job(job_id)
        job = get_job_by_id(job_id)
        assert job["status"] == "rejected"

    def test_mark_applied_sets_applied_at(self):
        job_id = insert_job("PM", "Co", "NY", "", "https://example.com/ap1", "linkedin")
        approve_job(job_id)
        mark_applied(job_id)
        job = get_job_by_id(job_id)
        assert job["status"] == "applied"
        assert job["applied_at"] is not None

    def test_invalid_status_raises(self):
        job_id = insert_job("PM", "Co", "NY", "", "https://example.com/inv", "linkedin")
        with pytest.raises(ValueError, match="Invalid status"):
            update_job_status(job_id, "BOGUS")

    def test_invalid_job_id_raises(self):
        with pytest.raises(ValueError, match="Invalid job_id"):
            update_job_status(-1, "approved")

    def test_invalid_job_id_type_raises(self):
        with pytest.raises(ValueError, match="Invalid job_id"):
            update_job_status("abc", "approved")

    def test_status_transition_chain(self):
        job_id = insert_job("PM", "Co", "NY", "", "https://example.com/chain", "linkedin")
        approve_job(job_id)
        mark_applied(job_id)
        update_job_status(job_id, "interviewing")
        update_job_status(job_id, "offered")
        job = get_job_by_id(job_id)
        assert job["status"] == "offered"


class TestGetJobs:
    def test_get_pending(self):
        insert_job("PM1", "Co", "NY", "", "https://example.com/p1", "linkedin")
        insert_job("PM2", "Co", "NY", "", "https://example.com/p2", "indeed")
        pending = get_pending_jobs()
        assert len(pending) == 2

    def test_get_pending_respects_limit(self):
        for i in range(10):
            insert_job(f"PM{i}", "Co", "NY", "", f"https://example.com/lim{i}", "linkedin")
        pending = get_pending_jobs(limit=3)
        assert len(pending) == 3

    def test_get_approved(self):
        id1 = insert_job("PM1", "Co", "NY", "", "https://example.com/g1", "linkedin")
        insert_job("PM2", "Co", "NY", "", "https://example.com/g2", "indeed")
        approve_job(id1)
        approved = get_approved_jobs()
        assert len(approved) == 1
        assert approved[0]["id"] == id1

    def test_get_all_applied(self):
        id1 = insert_job("PM1", "Co", "NY", "", "https://example.com/aa1", "linkedin")
        id2 = insert_job("PM2", "Co", "NY", "", "https://example.com/aa2", "indeed")
        mark_applied(id1)
        update_job_status(id2, "interviewing")
        applied = get_all_applied_jobs()
        assert len(applied) == 2

    def test_job_url_exists(self):
        insert_job("PM", "Co", "NY", "", "https://example.com/exists1", "linkedin")
        assert job_url_exists("https://example.com/exists1") is True
        assert job_url_exists("https://example.com/nothere") is False


class TestFollowup:
    def test_no_followup_if_recently_applied(self):
        job_id = insert_job("PM", "Co", "NY", "", "https://example.com/f1", "linkedin")
        mark_applied(job_id)
        # Just applied → should NOT need followup
        jobs = get_jobs_needing_followup()
        assert len(jobs) == 0

    def test_followup_needed_after_cutoff(self):
        job_id = insert_job("PM", "Co", "NY", "", "https://example.com/f2", "linkedin")
        # Manually set applied_at to 10 days ago
        conn = get_connection()
        old_date = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        conn.execute("UPDATE jobs SET status='applied', applied_at=? WHERE id=?", (old_date, job_id))
        conn.commit()
        conn.close()

        jobs = get_jobs_needing_followup()
        assert len(jobs) == 1
        assert jobs[0]["id"] == job_id

    def test_record_followup_resets_timer(self):
        job_id = insert_job("PM", "Co", "NY", "", "https://example.com/f3", "linkedin")
        conn = get_connection()
        old_date = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        conn.execute("UPDATE jobs SET status='applied', applied_at=? WHERE id=?", (old_date, job_id))
        conn.commit()
        conn.close()

        # Needs followup
        assert len(get_jobs_needing_followup()) == 1

        # Record followup → resets timer
        record_followup(job_id)
        assert len(get_jobs_needing_followup()) == 0

        # Verify followup_count incremented
        job = get_job_by_id(job_id)
        assert job["followup_count"] == 1

    def test_followup_after_second_period(self):
        """After followup, needs another followup after FOLLOWUP_DAYS."""
        job_id = insert_job("PM", "Co", "NY", "", "https://example.com/f4", "linkedin")
        conn = get_connection()
        old_date = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        conn.execute("UPDATE jobs SET status='applied', applied_at=? WHERE id=?", (old_date, job_id))
        conn.commit()
        conn.close()

        record_followup(job_id)
        # Immediately after followup → no need
        assert len(get_jobs_needing_followup()) == 0

        # Set last_followup_at to 10 days ago
        conn = get_connection()
        old_followup = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        conn.execute("UPDATE jobs SET last_followup_at=? WHERE id=?", (old_followup, job_id))
        conn.commit()
        conn.close()

        # Now needs followup again
        jobs = get_jobs_needing_followup()
        assert len(jobs) == 1


class TestStats:
    def test_empty_stats(self):
        s = get_stats()
        assert s["total"] == 0
        assert s["pending"] == 0
        assert s["applied_this_week"] == 0

    def test_stats_counts(self):
        id1 = insert_job("PM1", "Co", "NY", "", "https://example.com/s1", "linkedin")
        id2 = insert_job("PM2", "Co", "NY", "", "https://example.com/s2", "indeed")
        id3 = insert_job("PM3", "Co", "NY", "", "https://example.com/s3", "glassdoor")
        approve_job(id1)
        reject_job(id2)
        mark_applied(id3)

        s = get_stats()
        assert s["total"] == 3
        assert s["approved"] == 1
        assert s["rejected"] == 1
        assert s["applied"] == 1
        assert s["pending"] == 0
        assert s["applied_this_week"] == 1
        assert s["applied_this_month"] == 1


class TestCoverLetter:
    def test_set_cover_letter(self):
        job_id = insert_job("PM", "Co", "NY", "", "https://example.com/cl1", "linkedin")
        set_cover_letter(job_id, "Dear Hiring Manager...")
        job = get_job_by_id(job_id)
        assert job["cover_letter"] == "Dear Hiring Manager..."


class TestLogAction:
    def test_log_action_standalone(self):
        job_id = insert_job("PM", "Co", "NY", "", "https://example.com/la1", "linkedin")
        log_action(job_id, "test_action", "test detail")
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM application_log WHERE job_id = ? AND action = 'test_action'",
                (job_id,),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["detail"] == "test detail"
        finally:
            conn.close()

    def test_log_action_with_external_conn_no_premature_commit(self):
        """log_action with external conn should NOT commit — caller controls transaction."""
        job_id = insert_job("PM", "Co", "NY", "", "https://example.com/la2", "linkedin")
        conn = get_connection()
        try:
            conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (job_id,))
            log_action(job_id, "ext_test", "from external conn", conn=conn)
            # Neither the UPDATE nor the log INSERT should be committed yet
            # Verify by opening a separate connection
            conn2 = get_connection()
            try:
                job = conn2.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                # With WAL mode, the other connection won't see uncommitted changes
                assert job["status"] == "pending"
            finally:
                conn2.close()
            # Now commit
            conn.commit()
        finally:
            conn.close()

        # After commit, changes should be visible
        job = get_job_by_id(job_id)
        assert job["status"] == "approved"


class TestDuplicateApplyGuard:
    """Verify the pattern used by applicant/engine.py to prevent double-apply."""

    def test_applied_job_detected(self):
        job_id = insert_job("PM", "Co", "NY", "", "https://example.com/dup_apply", "linkedin")
        mark_applied(job_id)
        fresh = get_job_by_id(job_id)
        assert fresh["status"] == "applied"

    def test_approved_job_not_yet_applied(self):
        job_id = insert_job("PM", "Co", "NY", "", "https://example.com/dup_appr", "linkedin")
        approve_job(job_id)
        fresh = get_job_by_id(job_id)
        assert fresh["status"] != "applied"

    def test_get_job_by_id_returns_none_for_missing(self):
        assert get_job_by_id(999999) is None
