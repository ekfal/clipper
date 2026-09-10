# Agents

Start with `README.md`. It has the clone command, the two setup paths and what
each one actually needs.

The short version for an agent asked to make a clip:

1. `python edit.py` renders two clips from footage it generates itself. No
   network, no keys, no footage on disk. If this fails, nothing else will work,
   and the output says why.
2. `python job.py --list` prints the catalogue as JSON: caption styles, hook
   styles, frame modes and the music moods available on this box.
3. `python job.py --opening URL --content URL` renders and prints a JSON result
   holding the output path.

`job.py` is the contract. It prints JSON on stdout and progress on stderr, so
you never parse a traceback to tell the user what happened. Exit `3` with
`{"busy": true}` means another job is already running on this host: say so and
retry later rather than starting a second one. Any other non-zero exit prints
`{"ok": false, "error": ...}` along with the path of a diagnostic report.

Posting the file, reading the chat message and holding the conversation are
yours. `job.py` deliberately stops at the file.

Two limits worth telling a user about before they wait:

- Without `cookies.txt` the download is capped at 360p and the clip looks soft.
- Without `NINEROUTER_API_KEY` the hook and title come from the transcript
  rather than a model. The clip still renders.

`python preflight.py` reports which of those are missing on this box.

`CLAUDE.md` holds the working notes, the house rules and the antislop pointer.
Read it before changing code.
