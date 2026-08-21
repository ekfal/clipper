"""Platform registry — one entry point for publishing a clip anywhere.

Same shape as fetch.ROUTES: a dict of platform -> backend, so adding a
platform is a dict entry rather than a branch in the pipeline. Every backend
exposes the same three names:

    upload(conn, video_path, meta, account=None) -> dict with "url"
    available_accounts() -> [str]
    DailyLimitExceeded                            (deferral, not failure)

Backends are imported lazily. Instagram's module is useless without a public
HTTPS base and TikTok's without an approved app, and neither should be able to
stop a YouTube-only run from starting.
"""
import importlib

# platform -> module name. The pipeline publishes to youtube today; the other
# two are wired and waiting on platform approval, not on code.
BACKENDS = {
    "youtube": "upload_youtube",
    "tiktok": "upload_tiktok",
    "instagram": "upload_instagram",
}


def backend(platform):
    """Import and return one platform's module."""
    try:
        name = BACKENDS[platform]
    except KeyError:
        raise ValueError(f"unsupported platform: {platform}") from None
    return importlib.import_module(name)


def deferrals():
    """Every backend's 'try again later' exception, as one tuple for except.

    A platform whose module will not import cannot defer anything, so it is
    skipped rather than breaking the tuple for the platforms that do work.
    """
    out = []
    for platform in BACKENDS:
        try:
            out.append(backend(platform).DailyLimitExceeded)
        except Exception:
            continue
    return tuple(out)


def available_accounts(platform):
    return backend(platform).available_accounts()


def upload(conn, platform, video_path, meta, account=None, **kw):
    """Publish one clip. Returns the backend's result, always carrying "url"."""
    res = backend(platform).upload(conn, video_path, meta, account=account, **kw)
    if not res.get("url"):
        raise RuntimeError(f"{platform} upload returned no url: {res}")
    return res


def ready(platform):
    """(bool, reason) — whether this platform could publish right now.

    Reported by the dashboard and worth checking before a campaign is routed
    somewhere it cannot actually reach.
    """
    # A half-wired backend must report why, not raise — this is the call a
    # dashboard makes to render status, and an exception there hides the answer.
    try:
        mod = backend(platform)
        accounts = mod.available_accounts()
        if not accounts:
            return False, f"no credentials in {mod.TOKEN_DIR}"
        if platform == "tiktok" and not mod.DIRECT_OK:
            return True, f"{len(accounts)} account(s), draft only — audit not approved"
        if platform == "instagram" and not mod.PUBLIC_BASE:
            return False, "CLIPPER_PUBLIC_BASE unset — Meta fetches the file itself"
        return True, f"{len(accounts)} account(s)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


if __name__ == "__main__":
    # Self-check: registry wiring only, every backend stubbed where it touches
    # the network.
    import sys
    import types

    assert set(BACKENDS) == {"youtube", "tiktok", "instagram"}
    try:
        backend("snapchat")
        raise AssertionError("should have raised")
    except ValueError as e:
        assert "unsupported platform" in str(e)

    import upload_instagram
    import upload_tiktok
    import upload_youtube
    assert backend("tiktok") is upload_tiktok

    # every backend keeps the shared contract
    for platform in BACKENDS:
        mod = backend(platform)
        for name in ("upload", "available_accounts", "DailyLimitExceeded"):
            assert hasattr(mod, name), f"{platform} is missing {name}"

    d = deferrals()
    assert upload_youtube.DailyLimitExceeded in d and upload_tiktok.DailyLimitExceeded in d
    assert len(d) == 3, d

    # a backend that cannot import must not take the others down with it
    broken = types.ModuleType("broken_backend")
    sys.modules["broken_backend"] = broken          # no DailyLimitExceeded on it
    BACKENDS["broken"] = "broken_backend"
    try:
        assert len(deferrals()) == 3, "a broken backend leaked into the tuple"
        ok, why = ready("broken")
        assert ok is False and "no attribute" in why.lower(), why
    finally:
        del BACKENDS["broken"]

    # upload() insists on a url, because that is what the clips row stores
    fake = types.ModuleType("fake_backend")
    fake.DailyLimitExceeded = RuntimeError
    fake.available_accounts = lambda: ["a"]
    fake.TOKEN_DIR = "/tmp"
    fake.upload = lambda conn, path, meta, account=None: {"account": account}
    sys.modules["fake_backend"] = fake
    BACKENDS["fake"] = "fake_backend"
    try:
        try:
            upload(None, "fake", "x.mp4", {}, account="a")
            raise AssertionError("should have raised")
        except RuntimeError as e:
            assert "no url" in str(e), e
        fake.upload = lambda conn, path, meta, account=None: {"url": "https://x/1"}
        assert upload(None, "fake", "x.mp4", {}, account="a")["url"] == "https://x/1"
    finally:
        del BACKENDS["fake"]

    # readiness reports the real blocker per platform
    globals()["_saved"] = (upload_instagram.PUBLIC_BASE, upload_tiktok.DIRECT_OK)
    upload_instagram.available_accounts = lambda: ["leo"]
    upload_instagram.PUBLIC_BASE = ""
    ok, why = ready("instagram")
    assert ok is False and "PUBLIC_BASE" in why, why
    upload_instagram.PUBLIC_BASE = "https://cdn/x"
    assert ready("instagram")[0] is True

    upload_tiktok.available_accounts = lambda: ["leo"]
    upload_tiktok.DIRECT_OK = False
    ok, why = ready("tiktok")
    assert ok is True and "draft only" in why, why

    upload_youtube.available_accounts = lambda: []
    ok, why = ready("youtube")
    assert ok is False and "no credentials" in why, why
    print("uploaders.py self-check OK")
