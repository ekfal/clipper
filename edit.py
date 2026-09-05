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
import itertools
import shutil
import subprocess
import sys
import uuid
from collections import namedtuple

from PIL import Image, ImageDraw, ImageFont

# One timed image pasted onto the canvas: PNG path, position, visible window.
Overlay = namedtuple("Overlay", "path x y t_start t_end")

_counter = itertools.count()


def _name(prefix):
    """Short, unique PNG filename. A long clip carries hundreds of overlay
    inputs, and ffmpeg takes them as command-line args — full-length uuid names
    blow past the OS argument limit, so keep them tiny and run ffmpeg with the
    temp directory as its working directory."""
    return f"{prefix}{next(_counter):04d}.png"

_BASE = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.environ.get("CLIPPER_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
_PARENT = os.path.dirname(_BASE)

FONT_PATH = os.environ.get(
    "CLIPPER_FONT",
    r"C:\Windows\Fonts\arialbd.ttf" if sys.platform == "win32"
    else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
# Regular weight, for the un-emphasised half of a hook line. The reference
# style mixes both in one sentence, so the pair must be the same family.
FONT_PATH_REGULAR = os.environ.get(
    "CLIPPER_FONT_REGULAR",
    r"C:\Windows\Fonts\arial.ttf" if sys.platform == "win32"
    else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
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
BG_DARKEN = -0.12   # blurred fill is dimmed just enough to sit back
BLUR_SCALE = 0.25   # blur is computed at this scale, then upscaled back
SUB_Y = 1500
FONT_SIZE = 60
SPACING = 12
PADDING = 40
STROKE = 5
ACTIVE_COLOR = "#87CEFA"

# ---- phrase captions (reference style) -------------------------------------
# Whole phrases in one colour instead of a per-word highlight: gold by default,
# magenta for the beat the model marks as the punchline. Heavy black stroke is
# what keeps them legible over any footage.
PHRASE_COLOR = "#FFD24A"
PHRASE_ACCENT = "#FF3FA4"
PHRASE_FONT_SIZE = 54
PHRASE_STROKE = 6
PHRASE_LINE_GAP = 6
PHRASE_MAX_WORDS = 3      # words per line
PHRASE_MAX_LINES = 3      # lines per phrase before it is flushed
PHRASE_GAP_SPLIT = 0.45   # a pause this long ends the phrase
PHRASE_X_FRAC = 0.09      # left margin, fraction of canvas width
PHRASE_Y_FRAC = 0.61      # block BOTTOM, keeps it inside the footage band
# "phrase" (reference look) or "karaoke" (per-word highlight, PRD §3.6)
CAPTION_STYLE = os.environ.get("CLIPPER_CAPTION_STYLE", "phrase")
# How the footage sits on the canvas:
#   "fill" — scale to FILL_HEIGHT_FRAC of the canvas and crop to width. The
#            subject is bigger and the frame reads punchier on a phone, at the
#            cost of whatever leaves the left and right edges.
#   "fit"  — scale the whole frame in, never cropping; band height follows the
#            source aspect. Nothing is lost, the subject is smaller.
# "fill" is the default: for talking-head footage the tighter framing wins, and
# a centred speaker survives the side crop.
FRAME_MODE = os.environ.get("CLIPPER_FRAME_MODE", "fill")
FILL_HEIGHT_FRAC = 0.62

# Where the captions sit relative to the footage:
#   "below"  — the footage is shrunk to the reference clip's proportion and
#              stays centred; captions live in the clear strip under it.
#              Nothing is ever covered.
#   "inside" — captions sit on the footage (the reference look). The footage
#              stays as large as frame_mode allows.
# These cannot both be maximised: a 62%-tall video leaves no strip that is also
# clear of the platform's own UI, so "below" trades video size for a clean
# caption lane. See BELOW_* below.
CAPTION_PLACE = os.environ.get("CLIPPER_CAPTION_PLACE", "below")
# TikTok/Reels/Shorts paint their caption, handle and buttons over roughly the
# bottom 15% and right edge of the frame. Anything below this is at risk of
# being covered by the app, so the caption lane has to end above it.
PLATFORM_SAFE_BOTTOM = 0.82
# 40% centred is the proportion the reference clip uses throughout (measured
# 42/39/33/40% across its shots, always centred on 50%). It also happens to
# leave exactly enough room underneath for a caption lane.
BELOW_HEIGHT_FRAC = 0.40     # footage height when captions go below it
BELOW_CAPTION_BOTTOM = 0.81  # caption block bottom, inside the safe area
BELOW_MAX_LINES = 2          # the lane fits two lines, not three
BELOW_HOOK_Y = 1100          # hook card still rides on the footage, lower third


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
CLEAN_HOOK_Y = 1460   # hook block top, reference style (lower third)
CLEAN_SUB_Y = 1040    # karaoke line in full-frame mode (mid-frame, above hook)
HOOK_X_LEFT = 44      # left margin for reference-style boxes
HOOK_FONT_SIZE = 56
HOOK_DUR = 3.0        # seconds the hook stays on screen
HOOK_MAX_CHARS = 26   # wrap width per boxed line
HOOK_PAD_X, HOOK_PAD_Y = 26, 18


def _parse_emphasis(text):
    """Split "plain **bold** plain" into [(word, is_bold)] tokens.

    The reference style bolds only the loaded half of a hook sentence and
    leaves the connective words regular, which is what makes it read like a
    headline instead of a caption. metadata.py emits the ** markers.
    """
    tokens, bold = [], False
    for chunk in re.split(r"(\*\*)", text.strip()):
        if chunk == "**":
            bold = not bold
            continue
        for word in chunk.split():
            tokens.append((word, bold))
    return tokens


def _wrap_tokens(tokens, max_chars=HOOK_MAX_CHARS):
    """Greedy wrap of (word, bold) tokens into lines, emphasis preserved."""
    lines, cur, width = [], [], 0
    for word, bold in tokens:
        add = len(word) + (1 if cur else 0)
        if cur and width + add > max_chars:
            lines.append(cur)
            cur, width = [], 0
            add = len(word)
        cur.append((word, bold))
        width += add
    if cur:
        lines.append(cur)
    return lines


def _rich_line_image(line, bold_font, reg_font, fill="black"):
    """One hook line whose words may switch weight. Returns an RGBA image."""
    runs, buf, cur_bold = [], [], line[0][1]
    for word, bold in line:
        if bold != cur_bold:
            runs.append((" ".join(buf), cur_bold))
            buf, cur_bold = [], bold
        buf.append(word)
    runs.append((" ".join(buf), cur_bold))

    # _mixed_text_image pads each tile, so advance by the VISUAL width and let
    # the padding overlap — otherwise every weight switch reads as a double
    # space. Same trick the karaoke layer uses.
    pad = 8
    tiles, widths = [], []
    for text, bold in runs:
        img, vis_w = _mixed_text_image(text, bold_font if bold else reg_font, fill)
        tiles.append(img)
        widths.append(vis_w)
    space = reg_font.getlength(" ") if hasattr(reg_font, "getlength") else HOOK_FONT_SIZE * 0.3
    ink_w = sum(widths) + space * (len(tiles) - 1)
    height = max(t.height for t in tiles)
    img = Image.new("RGBA", (int(ink_w) + pad * 2, height), (0, 0, 0, 0))
    x = 0.0
    for t, w in zip(tiles, widths):
        img.paste(t, (int(x), (height - t.height) // 2), t)
        x += w + space
    return img.crop((pad, 0, pad + int(ink_w), height))


def _hook_layer(text, tmp_dir, dur=HOOK_DUR, y=HOOK_Y, left=False):
    """Visual hook — ONE white box behind every line, black text.

    The reference draws a single continuous card rather than a stack of
    per-line boxes, so the ragged right edge of the wrap stays inside one
    rectangle. left=True hugs HOOK_X_LEFT, otherwise the card is centered.
    Returns a list of Overlay specs (always exactly one).
    """
    try:
        bold_font = ImageFont.truetype(FONT_PATH, HOOK_FONT_SIZE)
    except OSError:
        bold_font = ImageFont.load_default()
    try:
        reg_font = ImageFont.truetype(FONT_PATH_REGULAR, HOOK_FONT_SIZE)
    except OSError:
        reg_font = bold_font

    lines = _wrap_tokens(_parse_emphasis(text))
    if not lines:
        return []
    rendered = [_rich_line_image(l, bold_font, reg_font) for l in lines]
    inner_w = max(r.width for r in rendered)
    inner_h = sum(r.height for r in rendered) + HOOK_PAD_Y // 2 * (len(rendered) - 1)

    card = Image.new("RGBA", (inner_w + HOOK_PAD_X * 2, inner_h + HOOK_PAD_Y * 2),
                     (255, 255, 255, 255))
    cy = HOOK_PAD_Y
    for r in rendered:
        card.paste(r, (HOOK_PAD_X, cy), r)
        cy += r.height + HOOK_PAD_Y // 2

    path = os.path.join(tmp_dir, _name("h"))
    card.save(path)
    x = HOOK_X_LEFT if left else (CANVAS_W - card.width) // 2
    return [Overlay(path, int(x), int(y), 0.0, dur)]


def group_phrases(words, gap_split=PHRASE_GAP_SPLIT,
                  max_words=PHRASE_MAX_WORDS, max_lines=PHRASE_MAX_LINES):
    """Group a word list into caption phrases, split on natural pauses.

    A phrase ends where the speaker pauses (>= gap_split seconds) or once it
    fills max_words * max_lines words — so a caption change lands on a beat in
    the speech rather than every third word.
    Returns [{start, end, lines: [[word, ...], ...]}].
    """
    phrases, cur = [], []
    limit = max_words * max_lines

    def flush():
        if not cur:
            return
        chunk = list(cur)
        phrases.append({
            "start": chunk[0]["start"],
            "end": chunk[-1]["end"],
            "lines": [chunk[i:i + max_words] for i in range(0, len(chunk), max_words)],
        })
        cur.clear()

    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if len(cur) >= limit or nxt is None or nxt["start"] - w["end"] >= gap_split:
            flush()
    flush()
    return phrases


def _phrase_layer(words, clip_start, tmp_dir, accent_words=(), y_frac=PHRASE_Y_FRAC,
                  centred=True, max_lines=PHRASE_MAX_LINES):
    """Phrase captions (reference style): whole lines in one colour, left
    aligned, heavy black stroke, sitting inside the footage band.

    y_frac is the BOTTOM of the block: captions grow upward, so a one-line and
    a three-line phrase share a baseline and neither can spill past the band on
    a wide source.

    One PNG per phrase instead of one per word — a 60s clip drops from ~150
    overlay inputs to ~25, which is most of the render cost.
    """
    try:
        font = ImageFont.truetype(FONT_PATH, PHRASE_FONT_SIZE)
    except OSError:
        font = ImageFont.load_default()
    accent = {a.lower().strip(".,!?") for a in accent_words if a}
    pad = PHRASE_STROKE + 8
    max_w = CANVAS_W - 2 * PADDING if centred else (
        CANVAS_W - int(PHRASE_X_FRAC * CANVAS_W) - PADDING)
    overlays = []

    for ph in group_phrases(words, max_lines=max_lines):
        plain = [re.sub(r"[.,!?]", "", w["word"].upper()) for line in ph["lines"] for w in line]
        color = (PHRASE_ACCENT
                 if accent and any(p.lower() in accent for p in plain)
                 else PHRASE_COLOR)
        tiles = []
        for line in ph["lines"]:
            text = " ".join(re.sub(r"[.,!?]", "", w["word"].upper()) for w in line)
            img, _ = _mixed_text_image(text, font, color, stroke_width=PHRASE_STROKE)
            if img.width > max_w:  # very long word — shrink the whole line
                s = max_w / img.width
                img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))))
            tiles.append(img)
        block_w = max(t.width for t in tiles)
        block_h = sum(t.height for t in tiles) + PHRASE_LINE_GAP * (len(tiles) - 1)
        block = Image.new("RGBA", (block_w, block_h), (0, 0, 0, 0))
        cy = 0
        for t in tiles:
            # every tile carries the same padding, so centring on the block is
            # symmetric and needs no pad correction
            block.paste(t, ((block_w - t.width) // 2 if centred else 0, cy), t)
            cy += t.height + PHRASE_LINE_GAP
        path = os.path.join(tmp_dir, _name("p"))
        block.save(path)

        t_start = max(0.0, ph["start"] - clip_start)
        t_end = max(t_start + 0.3, ph["end"] - clip_start)
        x = ((CANVAS_W - block_w) // 2 if centred
             else int(PHRASE_X_FRAC * CANVAS_W) - pad)
        overlays.append(Overlay(path, x, int(y_frac * CANVAS_H - block_h),
                                t_start, t_end))
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
            path = os.path.join(tmp_dir, _name("k"))
            line_img.save(path)
            overlays.append(Overlay(path, int((CANVAS_W - total) / 2) - pad,
                                    sub_y, w_start, w_end))
    return overlays


def render_clip(video_path, start, end, words, out_path, *,
                hook=None, split_screen=False, bgm=True, fps=FPS,
                bitrate=BITRATE, preset=PRESET, threads=THREADS,
                caption_style=CAPTION_STYLE, accent_words=(),
                frame_mode=FRAME_MODE, caption_place=CAPTION_PLACE):
    """Render one vertical clip [start, end) with burned-in captions.

    words: [{word,start,end}] with ABSOLUTE source timestamps; caller pre-slices
    to the segment. hook: headline text shown as a boxed overlay for the first
    seconds; **double asterisks** inside it render bold, the rest regular.

    caption_style="phrase" (default) draws whole phrases in one colour, left
    aligned inside the footage band — the reference look, and ~6x fewer overlay
    inputs. caption_style="karaoke" keeps the per-word highlight (PRD §3.6).
    accent_words tints the phrase carrying the punchline (see metadata.py).

    bgm accepts a track path (what pipeline.py passes, chosen by bgm.py from
    the clip's mood), True for a random pick, or False for none.

    caption_place="below" (default) sizes the footage like the reference clip
    (40% of the canvas, centred) so the captions fit in a clear strip beneath
    it, covering nothing; "inside" keeps the footage as large as frame_mode
    allows and lays the captions over it.

    frame_mode="fill" (default) crops the footage to canvas width at
    FILL_HEIGHT_FRAC of the canvas height; "fit" scales the whole frame in
    instead, losing nothing but showing the subject smaller. Either way the
    footage sits over a blurred copy of itself. split_screen=True keeps the
    legacy gameplay-bg layout (per-campaign toggle, PRD §3.6).
    Returns out_path.
    """
    dur = end - start
    tmp_dir = os.path.join(_BASE, f"temp_subs_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        below = caption_place == "below" and not split_screen
        if caption_style == "phrase" and not split_screen:
            overlays = _phrase_layer(
                words, start, tmp_dir, accent_words=accent_words,
                y_frac=BELOW_CAPTION_BOTTOM if below else PHRASE_Y_FRAC,
                max_lines=BELOW_MAX_LINES if below else PHRASE_MAX_LINES)
        else:
            sub_y = SUB_Y if split_screen else CLEAN_SUB_Y
            if below:
                sub_y = int(BELOW_CAPTION_BOTTOM * CANVAS_H) - FONT_SIZE * 2
            overlays = _karaoke_layer(words, start, tmp_dir, sub_y=sub_y)
        if hook:
            hook_y = HOOK_Y if split_screen else (
                BELOW_HOOK_Y if below else CLEAN_HOOK_Y)
            overlays += _hook_layer(hook, tmp_dir, min(HOOK_DUR, dur),
                                    y=hook_y, left=not split_screen)

        bg_video = _random_asset(BG_DIR, (".mp4", ".mov", ".webm")) if split_screen else None
        # bgm: a path chosen by bgm.py (production), or True for a random pick
        # — the latter is for the smoke test only, it is not reproducible.
        if isinstance(bgm, str):
            bgm_path = bgm
        elif bgm:
            bgm_path = _random_asset(BGM_DIR, (".mp3", ".wav", ".m4a"))
        else:
            bgm_path = None

        inputs = ["-ss", f"{start}", "-t", f"{dur}", "-i", os.path.abspath(video_path)]
        if bg_video:
            inputs += ["-stream_loop", "-1", "-t", f"{dur}",
                       "-i", os.path.abspath(bg_video)]
        bgm_idx = None
        if bgm_path:
            bgm_idx = 1 + (1 if bg_video else 0)
            inputs += ["-stream_loop", "-1", "-t", f"{dur}",
                       "-i", os.path.abspath(bgm_path)]
        first_overlay_idx = 1 + (1 if bg_video else 0) + (1 if bgm_path else 0)
        for ov in overlays:
            inputs += ["-i", os.path.basename(ov.path)]  # cwd is tmp_dir

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
                          f"eq=brightness={BG_DARKEN}[bg]")
            # captions below need the footage smaller, so it clears the lane
            box_h = int(CANVAS_H * (BELOW_HEIGHT_FRAC if below else FILL_HEIGHT_FRAC))
            if frame_mode == "fit":
                # never crop: the whole source frame lands as a band, its
                # height set by the source aspect (capped by box_h)
                chains.append(f"[mnsrc]scale=w={CANVAS_W}:h={box_h}:"
                              f"force_original_aspect_ratio=decrease:"
                              f"force_divisible_by=2[mn]")
            else:
                # fill: scale by height, then crop to canvas width. A source
                # narrower than the canvas is scaled up to it first, so the
                # crop never leaves a transparent edge.
                h = box_h // 2 * 2
                chains.append(f"[mnsrc]scale=-2:{h},"
                              f"scale=w='max(iw,{CANVAS_W})':h=-2,"
                              f"crop={CANVAS_W}:min(ih\\,{h})[mn]")
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
        with open(os.path.join(tmp_dir, "graph.txt"), "w", encoding="utf-8") as f:
            f.write(";".join(chains))

        cmd = ([FFMPEG, "-y", "-v", "error"] + inputs +
               ["-filter_complex_script", "graph.txt",
                "-map", vlabel, "-map", "[a]",
                "-c:v", CODEC, "-preset", preset, "-b:v", bitrate,
                "-pix_fmt", "yuv420p", "-r", str(fps),
                "-c:a", "aac", "-b:a", "128k",
                "-threads", str(threads), "-movflags", "+faststart",
                os.path.abspath(out_path)])
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp_dir)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[:600]}")
        return out_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    # Logic self-check first (no ffmpeg, no footage), then an optional render.
    assert _parse_emphasis("**Nekat!! Berani** Ngomong Ke **Mantan**") == [
        ("Nekat!!", True), ("Berani", True), ("Ngomong", False), ("Ke", False),
        ("Mantan", True)], _parse_emphasis("**a** b")
    assert _parse_emphasis("tanpa penanda") == [("tanpa", False), ("penanda", False)]
    lines = _wrap_tokens([(w, False) for w in "satu dua tiga empat lima".split()],
                         max_chars=10)
    assert ["".join(w for w, _ in l) for l in lines] == ["satudua", "tigaempat", "lima"], lines

    # phrases break on a pause, and cap at max_words * max_lines
    ws = [{"word": f"w{i}", "start": i * 0.3, "end": i * 0.3 + 0.25} for i in range(4)]
    ws += [{"word": f"x{i}", "start": 3.0 + i * 0.3, "end": 3.0 + i * 0.3 + 0.25}
           for i in range(2)]
    ph = group_phrases(ws)
    assert len(ph) == 2, ph                       # the 2s pause splits them
    assert [w["word"] for l in ph[0]["lines"] for w in l] == ["w0", "w1", "w2", "w3"]
    assert len(ph[0]["lines"]) == 2, ph[0]["lines"]        # 3 words per line
    assert ph[1]["start"] == 3.0 and ph[0]["end"] == ws[3]["end"]
    dense = [{"word": f"d{i}", "start": i * 0.2, "end": i * 0.2 + 0.15} for i in range(20)]
    assert all(sum(len(l) for l in p["lines"]) <= PHRASE_MAX_WORDS * PHRASE_MAX_LINES
               for p in group_phrases(dense))
    # a caption placed "below" must clear the footage by construction, for the
    # worst case (a full three-line phrase) — this is the whole point of the mode
    import tempfile as _tf
    _long = [{"word": f"KATAPANJANG{i}", "start": i * 0.2, "end": i * 0.2 + 0.15}
             for i in range(PHRASE_MAX_WORDS * BELOW_MAX_LINES)]
    _ov = _phrase_layer(_long, 0, _tf.mkdtemp(), y_frac=BELOW_CAPTION_BOTTOM,
                        max_lines=BELOW_MAX_LINES)
    _top = min(o.y for o in _ov)
    _bot = max(o.y + Image.open(o.path).height for o in _ov)
    _video_bottom = (0.5 + BELOW_HEIGHT_FRAC / 2) * CANVAS_H
    assert _top > _video_bottom, f"caption {_top} overlaps footage ending {_video_bottom}"
    assert _bot <= PLATFORM_SAFE_BOTTOM * CANVAS_H, (
        f"caption bottom {_bot} runs into the platform UI zone")
    print("edit.py logic self-check OK")

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
