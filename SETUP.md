# Setup guide

Everything needed to take Clipper from an empty VPS to publishing, and where
each credential comes from.

Two halves: what a machine can do, and what only a person can do. Hand the
first half to Hermes, keep the second.

---

## 1. What Hermes can install, and what it cannot

`install.sh` handles the whole machine side. It is idempotent — safe to re-run.

```bash
sudo bash install.sh
```

It installs packages (ffmpeg, fonts, nginx, cron), creates the `clipper`
service user, clones the repo to `/opt/clipper`, builds the virtualenv, creates
`/etc/clipper` at mode 700 with a generated connect key, writes both systemd
units and the cron entry, and drops an nginx site.

**What it cannot do, and why:**

| Step | Why a person has to do it |
|---|---|
| Register apps on TikTok / Meta / Google | requires a logged-in human in a console, plus business details |
| Grant OAuth consent for each account | a consent screen is a human decision by design |
| Export YouTube `cookies.txt` | comes from a logged-in browser session |
| Create `clippo_session.json` | Clippo login, possibly with a captcha |
| Point DNS at the VPS, run certbot | domain ownership |
| Verify the Clippo 6-digit bio code | edit the account bio by hand, once per account |

So Hermes gets to a running box with empty credential slots. Filling them is
yours.

### Instructing an agent

```
Run `sudo bash install.sh` on the VPS. It is idempotent, so re-run it rather
than fixing things by hand. Do not create, edit, or read anything inside
/etc/clipper beyond what the script does. When it finishes, report the output
of the "Next, by hand" block and stop — the remaining steps need credentials
you do not have.
```

Keep the agent out of `/etc/clipper`. It has repo access, and a token that
reaches a log or a diff is the failure mode that matters here.

---

## 2. Where every credential comes from

### 9Router — `NINEROUTER_BASE_URL`, `NINEROUTER_API_KEY`

Your existing 9Router instance. Every LLM call in the pipeline goes through it:
segment selection, metadata, category routing. Nothing works without it.

Check it from the VPS before anything else:

```bash
curl -H "Authorization: Bearer $NINEROUTER_API_KEY" $NINEROUTER_BASE_URL/models
```

### Discord webhook — `CLIPPER_DISCORD_WEBHOOK`

Server Settings → Integrations → Webhooks → New Webhook → pick a channel →
Copy Webhook URL.

This is the alert channel, not the task-reading bot. Unset means failures only
reach the log file.

### YouTube — `client_secrets.json`

1. [Google Cloud Console](https://console.cloud.google.com) → create a project.
2. APIs & Services → Library → enable **YouTube Data API v3**.
3. OAuth consent screen → External. Add yourself as a test user. Scopes:
   `youtube.upload` and `youtube.readonly`.
4. Credentials → Create credentials → **OAuth client ID** → Web application.
5. Authorised redirect URI: `https://your.domain/oauth/youtube/callback`
6. Download the JSON → `/etc/clipper/client_secrets.json`, mode 600.

One project covers every channel. Each channel is then connected separately at
`/oauth/youtube/start`, which writes `token_<name>.pickle`.

**The name matters twice.** It is the token filename *and* the category the
clip router picks between, so use the channel's niche — `Gaming`, `Podcast` —
and add a matching row with the same username in the accounts dashboard.
Without that row the channel never receives clips.

Quota: 10,000 units/day, and an upload costs ~1,600. That is about **6 uploads
per day per project**, not per channel. More than that needs a quota increase
request or a second project.

### YouTube cookies — `cookies.txt`

Without cookies YouTube serves at most 360p, and clips built from that look
soft once upscaled to 1080×1920.

Use a cookies.txt browser extension in a **private window**, export, close the
window and never reopen it — YouTube rotates cookies on live sessions, which
invalidates the export within days. Put it at `/etc/clipper/cookies.txt`.

On a flagged IP (most VPS ranges are), also run the PO token provider:

```bash
pip install -U bgutil-ytdlp-pot-provider
docker run -d --init -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider
```

It hooks into yt-dlp automatically. A PO token alone does **not** unlock higher
resolutions — it only stops requests being rejected. A flagged IP needs both.

### Clippo — `clippo_session.json`

A Playwright `storage_state` dump from a logged-in Clippo session. Log in
locally, save the storage state, copy it to `/etc/clipper/clippo_session.json`.

It expires. When it does, `crawl` fails, `watchdog` classifies it as
`NEEDS_HUMAN`, and you get a Discord ping naming this file.

### TikTok — `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`

1. [developer.tiktok.com](https://developers.tiktok.com) → register → create an app.
2. Add the **Content Posting API** product.
3. Add the **Login Kit** product; scopes `user.info.basic`, `user.info.profile`,
   `video.upload`, `video.publish`.
4. Redirect URI: `https://your.domain/oauth/tiktok/callback`
5. Terms of Service URL: `https://your.domain/terms`
   Privacy Policy URL: `https://your.domain/privacy`
   (both served by `connect.py` — no separate hosting needed)
6. Copy the client key and secret into `/etc/clipper/env`.

**One app, many creator accounts.** Each account is connected separately and
lands as `tiktok_<username>.json`.

Steps 1–6 take about a day. The audit does not — see §3.

### Instagram — `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`

Harder than TikTok, per account.

1. The Instagram account must be **Business**. Creator accounts cannot publish
   through the API. Convert it in the Instagram app first.
2. [developers.facebook.com](https://developers.facebook.com) → create an app →
   add the **Instagram** product with Instagram Login.
3. Redirect URI: `https://your.domain/oauth/instagram/callback`
4. Permissions: `instagram_business_basic`, `instagram_business_content_publish`.
5. App ID and secret go into `/etc/clipper/env`.

App Review is required for those permissions, one submission each, each with a
screencast. Budget 2–4 weeks. Advanced Access additionally needs Business
Verification.

Long-lived tokens last 60 days. The uploader renews inside the final week
automatically, but an account that sits idle past expiry has to be reconnected
by hand — Meta cannot renew a lapsed token.

### Public hosting — `CLIPPER_PUBLIC_BASE`

Instagram has no file upload: Meta's servers fetch the video from a URL. The
nginx site from `install.sh` already serves `/opt/clipper/out/` at `/clips/`,
so this is `https://your.domain/clips`. The pipeline deletes each file after a
confirmed upload, so nothing lingers.

### Connect key — `CLIPPER_CONNECT_KEY`

Generated by `install.sh`. Read it back with:

```bash
sudo grep CONNECT_KEY /etc/clipper/env
```

Anyone who reaches `/oauth/*/start` can write a credential file into the token
directory, so every connect URL carries `?key=…`. Unset means the flow is
closed rather than open. Treat it like a password.

---

## 3. TikTok audit — the long pole

Draft posting works immediately. **Direct posting does not** — until the
Content Posting audit passes, TikTok forces every direct post to `SELF_ONLY`,
so the clip earns no views. `CLIPPER_TIKTOK_DIRECT` stays unset until approval,
and `upload(direct=True)` refuses with that explanation.

Submitting needs:

- a public privacy policy URL — `https://your.domain/privacy` ✓
- a public terms URL — `https://your.domain/terms` ✓
- **a screen recording of the full posting flow** — record `/post`
- evidence the integration lives inside a finished product

`/post` exists to satisfy the UX rules the reviewer checks: the creator's own
nickname and avatar, a privacy selector built from the levels their account
actually offers, interaction toggles that stay locked when the account disables
them, and commercial content disclosure with the branded-content and music
links.

Record the whole flow: pick account → pick clip → write caption → choose
privacy → set toggles → disclose → post.

Expect 2–4 weeks and several rounds. Submit early; it runs in parallel with
everything else.

**Meanwhile, use draft mode.** Render, transcription, hook and metadata stay
automatic; only the final tap is manual. That is worth having on day one.

---

## 4. Order of operations

Nothing here blocks the next item, so run them in parallel where you can.

**Day one**
1. `sudo bash install.sh`
2. Point DNS at the VPS, set `server_name`, `certbot --nginx -d your.domain`
3. Fill in 9Router and the Discord webhook in `/etc/clipper/env`
4. Register the TikTok app and **submit the audit** — longest lead time
5. Create the social accounts, register them in the dashboard, start warm-up

**Once the domain resolves**
6. `client_secrets.json` → `/etc/clipper/`, connect each YouTube channel
7. `cookies.txt` and `clippo_session.json` → `/etc/clipper/`
8. `sudo -u clipper /opt/clipper/.venv/bin/python /opt/clipper/preflight.py`

**As approvals land**
9. TikTok audit passes → `CLIPPER_TIKTOK_DIRECT=1`
10. Meta App Review passes → connect Instagram Business accounts

Warm-up runs alongside all of it: 3 days for TikTok and YouTube, 7 for
Instagram. Accounts sit at `warming` and cannot receive clips until it elapses
and the dashboard promotes them.

---

## 5. Verifying

```bash
sudo -u clipper /opt/clipper/.venv/bin/python /opt/clipper/preflight.py
```

Checks ffmpeg, fonts, 9Router, cookies and session, then measures transcription
and render speed against the hour budget. Must pass before you trust the cron.

```bash
cd /opt/clipper && for f in *.py; do .venv/bin/python "$f" || break; done
.venv/bin/python watchdog.py --selfcheck
.venv/bin/python connect.py --selfcheck
```

All self-checks run offline with no credentials. That is what makes them usable
as an agent merge gate.

```bash
sudo -u clipper /opt/clipper/.venv/bin/python /opt/clipper/pipeline.py --submit-only --dry-run
```

Exercises the Clippo submission form without touching a real campaign.

```bash
systemctl status clipper-connect clipper-dashboard
tail -f /opt/clipper/clipper.log
sudo grep CONNECT_KEY /etc/clipper/env
```

The dashboard binds to `127.0.0.1:8000` with no authentication — reach it over
an SSH tunnel, never expose it:

```bash
ssh -L 8000:127.0.0.1:8000 you@your.domain
```

---

## 6. What is ready and what is waiting

| Platform | Publishing | Blocked by |
|---|---|---|
| YouTube | fully automatic | nothing |
| TikTok | draft — one manual tap per clip | Content Posting audit |
| Instagram | ready in code | App Review, Business account, public hosting |

`pipeline.py` still publishes to YouTube only (`PLATFORM = "youtube"`). The
registry accepts all three, but nothing routes a campaign to TikTok or
Instagram yet — that waits on the `platforms_required` decision, since Clippo
campaigns ask for TikTok and Instagram while the pipeline currently produces
YouTube.

For agent contracts and guardrails, see [HERMES.md](HERMES.md).
