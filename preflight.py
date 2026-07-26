"""Host readiness check — run this first on any new VPS.

Verifies the things that differ between machines (fonts, ffmpeg, cores, model
availability) and measures the two stages whose cost decides whether an hourly
run fits: transcription and rendering. Downloads a short public video if no
local footage is around, so it works on a fresh box.
"""
import os
import shutil
import sys
import time

_BASE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_URL = "https://youtu.be/IJE50gujMTg"
CLIP_SECONDS = 30

ok = True


def check(label, passed, detail=""):
    global ok
    ok = ok and passed
    print(f"[{'OK ' if passed else 'FAIL'}] {label}{': ' + detail if detail else ''}")


def main():
    global ok
    print(f"python {sys.version.split()[0]} on {sys.platform}, "
          f"{os.cpu_count()} cores\n")

    import edit
    check("ffmpeg", shutil.which(edit.FFMPEG) is not None or os.path.exists(edit.FFMPEG),
          edit.FFMPEG)
    check("text font", os.path.exists(edit.FONT_PATH), edit.FONT_PATH)
    check("emoji font", os.path.exists(edit.EMOJI_FONT), edit.EMOJI_FONT)

    import fetch
    check("yt cookies (else 360p only)", bool(fetch.YTDLP_COOKIES),
          fetch.YTDLP_COOKIES or "missing — clips will be built from 360p")

    import ai
    try:
        import requests
        r = requests.get(f"{ai.BASE_URL}/models",
                         headers={"Authorization": f"Bearer {ai.API_KEY}"}, timeout=10)
        check("9Router reachable", r.ok, f"{ai.BASE_URL} -> {r.status_code}")
    except Exception as e:
        check("9Router reachable", False, f"{ai.BASE_URL} -> {type(e).__name__}")

    session = os.path.join(os.path.dirname(_BASE), "clippo_session.json")
    session = os.environ.get("CLIPPO_SESSION", session)
    check("clippo session", os.path.exists(session), session)

    # --- timing ---
    videos = []
    media = os.path.join(_BASE, "media")
    for root, _, names in os.walk(media):
        videos += [os.path.join(root, n) for n in names if n.endswith(".mp4")]
    if not videos:
        print("\nno local footage, downloading a sample...")
        try:
            videos = fetch.fetch("youtube", SAMPLE_URL, "preflight")
        except Exception as e:
            check("sample download", False, str(e)[:120])
            return
    video = max(videos, key=os.path.getsize)
    print(f"\nusing {os.path.basename(video)}")

    import transcribe
    t = time.time()
    words, info = transcribe.transcribe(video)
    el = time.time() - t
    rtf = el / info["duration"] if info["duration"] else 0
    cached = el < 1
    print(f"transcribe : {el:.0f}s for {info['duration']:.0f}s audio "
          f"(rtf {rtf:.2f}{', cached' if cached else ''}, model {info.get('model')})")
    if not cached:
        check("transcribe rtf < 1.0", rtf < 1.0, f"{rtf:.2f}")

    import segments as sel
    base = words[0]["start"] if words else 0
    seg = sel.words_in(words, base, base + CLIP_SECONDS)
    out = os.path.join(_BASE, "preflight_out.mp4")
    t = time.time()
    edit.render_clip(video, base, base + CLIP_SECONDS, seg, out,
                     hook="Preflight render 🔥")
    el = time.time() - t
    ratio = el / CLIP_SECONDS
    print(f"render     : {el:.1f}s for {CLIP_SECONDS}s clip ({ratio:.2f}x realtime) "
          f"-> a 180s clip takes ~{ratio * 180:.0f}s")
    check("render < 1.0x realtime", ratio < 1.0, f"{ratio:.2f}x")
    os.path.exists(out) and os.remove(out)

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED — see above"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
