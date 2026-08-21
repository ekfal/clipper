# Hermes integration spec

For the developer wiring Hermes agents onto the Clipper pipeline.

Read this before writing any agent code. The seams already exist — nothing in
the repo needs to change to bring agents online. The job is to drive existing
commands and respect the guardrails, not to build new plumbing.

---

## 1. What the system does

Clipper turns long-form footage into short vertical clips and publishes them.
One hourly cron run does the whole cycle:

```
crawl     Clippo campaigns (JSON API via Playwright session) -> SQLite
process   fetch -> transcribe -> pick segments -> render -> upload to YouTube
sync      refresh view counts on published clips
submit    clips past 1000 views go back to Clippo in 10-slot batches
watchdog  route every failure to a bucket
```

Stack: Python 3.12, SQLite (WAL, single writer), yt-dlp, faster-whisper,
ffmpeg, Playwright, 9Router for all LLM calls. One VPS, one worker.

**There is no framework.** Every module is a plain file with a `__main__`
self-check. That is deliberate and should stay that way.

---

## 2. The split that matters

Two layers. Confusing them is the main way this integration goes wrong.

### Runtime layer — no agents, no LLM

`run.sh` on cron, `pipeline.py`, `watchdog.py`, `notify.py`. This layer already
works standalone. If Hermes is never installed, clips still get produced,
failures still get sorted, and a human still gets pinged.

**Do not put an agent in this loop.** Watching logs is a `grep` job. An LLM
polling logs every ten minutes burns tokens on deterministic work and — worse —
can report "all clear" over a real outage. The lookup table in `watchdog.py`
cannot hallucinate.

### Dev layer — this is where Hermes belongs

Agents read the failure queue, write fixes, and open PRs. They act on the repo,
never inside the hourly run.

---

## 3. Agent A — router / operator

**Model:** 9Router (cheap). **Job:** three things, only one of which needs a model.

| Task | Needs LLM? | How |
|---|---|---|
| Discord task message -> structured Task | **Yes** | free-text brief -> fields |
| Trigger an on-demand run | No | shell out to `run.sh` |
| Move failures to Agent B | No | `watchdog.py --tickets` |

### 3.1 Reading the failure queue

```bash
python watchdog.py --tickets
```

Returns JSON, already redacted:

```json
[
  {
    "fingerprint": "a3f9c1d2",
    "kind": "CODE_BUG",
    "stage": "EDITING",
    "error": "EDITING: RuntimeError: ffmpeg failed: no such filter",
    "task_id": 12,
    "seen": 3,
    "attempts": 0,
    "branch": "fix/a3f9c1d2"
  }
]
```

Empty array means nothing to do. That is the normal state — do not treat it as
an error, and do not go looking for work elsewhere.

`fingerprint` is `sha1(stage + first line with digits stripped)[:8]`. The digit
stripping means `ffmpeg failed on frame 129` and `frame 8842` are one incident,
not two. Use it as the branch name and the dedup key.

### 3.2 Handing work over and closing it

```python
import db, watchdog
conn = db.init_db()

watchdog.mark_attempt(conn, "a3f9c1d2")   # before Agent B starts
watchdog.close(conn, "a3f9c1d2")          # after the fix is merged
```

That is the entire contract. Two functions.

An incident that hits `CLIPPER_MAX_FIX_ATTEMPTS` (default 1) is escalated
automatically and stops appearing in `--tickets`. **Do not reset the counter to
retry.** A fix that failed is a conversation with a human, not a retry loop.

### 3.3 Reading tasks from Discord

Not built yet — the message format has not been seen. When it is:

`ClippoAdapter` in `clippo.py` defines the contract by example:
`authenticate` / `discover_tasks` / `check_feasibility` / `submit_clip`.
Build `DiscordAdapter` with the same four methods and register it:

```python
def active_adapters():
    return [ClippoAdapter(SESSION), DiscordAdapter(BOT_TOKEN, CHANNEL_ID)]
```

Everything downstream — dedup, the state machine, the segment allocator — then
works unchanged.

**Use the official Discord bot API.** Self-bots (user tokens) violate Discord's
ToS and get accounts banned, including the accounts used for Clippo
verification.

`fetch.ROUTES` needs a `"discord"` entry for CDN attachments —
`classify_source` already returns that string, but there is no downloader
behind it, so a Discord-hosted video currently raises `ValueError`.

### 3.4 What Agent A must NOT produce

**Do not have Agent A write titles or descriptions.** `metadata.py` already
generates `hook` / `title` / `description` / `youtube_tags` and — critically —
enforces campaign rules afterwards: mandatory hashtags are forced back in if
the model dropped them, `#Shorts` is appended, the title is capped at 100
characters.

An agent writing straight into the upload call skips that enforcement and
produces clips that silently violate campaign requirements.

Agent A supplies **requirements**; `metadata.py` produces the copy:

```python
Task(campaign_id=..., footage_urls=[...],
     requirements={"brief": ..., "hashtags": [...],
                   "platforms_required": [...], "duration_type": ...})
```

If a title really must come from Agent A, route it through
`metadata.generate` anyway so the enforcement still runs.

---

## 4. Agent B — developer

**Model:** Claude Max OAuth. **Job:** take a ticket, write the fix, open a PR.

### 4.1 Guardrails — all of them are load-bearing

Agent B writes code that runs hourly holding OAuth credentials for live YouTube
channels. One bad merge means a dead pipeline for an hour, or garbage published
to a real channel.

1. **Branch and PR, never push to `main`.** Branch name comes from the ticket:
   `fix/<fingerprint>`.
2. **Separate git worktree**, not the directory cron runs from.
3. **Merge gate — every self-check must pass:**
   ```bash
   for f in *.py; do python "$f" || exit 1; done
   python watchdog.py --selfcheck
   ```
   Every module runs offline with no credentials. That is deliberate — it is
   what makes this usable as a gate. Keep it true for any new module.
4. **One attempt per fingerprint.** Enforced by `mark_attempt`.
5. **Three tickets per day** (`CLIPPER_MAX_TICKETS_PER_DAY`). Past that,
   incidents are held and a human is pinged.
6. **Rollback:** tag the commit after each clean run. Two consecutive runs
   where every task fails should revert to that tag rather than stacking
   another fix on top.

### 4.2 Visual changes need a human, not a test

Self-checks cannot judge whether a subtitle sits in the right place. Split the
gate:

- **Logic** (`db`, `fetch`, `segments`, `upload_youtube`, `watchdog`, `notify`):
  green self-checks are enough to merge.
- **Visual** (`edit.py` — subtitle position, hook boxes, crop, blur):
  self-checks are necessary but never sufficient.

`edit.py`'s `__main__` already renders `smoke_split.mp4` and `smoke_clean.mp4`
(5 seconds each). Post both to Discord and require a human ✅ before merging.
No new code needed beyond attaching the files.

### 4.3 Which failures Agent B should never touch

Most failures in this system are **not code bugs**: expired YouTube cookies,
expired Clippo session, dead OAuth refresh token, daily upload quota, gdown
"too many accesses", 9Router unreachable.

`watchdog.py` already sorts these into `NEEDS_HUMAN` and `TRANSIENT`, and they
never reach `--tickets`. If Agent B is receiving them, the wiring is wrong —
it is reading the tasks table directly instead of the ticket queue. Fix the
wiring; do not teach the agent to handle them.

---

## 5. Security — read this one properly

Agent B has repo access and writes code. The realistic threat here is not disk
encryption, it is a token ending up in a PR diff or in a log posted to Discord.

**Rules:**

1. **Secrets live outside the repo.** `/etc/clipper/`, owned by a dedicated
   `clipper` user, directory `700`, files `600`. Every path is already an env
   var — nothing needs hardcoding.
2. **Agent B's worktree must not be able to read that directory.** Run agents
   as a different user. All self-checks pass without credentials, so the merge
   gate still works.
3. **Never disable redaction in `notify.py`.** It strips bearer tokens, refresh
   tokens, Google API keys, `ya29.` OAuth tokens, signed URL parameters and
   webhook URLs. `watchdog.open_tickets` redacts again before handing anything
   to an agent.
4. **`load_credentials` uses `pickle.load`.** Anyone who can write to the token
   directory gets arbitrary code execution, not just credential theft. The
   `700` permission is a hard requirement, not hygiene.
5. Bind the dashboard to `127.0.0.1` (already the default) and reach it over an
   SSH tunnel. It has no authentication.

**Never store credentials in:** the repo, agent prompts, ticket payloads,
Discord messages, or commit messages.

---

## 6. Environment

`.env` next to `pipeline.py`, or `EnvironmentFile=` in a systemd unit.

### Required

| Variable | Purpose |
|---|---|
| `NINEROUTER_BASE_URL` | 9Router endpoint — every LLM call goes through it |
| `NINEROUTER_API_KEY` | 9Router key |
| `CLIPPO_SESSION` | Playwright `storage_state` JSON for Clippo |
| `CLIPPER_TOKEN_DIR` | directory of `token_<account>.pickle` YouTube credentials |
| `CLIPPER_DISCORD_WEBHOOK` | alert target; unset means alerts only reach the log |

### Worth setting

| Variable | Default | Notes |
|---|---|---|
| `NINEROUTER_MODEL` | `ds/deepseek-v4-pro` | |
| `CLIPPER_YT_COOKIES` | `./cookies.txt` | **without it YouTube serves 360p** |
| `CLIPPER_DB` | `./clipper.sqlite` | |
| `CLIPPER_MEDIA` | `./media` | downloaded footage |
| `CLIPPER_OUT` | `./out` | rendered clips, deleted after upload |
| `CLIPPER_CLIPS_PER_TASK` | `2` | |
| `CLIPPER_TASKS_PER_RUN` | `1` | |
| `CLIPPER_WHISPER_MODEL` | `small` | |
| `CLIPPER_MAX_FIX_ATTEMPTS` | `1` | attempts per fingerprint before escalation |
| `CLIPPER_MAX_TICKETS_PER_DAY` | `3` | dev-agent ticket cap |
| `CLIPPER_YT_ACCOUNT` | `Test` | fallback when category detection fails |

Render and dashboard knobs (`CLIPPER_FFMPEG`, `CLIPPER_CODEC`,
`CLIPPER_PRESET`, `CLIPPER_BITRATE`, `CLIPPER_FPS`, `CLIPPER_THREADS`,
`CLIPPER_BG_DIR`, `CLIPPER_BGM_DIR`, `CLIPPER_DASH_HOST`, `CLIPPER_DASH_PORT`)
have working defaults.

---

## 7. VPS setup

```bash
python preflight.py     # ffmpeg, fonts, 9Router, cookies, session,
                        # plus transcribe and render speed against the hour budget
```

`preflight.py` must pass before cron is enabled. It measures the two stages
whose cost decides whether an hourly run fits.

```
0 * * * * /opt/clipper/run.sh
```

`run.sh` takes `flock` — a run that overruns the hour is skipped, not queued.
Two workers uploading at once would double-post. It also does
`git pull --ff-only`, which is how a merged fix reaches production.

---

## 8. Account registry — a gate, not a dashboard

`accounts.py` and `dashboard.py` (FastAPI, port 8000) manage social accounts
through `new -> warming -> farming -> campaign_ready`, with `paused` / `banned`
as manual off-ramps.

**This registry gates uploads.** `upload_youtube.eligible_accounts()` admits an
account only when:

- a `token_<name>.pickle` exists, **and**
- the registry holds the same name as a `campaign_ready` YouTube account, **and**
- it is under its `daily_post_limit` for today.

**The registry username must equal the token filename.** `token_Gaming.pickle`
needs a registry row with username `Gaming`. An account with a token but no
registry row never receives clips — that is intentional.

`accounts.evaluate()` also flags a likely shadowban (≤5 average views over 3+
clips) and suggests `paused`. Suggestions are applied by a human in the
dashboard, never automatically — a bad stat sync must not be able to promote an
account into campaign work.

Clippo's 6-digit bio-code verification is manual, once per account. It cannot
be automated; the bio has to be edited by hand.

---

## 9. Known gaps

Not bugs — unbuilt work, listed so nobody rediscovers them as surprises.

| Gap | Impact |
|---|---|
| `DiscordAdapter` missing | tasks only come from Clippo |
| `fetch.ROUTES["discord"]` missing | Discord CDN footage raises `ValueError` |
| `platforms_required` not gated | Clippo campaigns want TikTok/IG; the pipeline publishes YouTube, so submissions get rejected. **Product decision, not a code fix.** |
| `duration_type` ignored | `DURATION_RANGES` is hardcoded per platform |
| Brief compliance is substring matching | `_wants_clean_mode` matches fixed Indonesian phrases; a reworded brief slips through. Better: one LLM compliance pass per campaign at crawl time, cached into `requirements_json`. |
| No TikTok / Instagram uploader | both need weeks of platform app review before a single public post |
| Clips stranded on `FAILED` tasks | files stay in `out/`; harmless, no automatic retry reaches them |

---

## 10. Ground rules

- **No framework.** Plain modules, `__main__` self-checks, no test runner, no
  DI container, no plugin registry.
- **Every new module runs offline with no credentials**, or it breaks the merge
  gate.
- **Deterministic before model.** If a lookup table can decide it, a lookup
  table decides it.
- **Agents never touch the runtime loop.** They act on the repo.
- **`ponytail:` comments mark deliberate shortcuts** and name the upgrade path.
  Read them before "improving" the code they sit on.
