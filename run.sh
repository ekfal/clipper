#!/usr/bin/env bash
# Hourly entry point. Install with:
#   0 * * * * /opt/clipper/run.sh
#
# flock is the whole concurrency story: one VPS, one worker, one SQLite writer.
# A run that overruns the hour is skipped rather than queued — the next hour
# picks the work up, and two workers uploading at once would double-post.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="${CLIPPER_LOG:-$DIR/clipper.log}"
LOCK="${CLIPPER_LOCK:-/tmp/clipper.lock}"

exec 9>"$LOCK"
flock -n 9 || { echo "$(date -Is) previous run still going, skipping"; exit 0; }

cd "$DIR"
[ -f .env ] && set -a && . ./.env && set +a

# Fast-forward only: a merged fix reaches production here, and a divergent
# local commit stops the pull instead of being silently merged.
git pull --ff-only >/dev/null 2>&1 || echo "$(date -Is) git pull skipped"

echo "=== $(date -Is) run start"
python3 pipeline.py
echo "=== $(date -Is) run end (exit $?)"
