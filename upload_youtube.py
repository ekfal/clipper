"""YouTube Shorts uploader (PRD §3.9) — ported from legacy uploader.py.

Kept: publishAt scheduling (upload private, YouTube flips public on schedule),
daily WIB slots, resumable upload, daily-limit detection. Dropped: n8n webhook,
Windows file-lock retry loop, cleanup (pipeline owns file lifecycle).
Schedule bookkeeping moved from scheduled_slots.json into SQLite.
"""
import glob
import os
import pickle
from datetime import datetime, timedelta

from google.auth.transport.requests import Request
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


def available_accounts():
    """Account names taken from tokens/token_<name>.pickle."""
    return sorted(
        os.path.basename(p)[len("token_"):-len(".pickle")]
        for p in glob.glob(os.path.join(TOKEN_DIR, "token_*.pickle"))
    )


def load_credentials(account):
    """Load an account's credentials, refreshing and re-saving when expired."""
    path = os.path.join(TOKEN_DIR, f"token_{account}.pickle")
    with open(path, "rb") as f:
        creds = pickle.load(f)
    if not creds.valid and getattr(creds, "refresh_token", None):
        creds.refresh(Request())
        with open(path, "wb") as f:  # keep the refreshed token for next run
            pickle.dump(creds, f)
    return creds


CATEGORY_SYSTEM = """You classify Indonesian video clips into one of a fixed
set of channel categories. Output strictly one JSON object, no markdown."""

CATEGORY_USER = """Kategori yang tersedia: {options}

Judul klip: {title}
Transkrip: \"\"\"{transcript}\"\"\"

Pilih SATU kategori yang paling cocok untuk klip ini. Kalau tidak ada yang
benar-benar cocok, pilih yang paling mendekati.

Return JSON: {{"category": "<salah satu dari daftar>", "reason": "<1 kalimat>"}}"""


def detect_category(transcript, title="", accounts=None, default=None):
    """Pick the channel account whose category fits the clip.

    Returns (account, reason). Falls back to `default` (or the first account)
    when the model is unreachable or answers with something unknown, so a
    classification failure never blocks an upload.
    """
    accounts = accounts or available_accounts()
    if not accounts:
        raise RuntimeError(f"no token_*.pickle in {TOKEN_DIR}")
    fallback = default if default in accounts else accounts[0]
    try:
        import ai
        out = ai.chat_json(
            CATEGORY_SYSTEM,
            CATEGORY_USER.format(options=", ".join(accounts), title=title,
                                 transcript=transcript[:3000]),
        )
    except Exception as e:
        return fallback, f"detection failed ({type(e).__name__}), using {fallback}"
    choice = str(out.get("category") or "").strip()
    match = next((a for a in accounts if a.lower() == choice.lower()), None)
    if not match:
        return fallback, f"unknown category {choice!r}, using {fallback}"
    return match, str(out.get("reason") or "").strip()


def upload(conn, video_path, meta, account=ACCOUNT, schedule=True):
    """Upload one clip. Returns {video_id, url, publish_at, account}.

    schedule=True (production) uploads private with a publishAt slot, so
    YouTube flips it public on the timetable. schedule=False stays private
    with no timer — nothing goes public unless someone chooses to.
    Raises DailyLimitExceeded when the channel's daily quota is gone.
    """
    creds = load_credentials(account)
    yt = build("youtube", "v3", credentials=creds)

    publish_at = slot_key = None
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    if schedule:
        publish_at, slot_key = next_slot(conn, account)
        status["publishAt"] = publish_at
    body = {
        "snippet": {
            "title": meta["title"][:100],
            "description": meta["description"],
            "tags": meta.get("youtube_tags") or [],
            "categoryId": "22",
        },
        "status": status,
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
    if slot_key:
        claim_slot(conn, account, slot_key, os.path.basename(video_path))
    vid = response["id"]
    return {"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
            "publish_at": slot_key, "account": account}


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

    # category detection maps to a real account and degrades safely
    accts = available_accounts()
    assert accts, "no tokens found"
    import ai
    real = ai.chat_json
    try:
        ai.chat_json = lambda *a, **k: {"category": accts[-1].lower(), "reason": "r"}
        got, _ = detect_category("apa pun", accounts=accts)
        assert got == accts[-1], got                       # case-insensitive match
        ai.chat_json = lambda *a, **k: {"category": "Nonsense"}
        got, why = detect_category("x", accounts=accts, default=accts[0])
        assert got == accts[0] and "unknown" in why, (got, why)
        ai.chat_json = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        got, why = detect_category("x", accounts=accts, default=accts[0])
        assert got == accts[0] and "failed" in why, (got, why)
    finally:
        ai.chat_json = real
    print("upload_youtube.py self-check OK |", len(accts), "accounts:", ", ".join(accts))
