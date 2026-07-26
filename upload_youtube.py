"""YouTube Shorts uploader (PRD §3.9) — ported from legacy uploader.py.

Kept: publishAt scheduling (upload private, YouTube flips public on schedule),
daily WIB slots, resumable upload, daily-limit detection. Dropped: n8n webhook,
Windows file-lock retry loop, cleanup (pipeline owns file lifecycle).
Schedule bookkeeping moved from scheduled_slots.json into SQLite.
"""
import os
import pickle
from datetime import datetime, timedelta

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

_BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_DIR = os.environ.get("CLIPPER_TOKEN_DIR", os.path.join(os.path.dirname(_BASE), "tokens"))
ACCOUNT = os.environ.get("CLIPPER_YT_ACCOUNT", "Test")
SCHEDULE_SLOTS = ["10:00", "13:00", "15:00", "19:00", "21:00"]  # WIB
WIB_UTC_OFFSET = 7

SLOT_TABLE = """CREATE TABLE IF NOT EXISTS yt_slots (
    account TEXT, slot TEXT, clip TEXT, PRIMARY KEY (account, slot))"""


def next_slot(conn, account, now=None):
    """Next free daily slot after `now` (WIB). Returns (iso_utc, local_str)."""
    conn.execute(SLOT_TABLE)
    now = now or datetime.utcnow() + timedelta(hours=WIB_UTC_OFFSET)
    day = now.date()
    while True:
        for s in SCHEDULE_SLOTS:
            dt = datetime.strptime(f"{day} {s}", "%Y-%m-%d %H:%M")
            if dt <= now:
                continue
            key = dt.strftime("%Y-%m-%d %H:%M")
            taken = conn.execute(
                "SELECT 1 FROM yt_slots WHERE account=? AND slot=?", (account, key)
            ).fetchone()
            if not taken:
                utc = dt - timedelta(hours=WIB_UTC_OFFSET)
                return utc.isoformat() + ".000Z", key
        day += timedelta(days=1)


def claim_slot(conn, account, slot_key, clip_name):
    conn.execute("INSERT OR REPLACE INTO yt_slots VALUES (?,?,?)",
                 (account, slot_key, clip_name))
    conn.commit()


class DailyLimitExceeded(Exception):
    pass


def upload(conn, video_path, meta, account=ACCOUNT):
    """Upload one clip as private+publishAt. Returns {video_id, url, publish_at}.
    Raises DailyLimitExceeded when the channel's daily quota is gone."""
    token_path = os.path.join(TOKEN_DIR, f"token_{account}.pickle")
    with open(token_path, "rb") as f:
        creds = pickle.load(f)
    yt = build("youtube", "v3", credentials=creds)

    publish_at, slot_key = next_slot(conn, account)
    body = {
        "snippet": {
            "title": meta["title"][:100],
            "description": meta["description"],
            "tags": meta.get("youtube_tags") or [],
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_at,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    try:
        response = None
        while response is None:
            _, response = request.next_chunk()
    except Exception as e:
        if "uploadLimitExceeded" in str(e):
            raise DailyLimitExceeded(str(e)) from e
        raise
    claim_slot(conn, account, slot_key, os.path.basename(video_path))
    vid = response["id"]
    return {"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
            "publish_at": slot_key}


if __name__ == "__main__":
    # Offline self-check: slot picker skips past + claimed slots, rolls to next day.
    import sqlite3
    c = sqlite3.connect(":memory:")
    now = datetime(2026, 7, 26, 14, 30)  # WIB afternoon
    iso, local = next_slot(c, "Test", now=now)
    assert local == "2026-07-26 15:00", local
    assert iso == "2026-07-26T08:00:00.000Z", iso  # WIB-7
    claim_slot(c, "Test", local, "clip-a")
    _, local2 = next_slot(c, "Test", now=now)
    assert local2 == "2026-07-26 19:00", local2
    for s in ("19:00", "21:00"):
        claim_slot(c, "Test", f"2026-07-26 {s}", "x")
    _, local3 = next_slot(c, "Test", now=now)
    assert local3 == "2026-07-27 10:00", local3  # rolled to next day
    print("upload_youtube.py self-check OK")
