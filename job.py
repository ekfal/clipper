"""Agent-facing entry point: two links in, one clip out.

Built for an agent (Hermes) to drive over chat, so everything it needs is
machine readable and nothing needs a human at a terminal:

    python job.py --list                      # catalogue of styles and music
    python job.py --opening URL --content URL # render, print a JSON result

One job runs at a time per host, and a request arriving while another is in
flight exits 3 with {"busy": true} rather than queueing behind it.

Both commands print JSON on stdout and nothing else; progress goes to stderr.
A failure prints {"ok": false, "error": ...} and exits non-zero, so the caller
never has to parse a traceback to tell the user what went wrong. It also files
a diagnostic report (see report.py) and returns its path, so the failure can be
looked at later without shell access to the box.

This module deliberately stops at the file: posting to Discord, parsing the
chat message and holding the conversation belong to the agent, which knows its
own transport. What it gets from here is a stable contract.
"""
import argparse
import json
import os
import sys
import time
import traceback

_BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("CLIPPER_JOB_OUT", os.path.join(_BASE, "jobs"))
LOCK_PATH = os.environ.get("CLIPPER_JOB_LOCK", os.path.join(_BASE, ".job.lock"))
# Seconds to wait for a job already running. Zero means refuse immediately,
# which is the right answer over chat: a caller would rather be told to try
# again than watch a request hang.
LOCK_WAIT = float(os.environ.get("CLIPPER_JOB_LOCK_WAIT", "0"))


class Busy(RuntimeError):
    """Another job holds the machine."""


class _Lock:
    """One job at a time on this host.

    Whisper and ffmpeg each want most of a small VPS; two jobs in parallel do
    not run twice as fast, they run out of memory. The lock is advisory and
    per-host, held only for the duration of the run.
    """

    def __init__(self, path=LOCK_PATH, wait=LOCK_WAIT):
        self.path, self.wait, self.fh = path, wait, None

    def __enter__(self):
        import fcntl
        self.fh = open(self.path, "w")
        deadline = time.time() + self.wait
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fh.write(f"{os.getpid()} {time.time():.0f}\n")
                self.fh.flush()
                return self
            except OSError:
                if time.time() >= deadline:
                    self.fh.close()
                    self.fh = None
                    raise Busy(
                        "another clip is being rendered on this host — try "
                        "again in a minute")
                time.sleep(1.0)

    def __exit__(self, *exc):
        import fcntl
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None
        return False


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


# Catalogue: what an agent can ask for.

def catalogue():
    """Everything the caller may choose from, with a line of copy for each.

    The agent renders this straight into chat, so each entry carries the label
    a person should see rather than only the identifier the code wants.
    """
    import bgm
    import edit
    import segments

    tracks = bgm.load_tracks()
    return {
        "frame_mode": [
            {"id": "cover", "label": "Full frame",
             "desc": "Footage fills the whole 9:16 frame. Best for vertical "
                     "footage; crops the sides hard on landscape."},
            {"id": "fill", "label": "Band + blur",
             "desc": "Footage as a centred band over a blurred copy of itself. "
                     "A middle ground for landscape footage."},
            {"id": "fit", "label": "Whole frame, letterboxed",
             "desc": "Nothing is cropped. The subject ends up smallest."},
        ],
        "caption_style": [
            {"id": "phrase", "label": "Phrase captions",
             "desc": "Whole phrases in gold, two lines max, punchline tinted."},
            {"id": "karaoke", "label": "Word-by-word",
             "desc": "Per-word highlight as the speaker says it."},
        ],
        "hook_style": [
            {"id": "boxes", "label": "Pull quote",
             "desc": "Teal quote mark over stacked white boxes, ragged right."},
            {"id": "card", "label": "Single card",
             "desc": "One continuous white card, no quote mark."},
        ],
        "opening": [
            {"id": "broll", "label": "B-roll first",
             "desc": "Second link plays first with the hook over it, then cuts "
                     "to the content. Nothing of the speech is lost."},
            {"id": "direct", "label": "Straight in",
             "desc": "No opening clip. The content starts straight away with no "
                     "hook, unless you write hook text yourself, in which case "
                     "it rides over the first seconds and those carry no "
                     "subtitle."},
        ],
        "moods": list(bgm.MOODS),
        "music": [{"file": t["file"], "mood": t["mood"] or ["untagged"]}
                  for t in tracks],
        "duration_windows": {k: list(v) for k, v in segments.DURATION_RANGES.items()},
        "defaults": {
            "frame_mode": edit.FRAME_MODE,
            "caption_style": edit.CAPTION_STYLE,
            "hook_style": edit.HOOK_STYLE,
            "hook_seconds": edit.HOOK_DUR,
        },
        "accepted_links": ["youtube", "google drive"],
    }


# One job: two links in, one clip out.

def _fetch_one(url, tag):
    """Download one link to its own folder. Returns the biggest video file."""
    import fetch

    kind = fetch.classify_source(url)
    if kind not in fetch.ROUTES:
        raise ValueError(
            f"{tag}: {kind} links are not supported yet — send a YouTube or "
            f"Google Drive link")
    # Keyed by the URL, not by the run: two jobs never share a folder (a Drive
    # fetch walks the whole directory and would otherwise pick up the previous
    # job's file), and the same link retried reuses its download and its
    # cached transcript instead of paying for both again.
    slot = fetch.cache_tag(url)
    _log(f"[{tag}] downloading ({kind}) -> {slot}")
    try:
        files = fetch.fetch(kind, url, slot)
    except fetch.SourceTooBig as e:
        raise fetch.SourceTooBig(f"{tag}: {e}") from None
    if not files:
        raise RuntimeError(f"{tag}: nothing downloadable at that link")
    return max(files, key=os.path.getsize)



def _hook_for(asked, opening, topical, from_model):
    """Which hook text ends up on the clip, or None for no hook at all.

    A hook is drawn only when something asks for one: hook text the caller
    wrote, or an opening clip that needs text over it. With neither, the clip
    has no hook, rather than one the model wrote because it could.

    That is not only a style call. The seconds under a hook carry no subtitle,
    because nothing is allowed to share the screen with it, so a hook nobody
    asked for costs the opening line of speech.
    """
    if asked:
        return asked
    if opening:
        return topical or from_model
    return None


def _delivery_copy(path, max_mb):
    """Re-encode under max_mb when the render is too big to send. Returns the
    new path, or None when the original already fits or the squeeze fails.

    The render targets 6 Mbps because it is the file that gets uploaded to a
    platform, and a 90s clip at that rate is 66 MB. Discord takes 10 MB on a
    free account and 50 MB on Nitro Basic, so the thing a chat agent hands back
    has to be a smaller copy. The original is left alone: the delivery copy is
    for looking at, not for publishing.
    """
    import subprocess

    import edit

    if max_mb <= 0 or os.path.getsize(path) <= max_mb * 1024 * 1024:
        return None
    seconds = None
    try:
        import fetch
        seconds = fetch.probe_seconds(path)
    except Exception:
        pass
    if not seconds:
        return None
    # Leave headroom for the container and the audio track we are about to fix
    # at 96k; aiming at exactly the cap overshoots it often enough to matter.
    total_kbit = (max_mb * 8 * 1024) / seconds * 0.90
    video_kbit = max(300, int(total_kbit - 96))
    small = os.path.splitext(path)[0] + "_small.mp4"
    cmd = [edit.FFMPEG, "-y", "-v", "error", "-i", path,
           "-c:v", edit.CODEC, "-b:v", f"{video_kbit}k",
           "-maxrate", f"{int(video_kbit * 1.3)}k", "-bufsize", f"{video_kbit * 2}k",
           "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", small]
    if subprocess.run(cmd, capture_output=True, text=True).returncode != 0:
        return None
    if not os.path.exists(small):
        return None
    _log(f"delivery copy: {os.path.getsize(small) / 1048576:.1f} MB "
         f"(original {os.path.getsize(path) / 1048576:.1f} MB)")
    return small


def run(content_url, opening_url=None, hook=None, platform="youtube",
        start=None, seconds=None, mood=None, out=None, max_mb=0, **style):
    """Fetch, transcribe, pick a segment, render. Returns a result dict."""
    import bgm
    import edit
    import fetch
    import metadata
    import segments as selector
    import transcribe

    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = time.time()

    swept = fetch.prune_cache()
    if swept:
        _log(f"swept {len(swept)} cached download(s) past their TTL")

    content = _fetch_one(content_url, "content")
    opening = _fetch_one(opening_url, "opening") if opening_url else None

    _log("transcribing (cached beside the video)...")
    words, info = transcribe.transcribe(content)
    if not words:
        raise RuntimeError("no speech found in the content video")

    lo, hi = selector.duration_window(platform)
    if start is not None:
        seg_start = float(start)
        seg_end = seg_start + float(seconds or hi)
        seg_end = min(seg_end, info["duration"])
        topic_hook = None
    else:
        _log("choosing a segment...")
        picks = selector.pick_topical_segments(words, platform, 1,
                                               video_duration=info["duration"])
        if not picks:
            # Same ladder the pipeline uses: an unreachable router costs the
            # topic-aware cut, not the clip. Without this a 9Router hiccup
            # takes the whole chat flow down.
            _log("topical selection unavailable — falling back to heatmap")
            heat = fetch.heatmap_for(content)
            picks = [{"start": s, "end": e, "hook": None}
                     for s, e in selector.pick_segments(
                         info["duration"], heat, words, platform, 1)]
        if not picks:
            raise RuntimeError(
                f"could not find a self-contained {lo}-{hi}s segment — pass "
                f"--start to choose one by hand")
        seg_start, seg_end = picks[0]["start"], picks[0]["end"]
        topic_hook = picks[0].get("hook")

    seg_words = selector.words_in(words, seg_start, seg_end)
    seg_text = " ".join(w["word"] for w in seg_words)
    meta = metadata.generate(seg_text, {}, platform=platform)
    meta["hook"] = _hook_for(hook, opening, topic_hook, meta["hook"])

    track, why = bgm.pick(mood or meta.get("mood"), key=f"job:{int(seg_start)}")
    _log(f"bgm: {why}")

    out = out or os.path.join(
        OUT_DIR, f"clip_{int(time.time())}_{int(seg_start)}.mp4")
    _log(f"rendering {seg_end - seg_start:.0f}s...")
    edit.render_clip(content, seg_start, seg_end, seg_words, out,
                     hook=meta["hook"], bgm=track["path"] if track else False,
                     accent_words=meta.get("punchline_words") or (),
                     intro=opening, **style)

    small = _delivery_copy(out, max_mb)
    return {
        "ok": True,
        "file": os.path.abspath(out),
        "size_bytes": os.path.getsize(out),
        # Present only when the render was over --max-mb. Send this one; the
        # file above is the one that goes to a platform.
        "delivery_file": os.path.abspath(small) if small else None,
        "delivery_size_bytes": os.path.getsize(small) if small else None,
        "segment": [round(seg_start, 2), round(seg_end, 2)],
        "duration_sec": round(seg_end - seg_start, 2),
        "opening": bool(opening),
        "hook": meta["hook"],
        "title": meta["title"],
        "description": meta["description"],
        "mood": meta.get("mood"),
        "music": track["file"] if track else None,
        "attribution": (track or {}).get("attribution") or None,
        "elapsed_sec": round(time.time() - t0, 1),
    }


def _selftest():
    """Offline checks on the parts that are a contract, not an implementation."""
    # The hook rule the catalogue promises.
    assert _hook_for("Ditulis Tangan", None, "topical", "model") == "Ditulis Tangan"
    assert _hook_for("Ditulis Tangan", "/broll.mp4", "topical", "model") == "Ditulis Tangan"
    assert _hook_for(None, "/broll.mp4", "topical", "model") == "topical"
    assert _hook_for(None, "/broll.mp4", None, "model") == "model"
    # No opening and no text asked for: no hook, whatever the model wrote.
    assert _hook_for(None, None, "topical", "model") is None
    assert _hook_for(None, None, None, "model") is None
    assert _hook_for("", None, None, "model") is None

    # The catalogue is what an agent offers a user, so every id it advertises
    # has to be one the parser actually accepts.
    cat = catalogue()
    parser_choices = {
        "frame_mode": ("cover", "fill", "fit"),
        "caption_style": ("phrase", "karaoke"),
        "hook_style": ("boxes", "card"),
    }
    for key, allowed in parser_choices.items():
        ids = [o["id"] for o in cat[key]]
        assert set(ids) <= set(allowed), (key, ids, allowed)
    for key, value in cat["defaults"].items():
        if key in parser_choices:
            assert value in parser_choices[key], (key, value)

    # A delivery copy is only made when one is needed.
    assert _delivery_copy("/nonexistent.mp4", 0) is None

    print(json.dumps({"ok": True, "checked": "hook rule, catalogue, delivery cap"}))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="job.py", description=__doc__)
    p.add_argument("--selftest", action="store_true",
                   help="check the contract without touching the network")
    p.add_argument("--list", action="store_true",
                   help="print the catalogue of styles and music, then exit")
    p.add_argument("--content", help="link to the video the clip is cut from")
    p.add_argument("--opening", help="link to the b-roll shown first (optional)")
    p.add_argument("--hook", help="hook text; **bold** with double asterisks. "
                                  "Without this and without --opening the clip "
                                  "gets no hook at all")
    p.add_argument("--platform", default="youtube",
                   help="destination, sets the length window")
    p.add_argument("--start", type=float, help="cut from here instead of letting "
                                               "the model choose")
    p.add_argument("--seconds", type=float, help="clip length when --start is given")
    p.add_argument("--mood", help="override the music mood")
    p.add_argument("--out", help="output path")
    p.add_argument("--frame-mode", dest="frame_mode",
                   choices=("cover", "fill", "fit"))
    p.add_argument("--caption-style", dest="caption_style",
                   choices=("phrase", "karaoke"))
    p.add_argument("--hook-style", dest="hook_style", choices=("boxes", "card"))
    p.add_argument("--wait", type=float, default=None,
                   help="seconds to wait if another job holds the host")
    p.add_argument("--max-mb", dest="max_mb", type=float, default=0,
                   help="also write a smaller copy when the render exceeds "
                        "this, for a chat transport with an upload limit "
                        "(Discord: 10 free, 50 Nitro Basic)")
    a = p.parse_args(argv)

    if a.selftest:
        return _selftest()
    if a.list:
        print(json.dumps(catalogue(), indent=2, ensure_ascii=False))
        return 0
    if not a.content:
        print(json.dumps({"ok": False,
                          "error": "--content is required (or use --list)"}))
        return 2

    style = {k: v for k, v in
             (("frame_mode", a.frame_mode), ("caption_style", a.caption_style),
              ("hook_style", a.hook_style)) if v}
    try:
        with _Lock(wait=a.wait if a.wait is not None else LOCK_WAIT):
            res = run(a.content, a.opening, hook=a.hook, platform=a.platform,
                      start=a.start, seconds=a.seconds, mood=a.mood, out=a.out,
                      max_mb=a.max_mb, **style)
    except Busy as e:
        # not a failure of this job, so it gets no report and its own code —
        # the caller should retry rather than escalate
        print(json.dumps({"ok": False, "busy": True, "error": str(e)},
                         ensure_ascii=False))
        return 3
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        import report
        filed = report.capture(e, "job", context={
            "content": a.content, "opening": a.opening, "platform": a.platform,
            "start": a.start, "seconds": a.seconds, **style})
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        out.update(filed)
        print(json.dumps(out, ensure_ascii=False))
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
