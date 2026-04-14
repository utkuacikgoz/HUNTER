import sqlite3
from datetime import datetime, timedelta
from config.settings import DB_PATH, FOLLOWUP_DAYS


def get_connection():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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

        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_platform ON jobs(platform);
        CREATE INDEX IF NOT EXISTS idx_jobs_scraped_at ON jobs(scraped_at);
        CREATE INDEX IF NOT EXISTS idx_application_log_job_id ON application_log(job_id);
    """)
    conn.commit()
    conn.close()


def insert_job(title, company, location, salary, url, platform, description=""):
    conn = get_connection()
    try:
        description = (description or "")[:5000]
        conn.execute(
            """INSERT OR IGNORE INTO jobs (title, company, location, salary, url, platform, description)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (title, company, location, salary, url, platform, description),
        )
        conn.commit()
        cursor = conn.execute("SELECT id FROM jobs WHERE url = ?", (url,))
        row = cursor.fetchone()
        job_id = row["id"] if row else None
        if job_id:
            log_action(job_id, "scraped", f"Scraped from {platform}", conn=conn)
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
    conn.commit()
    if should_close:
        conn.close()


def get_pending_jobs(limit=50):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = 'pending' ORDER BY scraped_at DESC LIMIT ?",
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
        now = datetime.utcnow().isoformat()
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
        cutoff = (datetime.utcnow() - timedelta(days=FOLLOWUP_DAYS)).isoformat()
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE status = 'applied'
               AND (
                   (last_followup_at IS NULL AND applied_at <= ?)
                   OR (last_followup_at IS NOT NULL AND last_followup_at <= ?)
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
        now = datetime.utcnow().isoformat()
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
        stats = {}
        for status in ["pending", "approved", "rejected", "applied", "interviewing", "offered", "closed"]:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM jobs WHERE status = ?", (status,)
            ).fetchone()
            stats[status] = row["c"]
        stats["total"] = sum(stats.values())

        row = conn.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE applied_at >= date('now', '-7 days')"
        ).fetchone()
        stats["applied_this_week"] = row["c"]

        row = conn.execute(
            "SELECT COUNT(*) as c FROM jobs WHERE applied_at >= date('now', '-30 days')"
        ).fetchone()
        stats["applied_this_month"] = row["c"]

        return stats
    finally:
        conn.close()


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
