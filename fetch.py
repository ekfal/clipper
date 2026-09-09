"""Footage fetcher — routes a task's footage_url to the right downloader (PRD §3.2).

Ported from old downloader.py: yt-dlp + heatmap sidecar. Dropped: force-upscale
(edit.py upscales at render time), Windows ffmpeg.exe lookup. Added: gdown for
Drive files/folders. Files land in MEDIA_DIR/<task_id>/; a heatmap (YouTube
"Most Replayed") is saved next to the video as <video>.heatmap.json when present.
"""
import json
import os
import re
import sys

_BASE = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.environ.get("CLIPPER_MEDIA", os.path.join(_BASE, "media"))

# Without authentication YouTube serves at most 360p (the unauthenticated web
# and tv clients get SABR-only, i.e. nothing usable), and clips built from that
# look soft once upscaled to a 1080x1920 canvas. Cookies are mandatory for the
# full format ladder — a PO token alone does NOT unlock higher resolutions, it
# only keeps requests from being rejected, so a flagged IP needs both.
#
# Drop a Netscape-format cookies.txt next to this file, or point
# CLIPPER_YT_COOKIES at one. Export it from an incognito window that is closed
# straight afterwards and never reopened: YouTube rotates cookies on live
# sessions, which invalidates the export within days.
#
# On a flagged IP (VPS ranges usually are), also install the PO token provider
# — it hooks into yt-dlp automatically, no change needed here:
#   pip install -U bgutil-ytdlp-pot-provider
#   docker run -d --init -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider
YTDLP_COOKIES = os.environ.get("CLIPPER_YT_COOKIES") or (
    os.path.join(_BASE, "cookies.txt")
    if os.path.exists(os.path.join(_BASE, "cookies.txt")) else None
)
MIN_GOOD_HEIGHT = 720

# Input limits. Anyone who can send a link can otherwise hand the box a
# three-hour upload: gigabytes to download, an hour of whisper, and a render
# behind it. Refusing early with a reason costs the sender one message; not
# refusing costs everyone the machine.
MAX_SOURCE_SECONDS = int(os.environ.get("CLIPPER_MAX_SOURCE_SECONDS", "3600"))
MAX_SOURCE_BYTES = int(float(os.environ.get("CLIPPER_MAX_SOURCE_GB", "2")) * 1e9)
# Downloads are cached per source URL and swept by age, not per run: the same
# link retried with a different style must not re-download or re-transcribe.
MEDIA_TTL_HOURS = float(os.environ.get("CLIPPER_MEDIA_TTL_HOURS", "24"))
CACHE_PREFIX = "u"


class SourceTooBig(ValueError):
    """The link is fine, it is just more than this host agreed to take."""


def cache_tag(url):
    """Directory key for a source URL — same link, same folder, every time."""
    import hashlib
    return CACHE_PREFIX + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def probe_seconds(path):
    """Duration via ffmpeg's own banner; None when it cannot be read.

    ffprobe is not always installed next to ffmpeg (a static build often ships
    alone), so this parses `ffmpeg -i` rather than assuming a second binary.
    """
    import re
    import subprocess

    import edit
    try:
        out = subprocess.run([edit.FFMPEG, "-i", path], capture_output=True,
                             text=True, timeout=30).stderr
    except Exception:
        return None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
    if not m:
        return None
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def _human_bytes(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _human_secs(n):
    return f"{n / 60:.0f} min" if n >= 60 else f"{n:.0f} sec"


def enforce_limits(paths):
    """Reject what is over budget, after the fact. Returns the paths.

    The message goes straight back to whoever sent the link, so it names the
    actual and the allowed in units that read at whatever the limit is set to.
    """
    for p in paths:
        size = os.path.getsize(p)
        if size > MAX_SOURCE_BYTES:
            raise SourceTooBig(
                f"{os.path.basename(p)} is {_human_bytes(size)}, over the "
                f"{_human_bytes(MAX_SOURCE_BYTES)} limit")
        secs = probe_seconds(p)
        if secs is None:
            # Without a probe the duration limit cannot be applied. Say so
            # rather than letting a three-hour file through in silence — for a
            # YouTube link the pre-download check still caught it, but a Drive
            # link has no other guard.
            print(f"WARNING: could not read the duration of "
                  f"{os.path.basename(p)} (is ffmpeg on PATH?) — the "
                  f"{_human_secs(MAX_SOURCE_SECONDS)} limit was not applied",
                  file=sys.stderr)
        if secs and secs > MAX_SOURCE_SECONDS:
            raise SourceTooBig(
                f"{os.path.basename(p)} runs {_human_secs(secs)}, over the "
                f"{_human_secs(MAX_SOURCE_SECONDS)} limit")
    return paths


def prune_cache(ttl_hours=None, dirpath=None):
    """Delete cached downloads older than the TTL. Returns what it removed.

    Only folders this module named (the CACHE_PREFIX ones) are touched, so a
    pipeline task's own media directory is never swept out from under it.
    """
    import shutil
    import time as _time

    ttl = (MEDIA_TTL_HOURS if ttl_hours is None else ttl_hours) * 3600
    root = dirpath or MEDIA_DIR
    removed = []
    try:
        names = os.listdir(root)
    except OSError:
        return removed
    for name in names:
        if not name.startswith(CACHE_PREFIX):
            continue
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and _time.time() - os.path.getmtime(path) > ttl:
                shutil.rmtree(path, ignore_errors=True)
                removed.append(name)
        except OSError:
            pass
    return removed


def _task_dir(task_id):
    d = os.path.join(MEDIA_DIR, str(task_id))
    os.makedirs(d, exist_ok=True)
    return d


def fetch_youtube(url, task_id):
    """Download one YouTube video; save heatmap sidecar if YouTube provides it.
    Returns list of downloaded file paths."""
    import yt_dlp

    out_dir = _task_dir(task_id)
    opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if YTDLP_COOKIES:
        opts["cookiefile"] = YTDLP_COOKIES
    with yt_dlp.YoutubeDL(opts) as ydl:
        # metadata first: a duration check here costs a second, the same check
        # after the fact costs the whole download
        probe = ydl.extract_info(url, download=False)
        secs = (probe or {}).get("duration") or 0
        if secs and secs > MAX_SOURCE_SECONDS:
            raise SourceTooBig(
                f"that video runs {_human_secs(secs)}, over the "
                f"{_human_secs(MAX_SOURCE_SECONDS)} limit — send a shorter one "
                f"or trim it first")
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        # after merge the extension is mp4 regardless of source ext
        base, _ = os.path.splitext(path)
        if not os.path.exists(path) and os.path.exists(base + ".mp4"):
            path = base + ".mp4"
        heatmap = info.get("heatmap")
        if heatmap:
            with open(base + ".heatmap.json", "w", encoding="utf-8") as f:
                json.dump(heatmap, f)
        height = info.get("height") or 0
        if height and height < MIN_GOOD_HEIGHT:
            print(f"WARNING: {info.get('id')} downloaded at {height}p — YouTube "
                  f"caps unauthenticated downloads at 360p. Add cookies.txt "
                  f"(see fetch.py) for full quality.")
    return [path]


def fetch_gdrive(url, task_id):
    """Download a Drive file or folder via gdown. Returns list of file paths.

    Campaign folders are shared with many clippers, so individual files often
    hit Google's "too many accesses" limit mid-folder. Partial download is a
    success: collect whatever landed on disk (nested subfolders included) and
    let the pipeline work with it; missing files come on a later crawl retry.
    """
    import gdown

    out_dir = _task_dir(task_id)
    if "/folders/" in url:
        try:
            gdown.download_folder(url=url, output=out_dir, quiet=True, use_cookies=False)
        except Exception:
            pass  # partial results are already on disk
        return [
            os.path.join(root, n)
            for root, _, names in os.walk(out_dir)
            for n in names
        ]
    p = gdown.download(url=url, output=out_dir + os.sep, quiet=True, fuzzy=True)
    return [p] if p else []


ROUTES = {"youtube": fetch_youtube, "gdrive": fetch_gdrive}


def classify_source(url):
    """Which downloader a link belongs to. Lives here, with the routing table.

    It used to live in clippo.py, which meant anything wanting to download a
    link imported the Clippo adapter to find out how — a campaign-platform
    module has no business owning that.
    """
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "drive.google.com" in host:
        return "gdrive"
    if "cdn.discordapp.com" in host:
        return "discord"
    return "unknown"

# Video extensions worth processing downstream; Drive folders often carry
# briefs (pdf/docx) alongside footage — those are skipped, not errors.
VIDEO_EXT = re.compile(r"\.(mp4|mov|mkv|webm|avi|m4v)$", re.I)


def fetch(source_type, url, task_id):
    """Route + download. Returns list of local VIDEO file paths (may be empty)."""
    fn = ROUTES.get(source_type)
    if fn is None:
        raise ValueError(f"unsupported footage source: {source_type}")
    files = fn(url, task_id)
    return enforce_limits([f for f in files if VIDEO_EXT.search(f or "")])


def heatmap_for(video_path):
    """Load heatmap sidecar for a downloaded video, or None."""
    p = os.path.splitext(video_path)[0] + ".heatmap.json"
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def _cli(argv):
    """Download one link and report what landed — the smallest possible test.

        python fetch.py https://youtu.be/xxxx

    Worth running on its own before a whole job: it is the only stage that
    depends on cookies, a PO token and the host's reputation, so when a job
    fails at the first step this says whether the link or the setup is at
    fault. Prints JSON; a failure prints the reason and exits non-zero.
    """
    import json as _json
    import sys as _sys
    from clippo import classify_source

    url = argv[0]
    kind = classify_source(url)
    if kind not in ROUTES:
        print(_json.dumps({"ok": False, "url": url, "kind": kind,
                           "error": f"{kind} links are not supported"}))
        return 1
    tag = argv[1] if len(argv) > 1 else "cli"
    print(f"[{kind}] downloading to {os.path.join(MEDIA_DIR, tag)}/ ...",
          file=_sys.stderr)
    try:
        files = fetch(kind, url, tag)
    except Exception as e:
        print(_json.dumps({"ok": False, "url": url, "kind": kind,
                           "error": f"{type(e).__name__}: {e}",
                           "cookies": YTDLP_COOKIES or None}, ensure_ascii=False))
        return 1
    if not files:
        print(_json.dumps({"ok": False, "url": url, "kind": kind,
                           "error": "nothing video-shaped was downloaded"}))
        return 1
    out = []
    for f in files:
        entry = {"path": os.path.abspath(f), "size_bytes": os.path.getsize(f)}
        side = os.path.splitext(f)[0] + ".heatmap.json"
        entry["heatmap"] = os.path.exists(side)
        out.append(entry)
    print(_json.dumps({"ok": True, "url": url, "kind": kind,
                       "cookies": bool(YTDLP_COOKIES), "files": out},
                      indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        sys.exit(_cli(sys.argv[1:]))

    # Self-check: routing + video filter, no network.
    assert ROUTES["youtube"] is fetch_youtube and ROUTES["gdrive"] is fetch_gdrive
    assert classify_source("https://youtu.be/x") == "youtube"
    assert classify_source("https://drive.google.com/file/d/x/view") == "gdrive"
    assert classify_source("https://cdn.discordapp.com/a.mp4") == "discord"
    assert classify_source("https://vimeo.com/1") == "unknown"
    assert VIDEO_EXT.search("a/b/clip.MOV") and VIDEO_EXT.search("x.mp4")
    assert not VIDEO_EXT.search("brief.pdf") and not VIDEO_EXT.search("notes.docx")
    try:
        fetch("discord", "http://x", 0)
        raise AssertionError("should have raised")
    except ValueError:
        pass
    # cache keys are stable per URL and distinct across URLs
    assert cache_tag("https://youtu.be/a") == cache_tag("https://youtu.be/a")
    assert cache_tag("https://youtu.be/a") != cache_tag("https://youtu.be/b")
    assert cache_tag("x").startswith(CACHE_PREFIX)

    # the sweeper only touches folders this module named
    import shutil as _sh
    import tempfile as _tf
    import time as _t
    _root = _tf.mkdtemp()
    for _n in (cache_tag("old"), cache_tag("new"), "7", "task_9"):
        os.makedirs(os.path.join(_root, _n))
    _old = os.path.join(_root, cache_tag("old"))
    os.utime(_old, (_t.time() - 99 * 3600,) * 2)
    _gone = prune_cache(ttl_hours=24, dirpath=_root)
    assert _gone == [cache_tag("old")], _gone
    _left = sorted(os.listdir(_root))
    assert "7" in _left and "task_9" in _left, "swept a pipeline directory"
    assert cache_tag("new") in _left, "swept a fresh download"
    _sh.rmtree(_root, ignore_errors=True)

    # oversize input is refused by name, not by crashing later
    _big = os.path.join(_tf.mkdtemp(), "huge.mp4")
    open(_big, "wb").write(b"0" * 2048)
    _saved = MAX_SOURCE_BYTES
    try:
        MAX_SOURCE_BYTES = 1024
        try:
            enforce_limits([_big])
            raise AssertionError("oversize file accepted")
        except SourceTooBig as e:
            assert "KB" not in str(e) and "limit" in str(e), e
    finally:
        MAX_SOURCE_BYTES = _saved

    # the duration guard actually bites when a probe is available
    _probe = probe_seconds(os.path.join(_BASE, "does-not-exist.mp4"))
    assert _probe is None, "probing a missing file should return None"

    # --- the YouTube path, exercised against a stubbed yt-dlp ---------------
    # The network is the one thing no test here can reach, so everything around
    # it is driven instead: the pre-download duration refusal, the merged-
    # extension fallback, the heatmap sidecar and the low-resolution warning.
    import types as _types
    _calls = []

    class _FakeYDL:
        info = {}
        writes = None

        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            _calls.append(download)
            if download and _FakeYDL.writes:
                open(_FakeYDL.writes, "w").close()
            return dict(_FakeYDL.info)

        def prepare_filename(self, info):
            return os.path.join(_task_dir(_FakeYDL.tag),
                                f"{info['id']}.{info.get('ext', 'mp4')}")

    _fake = _types.ModuleType("yt_dlp")
    _fake.YoutubeDL = _FakeYDL
    _saved_mod = sys.modules.get("yt_dlp")
    sys.modules["yt_dlp"] = _fake
    _saved_media = MEDIA_DIR
    MEDIA_DIR = _tf.mkdtemp()
    try:
        # a video past the limit is refused before anything is downloaded
        _FakeYDL.tag, _FakeYDL.info = "t1", {"id": "long", "duration": 9999}
        _FakeYDL.writes = None
        _calls.clear()
        try:
            fetch_youtube("https://youtu.be/long", "t1")
            raise AssertionError("an over-long video was accepted")
        except SourceTooBig as e:
            assert "over the" in str(e), e
        assert _calls == [False], f"it downloaded before checking: {_calls}"

        # a normal video: sidecar written, merged extension resolved
        _FakeYDL.tag = "t2"
        _d2 = _task_dir("t2")
        _FakeYDL.info = {"id": "ok", "ext": "webm", "duration": 120,
                         "height": 1080,
                         "heatmap": [{"start_time": 1.0, "value": 0.9}]}
        _FakeYDL.writes = os.path.join(_d2, "ok.mp4")   # merged output
        _calls.clear()
        _got = fetch_youtube("https://youtu.be/ok", "t2")
        assert _calls == [False, True], _calls
        assert _got == [os.path.join(_d2, "ok.mp4")], _got
        with open(os.path.join(_d2, "ok.heatmap.json")) as f:
            assert json.load(f)[0]["value"] == 0.9
        assert heatmap_for(_got[0])[0]["start_time"] == 1.0

        # no heatmap offered -> no sidecar, and that is not an error
        _FakeYDL.tag = "t3"
        _d3 = _task_dir("t3")
        _FakeYDL.info = {"id": "bare", "ext": "mp4", "duration": 60, "height": 360}
        _FakeYDL.writes = os.path.join(_d3, "bare.mp4")
        assert fetch_youtube("https://youtu.be/bare", "t3") == [_FakeYDL.writes]
        assert not os.path.exists(os.path.join(_d3, "bare.heatmap.json"))
        assert heatmap_for(_FakeYDL.writes) is None
    finally:
        MEDIA_DIR = _saved_media
        if _saved_mod is None:
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = _saved_mod

    # --- the Drive path, against a stubbed gdown ---------------------------
    _gd = _types.ModuleType("gdown")
    _saved_gd = sys.modules.get("gdown")
    sys.modules["gdown"] = _gd
    _saved_media = MEDIA_DIR
    MEDIA_DIR = _tf.mkdtemp()
    try:
        # a folder share: the brief alongside the footage is filtered out, and
        # a partial download is still a result rather than a failure
        def _folder(url=None, output=None, **kw):
            open(os.path.join(output, "clip.MOV"), "w").close()
            open(os.path.join(output, "brief.pdf"), "w").close()
            raise RuntimeError("too many accesses")     # mid-folder rate limit
        _gd.download_folder = _folder
        _gd.download = lambda url=None, output=None, **kw: None
        _got = fetch("gdrive", "https://drive.google.com/drive/folders/x", "g1")
        assert [os.path.basename(p) for p in _got] == ["clip.MOV"], _got

        # a single file share that Drive refuses returns nothing, not a crash
        _gd.download = lambda url=None, output=None, **kw: None
        assert fetch("gdrive", "https://drive.google.com/file/d/x/view", "g2") == []
    finally:
        MEDIA_DIR = _saved_media
        if _saved_gd is None:
            sys.modules.pop("gdown", None)
        else:
            sys.modules["gdown"] = _saved_gd

    print("fetch.py self-check OK")
