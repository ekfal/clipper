# Vendored skills

`antislop*` is [anti-slop](https://github.com/miqdadbadjuber/anti-slop) by
Miqdad Badjuber, MIT licensed (`LICENSE.antislop`), copied here at upstream
commit `568d2f3c9894ddb1b865baf900504ca3ca91ccae` (2026-09-10). Six skills plus
`antislop-human/contrast-check.py`, unmodified.

They are checked in rather than fetched at session start for two reasons: the
version stays pinned, so a rule cannot change under a session mid-task, and
nothing downloads instructions at runtime. To move to a newer upstream, replace
the folders and update the commit above.

The pointer that loads them lives in `CLAUDE.md`, between the `antislop:start`
and `antislop:end` markers, along with the two calls this repo makes about where
the rules apply.
