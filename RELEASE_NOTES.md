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

## v0.2.1 — agent entry point

`job.py` is the contract an agent drives: two links in, one clip out.

```bash
python job.py --list                        # catalogue of styles and music, JSON
python job.py --content URL --opening URL   # render, JSON result on stdout
```

Both print JSON on stdout and nothing else; progress goes to stderr and a
failure prints `{"ok": false, "error": ...}` with a non-zero exit, so a caller
never has to read a traceback to tell someone what went wrong. `--list` returns
every choosable style with the label a person should see, plus the music
library, the mood vocabulary and the length windows — enough for an agent to
offer the options without hardcoding them.

The opening link answers a question the renderer had left open: b-roll comes
from the user, per job.

Posting to chat, parsing the request and holding the conversation stay with the
agent, which knows its own transport. This module stops at the file.

**Audio fix that came out of building it.** A BGM track shorter than the clip is
looped, and once it wraps past its own length it hands `amix` packets with no
timestamp; the muxer rejects them and the render fails outright. Re-stamping the
mixed result fixes it. This had been latent — it only shows when the clip
outruns the music, which an intro makes more likely. Verified across the matrix:
intro with and without BGM, BGM without intro, neither, and a clip shorter than
the track.

_Dalmislave_

---

## v0.2.2 — failure reports, and testing the download on its own

**`report.py`.** A traceback pasted out of a terminal loses what actually
decides the answer: which ffmpeg, whether cookies were there, how much disk was
left, which style the job ran with. Every failure in `job.py` and `pipeline.py`
now writes a JSON report carrying all of it, and the path comes back in the
job's own JSON so a caller can hand it on without shell access to the box.

Set `CLIPPER_GITHUB_TOKEN` and `CLIPPER_GITHUB_REPO` and it also files a GitHub
issue — the one channel a maintainer can read without access to the machine. A
repeat of a failure already open comments on that issue instead of opening
another: failures are fingerprinted by exception type plus the first line with
paths and numbers flattened, so the same wall hit nightly leaves one thread
rather than a pile.

Nothing secret goes in a report. API keys, cookie contents and OAuth tokens are
never read — only whether they are present, which is the diagnostic value. The
self-check asserts none of them appear in the output.

Use a fine-grained token scoped to issues on this repo alone. It sits on the
VPS, so it should be able to do nothing else.

**`python fetch.py URL`** downloads one link and reports what landed. Download
is the only stage that depends on cookies, a PO token and the host's
reputation, so when a job dies at the first step this says whether the link or
the setup is at fault — without spending a whole render to find out.

_Dalmislave_

---

## v0.2.3 — hardening the agent path

Three things that decide whether `job.py` can be pointed at a chat channel
where anyone can paste a link.

**Downloads are keyed by URL, not by run.** Every job used the same folder, and
a Drive fetch walks its whole directory — so the second job picked up the first
job's file. Verified before the fix: two jobs, `max(files, key=getsize)`
returned the previous job's video. Keying by a hash of the URL fixes that and
buys something back: the same link retried with a different style reuses its
download *and* its cached transcript instead of paying for both again. Cached
folders are swept by age (`CLIPPER_MEDIA_TTL_HOURS`, default 24), and the sweep
only touches folders it named, so a pipeline task's media is never pulled out
from under it.

**Input limits.** There were none. A three-hour link meant gigabytes down, an
hour of whisper, and a render behind it. A YouTube link is now checked against
`CLIPPER_MAX_SOURCE_SECONDS` (default 60 min) from its metadata *before* the
download, and everything is checked for size (`CLIPPER_MAX_SOURCE_GB`, default
2) and duration after. The refusal names the actual and the allowed in units
that read at whatever the limit is set to, because that message goes straight
back to whoever sent the link.

If the duration cannot be probed — ffmpeg missing from PATH — the limit is
skipped, but loudly. It used to be skipped in silence, which is the worse
failure: a Drive link has no other guard.

**One job at a time per host.** Whisper and ffmpeg each want most of a small
VPS; two in parallel do not run twice as fast, they run out of memory. A second
request exits `3` with `{"busy": true}` and no failure report — it is not a
fault of that job, and the caller should retry rather than escalate. `--wait N`
blocks instead, for callers that would rather queue.

_Dalmislave_

---

## v0.2.4 — the pipeline survives being killed

The two items that stood at the top of the gap list since the first review.

**A task killed mid-run is no longer lost.** `process()` only ever collected
`DISCOVERED`, so a worker killed during `EDITING` or `UPLOADING` left its task
in a state nothing would ever pick up again. Tasks sitting in a worker-held
state past `CLIPPER_STALE_MINUTES` (default 90 — longer than whisper on a long
video) are swept back into the queue at the start of each run.

**And it resumes rather than restarts.** Rendering is the expensive stage, so
clips an earlier attempt already produced are reused: the resume check runs
*before* segment selection, so a retry skips the model call too. Clip rows now
carry the metadata and account the upload needs, because asking the model again
would put a different title on a file that already exists. Measured on the
crash-and-resume cycle: **zero** extra model calls, and the title on the
published clip is the one from the first attempt.

A rendered file that has gone missing under its clip row is the other case: the
row is dropped and its timestamp released, since nothing can publish it any
more. Requeuing deliberately does *not* release reservations — a clip about to
be resumed would otherwise have its slot claimed by another task mid-flight.

**Failing a task no longer frees another task's segments.** The release deleted
every `segment_usage` row for the video, not the ranges the failing task
reserved. Two campaigns routinely share footage, so one failure re-opened
timestamps another task had already published — and the next run cut and
uploaded the same moment again. It now matches the exact range and skips
anything already out. The regression test builds precisely that situation: two
tasks on one video, one publishes, the other fails, and the published
reservations must survive.

An existing self-check had to be corrected to land this: its fixture inserted a
clip with no start or end, so it only passed because the old code ignored the
range entirely.

_Dalmislave_

---

## v0.2.5 — the render path stands on its own

Confirming what the agent path actually needs, and cutting what it does not.

`classify_source` moved from `clippo.py` to `fetch.py`. It decides which
downloader a link belongs to, so it belongs with the routing table; leaving it
in the Clippo adapter meant anything wanting to download a link imported a
campaign-platform module to find out how. `clippo.py` re-exports it, so the
adapter contract is unchanged.

`job.py` now falls back to heatmap selection when the router is unreachable,
the same ladder `pipeline.py` has always had. It used to raise instead, which
made a 9Router hiccup take the whole chat flow down for something it could
still have produced.

Demonstrated with the router hard-failing on every call: the clip still renders
— topical selection degrades to heatmap, metadata degrades to transcript-derived
copy, music still lands. And with `job.py` loaded and a job run, none of
`clippo`, `playwright`, `db`, `sqlite3`, `accounts`, `pipeline` or
`upload_youtube` is imported at all. The render path touches no campaign
platform, no database and no upload credentials.

_Dalmislave_

---

## v0.2.6 — the download path is exercised, not just described

The downloader is `fetch.py`. There is no `download.py` — that was the old
"Clipper Gemini" name, and nothing in this repo answers to it.

Until now the network stages were the untested half: routing and the file
filter were covered, but `fetch_youtube` and `fetch_gdrive` themselves had
never run, in CI or anywhere. That mattered more than usual because v0.2.3 put
a duration pre-check inside `fetch_youtube` — new code on the one path nothing
executed.

Both are now driven against stubbed `yt_dlp` and `gdown` modules. What that
actually covers:

* an over-long video is refused **before** the download — asserted by checking
  yt-dlp was called once with `download=False` and never with `download=True`;
* the merged-extension fallback, where yt-dlp reports `.webm` and the file on
  disk is the muxed `.mp4`;
* the heatmap sidecar being written, and read back by `heatmap_for`;
* no heatmap offered meaning no sidecar, which is not an error;
* the 360p warning firing when a download comes back under 720p;
* a Drive folder that rate-limits mid-way still returning what landed, with the
  brief PDF filtered out of the footage;
* a Drive file share that returns nothing coming back empty rather than
  crashing.

What this still does not cover, and cannot from here: the network itself.
Cookies, PO tokens and a host's reputation with YouTube are only testable on
the host. `python fetch.py URL` is that test.

_Dalmislave_

---

## v0.2.7 — stop capping the download at 1080p

360p is an authentication question, not a limit: YouTube serves it to
unauthenticated requests, and `cookies.txt` opens the full ladder. That was
already documented and is unchanged.

The real cap was in the format selector, which asked for
`bestvideo[ext=mp4]`. YouTube only publishes H.264 up to 1080p — everything
above that exists as VP9 or AV1 in webm — so pinning the container pinned the
resolution, cookies or not.

That matters because cover mode crops a landscape source to 9:16 and throws
away most of the width before filling the canvas:

| source | after the 9:16 crop | to 1080x1920 |
|---|---|---|
| 1080p | 607x1080 | upscaled 1.78x |
| 1440p | 810x1440 | upscaled 1.33x |
| 2160p | 1215x2160 | downscaled 1.12x |

A 1080p source is being blown up nearly twice over. The selector now caps by
height instead of container, defaulting to `CLIPPER_MAX_HEIGHT=1440` —
visibly better through a crop without the file size 2160p brings against
`CLIPPER_MAX_SOURCE_GB`. The `<=?` is a preference, so a video published only
above the cap still downloads rather than failing to match.

The stub test pins both halves: the selector must carry the height cap, and
must not name a container. Re-pinning mp4 for tidiness would silently put the
1080p ceiling back, so it fails the test instead.

Merged output may now be mkv when the best pair cannot go in mp4. That is
already handled — mkv is in the accepted extensions, and the renderer
re-encodes everything anyway.

_Dalmislave_

---

## Known gaps

Read this before relying on the pipeline unattended.

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
- **`job.py` has no Discord side.** It renders and returns JSON; delivering the
  file and holding the conversation are the agent's.
- **No queue.** A busy host refuses rather than holding the request; whether
  that is right depends on how the agent handles a retry.
- **The network itself is untested.** The download logic is driven against
  stubbed yt-dlp and gdown, but nothing here reaches YouTube or Drive; cookies,
  PO tokens and IP reputation are only testable on the host, with
  `python fetch.py URL`.

---

## Notes for whoever clones this

`edit.py` is where almost all of this release lives, and its constants at the
top are the knobs — canvas, colours, caption and hook geometry, safe-area
fractions. They were measured off the reference clips rather than guessed, and
the numbers are in the comments next to them.

Secrets stay out of the repo: `.env`, `cookies.txt`, `clippo_session.json` and
`tokens/` are gitignored and have to be placed by hand. `preflight.py` tells you
which are missing.
