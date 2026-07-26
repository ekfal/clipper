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
import subprocess
import sys
import uuid
from collections import namedtuple

from PIL import Image, ImageDraw, ImageFont

# One timed image pasted onto the canvas: PNG path, position, visible window.
Overlay = namedtuple("Overlay", "path x y t_start t_end")

_BASE = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.environ.get("CLIPPER_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
_PARENT = os.path.dirname(_BASE)

FONT_PATH = os.environ.get(
    "CLIPPER_FONT",
    r"C:\Windows\Fonts\arialbd.ttf" if sys.platform == "win32"
    else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
BG_DIR = os.environ.get("CLIPPER_BG_DIR", os.path.join(_PARENT, "background_video"))
BGM_DIR = os.environ.get("CLIPPER_BGM_DIR", os.path.join(_PARENT, "background_music"))
CODEC = os.environ.get("CLIPPER_CODEC", "libx264")
# Encoder knobs matter most on a small VPS: "medium" (x264's default) buys
# quality that a re-encoded social clip never shows. Tune per host via env.
PRESET = os.environ.get("CLIPPER_PRESET", "veryfast")
BITRATE = os.environ.get("CLIPPER_BITRATE", "6M")
THREADS = int(os.environ.get("CLIPPER_THREADS", str(os.cpu_count() or 4)))
FPS = int(os.environ.get("CLIPPER_FPS", "30"))
BGM_VOLUME = 0.1

CANVAS_W, CANVAS_H = 1080, 1920
BLUR_SIGMA = 28.0   # background blur strength (full-res equivalent)
BLUR_SCALE = 0.25   # blur is computed at this scale, then upscaled back
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


EMOJI_FONT = os.environ.get(
    "CLIPPER_EMOJI_FONT",
    r"C:\Windows\Fonts\seguiemj.ttf" if sys.platform == "win32"
    else "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
)
# emoji + ZWJ/variation-selector runs (rendered with the color-emoji font)
_EMOJI_RE = re.compile(
    "([\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF\u2600-\u27BF\u2B00-\u2BFF"
    "\uFE0F\u200D]+)"
)


def _emoji_tile(chunk, px):
    """Render an emoji run to an RGBA tile at height ~px. Noto Color Emoji is
    a bitmap font (fixed size 109) — render there and rescale when needed."""
    for size in (px, 109):
        try:
            f = ImageFont.truetype(EMOJI_FONT, size)
            bbox = f.getbbox(chunk)
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if w <= 0 or h <= 0:
                return None
            img = Image.new("RGBA", (w + 8, h + 8), (0, 0, 0, 0))
            ImageDraw.Draw(img).text((4 - bbox[0], 4 - bbox[1]), chunk, font=f,
                                     embedded_color=True)
            if size != px:
                img = img.resize((max(1, int(img.width * px / size)),
                                  max(1, int(img.height * px / size))))
            return img
        except OSError:
            continue
    return None


def _mixed_text_image(text, font, fill, stroke_width=0):
    """Render text that may contain emoji: text runs use `font`, emoji runs the
    color-emoji font. Returns (RGBA image, visual_text_width)."""
    px = getattr(font, "size", FONT_SIZE)
    tiles, vis_w = [], 0
    for chunk in _EMOJI_RE.split(text):
        if not chunk:
            continue
        if _EMOJI_RE.fullmatch(chunk):
            tile = _emoji_tile(chunk, px)
            if tile is not None:
                tiles.append(tile)
                vis_w += tile.width
            continue
        bbox = font.getbbox(chunk)
        left, top, right, bottom = bbox if bbox else (0, 0, 10, px)
        w, h = right - left, bottom - top
        pad = stroke_width + 8
        img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
        kw = {"stroke_width": stroke_width, "stroke_fill": "black"} if stroke_width else {}
        ImageDraw.Draw(img).text((pad - left, pad - top), chunk, font=font, fill=fill, **kw)
        tiles.append(img)
        vis_w += w
    if not tiles:
        return Image.new("RGBA", (10, px), (0, 0, 0, 0)), 10
    height = max(t.height for t in tiles)
    total_w = sum(t.width for t in tiles)
    canvas = Image.new("RGBA", (total_w, height), (0, 0, 0, 0))
    x = 0
    for t in tiles:
        canvas.paste(t, (x, (height - t.height) // 2), t)
        x += t.width
    return canvas, vis_w


HOOK_Y = 300          # hook block top, centered style (split-screen mode)
CLEAN_HOOK_Y = 1170   # hook block top, reference style (full-frame mode)
CLEAN_SUB_Y = 1040    # karaoke line in full-frame mode (mid-frame, above hook)
HOOK_X_LEFT = 44      # left margin for reference-style boxes
HOOK_FONT_SIZE = 58
HOOK_DUR = 3.0        # seconds the hook stays on screen
HOOK_MAX_CHARS = 22   # wrap width per boxed line
QUOTE_TEAL = "#3EC6A8"


def _quote_icon(tmp_dir, h=74):
    """Small teal rounded box with white quote marks (reference style)."""
    img = Image.new("RGBA", (int(h * 1.25), h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=12, fill=QUOTE_TEAL)
    try:
        f = ImageFont.truetype(FONT_PATH, int(h * 1.1))
    except OSError:
        f = ImageFont.load_default()
    q = "“"  # left double quote
    bbox = f.getbbox(q)
    d.text(((img.width - (bbox[2] - bbox[0])) / 2 - bbox[0],
            (img.height - (bbox[3] - bbox[1])) / 2 - bbox[1] + h * 0.12),
           q, font=f, fill="white")
    path = os.path.join(tmp_dir, f"q_{uuid.uuid4().hex}.png")
    img.save(path)
    return path


def _hook_layer(text, tmp_dir, dur=HOOK_DUR, y=HOOK_Y, left=False, icon=False):
    """Visual hook — stacked white boxes, black bold text (Hormozi style).
    left=True hugs HOOK_X_LEFT with a teal quote icon above (reference style);
    otherwise centered. Returns list of Overlay specs."""
    import textwrap
    try:
        font = ImageFont.truetype(FONT_PATH, HOOK_FONT_SIZE)
    except OSError:
        font = ImageFont.load_default()
    lines = textwrap.wrap(text.strip(), width=HOOK_MAX_CHARS)
    overlays = []
    pad_x, pad_y, gap = 18, 10, 8
    if icon:
        ip = _quote_icon(tmp_dir)
        iw, ih = Image.open(ip).size
        overlays.append(Overlay(ip, HOOK_X_LEFT if left else (CANVAS_W - iw) // 2,
                                y - ih - 12, 0.0, dur))
    for line in lines:
        content, _ = _mixed_text_image(line, font, "black")
        img = Image.new("RGBA", (content.width + pad_x * 2, content.height + pad_y * 2),
                        (255, 255, 255, 255))
        img.paste(content, (pad_x, pad_y), content)
        path = os.path.join(tmp_dir, f"h_{uuid.uuid4().hex}.png")
        img.save(path)
        x = HOOK_X_LEFT if left else (CANVAS_W - img.width) // 2
        overlays.append(Overlay(path, int(x), int(y), 0.0, dur))
        y += img.height + gap
    return overlays


def _karaoke_layer(words, clip_start, tmp_dir, sub_y=SUB_Y):
    """Karaoke captions as timed overlays.

    Each (chunk, active-word) state is flattened into ONE image rather than one
    per word, so a clip carries a third of the overlay inputs.
    """
    try:
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except OSError:
        font = ImageFont.load_default()
    max_w = CANVAS_W - PADDING * 2
    overlays = []
    chunks = [words[i:i + 3] for i in range(0, len(words), 3)]
    for chunk in chunks:
        for i_w, active in enumerate(chunk):
            w_start = max(0.0, active["start"] - clip_start)
            nxt = chunk[i_w + 1]["start"] if i_w + 1 < len(chunk) else active["end"]
            w_end = max(w_start + 0.1, nxt - clip_start)

            tiles, widths = [], []
            for j, w_item in enumerate(chunk):
                text = re.sub(r"[.,!?]", "", w_item["word"].upper())
                color = ACTIVE_COLOR if i_w == j else "white"
                img, tw = _mixed_text_image(text, font, color, stroke_width=STROKE)
                tiles.append(img)
                widths.append(tw)

            total = sum(widths) + SPACING * (len(tiles) - 1)
            scale = min(1.0, max_w / total) if total else 1.0
            if scale < 1.0:
                tiles = [t.resize((max(1, int(t.width * scale)),
                                   max(1, int(t.height * scale)))) for t in tiles]
                widths = [w * scale for w in widths]
                total = max_w

            height = max(t.height for t in tiles)
            line_img = Image.new("RGBA", (int(total) + 2 * int((STROKE + 8) * scale),
                                          height), (0, 0, 0, 0))
            pad = int((STROKE + 8) * scale)
            x = 0
            for j, t in enumerate(tiles):
                line_img.paste(t, (int(x), (height - t.height) // 2), t)
                x += widths[j] + SPACING * scale
            path = os.path.join(tmp_dir, f"k_{uuid.uuid4().hex}.png")
            line_img.save(path)
            overlays.append(Overlay(path, int((CANVAS_W - total) / 2) - pad,
                                    sub_y, w_start, w_end))
    return overlays


def render_clip(video_path, start, end, words, out_path, *,
                hook=None, split_screen=False, bgm=True, fps=FPS,
                bitrate=BITRATE, preset=PRESET, threads=THREADS):
    """Render one vertical clip [start, end) with karaoke captions.

    words: [{word,start,end}] with ABSOLUTE source timestamps; caller pre-slices
    to the segment. hook: headline text shown as boxed overlay for the first
    seconds (visual hook).

    Default = full-frame (reference style): footage fills the canvas, captions
    mid-frame, hook as left-aligned quote boxes lower-third. split_screen=True
    keeps the legacy gameplay-bg layout (per-campaign toggle, PRD §3.6).
    Returns out_path.
    """
    dur = end - start
    tmp_dir = os.path.join(_BASE, f"temp_subs_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        sub_y = SUB_Y if split_screen else CLEAN_SUB_Y
        overlays = _karaoke_layer(words, start, tmp_dir, sub_y=sub_y)
        if hook:
            overlays += _hook_layer(
                hook, tmp_dir, min(HOOK_DUR, dur),
                y=HOOK_Y if split_screen else CLEAN_HOOK_Y,
                left=not split_screen, icon=not split_screen)

        bg_video = _random_asset(BG_DIR, (".mp4", ".mov", ".webm")) if split_screen else None
        bgm_path = _random_asset(BGM_DIR, (".mp3", ".wav", ".m4a")) if bgm else None

        inputs = ["-ss", f"{start}", "-t", f"{dur}", "-i", video_path]
        if bg_video:
            inputs += ["-stream_loop", "-1", "-t", f"{dur}", "-i", bg_video]
        bgm_idx = None
        if bgm_path:
            bgm_idx = 1 + (1 if bg_video else 0)
            inputs += ["-stream_loop", "-1", "-t", f"{dur}", "-i", bgm_path]
        first_overlay_idx = 1 + (1 if bg_video else 0) + (1 if bgm_path else 0)
        for ov in overlays:
            inputs += ["-i", ov.path]

        cover = (f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=increase,"
                 f"crop={CANVAS_W}:{CANVAS_H}")
        chains = []
        if split_screen and bg_video:
            chains.append(f"[1:v]{cover},eq=brightness=-0.25[bg]")
            chains.append(f"[0:v]scale=-2:980,crop=min(iw\\,1040):980[mn]")
        else:
            # reference style: the footage itself, blurred, fills the frame
            chains.append(f"[0:v]split=2[bgsrc][mnsrc]")
            chains.append(f"[bgsrc]{cover},gblur=sigma={BLUR_SIGMA},"
                          f"eq=brightness=-0.20[bg]")
            chains.append(f"[mnsrc]scale=-2:{int(CANVAS_H * 0.62)},"
                          f"crop=min(iw\\,{CANVAS_W}):ih[mn]")
        chains.append("[bg][mn]overlay=(W-w)/2:(H-h)/2[v0]")

        for i, ov in enumerate(overlays):
            src_label = f"[v{i}]"
            dst_label = f"[v{i + 1}]"
            chains.append(
                f"{src_label}[{first_overlay_idx + i}:v]"
                f"overlay={ov.x}:{ov.y}:enable='between(t,{ov.t_start:.3f},{ov.t_end:.3f})'"
                f"{dst_label}")
        vlabel = f"[v{len(overlays)}]"

        if bgm_idx is not None:
            chains.append(f"[{bgm_idx}:a]volume={BGM_VOLUME}[bgm]")
            chains.append(f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0,"
                          f"afade=t=out:st={max(0, dur - 1):.2f}:d=1[a]")
        else:
            chains.append(f"[0:a]afade=t=out:st={max(0, dur - 1):.2f}:d=1[a]")

        # The graph can carry hundreds of overlay chains — pass it as a file so
        # the command never hits the OS argument-length limit.
        graph_path = os.path.join(tmp_dir, "graph.txt")
        with open(graph_path, "w", encoding="utf-8") as f:
            f.write(";".join(chains))

        cmd = ([FFMPEG, "-y", "-v", "error"] + inputs +
               ["-filter_complex_script", graph_path,
                "-map", vlabel, "-map", "[a]",
                "-c:v", CODEC, "-preset", preset, "-b:v", bitrate,
                "-pix_fmt", "yuv420p", "-r", str(fps),
                "-c:a", "aac", "-b:a", "128k",
                "-threads", str(threads), "-movflags", "+faststart", out_path])
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[:600]}")
        return out_path
    finally:
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
    hook = "Anak Muda Ini Sukses Jadi Clipper 😱 25 Juta Per Bulan !! 💰🔥"
    seg = list(seg)
    if len(seg) > 2:
        seg[2] = dict(seg[2], word=seg[2]["word"] + " 🔥")  # karaoke emoji path
    for mode, ss in (("split", True), ("clean", False)):
        out = os.path.join(_BASE, f"smoke_{mode}.mp4")
        render_clip(vid, 60, 65, seg, out, hook=hook, split_screen=ss, bgm=ss)
        assert os.path.exists(out) and os.path.getsize(out) > 100_000, mode
        print(f"smoke {mode}: OK ({os.path.getsize(out)//1000} KB)")
