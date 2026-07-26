"""Transcription — faster-whisper, word-level timestamps, Indonesian (PRD §3.3).

Replaces openai-whisper from the old stack. CPU-friendly: int8 compute. Output
is a flat word list [{word, start, end}] saved as <video>.words.json — the same
shape main.py's karaoke renderer consumed, so edit.py ports cleanly.
"""
import json
import os
import time

MODEL_SIZE = os.environ.get("CLIPPER_WHISPER_MODEL", "small")
# ponytail: module-level singleton, fine for single-worker; pool if we ever go multi-process
_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    return _model


def transcribe(video_path, language="id"):
    """Transcribe one video. Returns (words, info). Caches to sidecar JSON —
    re-running on the same file is a cheap read, keeping the stage idempotent."""
    sidecar = os.path.splitext(video_path)[0] + ".words.json"
    if os.path.exists(sidecar):
        with open(sidecar, encoding="utf-8") as f:
            cached = json.load(f)
        return cached["words"], cached["info"]

    model = _get_model()
    t0 = time.time()
    segments, info = model.transcribe(video_path, language=language, word_timestamps=True)
    words = []
    for seg in segments:  # generator — transcription happens during iteration
        for w in seg.words or []:
            words.append({"word": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3)})
    meta = {
        "duration": round(info.duration, 2),
        "language": info.language,
        "elapsed_sec": round(time.time() - t0, 1),
        "model": MODEL_SIZE,
    }
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump({"words": words, "info": meta}, f, ensure_ascii=False)
    return words, meta


if __name__ == "__main__":
    # Speed-test CLI: python transcribe.py <video>  (M2 gate runs this on the VPS)
    import sys
    if len(sys.argv) < 2:
        print("usage: python transcribe.py <video_path>")
        sys.exit(1)
    words, meta = transcribe(sys.argv[1])
    rtf = meta["elapsed_sec"] / meta["duration"] if meta["duration"] else 0
    print(f"duration={meta['duration']}s elapsed={meta['elapsed_sec']}s "
          f"rtf={rtf:.2f} words={len(words)} model={meta['model']}")
    print("first words:", " ".join(w["word"] for w in words[:12]))
