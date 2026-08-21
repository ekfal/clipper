"""YouTube Shorts uploader (PRD §3.9) — ported from legacy uploader.py.

Kept: publishAt scheduling (upload private, YouTube flips public on schedule),
daily WIB slots, resumable upload, daily-limit detection. Dropped: n8n webhook,
Windows file-lock retry loop, cleanup (pipeline owns file lifecycle).
Schedule bookkeeping moved from scheduled_slots.json into SQLite.
"""
import glob
import os
import pickle
import re
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


def posts_today(conn, account, now=None):
    """Clips already scheduled for this account today (WIB).

    The slot table is the post ledger — no extra column needed, a claimed slot
    is exactly one scheduled upload.
    """
    conn.execute(SLOT_TABLE)
    now = now or datetime.utcnow() + timedelta(hours=WIB_UTC_OFFSET)
    return conn.execute("SELECT COUNT(*) FROM yt_slots WHERE account=? AND slot LIKE ?",
                        (account, f"{now.date()}%")).fetchone()[0]


class DailyLimitExceeded(Exception):
    pass


def available_accounts():
    """Account names taken from tokens/token_<name>.pickle."""
    return sorted(
        os.path.basename(p)[len("token_"):-len(".pickle")]
        for p in glob.glob(os.path.join(TOKEN_DIR, "token_*.pickle"))
    )


def eligible_accounts(conn, now=None):
    """Token accounts cleared for campaign work and still under their day's cap.

    Cleared means accounts.py holds the same name as a `campaign_ready` YouTube
    account — so a paused, banned, still-warming or suspected-shadowbanned
    channel can never receive a clip. An account with a token but no registry
    row is NOT cleared: register it in the dashboard first.

    ponytail: registry username must equal the token file name; a mapping
    column only earns its keep once the two genuinely diverge.
    """
    import accounts
    accounts.init(conn)  # idempotent — pipeline never touches the registry otherwise
    ready = {r["username"].lower(): r for r in conn.execute(
        "SELECT * FROM accounts WHERE platform='youtube' AND status='campaign_ready'")}
    out = []
    for name in available_accounts():
        row = ready.get(name.lower())
        if row is None:
            continue
        cap = row["daily_post_limit"] or accounts.DEFAULT_DAILY_POST_LIMIT
        if posts_today(conn, name, now=now) < cap:
            out.append(name)
    return out


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


def video_id_of(published_url):
    """Extract the YouTube id from a stored published_url, or None."""
    m = re.search(r"[?&]v=([\w-]{11})|youtu\.be/([\w-]{11})|/shorts/([\w-]{11})",
                  published_url or "")
    return next((g for g in m.groups() if g), None) if m else None


def sync_views(conn, account=None, batch=50):
    """Refresh view counts on published clips. Returns rows updated.

    Nothing else writes clips.views_last_checked, and pipeline.eligible_clips
    gates submission on it — without this step no clip ever reaches Clippo.

    View counts are public, so any channel's credentials read every clip's
    count; no need to know which account owns which video. Videos still
    waiting on their publishAt slot report no statistics yet and are left
    alone, so they simply stay below the threshold until they go live.
    """
    rows = conn.execute(
        """SELECT clip_id, published_url FROM clips
            WHERE status IN ('PUBLISHED','SUBMITTED') AND published_url IS NOT NULL"""
    ).fetchall()
    by_video = {}
    for r in rows:
        vid = video_id_of(r["published_url"])
        if vid:
            by_video.setdefault(vid, []).append(r["clip_id"])
    if not by_video:
        return 0

    account = account or (available_accounts() or [None])[0]
    if account is None:
        raise RuntimeError(f"no token_*.pickle in {TOKEN_DIR}")
    yt = build("youtube", "v3", credentials=load_credentials(account))

    ids = list(by_video)
    n = 0
    for i in range(0, len(ids), batch):  # videos.list takes up to 50 ids per call
        res = yt.videos().list(part="statistics",
                               id=",".join(ids[i:i + batch])).execute()
        for item in res.get("items", []):
            views = int(item.get("statistics", {}).get("viewCount") or 0)
            for clip_id in by_video.get(item["id"], []):
                conn.execute("UPDATE clips SET views_last_checked=? WHERE clip_id=?",
                             (views, clip_id))
                n += 1
    conn.commit()
    return n


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

    # daily cap: claimed slots for today are the ledger, tomorrow's don't count
    assert posts_today(c, "Test", now=now) == 3, posts_today(c, "Test", now=now)
    claim_slot(c, "Test", "2026-07-27 10:00", "tomorrow")
    assert posts_today(c, "Test", now=now) == 3        # still today's three
    assert posts_today(c, "Other", now=now) == 0       # per account, not global

    # category detection maps to a real account and degrades safely.
    # Stubbed accounts keep this runnable on a box with no tokens/ dir.
    accts = available_accounts() or ["Test", "Gaming"]
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

    # eligibility gate: registry status and the daily cap both bind
    import accounts as acct_registry
    import db as _db
    import tempfile
    c2 = _db.connect(os.path.join(tempfile.mkdtemp(), "u.sqlite"))
    c2.executescript(_db.SCHEMA)
    acct_registry.init(c2)
    real_avail = globals()["available_accounts"]
    globals()["available_accounts"] = lambda: ["Ready", "Warming", "Unregistered"]
    try:
        rid = acct_registry.add(c2, "youtube", "Ready")
        acct_registry.update(c2, rid, status="campaign_ready", daily_post_limit=2)
        wid = acct_registry.add(c2, "youtube", "Warming")
        acct_registry.update(c2, wid, status="warming")
        assert eligible_accounts(c2, now=now) == ["Ready"], eligible_accounts(c2, now=now)
        claim_slot(c2, "Ready", "2026-07-26 10:00", "a")
        assert eligible_accounts(c2, now=now) == ["Ready"]     # 1 of 2 used
        claim_slot(c2, "Ready", "2026-07-26 13:00", "b")
        assert eligible_accounts(c2, now=now) == []            # cap reached
        acct_registry.update(c2, rid, status="banned")
        claim_slot(c2, "Ready", "2026-07-27 10:00", "c")
        assert eligible_accounts(c2, now=now) == []            # banned stays out
    finally:
        globals()["available_accounts"] = real_avail

    # published_url -> video id, every shape we ever store
    assert video_id_of("https://www.youtube.com/watch?v=IJE50gujMTg") == "IJE50gujMTg"
    assert video_id_of("https://youtu.be/IJE50gujMTg") == "IJE50gujMTg"
    assert video_id_of("https://www.youtube.com/shorts/IJE50gujMTg") == "IJE50gujMTg"
    assert video_id_of(None) is None and video_id_of("garbage") is None

    # sync_views writes the column pipeline.eligible_clips reads
    c2.execute("""INSERT INTO clips (clip_id, task_id, platform, published_url, status)
                  VALUES (1, 1, 'youtube', 'https://www.youtube.com/watch?v=aaaaaaaaaaa',
                          'PUBLISHED')""")
    c2.execute("""INSERT INTO clips (clip_id, task_id, platform, published_url, status)
                  VALUES (2, 1, 'youtube', 'https://youtu.be/bbbbbbbbbbb', 'PUBLISHED')""")
    c2.execute("""INSERT INTO clips (clip_id, task_id, platform, published_url, status)
                  VALUES (3, 1, 'youtube', NULL, 'EDITED')""")
    c2.commit()

    class _FakeYT:                     # videos().list(...).execute()
        def videos(self):
            return self
        def list(self, part, id):
            self.asked = id
            return self
        def execute(self):
            return {"items": [
                {"id": "aaaaaaaaaaa", "statistics": {"viewCount": "1500"}},
                {"id": "bbbbbbbbbbb", "statistics": {}},   # scheduled, not live yet
            ]}

    fake = _FakeYT()
    real_build, real_creds = globals()["build"], globals()["load_credentials"]
    globals()["build"] = lambda *a, **k: fake
    globals()["load_credentials"] = lambda a: None
    try:
        assert sync_views(c2, account="Ready") == 2
    finally:
        globals()["build"], globals()["load_credentials"] = real_build, real_creds
    seen = dict(c2.execute("SELECT clip_id, views_last_checked FROM clips").fetchall())
    assert seen[1] == 1500, seen
    assert seen[2] == 0, seen          # no statistics yet -> stays below threshold
    assert seen[3] is None, seen       # unpublished clip untouched
    assert "aaaaaaaaaaa" in fake.asked and "," in fake.asked  # batched into one call

    print("upload_youtube.py self-check OK |", len(accts), "accounts:", ", ".join(accts))
