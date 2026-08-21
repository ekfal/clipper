"""Instagram Reels uploader — Graph API content publishing.

Three steps, and the middle one is the part people forget:

  1. POST /{ig_user_id}/media          create a container from a video URL
  2. GET  /{container_id}?fields=status_code   wait for FINISHED
  3. POST /{ig_user_id}/media_publish  publish the finished container

There is no file upload. Meta's servers fetch the video from `video_url`
themselves, so the clip has to sit on a public HTTPS URL at publish time —
CLIPPER_PUBLIC_BASE plus the filename. Serve OUT_DIR over nginx:

    location /clips/ { alias /opt/clipper/out/; }

and set CLIPPER_PUBLIC_BASE=https://your.host/clips. The pipeline deletes the
file after a confirmed upload, so nothing lingers on the public path.

Requirements Meta enforces and this module cannot work around: an Instagram
*Business* account (Creator accounts cannot publish via the API), the
instagram_business_content_publish permission through App Review, and a Reel
between 5 and 90 seconds at 9:16.

Credentials: CLIPPER_TOKEN_DIR/instagram_<account>.json holding
{access_token, ig_user_id}.
"""
import json
import os
import time

import requests

GRAPH = os.environ.get("CLIPPER_GRAPH_BASE", "https://graph.instagram.com/v23.0")
_HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_DIR = os.environ.get("CLIPPER_TOKEN_DIR", os.path.join(os.path.dirname(_HERE), "tokens"))
PUBLIC_BASE = os.environ.get("CLIPPER_PUBLIC_BASE", "").rstrip("/")
TIMEOUT = 120
CAPTION_MAX = 2200
REEL_MIN_SEC, REEL_MAX_SEC = 5, 90


class InstagramError(RuntimeError):
    pass


class DailyLimitExceeded(InstagramError):
    """Mirrors upload_youtube.DailyLimitExceeded so pipeline defers alike."""


def token_path(account):
    return os.path.join(TOKEN_DIR, f"instagram_{account}.json")


def available_accounts():
    return sorted(
        f[len("instagram_"):-len(".json")]
        for f in os.listdir(TOKEN_DIR)
        if f.startswith("instagram_") and f.endswith(".json")
    ) if os.path.isdir(TOKEN_DIR) else []


def load_credentials(account):
    with open(token_path(account), encoding="utf-8") as f:
        creds = json.load(f)
    for key in ("access_token", "ig_user_id"):
        if not creds.get(key):
            raise InstagramError(f"instagram_{account}.json is missing {key}")
    return creds


def _call(method, url, creds, **kw):
    params = dict(kw.pop("params", {}), access_token=creds["access_token"])
    res = getattr(requests, method)(url, params=params, timeout=TIMEOUT, **kw)
    body = res.json() if res.content else {}
    err = body.get("error")
    if err:
        msg = f"{err.get('code')}: {err.get('message', '')}"
        # 4 and 32 are throttling, 613 is the publishing cap for the day
        if err.get("code") in (4, 32, 613) or res.status_code == 429:
            raise DailyLimitExceeded(msg)
        raise InstagramError(msg)
    return body


def public_url_for(video_path):
    if not PUBLIC_BASE:
        raise InstagramError(
            "CLIPPER_PUBLIC_BASE is unset — Meta fetches the video itself, so "
            "the clip must be reachable over public HTTPS")
    return f"{PUBLIC_BASE}/{os.path.basename(video_path)}"


def caption_for(meta):
    return (meta.get("description") or meta.get("title") or "").strip()[:CAPTION_MAX]


def publishing_limit(creds):
    """How many posts this account has already published in the 24h window.

    Meta is the authority on its own cap, so ask rather than hardcode a number.
    """
    body = _call("get", f"{GRAPH}/{creds['ig_user_id']}/content_publishing_limit",
                 creds, params={"fields": "config,quota_usage"})
    row = (body.get("data") or [{}])[0]
    return {"used": row.get("quota_usage", 0),
            "cap": (row.get("config") or {}).get("quota_total", 0)}


def wait_for(creds, container_id, tries=20, delay=6, sleep=time.sleep):
    """Block until the container finishes transcoding.

    Publishing an unfinished container fails, and the container id alone says
    nothing about whether Meta could actually fetch and process the file.
    """
    last = ""
    for _ in range(tries):
        body = _call("get", f"{GRAPH}/{container_id}", creds,
                     params={"fields": "status_code,status"})
        last = body.get("status_code", "")
        if last == "FINISHED":
            return True
        if last in ("ERROR", "EXPIRED"):
            raise InstagramError(f"container {last}: {body.get('status', '')}")
        sleep(delay)
    raise InstagramError(f"container still {last or 'IN_PROGRESS'} after {tries} checks")


def upload(conn, video_path, meta, account=None, video_url=None, sleep=time.sleep):
    """Publish one Reel. Returns {media_id, url, account}.

    conn is unused — the signature matches upload_youtube.upload so both sit
    behind the same registry entry.
    """
    account = account or (available_accounts() or [None])[0]
    if account is None:
        raise InstagramError(f"no instagram_*.json in {TOKEN_DIR}")
    creds = load_credentials(account)

    limit = publishing_limit(creds)
    if limit["cap"] and limit["used"] >= limit["cap"]:
        raise DailyLimitExceeded(
            f"{account} used {limit['used']}/{limit['cap']} posts in the last 24h")

    container = _call("post", f"{GRAPH}/{creds['ig_user_id']}/media", creds, params={
        "media_type": "REELS",
        "video_url": video_url or public_url_for(video_path),
        "caption": caption_for(meta),
    })
    container_id = container.get("id")
    if not container_id:
        raise InstagramError(f"no container id in response: {container}")

    wait_for(creds, container_id, sleep=sleep)

    published = _call("post", f"{GRAPH}/{creds['ig_user_id']}/media_publish", creds,
                      params={"creation_id": container_id})
    media_id = published.get("id")
    if not media_id:
        raise InstagramError(f"no media id in response: {published}")
    return {"media_id": media_id, "account": account,
            "url": f"https://www.instagram.com/reel/{media_id}/"}


if __name__ == "__main__":
    # Offline self-check: no network, every HTTP call stubbed.
    import tempfile

    calls = []

    class FakeResp:
        def __init__(self, body, status=200):
            self._b, self.status_code, self.content = body, status, b"x"

        def json(self):
            return self._b

    state = {"status": ["IN_PROGRESS", "FINISHED"], "used": 0, "cap": 50}

    def fake_get(url, **kw):
        calls.append(("GET", url, kw.get("params")))
        if url.endswith("content_publishing_limit"):
            return FakeResp({"data": [{"quota_usage": state["used"],
                                       "config": {"quota_total": state["cap"]}}]})
        nxt = state["status"].pop(0) if len(state["status"]) > 1 else state["status"][0]
        return FakeResp({"status_code": nxt, "status": "ok"})

    def fake_post(url, **kw):
        calls.append(("POST", url, kw.get("params")))
        if url.endswith("/media_publish"):
            return FakeResp({"id": "media99"})
        return FakeResp({"id": "cont1"})

    tmp = tempfile.mkdtemp()
    globals()["TOKEN_DIR"] = tmp
    globals()["PUBLIC_BASE"] = "https://cdn.example/clips"
    vid = os.path.join(tmp, "t1_vidA_100.mp4")
    open(vid, "wb").write(b"0" * 16)
    with open(token_path("leo"), "w", encoding="utf-8") as f:
        json.dump({"access_token": "tok", "ig_user_id": "179"}, f)

    assert available_accounts() == ["leo"], available_accounts()
    assert public_url_for(vid) == "https://cdn.example/clips/t1_vidA_100.mp4"

    real = requests.get, requests.post
    requests.get, requests.post = fake_get, fake_post
    try:
        res = upload(None, vid, {"description": "cuan #leogiovanni"}, account="leo",
                     sleep=lambda s: None)
        assert res["media_id"] == "media99", res
        assert res["url"].endswith("/reel/media99/")
        create = [c for c in calls if c[1].endswith("/media")][0][2]
        assert create["media_type"] == "REELS", create
        assert create["video_url"].startswith("https://cdn.example/clips/"), create
        assert create["caption"] == "cuan #leogiovanni"
        assert "access_token" in create                      # token on every call
        # it waited for FINISHED instead of publishing the raw container
        polls = [c for c in calls if c[0] == "GET" and "publishing_limit" not in c[1]]
        assert len(polls) == 2, polls

        # the account's own reported cap is what stops us, not a guess
        state["used"], state["cap"] = 50, 50
        try:
            upload(None, vid, {"description": "x"}, account="leo", sleep=lambda s: None)
            raise AssertionError("should have raised")
        except DailyLimitExceeded as e:
            assert "50/50" in str(e), e
        state["used"] = 0

        # a container that errors out is a failure, not something to publish
        state["status"] = ["ERROR"]
        try:
            upload(None, vid, {"description": "x"}, account="leo", sleep=lambda s: None)
            raise AssertionError("should have raised")
        except InstagramError as e:
            assert "ERROR" in str(e), e
        state["status"] = ["IN_PROGRESS", "FINISHED"]

        # throttling maps onto the exception the pipeline already defers on
        requests.post = lambda url, **kw: FakeResp({"error": {"code": 4, "message": "throttled"}})
        try:
            upload(None, vid, {"description": "x"}, account="leo", sleep=lambda s: None)
            raise AssertionError("should have raised")
        except DailyLimitExceeded:
            pass
    finally:
        requests.get, requests.post = real

    # without public hosting the upload cannot work, and says why
    globals()["PUBLIC_BASE"] = ""
    try:
        public_url_for(vid)
        raise AssertionError("should have raised")
    except InstagramError as e:
        assert "CLIPPER_PUBLIC_BASE" in str(e), e

    try:
        load_credentials("nobody")
        raise AssertionError("should have raised")
    except (FileNotFoundError, InstagramError):
        pass
    print("upload_instagram.py self-check OK")
