"""Tests for tracker/database.py — CRUD, status transitions, followup logic, stats."""
import os
import tempfile
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest

# Patch DB_PATH before importing database module
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

with mock.patch.dict(os.environ, {"DB_PATH": _tmp.name}):
    # Force settings to reload with test DB path
    import config.settings as settings
    settings.DB_PATH = type(settings.DB_PATH)(_tmp.name)

    from tracker.database import (
        approve_job,
        compute_dedup_key,
        get_all_applied_jobs,
        get_approved_jobs,
        get_company_velocity,
        get_connection,
        get_filter_precision,
        get_funnel,
        get_job_by_id,
        get_jobs_needing_followup,
        get_last_scrape_time,
        get_pending_jobs,
        get_stats,
        init_db,
        insert_job,
        job_url_exists,
        log_action,
        mark_applied,
        record_followup,
        record_scraper_run,
        reject_job,
        set_cover_letter,
        should_skip_scraper,
        update_job_status,
    )

# Initialize test DB
init_db()


@pytest.fixture(autouse=True)
def fresh_db():
    """Reset DB tables before each test."""
    conn = get_connection()
    conn.execute("DELETE FROM application_log")
    conn.execute("DELETE FROM jobs")
    conn.execute("DELETE FROM scraper_health")
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


class TestFilterPrecision:
    """Verify get_filter_precision groups approve/reject by the filter verdict."""

    def _seed_classified(self, url: str, verdict: str) -> int:
        return insert_job(
            "PM", "Acme", "Remote", "", url, "wellfound", "",
            region="eu", is_remote=True, sponsor_status="allowlist",
            filter_verdict=verdict, filter_reasons="seeded",
        )

    def test_empty(self):
        assert get_filter_precision() == {}

    def test_counts_approves_and_rejects_per_verdict(self):
        a = self._seed_classified("https://example.com/p1", "include")
        b = self._seed_classified("https://example.com/p2", "include")
        c = self._seed_classified("https://example.com/p3", "flag")
        log_action(a, "filter_outcome", "verdict=include;sponsor=allowlist;region=eu;decision=approve")
        log_action(b, "filter_outcome", "verdict=include;sponsor=allowlist;region=eu;decision=reject")
        log_action(c, "filter_outcome", "verdict=flag;sponsor=unknown;region=eu;decision=approve")

        p = get_filter_precision()
        assert p["include"] == {"approve": 1, "reject": 1}
        assert p["flag"] == {"approve": 1, "reject": 0}

    def test_ignores_non_filter_actions(self):
        a = self._seed_classified("https://example.com/p4", "include")
        log_action(a, "scraped", "ignored")
        log_action(a, "filter_outcome", "verdict=include;sponsor=allowlist;region=eu;decision=approve")
        p = get_filter_precision()
        assert p["include"] == {"approve": 1, "reject": 0}


class TestCompanyVelocity:
    """get_company_velocity groups jobs in the window by lowercased company."""

    def test_empty_db(self):
        assert get_company_velocity() == {}

    def test_counts_distinct_urls_per_company_case_insensitive(self):
        insert_job("PM A", "Stripe",  "Remote", "", "https://example.com/v1", "wellfound")
        insert_job("PM B", "STRIPE",  "Remote", "", "https://example.com/v2", "wellfound")
        insert_job("PM C", "stripe",  "Remote", "", "https://example.com/v3", "remoteok")
        insert_job("PM D", "FooCorp", "Remote", "", "https://example.com/v4", "wellfound")
        v = get_company_velocity()
        assert v == {"stripe": 3, "foocorp": 1}

    def test_outside_window_excluded(self):
        a = insert_job("PM", "Stripe", "Remote", "", "https://example.com/v5", "wellfound")
        conn = get_connection()
        old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        conn.execute("UPDATE jobs SET scraped_at = ? WHERE id = ?", (old, a))
        conn.commit()
        conn.close()
        assert get_company_velocity(days=14) == {}
        assert get_company_velocity(days=90) == {"stripe": 1}

    def test_zero_days_returns_empty(self):
        insert_job("PM", "Stripe", "Remote", "", "https://example.com/v6", "wellfound")
        assert get_company_velocity(days=0) == {}


class TestScraperHealth:
    """should_skip_scraper triggers only after N consecutive zero runs."""

    def test_no_history_does_not_skip(self):
        assert should_skip_scraper("wellfound", threshold=3) is False

    def test_three_zero_runs_skip(self):
        for _ in range(3):
            record_scraper_run("wellfound", 0)
        assert should_skip_scraper("wellfound", threshold=3) is True

    def test_recent_nonzero_resets(self):
        record_scraper_run("wellfound", 0)
        record_scraper_run("wellfound", 0)
        record_scraper_run("wellfound", 5)
        assert should_skip_scraper("wellfound", threshold=3) is False

    def test_threshold_zero_disables(self):
        for _ in range(10):
            record_scraper_run("wellfound", 0)
        assert should_skip_scraper("wellfound", threshold=0) is False

    def _age_runs(self, platform: str, days: int) -> None:
        """Backdate this platform's recorded runs by `days`."""
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE scraper_health SET ran_at = datetime('now', ?) WHERE platform = ?",
                (f"-{days} days", platform),
            )
            conn.commit()
        finally:
            conn.close()

    def test_skip_expires_after_retry_window(self):
        # A skipped run records nothing, so without an expiry the zero-streak would
        # never age out and a transient outage would kill the source permanently.
        for _ in range(3):
            record_scraper_run("wellfound", 0)
        assert should_skip_scraper("wellfound", threshold=3, retry_after_days=3) is True
        self._age_runs("wellfound", 4)
        assert should_skip_scraper("wellfound", threshold=3, retry_after_days=3) is False

    def test_retry_window_zero_keeps_skip_permanent(self):
        for _ in range(3):
            record_scraper_run("wellfound", 0)
        self._age_runs("wellfound", 365)
        assert should_skip_scraper("wellfound", threshold=3, retry_after_days=0) is True

    def test_failed_retry_starts_a_new_window(self):
        for _ in range(3):
            record_scraper_run("wellfound", 0)
        self._age_runs("wellfound", 4)
        assert should_skip_scraper("wellfound", threshold=3, retry_after_days=3) is False
        record_scraper_run("wellfound", 0)  # the retry also came back empty
        assert should_skip_scraper("wellfound", threshold=3, retry_after_days=3) is True


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


class TestFunnelAndWatchdog:
    def test_last_scrape_time_tracks_runs(self):
        before = get_last_scrape_time()  # may be set by earlier tests
        record_scraper_run("greenhouse", 5)
        after = get_last_scrape_time()
        assert after is not None
        if before is not None:
            assert after >= before

    def test_get_funnel_shape(self):
        insert_job("PM", "FunnelCo", "Remote", "", "https://example.com/funnel1",
                   "greenhouse", filter_verdict="include")
        f = get_funnel()
        assert "verdicts" in f and "statuses" in f
        assert "apply_confirmed" in f and "apply_failed" in f
        assert f["verdicts"].get("include", 0) >= 1
        assert f["statuses"].get("pending", 0) >= 1


class TestDedupSuppression:
    """A job the user already acted on suppresses its content-twins (same
    company+title) when they reappear under a different URL."""

    def test_compute_dedup_key_normalization(self):
        assert compute_dedup_key("Acme", "") is None
        assert compute_dedup_key("", "PM") is None
        assert compute_dedup_key(None, "PM") is None
        assert compute_dedup_key(" Acme ", " PM ") == "acme||pm"
        # case, doubled internal whitespace, and edge punctuation are neutralized
        assert (
            compute_dedup_key("Acme  Inc.", " Product   Manager ")
            == compute_dedup_key("acme  inc.", "Product Manager")
        )

    def test_twin_under_different_url_suppressed_after_skip(self):
        a = insert_job("Product Manager", "Acme", "Remote", "", "https://a.com/1", "greenhouse")
        reject_job(a)  # the Telegram "Skip" path sets status='rejected'
        b = insert_job("Product Manager", "Acme", "Remote", "", "https://b.com/2", "lever")
        assert b is not None  # twin is still stored (different URL)
        pending_ids = {j["id"] for j in get_pending_jobs()}
        assert b not in pending_ids

    @pytest.mark.parametrize("act", [approve_job, mark_applied])
    def test_twin_suppressed_for_approved_and_applied(self, act):
        a = insert_job("Designer", "Globex", "Remote", "", "https://a.com/d1", "greenhouse")
        act(a)
        b = insert_job("Designer", "Globex", "Remote", "", "https://b.com/d2", "lever")
        assert b not in {j["id"] for j in get_pending_jobs()}

    def test_distinct_roles_same_company_not_suppressed(self):
        a = insert_job("Senior PM", "Acme", "Remote", "", "https://a.com/s1", "greenhouse")
        reject_job(a)
        b = insert_job("PM", "Acme", "Remote", "", "https://b.com/s2", "lever")
        assert b in {j["id"] for j in get_pending_jobs()}

    def test_cosmetic_variants_are_twins(self):
        a = insert_job("Product Manager", "Acme  Inc.", "Remote", "", "https://a.com/c1", "greenhouse")
        reject_job(a)
        # different case + extra/trailing whitespace, different URL → still a twin
        b = insert_job("Product   Manager ", "acme  inc.", "Remote", "", "https://b.com/c2", "lever")
        assert b not in {j["id"] for j in get_pending_jobs()}

    def test_null_keys_do_not_collide(self):
        # empty company → NULL dedup_key for both rows
        a = insert_job("PM", "", "Remote", "", "https://a.com/n1", "greenhouse")
        b = insert_job("PM", "", "Remote", "", "https://b.com/n2", "lever")
        reject_job(a)
        assert b in {j["id"] for j in get_pending_jobs()}

    def test_pending_twins_do_not_suppress_each_other(self):
        a = insert_job("Product Manager", "Acme", "Remote", "", "https://a.com/t1", "greenhouse")
        b = insert_job("Product Manager", "Acme", "Remote", "", "https://b.com/t2", "lever")
        ids = {j["id"] for j in get_pending_jobs()}
        assert a in ids and b in ids  # neither acted on → both visible

    def test_backfill_populates_existing_null_keys(self):
        a = insert_job("Product Manager", "Acme", "Remote", "", "https://a.com/bf1", "greenhouse")
        # simulate a pre-feature row whose key was never computed
        conn = get_connection()
        conn.execute("UPDATE jobs SET dedup_key = NULL WHERE id = ?", (a,))
        conn.commit()
        conn.close()

        init_db()  # runs the idempotent backfill

        conn = get_connection()
        key = conn.execute("SELECT dedup_key FROM jobs WHERE id = ?", (a,)).fetchone()["dedup_key"]
        conn.close()
        assert key == "acme||product manager"

        reject_job(a)
        b = insert_job("Product Manager", "Acme", "Remote", "", "https://b.com/bf2", "lever")
        assert b not in {j["id"] for j in get_pending_jobs()}


class TestUnconfirmedSubmitTracking:
    """The apply engine records an unconfirmed submit so it never re-applies."""

    def test_absent_by_default(self):
        from tracker.database import has_unconfirmed_submit
        job_id = insert_job("PM", "Co", "Remote", "", "https://example.com/unconf1", "greenhouse")
        assert has_unconfirmed_submit(job_id) is False

    def test_detected_after_logging(self):
        from tracker.database import has_unconfirmed_submit
        job_id = insert_job("PM", "Co", "Remote", "", "https://example.com/unconf2", "greenhouse")
        log_action(job_id, "apply_submitted_unconfirmed", "no confirmation seen")
        assert has_unconfirmed_submit(job_id) is True

    def test_other_actions_do_not_count(self):
        from tracker.database import has_unconfirmed_submit
        job_id = insert_job("PM", "Co", "Remote", "", "https://example.com/unconf3", "greenhouse")
        log_action(job_id, "apply_failed", "selector missing")
        assert has_unconfirmed_submit(job_id) is False
