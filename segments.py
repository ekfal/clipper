"""Segment selection + allocation (PRD §3.4 + §3.5).

Priority 1: topical selection — the LLM reads the timestamped transcript and
returns segments that open when a topic starts and close when that same topic
is finished, so a clip is one complete thought rather than a fixed-length cut.
Duration follows the topic (inside the platform range), it is not sampled.

Fallback (LLM unavailable or nothing usable): YouTube heatmap ("Most Replayed")
scoring, then even distribution — the legacy behavior.

Allocation rules (§3.5) apply to every path: non-overlapping, >=30s gap between
segments, never reuse a (video_id, timestamp) already in segment_usage for that
platform. Boundaries snap to word edges so cuts never land mid-word.
"""
import random

MIN_GAP = 30.0  # §3.5: minimum seconds between allocated segments
# YouTube heatmaps always peak at t=0 (everyone "watches" the opening), so the
# top-valued point is an artifact, not a replayed moment. Skip the intro region
# — capped for short footage so brand clips stay usable.
INTRO_SKIP = 90.0

# §3.5 duration ranges per platform. These bound the topic, they don't set it:
# a clip ends when its topic ends, as long as the length lands in range.
# NOTE: youtube is 60-180 to match Clippo campaign requirements — clips over
# 60s upload as regular videos, not Shorts.
DURATION_RANGES = {
    "youtube": (60, 180),
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
    # never skip so much that nothing is left to clip
    intro = min(INTRO_SKIP, max(0.0, video_duration - lo * 2))

    candidates = []
    if heatmap:
        candidates = [p["start_time"] for p in
                      sorted(heatmap, key=lambda x: x.get("value", 0), reverse=True)]
    else:
        # even distribution fallback; slight jitter so retries differ
        n = max(count * 2, 4)
        step = max(1e-6, (video_duration - intro) / n)
        candidates = [intro + i * step + random.uniform(0, step * 0.3) for i in range(n)]

    for st in candidates:
        if len(out) >= count:
            break
        if st < intro:
            continue
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


# ---------------------------------------------------------------- topical (LLM)

TOPIC_SYSTEM = """You are a short-form video editor for Indonesian audiences.
You receive a timestamped transcript of a long video and must cut self-contained
clips from it. Output strictly one JSON object, no markdown."""

TOPIC_USER = """Transkrip bertimestamp (detik):
\"\"\"{transcript}\"\"\"

Pilih {count} segmen TERBAIK untuk klip pendek. Aturan WAJIB:
1. Segmen harus SATU TOPIK UTUH: mulai tepat saat pembicara MULAI membahas
   topik itu, berhenti tepat saat topik itu SELESAI dibahas. Jangan berhenti
   di tengah penjelasan, jangan lanjut masuk ke topik berikutnya.
2. Durasi mengikuti panjang topik, WAJIB antara {lo} dan {hi} detik.
   - Kalau topik A selesai tapi durasinya masih di bawah {lo} detik, LANJUTKAN
     ke topik B berikutnya yang masih nyambung, dan berhenti saat topik B
     selesai. Boleh gabung 2 topik, jangan lebih.
   - Berhenti tetap harus di batas topik yang tuntas, JANGAN dipanjangkan
     hanya untuk mengejar durasi, dan jangan sampai lewat {hi} detik.
   - Kalau satu topik lebih panjang dari {hi} detik, ambil bagian paling inti
     yang tetap utuh sebagai satu pemikiran.
3. Segmen harus bisa dipahami tanpa menonton video aslinya (self-contained):
   ada pembukaan konteks, isi, dan penutup/kesimpulan topik itu.
4. Prioritaskan topik dengan nilai tinggi: insight tajam, cerita menarik,
   angka/fakta mengejutkan, opini kontroversial, atau punchline.
5. Hindari intro, sapaan, basa-basi, iklan, dan closing channel.
6. Antar segmen tidak boleh tumpang tindih, minimal berjarak 30 detik.

Aturan HOOK (penting):
- Hook harus bikin penasaran, bukan merangkum. Buka rasa ingin tahu, tahan
  jawabannya — penonton harus merasa WAJIB nonton sampai habis.
- Kalau segmen berisi 2 topik, hook WAJIB menjembatani keduanya jadi satu
  pancingan utuh (misal pakai pola "bukan X, tapi Y", "ternyata...",
  "yang bikin kaget bukan itu"). Jangan cuma menyebut topik pertama.
- Maksimal 90 karakter, bahasa Indonesia santai, boleh 1-2 emoji relevan.

Return JSON:
{{
  "segments": [
    {{
      "start": <detik mulai, angka>,
      "end": <detik selesai, angka>,
      "topic": "<ringkas topik segmen ini, 1 kalimat; sebutkan kalau isinya 2 topik>",
      "reason_end": "<kenapa berhenti di titik itu — apa yang selesai dibahas>",
      "hook": "<headline pancingan sesuai aturan HOOK di atas>"
    }}
  ]
}}"""


def compress_transcript(words, bucket=8.0):
    """Group word list into ~`bucket`-second timestamped lines.

    Word-level JSON is far more detail than the model needs to locate topic
    boundaries, and costs many times the tokens. One line per few seconds keeps
    the timing resolution that matters while staying small.
    """
    if not words:
        return ""
    lines, cur, cur_start = [], [], words[0]["start"]
    for w in words:
        if w["start"] - cur_start >= bucket and cur:
            lines.append(f"[{cur_start:.0f}] " + " ".join(cur))
            cur, cur_start = [], w["start"]
        cur.append(w["word"])
    if cur:
        lines.append(f"[{cur_start:.0f}] " + " ".join(cur))
    return "\n".join(lines)


def _snap_start(words, target):
    """First word start at or after `target` (never cut into a word)."""
    starts = [w["start"] for w in words if w["start"] >= target]
    return min(starts) if starts else None


def pick_topical_segments(words, platform, count, existing=(), video_duration=None):
    """LLM-chosen segments that begin and end on topic boundaries.

    Returns [{start, end, topic, hook, reason_end}] — already snapped to word
    edges, duration-validated, gap-checked and deduped against `existing`.
    Returns [] if the model is unreachable or proposes nothing usable, so the
    caller can fall back to heatmap selection.
    """
    import ai

    lo, hi = DURATION_RANGES[platform]
    transcript = compress_transcript(words)
    if not transcript:
        return []
    try:
        out = ai.chat_json(
            TOPIC_SYSTEM,
            TOPIC_USER.format(transcript=transcript[:60000], count=count, lo=lo, hi=hi),
        )
    except Exception:
        return []

    taken = [tuple(e) for e in existing]
    picked = []
    for s in out.get("segments") or []:
        try:
            start = float(s["start"])
            end = float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        start = _snap_start(words, start)
        if start is None:
            continue
        end = _snap_end(words, start, end if video_duration is None
                        else min(end, video_duration))
        if end is None:
            continue
        dur = end - start
        if not (lo <= dur <= hi):
            continue
        if len(words_in(words, start, end)) < 15:
            continue
        if _overlaps(start, dur, taken):
            continue
        taken.append((start, end))
        picked.append({
            "start": start, "end": end,
            "topic": str(s.get("topic") or "").strip(),
            "hook": str(s.get("hook") or "").strip(),
            "reason_end": str(s.get("reason_end") or "").strip(),
        })
        if len(picked) >= count:
            break
    return picked


if __name__ == "__main__":
    # Self-check: synthetic 600s video, dense words, fake heatmap.
    random.seed(42)  # deterministic durations
    words = [{"word": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(1200)]
    heat = [{"start_time": t, "value": v} for t, v in
            [(100, 0.9), (300, 0.8), (105, 0.7), (500, 0.6)]]
    segs = pick_segments(600, heat, words, "youtube", 3)
    assert len(segs) == 3, segs
    lo_yt, hi_yt = DURATION_RANGES["youtube"]
    for s, e in segs:
        assert lo_yt <= e - s <= hi_yt + 0.5, (s, e)
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
    # intro artifact: t=0 always tops a YouTube heatmap and must be skipped
    heat0 = [{"start_time": 0.0, "value": 1.0}, {"start_time": 200.0, "value": 0.5}]
    segs3 = pick_segments(600, heat0, words, "youtube", 2)
    assert all(s >= INTRO_SKIP for s, _ in segs3), segs3
    # short footage: intro skip must not starve allocation
    short_words = [{"word": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4}
                   for i in range(600)]  # 300s, enough for the 60s minimum
    assert pick_segments(300, None, short_words, "youtube", 1), "short footage starved"

    # --- topical path (stubbed model) ---
    line = compress_transcript(words)
    lines = line.splitlines()
    assert line.startswith("[0] w0 "), line[:40]
    # ~one line per 8s bucket, every line tagged with its start second
    assert abs(len(lines) - words[-1]["start"] / 8) <= 1, len(lines)
    assert all(l.startswith("[") for l in lines)
    assert compress_transcript([]) == ""

    import ai
    real = ai.chat_json
    ai.chat_json = lambda *a, **k: {"segments": [
        {"start": 100.0, "end": 190.0, "topic": "T1", "hook": "H1 🔥", "reason_end": "R1"},
        {"start": 120.0, "end": 200.0, "topic": "overlap, must drop"},   # too close to #1
        {"start": 300.0, "end": 310.0, "topic": "too short, must drop"},  # < 60s
        {"start": 300.0, "end": 400.0, "topic": "T2", "hook": "H2", "reason_end": "R2"},
    ]}
    try:
        got = pick_topical_segments(words, "youtube", 3, video_duration=600)
        assert len(got) == 2, got
        assert got[0]["topic"] == "T1" and got[1]["topic"] == "T2", got
        assert got[0]["hook"] == "H1 🔥"
        assert all(60 <= g["end"] - g["start"] <= 180 for g in got), got
        # boundaries land on real word edges
        all_starts = {round(w["start"], 3) for w in words}
        all_ends = {round(w["end"], 3) for w in words}
        assert all(round(g["start"], 3) in all_starts for g in got)
        assert all(round(g["end"], 3) in all_ends for g in got)
        # already-used timestamps are respected
        assert pick_topical_segments(words, "youtube", 3, existing=[(g["start"], g["end"]) for g in got],
                                     video_duration=600) == []
        # model failure degrades to empty so the caller can fall back
        ai.chat_json = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        assert pick_topical_segments(words, "youtube", 2) == []
    finally:
        ai.chat_json = real
    print("segments.py self-check OK")
