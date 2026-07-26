"""SQLite state store for the clipper pipeline (slice-1 subset of PRD §4).

Single file, single writer (one VPS, one worker). No ORM — plain sqlite3.
State machine statuses live as plain strings; see STATUSES below.
"""
import json
import os
import sqlite3
import time

DB_PATH = os.environ.get("CLIPPER_DB", os.path.join(os.path.dirname(__file__), "clipper.sqlite"))

# Task state machine (PRD §3.0). FAILED reachable from any state.
STATUSES = [
    "DISCOVERED", "DOWNLOADING", "TRANSCRIBING", "ANALYZING",
    "EDITING", "UPLOADING", "PUBLISHED", "FAILED",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id           TEXT PRIMARY KEY,
    source_platform       TEXT NOT NULL,
    budget_remaining      INTEGER,
    requirements_json     TEXT,          -- normalized Task.requirements
    platform_specific_data TEXT,         -- JSON, per-platform extras (PRD §3.0)
    status                TEXT,
    discovered_at         TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id         TEXT NOT NULL,
    footage_url         TEXT NOT NULL,
    footage_source_type TEXT,            -- youtube | gdrive | discord
    status              TEXT NOT NULL,
    error               TEXT,
    created_at          TEXT,
    UNIQUE(campaign_id, footage_url)     -- dedup: one task per footage per campaign
);
CREATE TABLE IF NOT EXISTS clips (
    clip_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id            INTEGER NOT NULL,
    start_ts           REAL,
    end_ts             REAL,
    platform           TEXT,             -- youtube (slice-1) | tiktok | instagram
    video_id           TEXT,             -- source video identifier
    published_url      TEXT,
    views_last_checked INTEGER,
    status             TEXT
);
CREATE TABLE IF NOT EXISTS segment_usage (
    video_id TEXT, start_ts REAL, end_ts REAL, platform TEXT
);
CREATE TABLE IF NOT EXISTS submissions (
    submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id   TEXT, clip_ids TEXT, submitted_at TEXT, status TEXT
);
"""


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # tolerate reader during writer
    return conn


def init_db(path=DB_PATH):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def upsert_campaign(conn, task):
    """Store/refresh a campaign from a normalized Task. Returns True if new."""
    cur = conn.execute("SELECT 1 FROM campaigns WHERE campaign_id=?", (task.campaign_id,))
    is_new = cur.fetchone() is None
    conn.execute(
        """INSERT INTO campaigns
             (campaign_id, source_platform, budget_remaining, requirements_json,
              platform_specific_data, status, discovered_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(campaign_id) DO UPDATE SET
             budget_remaining=excluded.budget_remaining,
             requirements_json=excluded.requirements_json,
             platform_specific_data=excluded.platform_specific_data,
             status=excluded.status""",
        (task.campaign_id, task.source_platform, task.budget_remaining,
         json.dumps(task.requirements, ensure_ascii=False),
         json.dumps(task.platform_specific_data, ensure_ascii=False),
         task.status, _now()),
    )
    conn.commit()
    return is_new


def enqueue_footage(conn, campaign_id, footage_url, source_type):
    """Insert a DISCOVERED task. Idempotent via UNIQUE(campaign_id, footage_url).
    Returns task_id, or None if it already existed (dedup skip)."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO tasks
             (campaign_id, footage_url, footage_source_type, status, created_at)
           VALUES (?,?,?,?,?)""",
        (campaign_id, footage_url, source_type, "DISCOVERED", _now()),
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def tasks_by_status(conn, status):
    return conn.execute("SELECT * FROM tasks WHERE status=?", (status,)).fetchall()


def set_task_status(conn, task_id, status, error=None):
    assert status in STATUSES, f"unknown status {status}"
    conn.execute("UPDATE tasks SET status=?, error=? WHERE task_id=?", (status, error, task_id))
    conn.commit()
    # PRD gap fix: releasing a failed task must free its reserved segments,
    # else the timestamp stays blocked forever.
    if status == "FAILED":
        rows = conn.execute("SELECT video_id FROM clips WHERE task_id=?", (task_id,)).fetchall()
        for r in rows:
            if r["video_id"]:
                conn.execute("DELETE FROM segment_usage WHERE video_id=?", (r["video_id"],))
        conn.commit()


if __name__ == "__main__":
    # Self-check: schema builds, dedup + FAILED-cleanup behave.
    import tempfile
    from dataclasses import dataclass, field

    @dataclass
    class T:
        campaign_id: str = "c1"
        source_platform: str = "clippo"
        budget_remaining: int = 100
        requirements: dict = field(default_factory=dict)
        platform_specific_data: dict = field(default_factory=dict)
        status: str = "active"

    p = os.path.join(tempfile.mkdtemp(), "t.sqlite")
    c = init_db(p)
    assert upsert_campaign(c, T()) is True
    assert upsert_campaign(c, T()) is False           # dedup campaign
    tid = enqueue_footage(c, "c1", "http://x/1", "youtube")
    assert tid is not None
    assert enqueue_footage(c, "c1", "http://x/1", "youtube") is None  # dedup footage
    # reserve a segment, then fail the task -> segment freed
    c.execute("INSERT INTO clips(task_id, video_id, platform, status) VALUES (?,?,?,?)",
              (tid, "vidA", "youtube", "EDITING"))
    c.execute("INSERT INTO segment_usage VALUES (?,?,?,?)", ("vidA", 0.0, 10.0, "youtube"))
    c.commit()
    set_task_status(c, tid, "FAILED", "boom")
    assert c.execute("SELECT COUNT(*) FROM segment_usage").fetchone()[0] == 0
    assert len(tasks_by_status(c, "FAILED")) == 1
    print("db.py self-check OK")
