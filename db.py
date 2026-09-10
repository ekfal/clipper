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
# States a task can only be in while a worker is holding it. Nothing collects a
# task left in one of these — the process that owned it is gone — so they are
# what the stale sweep looks for (PRD §5, resume rather than restart).
IN_FLIGHT = ["DOWNLOADING", "TRANSCRIBING", "ANALYZING", "EDITING", "UPLOADING"]
# How long a task may sit in one of those before it is assumed abandoned.
# Longer than the slowest stage on the slowest host: whisper on a long video.
STALE_MINUTES = float(os.environ.get("CLIPPER_STALE_MINUTES", "90"))
# Clip states that mean the work is spent and must never be undone.
CLIP_DONE = ("PUBLISHED", "SUBMITTED")

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


# Columns added after the first schema shipped. ALTER TABLE ADD COLUMN is the
# only migration SQLite makes cheap, and every one of these is nullable, so an
# existing database upgrades in place without a rebuild.
_MIGRATIONS = [
    ("tasks", "updated_at", "TEXT"),
    ("clips", "meta_json", "TEXT"),     # title/description, so a resumed
    ("clips", "account", "TEXT"),       # upload does not re-ask the model
    ("clips", "created_at", "TEXT"),
]


def _migrate(conn):
    for table, column, decl in _MIGRATIONS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()


def init_db(path=DB_PATH):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
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


# Per-campaign destination override, set from the dashboard.
# Stored inside platform_specific_data rather than a new column: the column is
# already the documented home for per-campaign extras (PRD §3.0), and a
# migration for a field most campaigns never set is not worth it.
_OVERRIDE_KEY = "destinations_override"


def campaign_override(conn, campaign_id):
    """Platforms a human pinned for this campaign, or None for automatic."""
    row = conn.execute(
        "SELECT platform_specific_data FROM campaigns WHERE campaign_id=?",
        (campaign_id,)).fetchone()
    if not row or not row["platform_specific_data"]:
        return None
    try:
        data = json.loads(row["platform_specific_data"])
    except ValueError:
        return None
    value = data.get(_OVERRIDE_KEY) if isinstance(data, dict) else None
    return list(value) if value else None


def set_campaign_override(conn, campaign_id, platforms):
    """Pin the destinations for one campaign; pass None/[] to return to auto."""
    row = conn.execute(
        "SELECT platform_specific_data FROM campaigns WHERE campaign_id=?",
        (campaign_id,)).fetchone()
    if row is None:
        raise ValueError(f"no campaign {campaign_id!r}")
    try:
        data = json.loads(row["platform_specific_data"] or "{}")
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    if platforms:
        data[_OVERRIDE_KEY] = list(platforms)
    else:
        data.pop(_OVERRIDE_KEY, None)      # back to automatic
    conn.execute("UPDATE campaigns SET platform_specific_data=? WHERE campaign_id=?",
                 (json.dumps(data), campaign_id))
    conn.commit()


def all_campaigns(conn):
    return conn.execute(
        "SELECT * FROM campaigns ORDER BY discovered_at DESC").fetchall()


def tasks_by_status(conn, status):
    return conn.execute("SELECT * FROM tasks WHERE status=?", (status,)).fetchall()


def release_segments(conn, task_id):
    """Free the ranges this task reserved but never published.

    Deleting by video_id — which is what this used to do — frees every range on
    that video, including ones a DIFFERENT task already published. Two campaigns
    routinely share footage, so one failure there re-opens timestamps that are
    already live, and the next run cuts and uploads the same moment again.
    Matched on the exact range and skipping clips that are already out.
    """
    cur = conn.execute(
        """DELETE FROM segment_usage
            WHERE EXISTS (SELECT 1 FROM clips c
                           WHERE c.task_id = ?
                             AND c.video_id = segment_usage.video_id
                             AND c.start_ts = segment_usage.start_ts
                             AND c.end_ts   = segment_usage.end_ts
                             AND c.platform = segment_usage.platform
                             AND c.status NOT IN (%s))"""
        % ",".join("?" * len(CLIP_DONE)), (task_id, *CLIP_DONE))
    conn.commit()
    return cur.rowcount


def set_task_status(conn, task_id, status, error=None):
    assert status in STATUSES, f"unknown status {status}"
    conn.execute("UPDATE tasks SET status=?, error=?, updated_at=? WHERE task_id=?",
                 (status, error, _now(), task_id))
    conn.commit()
    # A failed task must free its reserved segments, or the timestamps stay
    # blocked forever — but only the ones it reserved and did not publish.
    if status == "FAILED":
        release_segments(conn, task_id)


def stale_tasks(conn, minutes=None):
    """Tasks abandoned mid-flight: in a worker-held state, not touched since.

    A task with no updated_at predates the column, so it is stale by
    definition — nothing has written to it since the migration.
    """
    cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(time.time() - (STALE_MINUTES if minutes is None else minutes) * 60))
    marks = ",".join("?" * len(IN_FLIGHT))
    return conn.execute(
        f"""SELECT * FROM tasks
             WHERE status IN ({marks})
               AND (updated_at IS NULL OR updated_at < ?)
          ORDER BY task_id""", (*IN_FLIGHT, cutoff)).fetchall()


def drop_clip(conn, clip_id):
    """Remove a clip that will never be uploaded, and free its segment.

    Used when a rendered file has gone missing under a clip row: the row and
    its reservation are then holding a timestamp nothing can publish. A clip
    already out stays put — its reservation is what stops a re-cut.
    """
    row = conn.execute(
        "SELECT video_id, start_ts, end_ts, platform, status FROM clips "
        "WHERE clip_id=?", (clip_id,)).fetchone()
    if row is None or row["status"] in CLIP_DONE:
        return False
    conn.execute(
        """DELETE FROM segment_usage
            WHERE video_id=? AND start_ts=? AND end_ts=? AND platform=?""",
        (row["video_id"], row["start_ts"], row["end_ts"], row["platform"]))
    conn.execute("DELETE FROM clips WHERE clip_id=?", (clip_id,))
    conn.commit()
    return True


def requeue_stale(conn, minutes=None):
    """Return abandoned tasks to the queue. Returns [(task_id, was_status)].

    Reservations are deliberately left alone. A task abandoned after rendering
    still has its clips on disk and will resume onto them, so freeing their
    timestamps here would let another task claim a range that is about to be
    published. Whatever turns out to be unresumable is released where that is
    visible — pipeline.py, which knows whether the file survived — and a task
    that fails outright releases the rest through set_task_status.
    """
    out = []
    for row in stale_tasks(conn, minutes):
        conn.execute(
            "UPDATE tasks SET status='DISCOVERED', error=?, updated_at=? WHERE task_id=?",
            (f"requeued: abandoned in {row['status']}", _now(), row["task_id"]))
        out.append((row["task_id"], row["status"]))
    conn.commit()
    return out


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
    # reserve a segment, then fail the task -> that segment is freed. The clip
    # row carries the range it reserved, as pipeline.py writes it: the release
    # matches on the range, not on the whole video.
    c.execute("""INSERT INTO clips(task_id, video_id, platform, status, start_ts, end_ts)
                 VALUES (?,?,?,?,?,?)""", (tid, "vidA", "youtube", "EDITED", 0.0, 10.0))
    c.execute("INSERT INTO segment_usage VALUES (?,?,?,?)", ("vidA", 0.0, 10.0, "youtube"))
    c.commit()
    set_task_status(c, tid, "FAILED", "boom")
    assert c.execute("SELECT COUNT(*) FROM segment_usage").fetchone()[0] == 0
    assert len(tasks_by_status(c, "FAILED")) == 1

    # destination override round-trips and does not disturb sibling keys
    assert upsert_campaign(c, T(campaign_id="c2",
                                platform_specific_data={"clip_batch_count": 5})) is True
    cid = "c2"
    assert campaign_override(c, cid) is None                  # automatic by default
    set_campaign_override(c, cid, ["tiktok"])
    assert campaign_override(c, cid) == ["tiktok"]
    kept = json.loads(c.execute(
        "SELECT platform_specific_data FROM campaigns WHERE campaign_id=?",
        (cid,)).fetchone()["platform_specific_data"])
    assert "clip_batch_count" in kept, kept        # existing extras survive
    set_campaign_override(c, cid, None)
    assert campaign_override(c, cid) is None       # back to automatic
    assert "clip_batch_count" in json.loads(c.execute(
        "SELECT platform_specific_data FROM campaigns WHERE campaign_id=?",
        (cid,)).fetchone()["platform_specific_data"])
    try:
        set_campaign_override(c, "nope", ["tiktok"])
        raise AssertionError("unknown campaign accepted")
    except ValueError:
        pass

    # A failure must not free another task's published segments.
    c2 = init_db(os.path.join(tempfile.mkdtemp(), "t2.sqlite"))
    upsert_campaign(c2, T(campaign_id="A"))
    upsert_campaign(c2, T(campaign_id="B"))
    ta = enqueue_footage(c2, "A", "http://x/1", "youtube")
    tb = enqueue_footage(c2, "B", "http://x/1", "youtube")
    # task A published two clips from vid1; task B rendered one and then fails
    for s, e in ((100, 160), (300, 360)):
        c2.execute("""INSERT INTO clips(task_id,video_id,platform,status,start_ts,end_ts)
                      VALUES (?,?,?,?,?,?)""", (ta, "vid1", "youtube", "PUBLISHED", s, e))
        c2.execute("INSERT INTO segment_usage VALUES ('vid1',?,?,'youtube')", (s, e))
    c2.execute("""INSERT INTO clips(task_id,video_id,platform,status,start_ts,end_ts)
                  VALUES (?,?,?,?,?,?)""", (tb, "vid1", "youtube", "EDITED", 500, 560))
    c2.execute("INSERT INTO segment_usage VALUES ('vid1',500,560,'youtube')")
    c2.commit()
    set_task_status(c2, tb, "FAILED", "render blew up")
    left = sorted(tuple(r)[1:3] for r in c2.execute("SELECT * FROM segment_usage"))
    assert left == [(100.0, 160.0), (300.0, 360.0)], left   # published ones survive
    assert len(left) == 2, "a failure freed segments it did not own"

    # Abandoned tasks come back to the queue.
    tc = enqueue_footage(c2, "A", "http://x/2", "youtube")
    set_task_status(c2, tc, "EDITING")
    assert stale_tasks(c2, minutes=60) == [], "a fresh task is not stale"
    c2.execute("UPDATE tasks SET updated_at='2020-01-01T00:00:00Z' WHERE task_id=?", (tc,))
    c2.commit()
    assert [r["task_id"] for r in stale_tasks(c2, minutes=60)] == [tc]
    assert requeue_stale(c2, minutes=60) == [(tc, "EDITING")]
    row = c2.execute("SELECT status, error FROM tasks WHERE task_id=?", (tc,)).fetchone()
    assert row["status"] == "DISCOVERED" and "abandoned in EDITING" in row["error"], dict(row)
    assert requeue_stale(c2, minutes=60) == []          # nothing left to sweep
    # sweeping must not have touched the published reservations
    assert c2.execute("SELECT COUNT(*) FROM segment_usage").fetchone()[0] == 2

    # a requeue keeps reservations, so a resumable clip does not lose its slot
    te = enqueue_footage(c2, "A", "http://x/4", "youtube")
    c2.execute("""INSERT INTO clips(task_id,video_id,platform,status,start_ts,end_ts)
                  VALUES (?,?,?,?,?,?)""", (te, "vid9", "youtube", "EDITED", 10, 50))
    c2.execute("INSERT INTO segment_usage VALUES ('vid9',10,50,'youtube')")
    set_task_status(c2, te, "UPLOADING")
    c2.execute("UPDATE tasks SET updated_at='2020-01-01T00:00:00Z' WHERE task_id=?", (te,))
    c2.commit()
    requeue_stale(c2, minutes=60)
    held = c2.execute("SELECT COUNT(*) FROM segment_usage WHERE video_id='vid9'").fetchone()[0]
    assert held == 1, "requeue freed a slot a resumable clip still needs"

    # dropping an unresumable clip frees exactly its own range
    cid = c2.execute("SELECT clip_id FROM clips WHERE task_id=?", (te,)).fetchone()["clip_id"]
    assert drop_clip(c2, cid) is True
    assert c2.execute("SELECT COUNT(*) FROM segment_usage WHERE video_id='vid9'").fetchone()[0] == 0
    assert c2.execute("SELECT COUNT(*) FROM segment_usage").fetchone()[0] == 2  # others intact
    pub = c2.execute("SELECT clip_id FROM clips WHERE status='PUBLISHED'").fetchone()["clip_id"]
    assert drop_clip(c2, pub) is False, "a published clip must not be droppable"

    # a task mid-flight but recently touched is left alone
    td = enqueue_footage(c2, "B", "http://x/3", "youtube")
    set_task_status(c2, td, "UPLOADING")
    assert requeue_stale(c2, minutes=60) == []
    assert c2.execute("SELECT status FROM tasks WHERE task_id=?",
                      (td,)).fetchone()["status"] == "UPLOADING"
    print("db.py self-check OK")
