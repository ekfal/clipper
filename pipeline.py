"""Pipeline orchestrator (PRD §3) — hourly entry point.

crawl(): discover -> feasibility -> persist campaigns + footage tasks.
process(): advance queued tasks through the state machine:
    DISCOVERED -> DOWNLOADING -> TRANSCRIBING -> ANALYZING -> EDITING
    -> UPLOADING -> PUBLISHED   (FAILED from anywhere, segments freed by db.py)

Idempotent: every stage re-runs safely (fetch skips existing files, transcribe
reads its sidecar cache, segment allocator respects segment_usage).
"""
import json
import os
import traceback

import accounts
import bgm as bgm_lib
import db
import edit
import fetch
import metadata
import segments as selector
import transcribe
import upload_youtube
from clippo import ClippoAdapter, classify_source

_BASE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.environ.get("CLIPPO_SESSION", os.path.join(_BASE, "..", "clippo_session.json"))
OUT_DIR = os.environ.get("CLIPPER_OUT", os.path.join(_BASE, "out"))
CLIPS_PER_TASK = int(os.environ.get("CLIPPER_CLIPS_PER_TASK", "2"))
TASKS_PER_RUN = int(os.environ.get("CLIPPER_TASKS_PER_RUN", "1"))
PLATFORM = "youtube"  # slice-1 upload target

# Surfaces Clippo pays for. Its submission form takes only these two URLs, so a
# clip published anywhere else earns nothing from a campaign.
CAMPAIGN_PLATFORMS = ("tiktok", "instagram")
# Surfaces we can actually publish to right now (a working uploader). Widen as
# each platform's API clears its audit.
PUBLISH_PLATFORMS = tuple(
    p.strip() for p in os.environ.get("CLIPPER_PUBLISH_PLATFORMS", PLATFORM).split(",")
    if p.strip())


# Registry (PRD §3.0). Add adapters here, not in the loop.
def active_adapters():
    return [ClippoAdapter(os.path.abspath(SESSION))]


def crawl(conn):
    """One discovery pass across all active adapters."""
    n_campaigns = n_tasks = 0
    for adapter in active_adapters():
        ctx = adapter.authenticate()
        try:
            for task in adapter.discover_tasks(ctx):
                if not adapter.check_feasibility(task):
                    continue
                db.upsert_campaign(conn, task)
                n_campaigns += 1
                for url in task.footage_urls:
                    src = classify_source(url)
                    if src in ("youtube", "gdrive"):
                        if db.enqueue_footage(conn, task.campaign_id, url, src):
                            n_tasks += 1
        finally:
            adapter.close()
    return n_campaigns, n_tasks


def _requirements(conn, campaign_id):
    row = conn.execute("SELECT requirements_json FROM campaigns WHERE campaign_id=?",
                       (campaign_id,)).fetchone()
    return json.loads(row["requirements_json"]) if row and row["requirements_json"] else {}


def _destinations(conn, campaign_id, reqs):
    """Surfaces this clip will be published to, which is what sizes it.

    Derived, not chosen: the campaign brief says which platforms it wants, the
    accounts table says where we have an account cleared for campaign work, and
    only platforms with a working uploader are publishable. A human can pin the
    set per campaign from the dashboard when the rule gets it wrong.

    Paying surfaces are sized on their own. YouTube is a farming channel —
    Clippo cannot accept a YouTube URL — so letting its ceiling into the set
    would shorten a clip TikTok would have paid for, to suit a destination that
    pays nothing. It is only the destination when nothing paying is live, which
    is exactly slice-1 today.
    Returns (destinations, reason).
    """
    publishable = [p for p in PUBLISH_PLATFORMS if p in selector.DURATION_RANGES]
    ready = accounts.ready_platforms(conn)

    pinned = db.campaign_override(conn, campaign_id)
    if pinned:
        keep = [p for p in pinned if p in selector.DURATION_RANGES]
        if keep:
            return keep, "pinned in the dashboard"

    required = [p for p in (reqs.get("platforms_required") or [])
                if p in selector.DURATION_RANGES]
    paying = [p for p in publishable
              if p in CAMPAIGN_PLATFORMS and p in required and p in ready]
    if paying:
        return paying, "campaign platforms with a ready account"

    farming = [p for p in publishable if p not in CAMPAIGN_PLATFORMS]
    if farming:
        why = "no paying platform live — farming channels only"
        if required:
            why += f" (campaign wants {'+'.join(required)})"
        return farming, why
    return list(publishable), "fallback: no ready account on any destination"


def _wants_clean_mode(reqs):
    """PRD §3.6: campaign brief forbidding visual additions forces clean mode."""
    brief = (reqs.get("brief") or "").lower()
    banned = ("jangan tambahkan elemen", "jangan menambahkan elemen",
              "tanpa modifikasi visual", "jangan diedit berlebihan",
              "no additional element")
    return any(b in brief for b in banned)


def process_task(conn, task, dry_run=False):
    """Run one task through download -> ... -> published. Raises on failure.

    dry_run stops after EDITING: the clips are rendered and left on disk, and
    nothing is published, claimed or recorded. It is the way to exercise the
    whole chain on a real campaign — Clippo, yt-dlp, whisper, 9Router, ffmpeg —
    without spending a YouTube upload or a campaign submission slot on a test.
    Because it writes no clips and no segment_usage rows, the same segments are
    still free for the real run afterwards.
    """
    task_id = task["task_id"]
    reqs = _requirements(conn, task["campaign_id"])
    os.makedirs(OUT_DIR, exist_ok=True)

    db.set_task_status(conn, task_id, "DOWNLOADING")
    videos = fetch.fetch(task["footage_source_type"], task["footage_url"], task_id)
    if not videos:
        raise RuntimeError("no video files fetched")
    video_path = max(videos, key=os.path.getsize)  # main footage = biggest file
    video_id = os.path.splitext(os.path.basename(video_path))[0]

    db.set_task_status(conn, task_id, "TRANSCRIBING")
    words, info = transcribe.transcribe(video_path)
    if not words:
        raise RuntimeError("empty transcript")

    db.set_task_status(conn, task_id, "ANALYZING")
    rows = conn.execute(
        "SELECT start_ts, end_ts FROM segment_usage WHERE video_id=? AND platform=?",
        (video_id, PLATFORM)).fetchall()
    used = [(r["start_ts"], r["end_ts"]) for r in rows]
    dests, why = _destinations(conn, task["campaign_id"], reqs)
    window = selector.duration_window(dests)
    if window is None:
        raise RuntimeError(
            f"no clip length fits every destination {dests} — "
            f"windows {[selector.DURATION_RANGES[d] for d in dests]}")
    print(f"  destinations {'+'.join(dests)} ({why}) -> {window[0]}-{window[1]}s")
    # topic-aware cuts first: a clip should end when its topic ends
    picks = selector.pick_topical_segments(
        words, dests, CLIPS_PER_TASK, existing=used,
        video_duration=info["duration"])
    if not picks:
        heatmap = fetch.heatmap_for(video_path)
        picks = [{"start": s, "end": e, "hook": None, "topic": ""}
                 for s, e in selector.pick_segments(
                     info["duration"], heatmap, words, dests,
                     CLIPS_PER_TASK, existing=used)]
    if not picks:
        raise RuntimeError("no viable segments (all used or too little speech)")

    db.set_task_status(conn, task_id, "EDITING")
    # slice-1 renders reference style everywhere; a brief that bans visual
    # additions also disables BGM (PRD §3.6 compliance scan)
    allow_fx = not _wants_clean_mode(reqs)
    rendered = []  # (clip_id, path, meta, account)
    used_tracks = []  # sibling clips of this task should not share a track
    for pick in picks:
        start, end = pick["start"], pick["end"]
        seg_words = selector.words_in(words, start, end)
        seg_text = " ".join(w["word"] for w in seg_words)
        meta = metadata.generate(seg_text, reqs, platform=PLATFORM)
        # the topical pass saw the whole video, so its hook knows the context
        # this segment was cut from; metadata only ever sees the segment
        if pick.get("hook"):
            meta["hook"] = pick["hook"]
        out_path = os.path.join(OUT_DIR, f"t{task_id}_{video_id}_{int(start)}.mp4")

        # BGM by mood. The key is the clip's identity, so a re-render after a
        # retry lands on the same track (PRD §5) instead of a random one.
        track = None
        if allow_fx:
            track, why = bgm_lib.pick(meta.get("mood"),
                                      key=f"{video_id}:{int(start)}",
                                      exclude=used_tracks)
            print(f"  clip {int(start)}s -> bgm {why}")
            if track:
                used_tracks.append(track["path"])
                # royalty-free is not attribution-free; when the manifest names
                # a credit line it has to reach the published description
                if track["attribution"] and track["attribution"] not in meta["description"]:
                    meta["description"] += f"\n\n{track['attribution']}"

        edit.render_clip(video_path, start, end, seg_words, out_path,
                         hook=meta["hook"], split_screen=False,
                         bgm=track["path"] if track else False,
                         accent_words=meta.get("punchline_words") or ())
        clip_id = None
        if not dry_run:
            cur = conn.execute(
                """INSERT INTO clips (task_id, start_ts, end_ts, platform, video_id, status)
                   VALUES (?,?,?,?,?,?)""",
                (task_id, start, end, PLATFORM, video_id, "EDITED"))
            conn.execute("INSERT INTO segment_usage VALUES (?,?,?,?)",
                         (video_id, start, end, PLATFORM))
            conn.commit()
            clip_id = cur.lastrowid
        # route each clip to the channel whose category fits its content
        try:
            account, why = upload_youtube.detect_category(
                seg_text, title=meta["title"], default=upload_youtube.ACCOUNT)
        except RuntimeError as e:
            if not dry_run:
                raise
            account, why = None, f"skipped ({e})"   # no tokens on a test box
        print(f"  clip {int(start)}s -> account {account} ({why})")
        rendered.append((clip_id, out_path, meta, account))

    if dry_run:
        db.set_task_status(conn, task_id, "DISCOVERED")   # nothing was consumed
        print(f"  DRY RUN: {len(rendered)} clip(s) left in {OUT_DIR}, nothing "
              f"uploaded and no segments claimed")
        for _, path, meta, _ in rendered:
            print(f"    {path}  ({os.path.getsize(path) // 1024} KB)")
            print(f"      title: {meta['title']}")
        return rendered

    db.set_task_status(conn, task_id, "UPLOADING")
    for clip_id, path, meta, account in rendered:
        try:
            res = upload_youtube.upload(conn, path, meta, account=account)
        except upload_youtube.DailyLimitExceeded:
            conn.execute("UPDATE clips SET status='EDITED' WHERE clip_id=?", (clip_id,))
            conn.commit()
            raise
        conn.execute(
            "UPDATE clips SET published_url=?, status='PUBLISHED' WHERE clip_id=?",
            (res["url"], clip_id))
        conn.commit()
        os.remove(path)  # PRD §3.9: delete only after confirmed upload

    db.set_task_status(conn, task_id, "PUBLISHED")


def process(conn, limit=TASKS_PER_RUN, dry_run=False):
    """Advance up to `limit` queued tasks. Returns (done, failed)."""
    done = failed = 0
    rows = db.tasks_by_status(conn, "DISCOVERED")[:limit]
    for task in rows:
        try:
            process_task(conn, task, dry_run=dry_run)
            done += 1
        except upload_youtube.DailyLimitExceeded:
            db.set_task_status(conn, task["task_id"], "DISCOVERED",
                               "daily upload limit; retry next run")
            break
        except Exception as e:
            traceback.print_exc()
            db.set_task_status(conn, task["task_id"], "FAILED", str(e)[:500])
            failed += 1
    return done, failed


def eligible_clips(conn, min_views=None):
    """Published clips past the view threshold that were never submitted."""
    from clippo import MIN_VIEWS
    threshold = MIN_VIEWS if min_views is None else min_views
    return conn.execute(
        """SELECT c.clip_id, c.published_url, c.views_last_checked, t.campaign_id
             FROM clips c JOIN tasks t ON t.task_id = c.task_id
            WHERE c.status = 'PUBLISHED'
              AND c.published_url IS NOT NULL
              AND COALESCE(c.views_last_checked, 0) >= ?
         ORDER BY t.campaign_id, c.views_last_checked DESC""",
        (threshold,)).fetchall()


def submit_eligible(conn, adapter=None, dry_run=False):
    """Group eligible clips by campaign and submit them in 10-slot batches.

    Clips past the threshold but beyond a campaign's ten slots stay eligible
    and go out on the next cycle (PRD §3.11) — nothing is dropped.
    Returns a list of per-batch results.
    """
    from clippo import MAX_SLOTS

    rows = eligible_clips(conn)
    by_campaign = {}
    for r in rows:
        by_campaign.setdefault(r["campaign_id"], []).append(r)
    if not by_campaign:
        return []

    adapter = adapter or active_adapters()[0]
    ctx = adapter.authenticate()
    results = []
    try:
        for campaign_id, clips in by_campaign.items():
            batch = clips[:MAX_SLOTS]          # one batch per run per campaign
            deferred = len(clips) - len(batch)
            urls = [c["published_url"] for c in batch]
            res = adapter.submit_clip(ctx, campaign_id, urls, dry_run=dry_run)
            if not dry_run and res.get("submitted"):
                ids = [c["clip_id"] for c in batch]
                conn.execute(
                    """INSERT INTO submissions
                         (campaign_id, clip_ids, submitted_at, status)
                       VALUES (?,?,?,'submitted')""",
                    (campaign_id, json.dumps(ids), db._now()))
                conn.executemany("UPDATE clips SET status='SUBMITTED' WHERE clip_id=?",
                                 [(i,) for i in ids])
                conn.commit()
            res["deferred"] = deferred
            results.append(res)
    finally:
        adapter.close()
    return results


if __name__ == "__main__":
    import sys

    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        print("usage: python pipeline.py [--crawl-only | --process-only] [--dry-run]\n"
              "  --dry-run   render the clips and stop: nothing is uploaded, no\n"
              "              segment is claimed, the task stays queued. Use this\n"
              "              to test the whole chain end to end on a real campaign.")
        sys.exit(0)

    dry = "--dry-run" in sys.argv
    conn = db.init_db()
    if "--crawl-only" in sys.argv:
        print("crawl:", crawl(conn))
    elif "--process-only" in sys.argv:
        print("process:", process(conn, dry_run=dry))
    else:
        print("crawl:", crawl(conn))
        print("process:", process(conn, dry_run=dry))
