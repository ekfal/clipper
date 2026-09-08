"""Agent-facing entry point: two links in, one clip out.

Built for an agent (Hermes) to drive over chat, so everything it needs is
machine readable and nothing needs a human at a terminal:

    python job.py --list                      # catalogue of styles and music
    python job.py --opening URL --content URL # render, print a JSON result

Both commands print JSON on stdout and nothing else; progress goes to stderr.
A failure prints {"ok": false, "error": ...} and exits non-zero, so the caller
never has to parse a traceback to tell the user what went wrong.

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


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------- catalogue

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
             "desc": "Hook rides over the opening seconds of the content. Those "
                     "seconds carry no subtitle."},
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


# ------------------------------------------------------------------- job

def _fetch_one(url, tag):
    """Download one link to its own folder. Returns the biggest video file."""
    import fetch
    from clippo import classify_source

    kind = classify_source(url)
    if kind not in fetch.ROUTES:
        raise ValueError(
            f"{tag}: {kind} links are not supported yet — send a YouTube or "
            f"Google Drive link")
    _log(f"[{tag}] downloading ({kind})...")
    files = fetch.fetch(kind, url, f"job_{tag}")
    if not files:
        raise RuntimeError(f"{tag}: nothing downloadable at that link")
    return max(files, key=os.path.getsize)


def run(content_url, opening_url=None, hook=None, platform="youtube",
        start=None, seconds=None, mood=None, out=None, **style):
    """Fetch, transcribe, pick a segment, render. Returns a result dict."""
    import bgm
    import edit
    import metadata
    import segments as selector
    import transcribe

    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = time.time()

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
            raise RuntimeError(
                f"could not find a self-contained {lo}-{hi}s segment — pass "
                f"--start to choose one by hand")
        seg_start, seg_end = picks[0]["start"], picks[0]["end"]
        topic_hook = picks[0].get("hook")

    seg_words = selector.words_in(words, seg_start, seg_end)
    seg_text = " ".join(w["word"] for w in seg_words)
    meta = metadata.generate(seg_text, {}, platform=platform)
    if hook:
        meta["hook"] = hook
    elif topic_hook:
        meta["hook"] = topic_hook

    track, why = bgm.pick(mood or meta.get("mood"), key=f"job:{int(seg_start)}")
    _log(f"bgm: {why}")

    out = out or os.path.join(
        OUT_DIR, f"clip_{int(time.time())}_{int(seg_start)}.mp4")
    _log(f"rendering {seg_end - seg_start:.0f}s...")
    edit.render_clip(content, seg_start, seg_end, seg_words, out,
                     hook=meta["hook"], bgm=track["path"] if track else False,
                     accent_words=meta.get("punchline_words") or (),
                     intro=opening, **style)

    return {
        "ok": True,
        "file": os.path.abspath(out),
        "size_bytes": os.path.getsize(out),
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


def main(argv=None):
    p = argparse.ArgumentParser(prog="job.py", description=__doc__)
    p.add_argument("--list", action="store_true",
                   help="print the catalogue of styles and music, then exit")
    p.add_argument("--content", help="link to the video the clip is cut from")
    p.add_argument("--opening", help="link to the b-roll shown first (optional)")
    p.add_argument("--hook", help="hook text; **bold** with double asterisks")
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
    a = p.parse_args(argv)

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
        res = run(a.content, a.opening, hook=a.hook, platform=a.platform,
                  start=a.start, seconds=a.seconds, mood=a.mood, out=a.out,
                  **style)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False))
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
