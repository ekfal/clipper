"""TikTok Content Posting API v2 uploader.

Two ways in, and the difference decides whether a clip is worth anything:

  draft  (default)  POST /post/publish/inbox/video/init/
                    Lands in the creator's TikTok inbox. Works today, before
                    any audit. The creator writes the caption and taps post,
                    so it is not unattended — use it to prove the integration.

  direct (opt-in)   POST /post/publish/video/init/
                    Goes straight to the feed. Needs the Content Posting API
                    audit. Until that passes TikTok forces every post to
                    SELF_ONLY, i.e. nobody but the creator sees it and the
                    clip earns no views — so direct=True before approval is
                    worse than useless, it burns the footage. DIRECT_OK gates
                    it deliberately.

Credentials: CLIPPER_TOKEN_DIR/tiktok_<account>.json holding
{client_key, client_secret, access_token, refresh_token, expires_at, open_id}.
Same directory and per-account convention as the YouTube token pickles.
"""
import json
import os
import time

import requests

BASE = "https://open.tiktokapis.com/v2"
_HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_DIR = os.environ.get("CLIPPER_TOKEN_DIR", os.path.join(os.path.dirname(_HERE), "tokens"))
TIMEOUT = 120
# Set only once TikTok has approved the Content Posting audit for this app.
DIRECT_OK = os.environ.get("CLIPPER_TIKTOK_DIRECT", "").lower() in ("1", "true", "yes")
CHUNK_LIMIT = 64 * 1024 * 1024   # TikTok takes a file this size as one chunk
CAPTION_MAX = 2200


class TikTokError(RuntimeError):
    pass


class DailyLimitExceeded(TikTokError):
    """Mirrors upload_youtube.DailyLimitExceeded so pipeline can defer alike."""


def token_path(account):
    return os.path.join(TOKEN_DIR, f"tiktok_{account}.json")


def available_accounts():
    return sorted(
        f[len("tiktok_"):-len(".json")]
        for f in os.listdir(TOKEN_DIR)
        if f.startswith("tiktok_") and f.endswith(".json")
    ) if os.path.isdir(TOKEN_DIR) else []


def _save(account, creds):
    path = token_path(account)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(creds, f)
    try:
        os.chmod(path, 0o600)   # no-op on Windows, load-bearing on the VPS
    except OSError:
        pass


def load_credentials(account, now=None):
    """Read the account's tokens, refreshing when the access token is stale.

    TikTok access tokens last a day; the refresh token is what has to survive,
    so a refreshed pair is written straight back to disk.
    """
    with open(token_path(account), encoding="utf-8") as f:
        creds = json.load(f)
    now = now or time.time()
    if creds.get("expires_at", 0) - 60 > now:
        return creds
    if not creds.get("refresh_token"):
        raise TikTokError(f"tiktok_{account}.json has no refresh_token — re-run the OAuth flow")

    res = requests.post(
        f"{BASE}/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"client_key": creds["client_key"], "client_secret": creds["client_secret"],
              "grant_type": "refresh_token", "refresh_token": creds["refresh_token"]},
        timeout=TIMEOUT)
    body = res.json()
    if "access_token" not in body:
        # invalid_grant here means a dead refresh token — watchdog routes that
        # string to NEEDS_HUMAN, which is exactly right: only a human can redo
        # the OAuth flow.
        raise TikTokError(f"tiktok refresh failed: {body.get('error')} "
                          f"{body.get('error_description', '')}")
    creds.update(access_token=body["access_token"],
                 refresh_token=body.get("refresh_token", creds["refresh_token"]),
                 expires_at=now + int(body.get("expires_in", 86400)))
    _save(account, creds)
    return creds


def _api(creds, path, payload):
    res = requests.post(f"{BASE}{path}", timeout=TIMEOUT,
                        headers={"Authorization": f"Bearer {creds['access_token']}",
                                 "Content-Type": "application/json; charset=UTF-8"},
                        json=payload)
    body = res.json() if res.content else {}
    err = (body.get("error") or {})
    code = err.get("code", "ok")
    if code not in ("ok", "", None):
        msg = f"{code}: {err.get('message', '')}"
        if "rate_limit" in code or "daily" in code or res.status_code == 429:
            raise DailyLimitExceeded(msg)
        raise TikTokError(msg)
    return body.get("data", {})


def creator_info(creds):
    """Nickname, avatar and the privacy levels this creator may choose.

    The audit requires the posting surface to show the creator and let them
    pick a privacy level, so this is the call that feeds that screen — and it
    also reveals when an account is barred from posting right now.
    """
    return _api(creds, "/post/publish/creator_info/query/", {})


def caption_for(meta):
    """TikTok caption: hashtags live in the caption, so the description is it."""
    text = (meta.get("description") or meta.get("title") or "").strip()
    return text[:CAPTION_MAX]


def upload(conn, video_path, meta, account=None, direct=False, poll=False):
    """Send one clip to TikTok. Returns {publish_id, account, mode, url}.

    conn is unused — the signature matches upload_youtube.upload so both sit
    behind the same registry entry.
    """
    account = account or (available_accounts() or [None])[0]
    if account is None:
        raise TikTokError(f"no tiktok_*.json in {TOKEN_DIR}")
    if direct and not DIRECT_OK:
        raise TikTokError(
            "direct posting is off: until the TikTok Content Posting audit "
            "passes every direct post is forced to SELF_ONLY and earns no "
            "views. Set CLIPPER_TIKTOK_DIRECT=1 once approved.")

    creds = load_credentials(account)
    size = os.path.getsize(video_path)
    if size > CHUNK_LIMIT:
        raise TikTokError(f"{size} bytes exceeds the {CHUNK_LIMIT}-byte single-chunk limit")
    source = {"source": "FILE_UPLOAD", "video_size": size,
              "chunk_size": size, "total_chunk_count": 1}

    if direct:
        info = creator_info(creds)
        allowed = info.get("privacy_level_options") or ["SELF_ONLY"]
        level = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in allowed else allowed[0]
        data = _api(creds, "/post/publish/video/init/", {
            "post_info": {"title": caption_for(meta), "privacy_level": level},
            "source_info": source})
    else:
        data = _api(creds, "/post/publish/inbox/video/init/", {"source_info": source})

    publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
    if not (publish_id and upload_url):
        raise TikTokError(f"init returned no upload target: {data}")

    with open(video_path, "rb") as f:
        put = requests.put(upload_url, data=f, timeout=TIMEOUT,
                           headers={"Content-Type": "video/mp4",
                                    "Content-Length": str(size),
                                    "Content-Range": f"bytes 0-{size - 1}/{size}"})
    if not (200 <= put.status_code < 300):
        raise TikTokError(f"upload PUT {put.status_code}: {put.text[:200]}")

    out = {"publish_id": publish_id, "account": account,
           "mode": "direct" if direct else "draft",
           "url": f"https://www.tiktok.com/@{account}"}
    if poll:
        out["status"] = wait_for(creds, publish_id)
    return out


def wait_for(creds, publish_id, tries=10, delay=6, sleep=time.sleep):
    """Poll until TikTok finishes processing. Returns the last status payload.

    Processing is asynchronous: a clean PUT only means TikTok took the bytes,
    not that the post exists.
    """
    last = {}
    for _ in range(tries):
        last = _api(creds, "/post/publish/status/fetch/", {"publish_id": publish_id})
        status = last.get("status")
        if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return last
        if status == "FAILED":
            raise TikTokError(f"publish failed: {last.get('fail_reason', last)}")
        sleep(delay)
    return last


if __name__ == "__main__":
    # Offline self-check: no network, every HTTP call stubbed.
    import types

    calls = []

    class FakeResp:
        def __init__(self, body, status=200):
            self._b, self.status_code, self.content = body, status, b"x"
            self.text = json.dumps(body)

        def json(self):
            return self._b

    def fake_post(url, **kw):
        calls.append(("POST", url, kw.get("json") or kw.get("data")))
        if url.endswith("/oauth/token/"):
            return FakeResp({"access_token": "new", "refresh_token": "r2", "expires_in": 86400})
        if url.endswith("/creator_info/query/"):
            return FakeResp({"data": {"creator_nickname": "leo",
                                      "privacy_level_options": ["PUBLIC_TO_EVERYONE",
                                                                "SELF_ONLY"]}})
        if url.endswith("/status/fetch/"):
            return FakeResp({"data": {"status": "SEND_TO_USER_INBOX"}})
        return FakeResp({"data": {"publish_id": "pid1", "upload_url": "https://up/x"}})

    def fake_put(url, **kw):
        calls.append(("PUT", url, kw.get("headers", {}).get("Content-Range")))
        return FakeResp({}, 200)

    import tempfile
    tmp = tempfile.mkdtemp()
    globals()["TOKEN_DIR"] = tmp
    vid = os.path.join(tmp, "clip.mp4")
    with open(vid, "wb") as f:
        f.write(b"0" * 2048)

    def write_creds(**over):
        creds = {"client_key": "k", "client_secret": "s", "access_token": "old",
                 "refresh_token": "r1", "expires_at": 0, "open_id": "o"}
        creds.update(over)
        _save("leo", creds)

    write_creds()
    assert available_accounts() == ["leo"], available_accounts()

    real = requests.post, requests.put
    requests.post, requests.put = fake_post, fake_put
    try:
        # a stale token refreshes and the new pair is written back to disk
        c = load_credentials("leo", now=1000)
        assert c["access_token"] == "new" and c["refresh_token"] == "r2", c
        with open(token_path("leo"), encoding="utf-8") as f:
            assert json.load(f)["access_token"] == "new"
        # a live token is used as-is, no refresh call
        n = len(calls)
        assert load_credentials("leo", now=1000)["access_token"] == "new"
        assert len(calls) == n, "refreshed a token that was still valid"

        meta = {"title": "t", "description": "cuan #leogiovanni #Shorts"}
        res = upload(None, vid, meta, account="leo")
        assert res["mode"] == "draft" and res["publish_id"] == "pid1", res
        assert any(u.endswith("/inbox/video/init/") for _, u, _ in calls)
        rng = [c for c in calls if c[0] == "PUT"][-1][2]
        assert rng == "bytes 0-2047/2048", rng

        # direct posting stays shut until the audit passes
        try:
            upload(None, vid, meta, account="leo", direct=True)
            raise AssertionError("direct should be gated")
        except TikTokError as e:
            assert "audit" in str(e), e

        globals()["DIRECT_OK"] = True
        calls.clear()
        res = upload(None, vid, meta, account="leo", direct=True, poll=True)
        assert res["mode"] == "direct", res
        init = [c for c in calls if c[1].endswith("/post/publish/video/init/")][0]
        assert init[2]["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE", init
        assert init[2]["post_info"]["title"].startswith("cuan #leogiovanni")
        assert res["status"]["status"] == "SEND_TO_USER_INBOX", res

        # rate limits surface as the same exception the pipeline already defers on
        requests.post = lambda url, **kw: FakeResp(
            {"error": {"code": "rate_limit_exceeded", "message": "slow down"}}, 429)
        try:
            upload(None, vid, meta, account="leo")
            raise AssertionError("should have raised")
        except DailyLimitExceeded:
            pass

        # a dead refresh token says so in words watchdog routes to NEEDS_HUMAN
        requests.post = lambda url, **kw: FakeResp(
            {"error": "invalid_grant", "error_description": "refresh token expired"})
        write_creds(expires_at=0)
        try:
            load_credentials("leo", now=1000)
            raise AssertionError("should have raised")
        except TikTokError as e:
            assert "invalid_grant" in str(e), e
            import watchdog
            assert watchdog.classify(str(e))[0] == "NEEDS_HUMAN", str(e)
    finally:
        requests.post, requests.put = real

    assert len(caption_for({"description": "x" * 5000})) == CAPTION_MAX
    print("upload_tiktok.py self-check OK")
