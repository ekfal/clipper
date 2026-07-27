"""Social account registry — lifecycle, thresholds and public-stat sync.

Accounts move new -> warming -> farming -> campaign_ready, with paused/banned
as manual off-ramps (PRD §3.8 states, plus the warm-up phase every clipper
guide insists on). A campaign account must be past warm-up, over its follower
target, and verified with Clippo's 6-digit bio code.

Stats come from the public profile page via Playwright; the official
TikTok/Instagram APIs need app audits that are still pending.
"""
import re
import time

import db

PLATFORMS = ("tiktok", "instagram", "youtube")
STATUSES = ("new", "warming", "farming", "campaign_ready", "paused", "banned")

# Warm-up days before an account should post campaign work. Instagram filters
# new accounts hardest; TikTok is aggressive but forgives quickly.
WARMUP_DAYS = {"tiktok": 3, "instagram": 7, "youtube": 3}
DEFAULT_FOLLOWER_TARGET = 10
# A healthy new account sees 50+ views in 24h; 0-5 views across several clips
# is the shadowban signature, not slow growth.
SHADOWBAN_VIEW_CEILING = 5
SHADOWBAN_MIN_CLIPS = 3
DEFAULT_DAILY_POST_LIMIT = 3  # guides converge on 3-5 per account per day

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    platform          TEXT NOT NULL,
    username          TEXT NOT NULL,
    profile_url       TEXT,
    niche             TEXT,
    status            TEXT NOT NULL DEFAULT 'new',
    followers         INTEGER DEFAULT 0,
    following         INTEGER DEFAULT 0,
    video_count       INTEGER DEFAULT 0,
    likes             INTEGER DEFAULT 0,
    followers_target  INTEGER DEFAULT 10,
    warmup_started_at TEXT,
    verified_bio_code TEXT,
    verified_at       TEXT,
    device_label      TEXT,
    proxy_label       TEXT,
    daily_post_limit  INTEGER DEFAULT 3,
    last_post_at      TEXT,
    recent_avg_views  INTEGER,
    recent_clip_count INTEGER DEFAULT 0,
    notes             TEXT,
    created_at        TEXT,
    synced_at         TEXT,
    UNIQUE(platform, username)
);
"""


def init(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _days_since(iso):
    if not iso:
        return None
    try:
        t = time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return (time.time() - time.mktime(t)) / 86400


def profile_url(platform, username):
    u = username.lstrip("@")
    return {
        "tiktok": f"https://www.tiktok.com/@{u}",
        "instagram": f"https://www.instagram.com/{u}/",
        "youtube": f"https://www.youtube.com/@{u}",
    }.get(platform, "")


def add(conn, platform, username, niche=None, followers_target=DEFAULT_FOLLOWER_TARGET,
        device_label=None, proxy_label=None, notes=None):
    """Register an account. Returns account_id."""
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r}")
    username = username.strip().lstrip("@")
    if not username:
        raise ValueError("username required")
    cur = conn.execute(
        """INSERT INTO accounts (platform, username, profile_url, niche, status,
             followers_target, device_label, proxy_label, daily_post_limit,
             notes, created_at)
           VALUES (?,?,?,?,'new',?,?,?,?,?,?)""",
        (platform, username, profile_url(platform, username), niche,
         followers_target, device_label, proxy_label, DEFAULT_DAILY_POST_LIMIT,
         notes, _now()))
    conn.commit()
    return cur.lastrowid


def update(conn, account_id, **fields):
    """Patch arbitrary columns. Unknown keys are rejected, not silently dropped."""
    allowed = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown fields: {sorted(bad)}")
    if not fields:
        return
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"unknown status {fields['status']!r}")
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE accounts SET {sets} WHERE account_id=?",
                 (*fields.values(), account_id))
    conn.commit()


def delete(conn, account_id):
    conn.execute("DELETE FROM accounts WHERE account_id=?", (account_id,))
    conn.commit()


def all_accounts(conn):
    return conn.execute("SELECT * FROM accounts ORDER BY platform, username").fetchall()


def get(conn, account_id):
    return conn.execute("SELECT * FROM accounts WHERE account_id=?",
                        (account_id,)).fetchone()


def start_warmup(conn, account_id):
    update(conn, account_id, status="warming", warmup_started_at=_now())


def verify(conn, account_id, code):
    """Record the Clippo 6-digit bio code (§3.8 — done by hand, once)."""
    code = str(code).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("bio code must be 6 digits")
    update(conn, account_id, verified_bio_code=code, verified_at=_now())


def evaluate(row):
    """Suggest the status this account should be in, with a reason.

    Returns (suggested_status, reason). Suggestion only — the dashboard shows
    it and a human applies it, so a bad stat sync can't silently promote an
    account into campaign work.
    """
    status = row["status"]
    if status in ("paused", "banned"):
        return status, "manually held"

    views, clips = row["recent_avg_views"], row["recent_clip_count"] or 0
    if (views is not None and views <= SHADOWBAN_VIEW_CEILING
            and clips >= SHADOWBAN_MIN_CLIPS):
        return "paused", (f"possible shadowban: {views} avg views over {clips} clips "
                          f"(healthy is 50+ within 24h)")

    if status == "new":
        return "warming", "not warmed up yet — start the warm-up routine"

    days = _days_since(row["warmup_started_at"])
    need = WARMUP_DAYS.get(row["platform"], 3)
    if status == "warming":
        if days is None:
            return "warming", "warm-up start date missing"
        if days < need:
            return "warming", f"warming {days:.1f}/{need} days on {row['platform']}"
        return "farming", f"warm-up done ({days:.1f} days) — safe to start posting"

    target = row["followers_target"] or DEFAULT_FOLLOWER_TARGET
    if status == "farming":
        if (row["followers"] or 0) < target:
            return "farming", f"{row['followers'] or 0}/{target} followers"
        if not row["verified_bio_code"]:
            return "farming", (f"{row['followers']} followers — needs the Clippo "
                               f"6-digit bio code before campaign work")
        return "campaign_ready", f"{row['followers']} followers and verified"

    return status, f"{row['followers'] or 0} followers"


_NUM = re.compile(r"([\d.,]+)\s*([KMB])?", re.I)


def parse_count(text):
    """'1.2M' / '45.6K' / '1,234' -> int."""
    m = _NUM.fullmatch(text.strip())
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    return int(num * {"k": 1e3, "m": 1e6, "b": 1e9}[m.group(2).lower()]) if m.group(2) else int(num)


def _stats_from_text(body):
    """Pull follower/following/like counts from a rendered profile page.

    The counts sit immediately before their labels on both TikTok and
    Instagram, in either English or Indonesian.
    """
    labels = {
        "followers": ("followers", "pengikut"),
        "following": ("following", "mengikuti", "diikuti"),
        "likes": ("likes", "suka"),
        "video_count": ("posts", "postingan", "kiriman"),
    }
    out = {}
    tokens = [t.strip() for t in body.splitlines() if t.strip()]
    for i, tok in enumerate(tokens):
        low = tok.lower()
        for field, names in labels.items():
            if field in out or low not in names:
                continue
            if i:
                val = parse_count(tokens[i - 1])
                if val is not None:
                    out[field] = val
    # single-line layouts: "1.2M followers"
    for field, names in labels.items():
        if field in out:
            continue
        for name in names:
            m = re.search(rf"([\d.,]+[KMB]?)\s+{name}\b", body, re.I)
            if m:
                out[field] = parse_count(m.group(1))
                break
    return out


def sync(conn, account_id, timeout_ms=45000):
    """Refresh public stats from the platform profile page. Returns dict of
    what changed, or raises with why it could not read the profile."""
    from playwright.sync_api import sync_playwright

    row = get(conn, account_id)
    if row is None:
        raise ValueError(f"no account {account_id}")
    url = row["profile_url"] or profile_url(row["platform"], row["username"])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0 Safari/537.36").new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3000)
            body = page.inner_text("body")
        finally:
            browser.close()

    stats = _stats_from_text(body)
    if not stats:
        raise RuntimeError("could not read stats — profile private, renamed, "
                           "or the platform served a bot wall")
    stats["synced_at"] = _now()
    update(conn, account_id, **stats)
    return stats


if __name__ == "__main__":
    import tempfile, os
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "a.sqlite"))
    init(conn)

    assert parse_count("1.2M") == 1_200_000
    assert parse_count("45.6K") == 45_600
    assert parse_count("1,234") == 1234
    assert parse_count("nope") is None

    stats = _stats_from_text("Leo\nleo\n0\nFollowing\n7\nFollowers\n0\nLikes")
    assert stats["followers"] == 7 and stats["following"] == 0, stats
    assert _stats_from_text("1.2M followers 300 following")["followers"] == 1_200_000

    aid = add(conn, "tiktok", "@clipper01", niche="Entertainment")
    row = get(conn, aid)
    assert row["username"] == "clipper01"  # @ stripped
    assert row["profile_url"].endswith("/@clipper01")
    assert evaluate(row)[0] == "warming"

    start_warmup(conn, aid)
    assert evaluate(get(conn, aid))[0] == "warming"          # day 0 of 3
    update(conn, aid, warmup_started_at="2026-07-01T00:00:00Z")
    assert evaluate(get(conn, aid))[0] == "farming"          # warm-up elapsed

    update(conn, aid, status="farming", followers=4)
    got, why = evaluate(get(conn, aid))
    assert got == "farming" and "4/10" in why, why

    update(conn, aid, followers=12)
    got, why = evaluate(get(conn, aid))
    assert got == "farming" and "bio code" in why, why      # verification gate

    verify(conn, aid, "123456")
    assert evaluate(get(conn, aid))[0] == "campaign_ready"

    update(conn, aid, recent_avg_views=2, recent_clip_count=4)
    got, why = evaluate(get(conn, aid))
    assert got == "paused" and "shadowban" in why, why      # health beats promotion

    try:
        verify(conn, aid, "12ab")
        raise AssertionError("bad code accepted")
    except ValueError:
        pass
    try:
        update(conn, aid, nonsense=1)
        raise AssertionError("unknown field accepted")
    except ValueError:
        pass
    print("accounts.py self-check OK")
