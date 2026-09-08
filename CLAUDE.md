# Clipper — working notes

## Deploy convention (standing rule)

Every deploy ships with release notes. Add the release to `RELEASE_NOTES.md`
before pushing the deploy, and sign it **Dalmislave**. Notes state what changed,
what it costs, and — importantly — what is still broken, since that is what
decides whether the pipeline can be left running unattended.

## Shape of the thing

`pipeline.py` is the hourly loop (crawl → fetch → transcribe → select → render →
upload). `job.py` is the agent-facing entry point: two links in, one clip and a
JSON result out. `edit.py` is the renderer and holds nearly all the visual
constants — they were measured off reference clips, and the numbers are in the
comments beside them.

Three ways in, cheapest first:

    python edit.py preview FOOTAGE --hook "..."   # one local file, no services
    python job.py --list                          # catalogue, JSON
    python pipeline.py --dry-run                  # whole chain, uploads nothing

## House rules

- Every module has a `__main__` self-check. Run the ones you touched; they are
  the test suite.
- A dry run must consume nothing: no `clips` row, no `segment_usage` row, and
  the task goes back to `DISCOVERED`.
- Secrets stay out of the repo (`.env`, `cookies.txt`, `clippo_session.json`,
  `tokens/`). `preflight.py` reports which are missing.
- Anything drawn on the frame must clear the platform UI band at the bottom.
  Anchor overlays by their bottom edge so a longer wrap grows upward.
