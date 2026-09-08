# Clipper — Release Notes

**v0.2 "reference style"** · branch `claude/code-clipper-review-v9e3im`
15 commits, 12 files, +1343 / −122 · one new module (`bgm.py`), one new manifest
(`requirements.txt`)

_Dalmislave_

---

## What this release is

v0.1 proved the spine: crawl Clippo → download → transcribe → pick a segment →
render → upload to YouTube. It looked nothing like the clips it was imitating.

v0.2 is about the picture. The renderer was rebuilt against two reference clips
until the output matches their format, and the parts of the pipeline that
decide *what* gets rendered — how long a clip may be, where it will be posted,
which music sits under it — stopped being hardcoded.

Nothing here changes how tasks are discovered or uploaded.

---

## Renderer

**The footage fills the frame.** `frame_mode="cover"` (new default) crops the
source to 9:16 and uses it as the whole canvas — no band, no blurred fill. The
earlier band was an artifact of feeding it landscape crops. `fill` (a 40% band
over a blurred copy) and `fit` (whole frame letterboxed) remain for landscape
sources, where covering means a hard zoom.

**Phrase captions.** Whole phrases in one colour instead of a per-word
highlight: gold with a heavy black stroke, centred, two lines maximum, with the
punchline tinted magenta. Phrases break where the speaker pauses; when one runs
past six words it is cut at the clearest pause inside the window and continued
as the next caption, never truncated. The per-word karaoke style is still there
behind `caption_style="karaoke"`.

Side effect worth knowing: this is roughly one overlay PNG per phrase instead
of one per word. A 60-second clip drops from ~150 ffmpeg inputs to ~25.

**Pull-quote hook.** A teal quote mark above a stack of white boxes, one per
wrapped line, so the right edge stays ragged. `**Double asterisks**` in the hook
text render bold and the rest regular; an unmarked hook renders bold
throughout. `hook_style="card"` gives the single continuous card instead.

**Two openings.** Pass `intro=` a b-roll clip and the hook lives there, cutting
to the segment when the card leaves — the way both references open. Without one
the hook rides over the opening seconds instead. The trade-off is real: with no
intro, the seconds under the hook carry no subtitle, because nothing is allowed
to share the screen with it.

**Everything stays out of the platform UI.** TikTok, Reels and Shorts paint over
roughly the bottom 15% of the frame. Captions and the hook are both anchored by
their *bottom* edge and grow upward, so neither a long hook nor a three-line
caption can walk off under the app. The self-check asserts it for one-, three-
and five-line hooks.

---

## Music

New `bgm.py`. The model never picks a file — it cannot hear music, and a
hallucinated filename is unrenderable. It labels the *clip* with one of seven
moods, which it can do from the transcript, and the mood resolves to a track
locally against `background_music/tracks.json`. Adding or retiring a track never
touches the prompt, and the label rides along in the metadata call that already
runs per clip, so it costs no extra round-trip.

Within a mood the track is chosen by a stable hash of the clip's identity: the
same clip re-rendered after a retry gets the same track, which `random.choice`
could not promise. Sibling clips of one task exclude each other's track.

Royalty-free is not attribution-free: a manifest entry with an `attribution`
line has it appended to the clip's description before upload.

```json
{"tracks": [
  {"file": "01.mp3", "mood": ["hype"], "attribution": "Music: X by Y (CC BY 4.0)"},
  {"file": "02.mp3", "mood": ["hype", "funny"]}
]}
```

Without a manifest, moods are read from filenames like `03_tense_slowbuild.mp3`.
An empty folder means no BGM, not an error.

---

## Clip length and destinations

**One cut has to fit everywhere it goes.** `duration_window()` takes the whole
destination set and returns the tightest window — highest floor, lowest ceiling.
YouTube Shorts in the set binds at 90s; without it, TikTok's 180 opens up.

| Platform | Window |
|---|---|
| YouTube Shorts | 30–90s |
| TikTok | 30–180s |
| Instagram Reels | 30–90s |

Overridable per host: `CLIPPER_DURATION_YOUTUBE=20-90`.

The old floors were 60s for TikTok and Instagram, which meant a sharp
thirty-second moment could never be clipped for them at all.

**Destinations are derived, not chosen.** The campaign brief says which
platforms it wants; the `accounts` table says where there is an account cleared
for campaign work; only platforms with a working uploader are publishable. The
intersection is the destination set. A human can pin it per campaign from the
dashboard when the rule gets it wrong.

The `accounts` table had been inert since it was written — a paused or banned
account changed nothing. It is now the gate, and it reads the *applied* status:
the promotion `evaluate()` suggests is explicitly not enough, so a bad stat sync
cannot push a platform into campaign work on its own.

Clippo's submission form takes only TikTok and Instagram URLs, so a clip
published to YouTube earns nothing from a campaign. Paying surfaces are
therefore sized on their own; YouTube is a destination only while nothing paying
is live — which is the current state.

---

## Reliability

- `metadata.generate` no longer fails the whole task when 9Router is
  unreachable; it degrades to transcript-derived copy.
- `ai.py` retries transport errors and 5xx with backoff, and still raises 4xx
  immediately.
- Dashboard escaping now covers quotes and `>`. Campaign ids come from Clippo's
  API and reach a `value="..."` attribute.
- Google client imports are lazy, so `pipeline.py --help` and `preflight.py`
  work on a box that has not finished its setup — which is the box being
  diagnosed.

---

## Trying it

```bash
pip install -r requirements.txt
playwright install chromium          # only for Clippo and profile stats
```

Render one clip from a local file — no Clippo, no 9Router, no upload:

```bash
python edit.py preview footage.mp4 --start 0 --end 45 \
  --hook "**Cara Paling Elegan** Nanya Nama Orang Kalau **Kamu Terlanjur Lupa!**" \
  --bgm background_music/01.mp3 --out coba.mp4
```

The transcript comes from faster-whisper and is cached beside the video, so the
second run is instant. Other flags: `--intro`, `--frame-mode`,
`--caption-style`, `--hook-style`, `--words`.

Check a server before a real run, then exercise the whole chain without
spending an upload:

```bash
python preflight.py                  # fonts, ffmpeg, 9Router, session, BGM, accounts
python pipeline.py --dry-run         # renders real clips, uploads nothing
```

`--dry-run` writes no clip rows and no `segment_usage` rows and returns the task
to `DISCOVERED`, so the same segments stay free for the real run.

Every module runs its own checks: `python db.py`, `python segments.py`,
`python bgm.py`, `python edit.py`, and so on.

---

## Known gaps

Read this before relying on the pipeline unattended.

- **Tasks that crash mid-run are stranded.** `process()` only picks up
  `DISCOVERED`; a task killed during `EDITING` or `UPLOADING` stays there and
  nothing collects it. The PRD asks for resume-from-last-state and this does not
  do it yet. This is the most dangerous item on the list.
- **`segment_usage` is cleared per `video_id` on failure**, not per task, so a
  failure on one campaign can free segments another campaign already published.
- **No view checker.** `submit_eligible()` filters on `views_last_checked`,
  which nothing writes, so Clippo submission never fires.
- **`recent_avg_views` is never written**, so the dashboard's shadowban
  detection cannot trigger.
- **The b-roll intro is not wired into the pipeline.** `render_clip` accepts it;
  `pipeline.py` never passes one, because where b-roll comes from is undecided.
- **TikTok and Instagram uploaders do not exist**, so those windows are
  unreachable in practice.
- **No BGM ducking.** Music sits at a fixed 0.1 under speech rather than
  stepping back for it.

---

## Notes for whoever clones this

`edit.py` is where almost all of this release lives, and its constants at the
top are the knobs — canvas, colours, caption and hook geometry, safe-area
fractions. They were measured off the reference clips rather than guessed, and
the numbers are in the comments next to them.

Secrets stay out of the repo: `.env`, `cookies.txt`, `clippo_session.json` and
`tokens/` are gitignored and have to be placed by hand. `preflight.py` tells you
which are missing.
