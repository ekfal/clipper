# Clipper

Turns a long video into a vertical short: download, transcribe, pick a segment,
render captions and a hook over it, upload. Built for Indonesian footage and
for TikTok, Reels and YouTube Shorts.

There are two ways to use it, and they need different amounts of setup.

**The clip path** takes a link and returns an mp4. It touches no database, no
Clippo account and no upload token. This is the one to start with, and the one
an agent drives.

**The full pipeline** crawls Clippo campaigns on a schedule, tracks accounts and
publishes. It needs everything below plus credentials that cannot be checked
into a repo.

## Render a clip

```bash
git clone -b claude/code-clipper-review-v9e3im \
    https://github.com/ekfal/clipper.git
cd clipper

apt install -y ffmpeg fonts-dejavu-core fonts-noto-color-emoji
pip install -r requirements-clip.txt

python edit.py                 # renders two clips from footage it generates
```

That last command is the check that matters. It builds its own source video
with ffmpeg, renders both modes and prints the file sizes, so it needs no
network, no footage and no keys. If it prints `smoke split: OK` the box can
render, and everything after this is about getting real footage into it.

> **The branch is not optional.** `main` is behind by around 3000 lines and does
> not contain `job.py`, `bgm.py` or `report.py` at all. Clone the branch named
> above until it merges.

Then, with a real link:

```bash
cp .env.example .env           # nothing in it is required; see the file
python job.py --list           # styles, frame modes and music, as JSON
python job.py --opening OPENING_URL --content CONTENT_URL
```

`job.py` prints JSON on stdout and progress on stderr, so a caller never has to
parse a traceback. A failure prints `{"ok": false, "error": ...}` and files a
diagnostic report whose path comes back in the same JSON.

One job runs at a time per host. A request arriving while another is in flight
exits `3` with `{"busy": true}` rather than queueing behind it.

To work on a local file instead, skipping download and the copy model:

```bash
python edit.py preview FOOTAGE.mp4 --hook "**Bold bit** and the rest"
```

## What you actually need

| For | Install | Credentials |
|---|---|---|
| `edit.py` self-check | ffmpeg, fonts, `requirements-clip.txt` | none |
| `edit.py preview` on your own file | same | none |
| `job.py` on a real link | same | `cookies.txt` for anything above 360p |
| Better titles and hooks | same | `NINEROUTER_API_KEY` |
| Upload, dashboard, Clippo | `requirements.txt` | tokens, session, see below |

Two things decide clip quality more than anything else in the config:

**`cookies.txt`.** Without it YouTube serves 360p at best, and a 9:16 crop of
360p upscaled to 1080x1920 looks soft no matter what the renderer does. Export
one from an incognito window that you close straight afterwards; YouTube
rotates cookies on live sessions, which invalidates the export within days.
`fetch.py` has the full note, including the PO-token provider a flagged VPS
also needs.

**`NINEROUTER_API_KEY`.** Without it the hook and title come from the transcript
instead of a model. The clip still renders; the copy is just plainer.

Everything else has a default. `python preflight.py` reports what is missing on
this particular box and times the two stages whose cost decides whether an
hourly run fits.

## Secrets are not in the repo

`.env`, `cookies.txt`, `clippo_session.json` and `tokens/` are gitignored and
always will be. Cloning gets you the code, never the credentials: those are
placed on the box by hand. `.env.example` documents every variable the code
reads, and `preflight.py` names the ones that are missing.

## The full pipeline

```bash
pip install -r requirements.txt
playwright install chromium

python pipeline.py --dry-run   # renders real clips, uploads nothing
python pipeline.py             # one pass of the hourly loop
python dashboard.py            # account console on http://localhost:8000
```

A dry run consumes nothing: no `clips` row, no `segment_usage` row, and the task
returns to `DISCOVERED`.

## Layout

| File | What it is |
|---|---|
| `job.py` | Agent entry point. Two links in, one clip and a JSON result out. |
| `edit.py` | The renderer, and nearly all the visual constants. |
| `fetch.py` | Download, cache and input limits. `python fetch.py URL` tests it alone. |
| `transcribe.py` | faster-whisper, word-level, cached beside the video. |
| `segments.py` | Which part of the video becomes the clip. |
| `metadata.py` | Hook, title, description, hashtags. Enforces the copy rules. |
| `bgm.py` | Matches a curated track to the mood of a clip. |
| `pipeline.py` | The hourly loop. |
| `clippo.py` | Clippo.id campaign discovery and submission. |
| `accounts.py`, `dashboard.py` | Account lifecycle, and a console over it. |
| `upload_youtube.py` | Upload and quota handling. |
| `report.py` | Diagnostics on failure, optionally filed as a GitHub issue. |
| `preflight.py` | What this box is missing, and what the slow stages cost. |

Every module has a `__main__` self-check. Run the ones you touch; they are the
test suite. `CLAUDE.md` holds the working notes and the house rules,
`RELEASE_NOTES.md` what changed and what is still broken.

## Known limits

`RELEASE_NOTES.md` keeps the current list under **Known gaps**, and it is worth
reading before leaving the pipeline running unattended. The short version: there
is no view checker, so Clippo submission never fires; the TikTok and Instagram
uploaders do not exist; and the b-roll intro is wired into `job.py` but not into
`pipeline.py`.
