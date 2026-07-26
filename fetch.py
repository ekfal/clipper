"""Footage fetcher — routes a task's footage_url to the right downloader (PRD §3.2).

Ported from old downloader.py: yt-dlp + heatmap sidecar. Dropped: force-upscale
(edit.py upscales at render time), Windows ffmpeg.exe lookup. Added: gdown for
Drive files/folders. Files land in MEDIA_DIR/<task_id>/; a heatmap (YouTube
"Most Replayed") is saved next to the video as <video>.heatmap.json when present.
"""
import json
import os
import re

_BASE = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.environ.get("CLIPPER_MEDIA", os.path.join(_BASE, "media"))

# Without authentication YouTube only serves up to 360p, and clips built from
# that look soft once upscaled to a 1080x1920 canvas. Drop a Netscape-format
# cookies.txt next to this file (or point CLIPPER_YT_COOKIES at one) to get the
# full ladder — the approach the PRD validated on the VPS.
YTDLP_COOKIES = os.environ.get("CLIPPER_YT_COOKIES") or (
    os.path.join(_BASE, "cookies.txt")
    if os.path.exists(os.path.join(_BASE, "cookies.txt")) else None
)
MIN_GOOD_HEIGHT = 720


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

# Video extensions worth processing downstream; Drive folders often carry
# briefs (pdf/docx) alongside footage — those are skipped, not errors.
VIDEO_EXT = re.compile(r"\.(mp4|mov|mkv|webm|avi|m4v)$", re.I)


def fetch(source_type, url, task_id):
    """Route + download. Returns list of local VIDEO file paths (may be empty)."""
    fn = ROUTES.get(source_type)
    if fn is None:
        raise ValueError(f"unsupported footage source: {source_type}")
    files = fn(url, task_id)
    return [f for f in files if VIDEO_EXT.search(f or "")]


def heatmap_for(video_path):
    """Load heatmap sidecar for a downloaded video, or None."""
    p = os.path.splitext(video_path)[0] + ".heatmap.json"
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


if __name__ == "__main__":
    # Self-check: routing + video filter, no network.
    assert ROUTES["youtube"] is fetch_youtube and ROUTES["gdrive"] is fetch_gdrive
    assert VIDEO_EXT.search("a/b/clip.MOV") and VIDEO_EXT.search("x.mp4")
    assert not VIDEO_EXT.search("brief.pdf") and not VIDEO_EXT.search("notes.docx")
    try:
        fetch("discord", "http://x", 0)
        raise AssertionError("should have raised")
    except ValueError:
        pass
    print("fetch.py self-check OK")
