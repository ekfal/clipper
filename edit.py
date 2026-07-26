"""Editing pipeline — renders one clip from source footage (PRD §3.6).

Ported from old main.py. Kept: PIL karaoke captions (3-word chunks, active-word
highlight), split-screen (bg gameplay + centered main video), bg darken/zoom,
BGM mix + fadeout. Dropped: RGB-shift anti-copyright (brand footage is legal —
decision 2026-07-26), upscale fx (canvas is fixed 1080x1920, the old check was
a no-op there), torch/nvenc probing (VPS is CPU; codec via env).

Split-screen and BGM are toggleable per call (PRD: campaign brief may forbid
visual additions -> clean mode).
"""
import os
import random
import re
import shutil
import sys
import uuid

from PIL import Image, ImageDraw, ImageFont
from moviepy import (
    VideoFileClip, CompositeVideoClip, AudioFileClip, ColorClip,
    ImageClip, CompositeAudioClip,
)
import moviepy.audio.fx as afx

_BASE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_BASE)

FONT_PATH = os.environ.get(
    "CLIPPER_FONT",
    r"C:\Windows\Fonts\arialbd.ttf" if sys.platform == "win32"
    else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
BG_DIR = os.environ.get("CLIPPER_BG_DIR", os.path.join(_PARENT, "background_video"))
BGM_DIR = os.environ.get("CLIPPER_BGM_DIR", os.path.join(_PARENT, "background_music"))
CODEC = os.environ.get("CLIPPER_CODEC", "libx264")
BGM_VOLUME = 0.1

CANVAS_W, CANVAS_H = 1080, 1920
SUB_Y = 1500
FONT_SIZE = 60
SPACING = 12
PADDING = 40
STROKE = 5
ACTIVE_COLOR = "#87CEFA"


def _random_asset(dirpath, exts):
    try:
        files = [os.path.join(dirpath, f) for f in os.listdir(dirpath)
                 if f.lower().endswith(exts)]
        return random.choice(files) if files else None
    except OSError:
        return None


def _darken(clip, factor=None):
    f = factor or random.uniform(0.4, 0.7)
    return clip.image_transform(lambda im: (im.astype("float") * f).clip(0, 255).astype("uint8"))


def _random_zoom_crop(clip, tw=CANVAS_W, th=CANVAS_H):
    zoom = random.uniform(1.2, 1.5)
    tmp = clip.resized(width=int(tw * zoom))
    if tmp.h < th:
        tmp = clip.resized(height=int(th * zoom))
    x1 = random.randint(0, max(0, tmp.w - tw))
    y1 = random.randint(0, max(0, tmp.h - th))
    return tmp.cropped(x1=x1, y1=y1, width=tw, height=th)


def _word_png(text, font, color, tmp_dir):
    """Render one word to a transparent PNG; return (ImageClip, true_text_width)."""
    bbox = font.getbbox(text)
    left, top, right, bottom = bbox if bbox else (0, 0, 10, FONT_SIZE)
    w, h = right - left, bottom - top
    pad = STROKE + 8
    img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((pad - left, pad - top), text, font=font, fill=color,
                             stroke_width=STROKE, stroke_fill="black")
    path = os.path.join(tmp_dir, f"w_{uuid.uuid4().hex}.png")
    img.save(path)
    return ImageClip(path), w


HOOK_Y = 300          # hook block top (over bg / top of main video)
HOOK_FONT_SIZE = 58
HOOK_DUR = 3.0        # seconds the hook stays on screen
HOOK_MAX_CHARS = 22   # wrap width per boxed line


def _hook_layer(text, tmp_dir, dur=HOOK_DUR):
    """Opening visual hook — Hormozi-style stacked white boxes, black bold text.
    Shown for the first `dur` seconds. Returns list of positioned ImageClips."""
    import textwrap
    try:
        font = ImageFont.truetype(FONT_PATH, HOOK_FONT_SIZE)
    except OSError:
        font = ImageFont.load_default()
    lines = textwrap.wrap(text.strip(), width=HOOK_MAX_CHARS)
    clips, y = [], HOOK_Y
    pad_x, pad_y, gap = 18, 10, 8
    for line in lines:
        bbox = font.getbbox(line)
        left, top, right, bottom = bbox if bbox else (0, 0, 10, HOOK_FONT_SIZE)
        w, h = right - left, bottom - top
        img = Image.new("RGBA", (w + pad_x * 2, h + pad_y * 2), (255, 255, 255, 255))
        ImageDraw.Draw(img).text((pad_x - left, pad_y - top), line, font=font, fill="black")
        path = os.path.join(tmp_dir, f"h_{uuid.uuid4().hex}.png")
        img.save(path)
        ic = ImageClip(path).with_position(((CANVAS_W - img.width) / 2, y)) \
                            .with_start(0).with_duration(dur)
        clips.append(ic)
        y += img.height + gap
    return clips


def _karaoke_layer(words, clip_start, tmp_dir):
    """Build karaoke caption ImageClips for words (absolute timestamps)."""
    try:
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except OSError:
        font = ImageFont.load_default()
    max_w = CANVAS_W - PADDING * 2
    subs = []
    chunks = [words[i:i + 3] for i in range(0, len(words), 3)]
    for chunk in chunks:
        for i_w, active in enumerate(chunk):
            w_start = max(0.0, active["start"] - clip_start)
            nxt = chunk[i_w + 1]["start"] if i_w + 1 < len(chunk) else active["end"]
            w_end = max(w_start + 0.1, nxt - clip_start)

            clips, widths = [], []
            for j, w_item in enumerate(chunk):
                text = re.sub(r"[.,!?]", "", w_item["word"].upper())
                color = ACTIVE_COLOR if i_w == j else "white"
                ic, tw = _word_png(text, font, color, tmp_dir)
                clips.append(ic)
                widths.append(tw)

            total = sum(widths) + SPACING * (len(clips) - 1)
            scale = 1.0
            if total > max_w:
                scale = max_w / total
                clips = [c.resized(scale) for c in clips]
                widths = [w * scale for w in widths]
                total = max_w

            x = (CANVAS_W - total) / 2
            pad = (STROKE + 8) * scale
            for j, ic in enumerate(clips):
                subs.append(ic.with_position((x - pad, SUB_Y))
                              .with_start(w_start).with_duration(w_end - w_start))
                x += widths[j] + SPACING * scale
    return subs


def render_clip(video_path, start, end, words, out_path, *,
                hook=None, split_screen=True, bgm=True, fps=30, bitrate="10M"):
    """Render one vertical clip [start, end) with karaoke captions.

    words: [{word,start,end}] with ABSOLUTE source timestamps; caller pre-slices
    to the segment. hook: headline text shown as boxed overlay for the first
    seconds (visual hook). split_screen=False -> clean mode (PRD §3.6).
    Returns out_path.
    """
    dur = end - start
    tmp_dir = os.path.join(_BASE, f"temp_subs_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir, exist_ok=True)
    src = VideoFileClip(video_path)
    bg_raw = bgm_clip = None
    try:
        if split_screen:
            bg_path = _random_asset(BG_DIR, (".mp4", ".mov", ".webm"))
            if bg_path:
                bg_raw = VideoFileClip(bg_path).without_audio()
                if bg_raw.duration < dur:  # loop short bg by tiling from 0
                    from moviepy import concatenate_videoclips
                    n = int(dur / bg_raw.duration) + 1
                    bg_raw = concatenate_videoclips([bg_raw] * n)
                bg = _darken(_random_zoom_crop(bg_raw.subclipped(0, dur).resized(height=CANVAS_H)))
            else:
                bg = ColorClip(size=(CANVAS_W, CANVAS_H), color=(0, 0, 0), duration=dur)
            main = (src.subclipped(start, end).resized(height=980))
            main = main.cropped(x_center=main.w / 2, width=min(1040, main.w), height=980)
            main = main.with_position("center")
        else:
            bg = ColorClip(size=(CANVAS_W, CANVAS_H), color=(0, 0, 0), duration=dur)
            main = src.subclipped(start, end).resized(height=CANVAS_H)
            if main.w > CANVAS_W:
                main = main.cropped(x_center=main.w / 2, width=CANVAS_W, height=CANVAS_H)
            main = main.with_position("center")

        subs = _karaoke_layer(words, start, tmp_dir)
        hook_clips = _hook_layer(hook, tmp_dir, min(HOOK_DUR, dur)) if hook else []
        comp = CompositeVideoClip([bg, main] + subs + hook_clips,
                                  size=(CANVAS_W, CANVAS_H))

        audio = main.audio
        if bgm and audio:
            bgm_path = _random_asset(BGM_DIR, (".mp3", ".wav", ".m4a"))
            if bgm_path:
                bgm_clip = AudioFileClip(bgm_path)
                if bgm_clip.duration >= dur:
                    bgm_clip = bgm_clip.subclipped(0, dur)
                audio = CompositeAudioClip([audio, bgm_clip.with_volume_scaled(BGM_VOLUME)])
        if audio:
            comp = comp.with_audio(audio.with_effects([afx.AudioFadeOut(1.0)]))

        comp.write_videofile(out_path, fps=fps, codec=CODEC, bitrate=bitrate, logger=None)
        return out_path
    finally:
        for c in (bg_raw, bgm_clip, src):
            try:
                c and c.close()
            except Exception:
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    # Smoke: render 5s from a fetched video, both modes. Needs media/3 present.
    import json
    vid = os.path.join(_BASE, "media", "3", "IJE50gujMTg.mp4")
    sidecar = os.path.join(_BASE, "media", "3", "IJE50gujMTg.words.json")
    if not (os.path.exists(vid) and os.path.exists(sidecar)):
        print("smoke skipped: fetch media/3 first")
        sys.exit(0)
    with open(sidecar, encoding="utf-8") as f:
        all_words = json.load(f)["words"]
    seg = [w for w in all_words if 60 <= w["start"] < 65]
    hook = "Anak Muda Ini Sukses Jadi Clipper, 25 Juta Per Bulan !!"
    for mode, ss in (("split", True), ("clean", False)):
        out = os.path.join(_BASE, f"smoke_{mode}.mp4")
        render_clip(vid, 60, 65, seg, out, hook=hook, split_screen=ss, bgm=ss)
        assert os.path.exists(out) and os.path.getsize(out) > 100_000, mode
        print(f"smoke {mode}: OK ({os.path.getsize(out)//1000} KB)")
