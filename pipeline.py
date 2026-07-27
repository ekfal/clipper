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
PLATFORM = "youtube"  # slice-1


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


def _wants_clean_mode(reqs):
    """PRD §3.6: campaign brief forbidding visual additions forces clean mode."""
    brief = (reqs.get("brief") or "").lower()
    banned = ("jangan tambahkan elemen", "jangan menambahkan elemen",
              "tanpa modifikasi visual", "jangan diedit berlebihan",
              "no additional element")
    return any(b in brief for b in banned)


def process_task(conn, task):
    """Run one task through download -> ... -> published. Raises on failure."""
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
    # topic-aware cuts first: a clip should end when its topic ends
    picks = selector.pick_topical_segments(
        words, PLATFORM, CLIPS_PER_TASK, existing=used,
        video_duration=info["duration"])
    if not picks:
        heatmap = fetch.heatmap_for(video_path)
        picks = [{"start": s, "end": e, "hook": None, "topic": ""}
                 for s, e in selector.pick_segments(
                     info["duration"], heatmap, words, PLATFORM,
                     CLIPS_PER_TASK, existing=used)]
    if not picks:
        raise RuntimeError("no viable segments (all used or too little speech)")

    db.set_task_status(conn, task_id, "EDITING")
    # slice-1 renders reference style everywhere; a brief that bans visual
    # additions also disables BGM (PRD §3.6 compliance scan)
    allow_fx = not _wants_clean_mode(reqs)
    rendered = []  # (clip_id, path, meta)
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
        edit.render_clip(video_path, start, end, seg_words, out_path,
                         hook=meta["hook"], split_screen=False, bgm=allow_fx)
        cur = conn.execute(
            """INSERT INTO clips (task_id, start_ts, end_ts, platform, video_id, status)
               VALUES (?,?,?,?,?,?)""",
            (task_id, start, end, PLATFORM, video_id, "EDITED"))
        conn.execute("INSERT INTO segment_usage VALUES (?,?,?,?)",
                     (video_id, start, end, PLATFORM))
        conn.commit()
        # route each clip to the channel whose category fits its content
        account, why = upload_youtube.detect_category(
            seg_text, title=meta["title"], default=upload_youtube.ACCOUNT)
        print(f"  clip {int(start)}s -> account {account} ({why})")
        rendered.append((cur.lastrowid, out_path, meta, account))

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


def process(conn, limit=TASKS_PER_RUN):
    """Advance up to `limit` queued tasks. Returns (done, failed)."""
    done = failed = 0
    rows = db.tasks_by_status(conn, "DISCOVERED")[:limit]
    for task in rows:
        try:
            process_task(conn, task)
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


if __name__ == "__main__":
    import sys
    conn = db.init_db()
    if "--crawl-only" in sys.argv:
        print("crawl:", crawl(conn))
    elif "--process-only" in sys.argv:
        print("process:", process(conn))
    else:
        print("crawl:", crawl(conn))
        print("process:", process(conn))
