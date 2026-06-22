"""End-to-end wiring tests for the orchestrator in main.py.

The scrape layer, Telegram dispatch, and the apply engine are stubbed, but the
database is a real temp SQLite file — so classification, persistence, the
drop-vs-pending split, ranking, and dispatch are all exercised for real. This is
the only test that covers the full hunt/apply orchestration (units cover the
pieces in isolation).
"""
import tempfile

import pytest

import main
import tracker.database as database


@pytest.fixture
def temp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setattr(database, "DB_PATH", type(database.DB_PATH)(tmp.name))
    database.init_db()
    return tmp.name


async def test_hunt_classifies_persists_and_dispatches(temp_db, monkeypatch):
    # One clearly includable job (allowlisted EU/remote) and one hard drop
    # (US-only + no sponsorship). Both should persist; only the first dispatches.
    scraped = [
        {
            "title": "Senior Product Manager",
            "company": "Stripe",  # default sponsor allowlist -> include
            "location": "Remote, Europe",
            "salary": "$120k",
            "url": "https://example.com/stripe-pm",
            "platform": "remoteok",
            "description": "Great fully remote role across the EU.",
        },
        {
            "title": "US PM",
            "company": "AcmeUS",
            "location": "Remote",
            "salary": "",
            "url": "https://example.com/acme-pm",
            "platform": "remoteok",
            "description": "Must be located in the United States. No visa sponsorship.",
        },
    ]

    async def fake_scrape_all():
        return scraped

    captured = {}

    async def fake_send_jobs_batch(pending):
        captured["pending"] = pending

    monkeypatch.setattr(main, "_scrape_all", fake_scrape_all)
    monkeypatch.setattr(main, "send_jobs_batch", fake_send_jobs_batch)

    result = await main.hunt()

    # Both scraped & stored; only the includable one is pending -> dispatched.
    assert result == {"scraped": 2, "new": 2, "sent_to_telegram": 1}
    assert [j["title"] for j in captured["pending"]] == ["Senior Product Manager"]

    # Both rows persisted with the verdict the filter assigned.
    conn = database.get_connection()
    try:
        verdicts = {
            row["url"]: row["filter_verdict"]
            for row in conn.execute("SELECT url, filter_verdict FROM jobs")
        }
    finally:
        conn.close()
    assert verdicts["https://example.com/stripe-pm"] == "include"
    assert verdicts["https://example.com/acme-pm"] == "drop"


async def test_drops_do_not_consume_review_budget(temp_db, monkeypatch):
    # MAX_JOBS_PER_DAY caps REVIEWABLE jobs, not total inserts: a burst of drops
    # ahead of the good jobs must not exhaust the budget before the includes are
    # reached. (Under the old total-insert cap, the leading drops would stop the
    # loop and the includes would never be classified.)
    monkeypatch.setattr(main, "MAX_JOBS_PER_DAY", 2)
    scraped = [
        {
            "title": "US PM", "company": f"AcmeUS{i}", "location": "Remote",
            "salary": "", "url": f"https://example.com/drop-{i}", "platform": "remoteok",
            "description": "Must be located in the United States. No visa sponsorship.",
        }
        for i in range(5)
    ] + [
        {
            "title": "Senior Product Manager", "company": "Stripe",
            "location": "Remote, Europe", "salary": "",
            "url": f"https://example.com/keep-{i}", "platform": "remoteok",
            "description": "EU remote role.",
        }
        for i in range(2)
    ]

    async def fake_scrape_all():
        return scraped

    captured = {}

    async def fake_send_jobs_batch(pending):
        captured["pending"] = pending

    monkeypatch.setattr(main, "_scrape_all", fake_scrape_all)
    monkeypatch.setattr(main, "send_jobs_batch", fake_send_jobs_batch)

    result = await main.hunt()

    # All 7 persisted; both includes dispatched despite cap=2 and 5 leading drops.
    assert result["new"] == 7
    assert result["sent_to_telegram"] == 2
    assert {j["title"] for j in captured["pending"]} == {"Senior Product Manager"}


async def test_apply_marks_approved_jobs_applied(temp_db, monkeypatch):
    job_id = database.insert_job(
        title="PM", company="Acme", location="Remote", salary="",
        url="https://example.com/approved", platform="remoteok", description="",
    )
    database.approve_job(job_id)

    async def fake_apply_to_approved(approved, headless=True):
        # Stand in for the Playwright engine: mark each approved job applied.
        for j in approved:
            database.mark_applied(j["id"])
        return {"total": len(approved), "success": len(approved), "failed": 0, "needs_manual": 0}

    async def fake_send_stats():
        return None

    monkeypatch.setattr(main, "apply_to_approved_jobs", fake_apply_to_approved)
    monkeypatch.setattr(main, "send_stats_message", fake_send_stats)

    result = await main.apply()

    assert result["success"] == 1
    assert database.get_job_by_id(job_id)["status"] == "applied"
