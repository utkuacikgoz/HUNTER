import re
import sqlite3
from datetime import UTC, datetime, timedelta

from config.settings import DB_PATH, FOLLOWUP_DAYS


def get_connection():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_FILTER_COLUMNS = (
    ("region", "TEXT"),
    ("is_remote", "INTEGER"),
    ("is_freelance", "INTEGER"),
    ("sponsor_status", "TEXT"),
    ("filter_verdict", "TEXT"),
    ("filter_reasons", "TEXT"),
    # Content fingerprint (company + title) used to suppress re-surfacing a job
    # the user already acted on when it reappears under a different URL.
    ("dedup_key", "TEXT"),
)

# Statuses that mean "the user already acted on this job"; a still-pending twin
# (same dedup_key, different URL) of any of these is hidden from review.
_ACTED_STATUSES = ("rejected", "approved", "applied", "interviewing", "offered")

_DEDUP_WS = re.compile(r"\s+")
_DEDUP_EDGE_PUNCT = re.compile(r"^[\W_]+|[\W_]+$")


def compute_dedup_key(company: str | None, title: str | None) -> str | None:
    """Conservative content signature: ``"company||title"``, each lowercased,
    internal whitespace collapsed, surrounding punctuation stripped.

    Deliberately not aggressive — no stemming or seniority-word dropping — so
    distinct roles like "Senior PM" vs "PM" at the same company stay distinct.
    Returns ``None`` when either field is empty so NULL keys never collide
    (SQL ``=`` is never true for NULL=NULL); see ``get_pending_jobs``.
    """
    def _norm(value: str | None) -> str:
        text = _DEDUP_WS.sub(" ", (value or "").lower()).strip()
        return _DEDUP_EDGE_PUNCT.sub("", text).strip()

    company_norm = _norm(company)
    title_norm = _norm(title)
    if not company_norm or not title_norm:
        return None
    return f"{company_norm}||{title_norm}"


def _add_missing_columns(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for name, sql_type in _FILTER_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}")


def _backfill_dedup_keys(conn):
    """Populate dedup_key for rows that predate the column. Idempotent: only
    NULL keys are recomputed, so this is a no-op after the first run."""
    for row in conn.execute(
        "SELECT id, company, title FROM jobs WHERE dedup_key IS NULL"
    ).fetchall():
        key = compute_dedup_key(row["company"], row["title"])
        if key is not None:
            conn.execute("UPDATE jobs SET dedup_key = ? WHERE id = ?", (key, row["id"]))


def init_db():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT,
            salary TEXT,
            url TEXT UNIQUE NOT NULL,
            platform TEXT NOT NULL,
            description TEXT,
            scraped_at TEXT NOT NULL DEFAULT (datetime('now')),
            status TEXT NOT NULL DEFAULT 'pending',
            -- status: pending, approved, rejected, applied, interviewing, offered, closed
            applied_at TEXT,
            last_followup_at TEXT,
            followup_count INTEGER DEFAULT 0,
            notes TEXT,
            cover_letter TEXT
        );

        CREATE TABLE IF NOT EXISTS application_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            -- action: scraped, sent_to_telegram, approved, rejected, applied, followup_sent, status_changed
            detail TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (job_id) REFERENCES jobs(id)
        );

        CREATE TABLE IF NOT EXISTS scraper_health (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            jobs_found INTEGER NOT NULL,
            ran_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_platform ON jobs(platform);
        CREATE INDEX IF NOT EXISTS idx_jobs_scraped_at ON jobs(scraped_at);
        CREATE INDEX IF NOT EXISTS idx_application_log_job_id ON application_log(job_id);
        CREATE INDEX IF NOT EXISTS idx_scraper_health_platform_ran ON scraper_health(platform, ran_at);
    """)
    _add_missing_columns(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_filter_verdict ON jobs(filter_verdict)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_dedup_key ON jobs(dedup_key)")
    _backfill_dedup_keys(conn)
    conn.commit()
    conn.close()


def record_scraper_run(platform: str, jobs_found: int) -> None:
    """Persist the result of a scraper run for health/skip decisions."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO scraper_health (platform, jobs_found) VALUES (?, ?)",
            (platform, jobs_found),
        )
        conn.commit()
    finally:
        conn.close()


def get_company_velocity(days: int = 14) -> dict[str, int]:
    """Return {company_lowercase: distinct_role_count} over the last `days` days.

    Roles are deduped by URL (the unique key for a posting). The lookup is keyed
    lowercase so callers can match against any-case company strings.
    """
    if days <= 0:
        return {}
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT LOWER(TRIM(company)) AS c, COUNT(DISTINCT url) AS n
               FROM jobs
               WHERE TRIM(company) != ''
                 AND scraped_at >= datetime('now', ?)
               GROUP BY LOWER(TRIM(company))""",
            (f"-{int(days)} days",),
        ).fetchall()
    finally:
        conn.close()
    return {row["c"]: row["n"] for row in rows}


def should_skip_scraper(platform: str, threshold: int) -> bool:
    """True iff the last `threshold` runs of this platform all returned 0 jobs.

    A threshold of 0 disables the check entirely.
    """
    if threshold <= 0:
        return False
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT jobs_found FROM scraper_health WHERE platform = ? ORDER BY ran_at DESC LIMIT ?",
            (platform, threshold),
        ).fetchall()
    finally:
        conn.close()
    if len(rows) < threshold:
        return False
    return all(row["jobs_found"] == 0 for row in rows)


def insert_job(
    title,
    company,
    location,
    salary,
    url,
    platform,
    description="",
    *,
    region: str | None = None,
    is_remote: bool | None = None,
    is_freelance: bool | None = None,
    sponsor_status: str | None = None,
    filter_verdict: str | None = None,
    filter_reasons: str | None = None,
):
    conn = get_connection()
    try:
        description = (description or "")[:5000]
        dedup_key = compute_dedup_key(company, title)
        conn.execute(
            """INSERT OR IGNORE INTO jobs
               (title, company, location, salary, url, platform, description,
                region, is_remote, is_freelance, sponsor_status, filter_verdict,
                filter_reasons, dedup_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title, company, location, salary, url, platform, description,
                region,
                int(is_remote) if is_remote is not None else None,
                int(is_freelance) if is_freelance is not None else None,
                sponsor_status, filter_verdict, filter_reasons, dedup_key,
            ),
        )
        was_inserted = conn.execute("SELECT changes()").fetchone()[0] > 0
        if not was_inserted:
            conn.commit()
            return None
        cursor = conn.execute("SELECT id FROM jobs WHERE url = ?", (url,))
        row = cursor.fetchone()
        job_id = row["id"] if row else None
        if job_id:
            log_action(job_id, "scraped", f"Scraped from {platform}", conn=conn)
        conn.commit()
        return job_id
    finally:
        conn.close()


def log_action(job_id, action, detail="", conn=None):
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True
    conn.execute(
        "INSERT INTO application_log (job_id, action, detail) VALUES (?, ?, ?)",
        (job_id, action, detail),
    )
    if should_close:
        conn.commit()
        conn.close()


def get_pending_jobs(limit=50):
    # Hide a pending job whose content fingerprint matches one the user already
    # acted on (skipped/approved/applied/...) under a different URL. dedup_key IS
    # NULL rows are never suppressed (we couldn't fingerprint them). The status
    # set is a hardcoded literal — no user input, no injection surface.
    acted = ", ".join(f"'{s}'" for s in _ACTED_STATUSES)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT * FROM jobs
               WHERE status = 'pending'
                 AND (filter_verdict IS NULL OR filter_verdict != 'drop')
                 AND (
                   dedup_key IS NULL
                   OR NOT EXISTS (
                     SELECT 1 FROM jobs acted
                     WHERE acted.dedup_key = jobs.dedup_key
                       AND acted.status IN ({acted})
                   )
                 )
               ORDER BY
                 CASE filter_verdict
                   WHEN 'include' THEN 0
                   WHEN 'flag' THEN 1
                   ELSE 2
                 END,
                 scraped_at DESC
               LIMIT ?""",  # nosec B608 — the only interpolation, {acted}, is built from the hardcoded _ACTED_STATUSES literal above; no user input reaches this query
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_approved_jobs():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = 'approved' ORDER BY scraped_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_job_status(job_id, status):
    VALID_STATUSES = {"pending", "approved", "rejected", "applied", "interviewing", "offered", "closed"}
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")
    if not isinstance(job_id, int) or job_id <= 0:
        raise ValueError(f"Invalid job_id: {job_id}")
    conn = get_connection()
    try:
        now = datetime.now(UTC).isoformat()
        if status == "applied":
            conn.execute(
                "UPDATE jobs SET status = ?, applied_at = ? WHERE id = ?",
                (status, now, job_id),
            )
        else:
            conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
        log_action(job_id, "status_changed", f"Status -> {status}", conn=conn)
        conn.commit()
    finally:
        conn.close()


def approve_job(job_id):
    update_job_status(job_id, "approved")


def reject_job(job_id):
    update_job_status(job_id, "rejected")


def mark_applied(job_id):
    update_job_status(job_id, "applied")


def set_cover_letter(job_id, cover_letter):
    conn = get_connection()
    try:
        conn.execute("UPDATE jobs SET cover_letter = ? WHERE id = ?", (cover_letter, job_id))
        conn.commit()
    finally:
        conn.close()


def get_jobs_needing_followup():
    conn = get_connection()
    try:
        cutoff = (datetime.now(UTC) - timedelta(days=FOLLOWUP_DAYS)).isoformat()
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE status = 'applied'
               AND (
                   (last_followup_at IS NULL AND datetime(applied_at) <= datetime(?))
                   OR (last_followup_at IS NOT NULL AND datetime(last_followup_at) <= datetime(?))
               )
               ORDER BY applied_at ASC""",
            (cutoff, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def record_followup(job_id):
    conn = get_connection()
    try:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE jobs SET last_followup_at = ?, followup_count = followup_count + 1 WHERE id = ?",
            (now, job_id),
        )
        log_action(job_id, "followup_sent", f"Follow-up sent at {now}", conn=conn)
        conn.commit()
    finally:
        conn.close()


def get_stats():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) as c FROM jobs GROUP BY status"
        ).fetchall()
        stats = {row["status"]: row["c"] for row in rows}
        for s in ["pending", "approved", "rejected", "applied", "interviewing", "offered", "closed"]:
            stats.setdefault(s, 0)
        stats["total"] = sum(stats[s] for s in ["pending", "approved", "rejected", "applied", "interviewing", "offered", "closed"])

        row = conn.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE datetime(applied_at) >= datetime('now', '-7 days')"
        ).fetchone()
        stats["applied_this_week"] = row["c"] if row else 0

        row = conn.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE datetime(applied_at) >= datetime('now', '-30 days')"
        ).fetchone()
        stats["applied_this_month"] = row["c"] if row else 0

        return stats
    finally:
        conn.close()


def get_filter_precision() -> dict[str, dict[str, int]]:
    """Return approve/reject counts grouped by the job's filter_verdict.

    Counts only come from logged user decisions (`filter_outcome` rows in application_log),
    so legacy jobs that pre-date the filter aren't counted in either direction.
    Shape: {"include": {"approve": 12, "reject": 3}, "flag": {...}, ...}
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT j.filter_verdict AS verdict, l.detail AS detail
               FROM application_log l
               JOIN jobs j ON j.id = l.job_id
               WHERE l.action = 'filter_outcome'"""
        ).fetchall()
    finally:
        conn.close()

    out: dict[str, dict[str, int]] = {}
    for row in rows:
        verdict = row["verdict"] or "unknown"
        # detail format: "verdict=X;sponsor=Y;region=Z;decision=W"
        decision = "unknown"
        for kv in (row["detail"] or "").split(";"):
            if kv.startswith("decision="):
                decision = kv.split("=", 1)[1]
                break
        bucket = out.setdefault(verdict, {"approve": 0, "reject": 0})
        if decision in bucket:
            bucket[decision] += 1
    return out


def get_job_by_id(job_id):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_applied_jobs():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('applied', 'interviewing', 'offered') ORDER BY applied_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def job_url_exists(url):
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
        return row is not None
    finally:
        conn.close()


def get_last_scrape_time() -> str | None:
    """ISO timestamp of the most recent scraper run, or None if never run.

    Used by the boot catch-up watchdog to decide whether a hunt was missed
    while the worker was down.
    """
    conn = get_connection()
    try:
        row = conn.execute("SELECT MAX(ran_at) AS t FROM scraper_health").fetchone()
        return row["t"] if row and row["t"] else None
    finally:
        conn.close()


def get_funnel() -> dict:
    """End-to-end funnel counts for the /report command and alerting.

    Returns scraped-job counts by filter verdict, status distribution, and
    confirmed/failed apply counts from the application log.
    """
    conn = get_connection()
    try:
        vrows = conn.execute(
            "SELECT COALESCE(filter_verdict, 'unknown') AS v, COUNT(*) AS c FROM jobs GROUP BY v"
        ).fetchall()
        srows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM jobs GROUP BY status"
        ).fetchall()
        arows = conn.execute(
            "SELECT action, COUNT(*) AS c FROM application_log "
            "WHERE action IN ('applied', 'apply_failed') GROUP BY action"
        ).fetchall()
        actions = {r["action"]: r["c"] for r in arows}
        return {
            "verdicts": {r["v"]: r["c"] for r in vrows},
            "statuses": {r["status"]: r["c"] for r in srows},
            "apply_confirmed": actions.get("applied", 0),
            "apply_failed": actions.get("apply_failed", 0),
        }
    finally:
        conn.close()
