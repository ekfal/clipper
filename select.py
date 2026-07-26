"""Segment selection + allocation (PRD §3.4 + §3.5).

Priority 1: YouTube heatmap ("Most Replayed") — real viewer behavior.
Fallback: even distribution across the video (old transcribe.py pattern).
9Router LLM scoring plugs in at M4 for heatmap-less footage (Drive files).

Allocation rules (§3.5): non-overlapping, >=30s gap between segments, never
reuse a (video_id, timestamp) already in segment_usage for that platform.
End timestamps snap to the last word boundary inside the duration cap so cuts
land at natural speech ends, not mid-word.
"""
import random

MIN_GAP = 30.0  # §3.5: minimum seconds between allocated segments

# §3.5 duration ranges per platform
DURATION_RANGES = {
    "youtube": (30, 60),     # Shorts hard cap 60s
    "tiktok": (60, 180),
    "instagram": (60, 90),
}


def _overlaps(start, dur, taken, gap=MIN_GAP):
    return any(start < t_end + gap and t_start < start + dur + gap
               for t_start, t_end in taken)


def _snap_end(words, start, max_end):
    """Latest word end within (start, max_end]; None if no speech in range."""
    ends = [w["end"] for w in words if start < w["end"] <= max_end]
    return max(ends) if ends else None


def pick_segments(video_duration, heatmap, words, platform, count,
                  existing=(), min_words=15):
    """Return up to `count` (start, end) segments for `platform`.

    heatmap: [{start_time, value}] or None. words: full transcript word list.
    existing: (start, end) pairs already used for this video+platform
    (from segment_usage) — treated as taken.
    """
    lo, hi = DURATION_RANGES[platform]
    taken = [tuple(e) for e in existing]
    out = []

    candidates = []
    if heatmap:
        candidates = [p["start_time"] for p in
                      sorted(heatmap, key=lambda x: x.get("value", 0), reverse=True)]
    else:
        # even distribution fallback; slight jitter so retries differ
        n = max(count * 2, 4)
        step = video_duration / n
        candidates = [i * step + random.uniform(0, step * 0.3) for i in range(n)]

    for st in candidates:
        if len(out) >= count:
            break
        # snap-to-word always shortens the segment, so leave headroom above
        # the platform minimum or boundary snapping rejects every lo-length pick
        dur = random.randint(min(lo + 5, hi), hi)
        if st + lo > video_duration:
            continue
        end = _snap_end(words, st, min(st + dur, video_duration))
        if end is None or end - st < lo:
            continue
        seg_words = [w for w in words if st <= w["start"] < end]
        if len(seg_words) < min_words:
            continue
        if _overlaps(st, end - st, taken):
            continue
        out.append((st, end))
        taken.append((st, end))
    return out


def words_in(words, start, end):
    return [w for w in words if start <= w["start"] < end]


if __name__ == "__main__":
    # Self-check: synthetic 600s video, dense words, fake heatmap.
    random.seed(42)  # deterministic durations
    words = [{"word": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(1200)]
    heat = [{"start_time": t, "value": v} for t, v in
            [(100, 0.9), (300, 0.8), (105, 0.7), (500, 0.6)]]
    segs = pick_segments(600, heat, words, "youtube", 3)
    assert len(segs) == 3, segs
    for s, e in segs:
        assert 30 <= e - s <= 60.5, (s, e)
    # 105 must be rejected (overlaps/gap-conflicts with 100)
    starts = [s for s, _ in segs]
    assert 100 in starts and 105 not in starts, starts
    # existing usage blocks reallocation
    segs2 = pick_segments(600, heat, words, "youtube", 4, existing=segs)
    assert all(s not in starts for s, _ in segs2), segs2
    # end snaps to a word boundary
    all_ends = {round(w["end"], 3) for w in words}
    assert all(round(e, 3) in all_ends for _, e in segs)
    # no-heatmap fallback still allocates
    assert len(pick_segments(600, None, words, "youtube", 3)) == 3
    print("select.py self-check OK")
