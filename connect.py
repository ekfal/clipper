"""Connect app — OAuth onboarding plus the screens TikTok's audit requires.

    python connect.py     ->  http://127.0.0.1:8001

Four jobs, all of them prerequisites for auto-posting rather than nice extras:

  /                    link an account, see which are connected
  /oauth/<p>/callback  exchange the code, write tiktok_<n>.json / instagram_<n>.json
  /privacy, /terms     public URLs both TikTok and Meta demand at review
  /post                the posting screen the TikTok audit checks

TikTok will not approve direct posting unless the surface a creator posts
from shows their own nickname and avatar, lets them choose a privacy level,
honours the interaction toggles their account disables, and offers commercial
disclosure. /post is that surface — it is what you record for the submission.

Runs behind nginx with TLS: OAuth redirect URIs must be public HTTPS, but the
app itself binds to localhost.

    location / { proxy_pass http://127.0.0.1:8001; }
"""
import os
import secrets
import time
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import upload_instagram
import upload_tiktok
import upload_youtube

app = FastAPI(title="Clipper connect")
HOST = os.environ.get("CLIPPER_CONNECT_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLIPPER_CONNECT_PORT", "8001"))
# Public origin the platforms redirect back to; must match the app settings.
PUBLIC_ORIGIN = os.environ.get("CLIPPER_CONNECT_ORIGIN", f"http://{HOST}:{PORT}").rstrip("/")
OUT_DIR = os.environ.get("CLIPPER_OUT", os.path.join(os.path.dirname(__file__), "out"))
BRAND = os.environ.get("CLIPPER_BRAND", "Clipper")
CONTACT = os.environ.get("CLIPPER_CONTACT", "")

TIKTOK_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "")
TIKTOK_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")
TIKTOK_SCOPES = "user.info.basic,user.info.profile,video.upload,video.publish"
IG_ID = os.environ.get("INSTAGRAM_APP_ID", "")
IG_SECRET = os.environ.get("INSTAGRAM_APP_SECRET", "")
IG_SCOPES = "instagram_business_basic,instagram_business_content_publish"
# YouTube uses the Google client-secrets file rather than a key pair in env.
CLIENT_SECRETS = os.environ.get(
    "CLIPPER_CLIENT_SECRETS", os.path.join(os.path.dirname(__file__), "client_secrets.json"))
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
             "https://www.googleapis.com/auth/youtube.readonly"]

# Anyone who reaches /oauth/*/start can write a credential file into the token
# directory, so the flow is gated. Unset means the connect flow is closed —
# failing shut is the only safe default for an endpoint that stores tokens.
CONNECT_KEY = os.environ.get("CLIPPER_CONNECT_KEY", "")

# state -> (platform, created_at). CSRF protection: a callback whose state we
# did not issue is rejected outright.
_PENDING = {}
STATE_TTL = 600


def _esc(v):
    return ("" if v is None else str(v)).replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def _issue_state(platform, extra=None):
    now = time.time()
    for k, (_, born, _x) in list(_PENDING.items()):
        if now - born > STATE_TTL:
            del _PENDING[k]
    state = secrets.token_urlsafe(24)
    _PENDING[state] = (platform, now, extra)
    return state


def _take_state(state, platform):
    """One-shot check: a state is valid once, for the platform that issued it.

    Validated before it is consumed — popping first would let a callback for
    the wrong platform destroy a legitimate pending state.
    """
    got = _PENDING.get(state)
    if not got or got[0] != platform or time.time() - got[1] > STATE_TTL:
        raise ValueError("invalid or expired state")
    del _PENDING[state]
    return got[2]


def _gate(key):
    if not CONNECT_KEY:
        raise PermissionError("CLIPPER_CONNECT_KEY is unset — the connect flow is closed")
    if not secrets.compare_digest(key or "", CONNECT_KEY):
        raise PermissionError("wrong connect key")


def redirect_uri(platform):
    return f"{PUBLIC_ORIGIN}/oauth/{platform}/callback"


# ---------------------------------------------------------------- shell

CSS = """
:root{--bg:#f4f6f5;--fg:#15201e;--mut:#5f6f6b;--line:#d6dcda;--card:#fff;--acc:#0c5a4e}
@media(prefers-color-scheme:dark){:root{--bg:#101413;--fg:#e6eae8;--mut:#93a19d;
--line:#28312f;--card:#171d1c;--acc:#54d0b5}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:1.6rem;margin:0 0 6px}h2{font-size:1.1rem;margin:32px 0 10px}
.sub{color:var(--mut);margin:0 0 28px}
.card{background:var(--card);border:1px solid var(--line);padding:18px;margin:0 0 16px}
.row{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.av{width:48px;height:48px;border-radius:50%;object-fit:cover;background:var(--line);flex:none}
label{display:block;font-size:.82rem;color:var(--mut);margin:14px 0 4px}
input[type=text],select{width:100%;padding:9px 10px;background:var(--bg);
color:var(--fg);border:1px solid var(--line);font:inherit}
.check{display:flex;gap:9px;align-items:flex-start;margin:9px 0;font-size:.92rem}
.check input{margin-top:5px;flex:none}
.check.off{opacity:.55}
button{background:var(--acc);color:var(--bg);border:0;padding:10px 18px;
font:inherit;font-weight:600;cursor:pointer;margin-top:18px}
button.sec{background:transparent;color:var(--acc);border:1px solid var(--acc)}
a{color:var(--acc)}
.msg{border-left:3px solid var(--acc);padding:10px 14px;margin:0 0 18px;background:var(--card)}
.msg.bad{border-color:#c0392b}
.note{font-size:.85rem;color:var(--mut)}
code{font-family:ui-monospace,Consolas,monospace;font-size:.85em}
table{width:100%;border-collapse:collapse;font-size:.92rem}
td,th{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500;font-size:.78rem;text-transform:uppercase}
"""


def page(title, body):
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{_esc(title)} · {_esc(BRAND)}</title><style>{CSS}</style></head>'
            f'<body><div class="wrap">{body}</div></body></html>')


def _msg(text, bad=False):
    return f'<div class="msg{" bad" if bad else ""}">{_esc(text)}</div>' if text else ""


# ---------------------------------------------------------------- connect

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    key = request.query_params.get("key", "")
    rows = ""
    for platform, mod in (("youtube", upload_youtube), ("tiktok", upload_tiktok),
                          ("instagram", upload_instagram)):
        for name in mod.available_accounts():
            rows += f"<tr><td>{_esc(platform)}</td><td>{_esc(name)}</td></tr>"
    rows = rows or '<tr><td colspan="2" class="note">nothing connected yet</td></tr>'

    forms = ""
    MISSING = {"youtube": f"a client_secrets.json at {CLIENT_SECRETS}",
               "tiktok": "TIKTOK_CLIENT_KEY", "instagram": "INSTAGRAM_APP_ID"}
    for platform, configured in (("youtube", os.path.exists(CLIENT_SECRETS)),
                                 ("tiktok", bool(TIKTOK_KEY)),
                                 ("instagram", bool(IG_ID))):
        if configured:
            extra = ('<input type="text" name="name" placeholder="channel name, e.g. Gaming">'
                     if platform == "youtube" else "")
            forms += (f'<form method="get" action="/oauth/{platform}/start">'
                      f'<input type="hidden" name="key" value="{_esc(key)}">{extra}'
                      f'<button>Connect {platform.title()}</button></form>')
        else:
            forms += (f'<p class="note">{platform.title()}: needs '
                      f'<code>{_esc(MISSING[platform])}</code> first.</p>')

    return page("Connect accounts", f"""
<h1>Connect accounts</h1>
<p class="sub">Link a creator account so {_esc(BRAND)} can publish on its behalf.
You can disconnect at any time from the app's settings on the platform itself.</p>
{_msg(request.query_params.get("msg", ""), request.query_params.get("bad") == "1")}
<div class="card"><h2 style="margin-top:0">Connected</h2>
<table><tr><th>Platform</th><th>Account</th></tr>{rows}</table></div>
<div class="card"><div class="row">{forms}</div>
<p class="note" style="margin-top:16px">Redirect URIs to register with each platform:<br>
<code>{_esc(redirect_uri("youtube"))}</code><br>
<code>{_esc(redirect_uri("tiktok"))}</code><br>
<code>{_esc(redirect_uri("instagram"))}</code></p></div>
<p class="note"><a href="/privacy">Privacy policy</a> · <a href="/terms">Terms</a>
· <a href="/post?key={_esc(key)}">Post a clip</a></p>""")


@app.get("/oauth/tiktok/start")
def tiktok_start(request: Request):
    try:
        _gate(request.query_params.get("key", ""))
    except PermissionError as e:
        return _home(str(e), bad=True)
    q = urlencode({"client_key": TIKTOK_KEY, "scope": TIKTOK_SCOPES,
                   "response_type": "code", "redirect_uri": redirect_uri("tiktok"),
                   "state": _issue_state("tiktok")})
    return RedirectResponse(f"https://www.tiktok.com/v2/auth/authorize/?{q}", status_code=303)


@app.get("/oauth/tiktok/callback")
def tiktok_callback(request: Request):
    p = request.query_params
    if p.get("error"):
        return _home(f"TikTok declined: {p.get('error_description') or p['error']}", bad=True)
    try:
        _take_state(p.get("state", ""), "tiktok")
    except ValueError as e:
        return _home(str(e), bad=True)

    res = requests.post("https://open.tiktokapis.com/v2/oauth/token/", timeout=60,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        data={"client_key": TIKTOK_KEY, "client_secret": TIKTOK_SECRET,
                              "code": p.get("code", ""), "grant_type": "authorization_code",
                              "redirect_uri": redirect_uri("tiktok")})
    body = res.json()
    if not body.get("access_token"):
        return _home(f"token exchange failed: {body.get('error_description') or body.get('error')}",
                     bad=True)

    info = requests.get("https://open.tiktokapis.com/v2/user/info/", timeout=60,
                        headers={"Authorization": f"Bearer {body['access_token']}"},
                        params={"fields": "open_id,display_name,avatar_url,username"})
    user = (info.json().get("data") or {}).get("user") or {}
    name = user.get("username") or user.get("display_name") or body.get("open_id", "account")
    name = "".join(c for c in name if c.isalnum() or c in "-_") or "account"

    upload_tiktok._save(name, {
        "client_key": TIKTOK_KEY, "client_secret": TIKTOK_SECRET,
        "access_token": body["access_token"], "refresh_token": body.get("refresh_token"),
        "expires_at": time.time() + int(body.get("expires_in", 86400)),
        "open_id": body.get("open_id"), "display_name": user.get("display_name"),
        "avatar_url": user.get("avatar_url"),
    })
    return _home(f"TikTok account {name} connected")


@app.get("/oauth/instagram/start")
def instagram_start(request: Request):
    try:
        _gate(request.query_params.get("key", ""))
    except PermissionError as e:
        return _home(str(e), bad=True)
    q = urlencode({"client_id": IG_ID, "redirect_uri": redirect_uri("instagram"),
                   "scope": IG_SCOPES, "response_type": "code",
                   "state": _issue_state("instagram")})
    return RedirectResponse(f"https://www.instagram.com/oauth/authorize?{q}", status_code=303)


@app.get("/oauth/instagram/callback")
def instagram_callback(request: Request):
    p = request.query_params
    if p.get("error"):
        return _home(f"Instagram declined: {p.get('error_description') or p['error']}", bad=True)
    try:
        _take_state(p.get("state", ""), "instagram")
    except ValueError as e:
        return _home(str(e), bad=True)

    short = requests.post("https://api.instagram.com/oauth/access_token", timeout=60,
                          data={"client_id": IG_ID, "client_secret": IG_SECRET,
                                "grant_type": "authorization_code",
                                "redirect_uri": redirect_uri("instagram"),
                                "code": p.get("code", "")}).json()
    if not short.get("access_token"):
        return _home(f"token exchange failed: {short.get('error_message') or short.get('error')}",
                     bad=True)

    # The short-lived token lasts an hour and cannot be renewed; only the
    # long-lived one is worth storing.
    long = requests.get(f"{upload_instagram.GRAPH}/access_token", timeout=60,
                        params={"grant_type": "ig_exchange_token",
                                "client_secret": IG_SECRET,
                                "access_token": short["access_token"]}).json()
    token = long.get("access_token")
    if not token:
        return _home(f"long-lived exchange failed: {long.get('error')}", bad=True)

    me = requests.get(f"{upload_instagram.GRAPH}/me", timeout=60,
                      params={"fields": "user_id,username", "access_token": token}).json()
    name = me.get("username") or str(short.get("user_id", "account"))
    name = "".join(c for c in name if c.isalnum() or c in "-_.") or "account"

    upload_instagram._save(name, {
        "access_token": token,
        "ig_user_id": me.get("user_id") or short.get("user_id"),
        "expires_at": time.time() + int(long.get("expires_in", 60 * 86400)),
        "username": me.get("username"),
    })
    return _home(f"Instagram account {name} connected")


def _home(msg="", bad=False):
    q = urlencode({"msg": msg, "bad": "1" if bad else ""})
    return RedirectResponse(f"/?{q}", status_code=303)


def _safe_name(raw, fallback="account"):
    return "".join(c for c in (raw or "") if c.isalnum() or c in "-_") or fallback


def _yt_flow():
    """Google's web OAuth flow for one YouTube channel."""
    from google_auth_oauthlib.flow import Flow
    if not os.path.exists(CLIENT_SECRETS):
        raise FileNotFoundError(
            f"{CLIENT_SECRETS} not found — download the OAuth client JSON from "
            f"Google Cloud Console and put it there")
    return Flow.from_client_secrets_file(CLIENT_SECRETS, scopes=YT_SCOPES,
                                         redirect_uri=redirect_uri("youtube"))


@app.get("/oauth/youtube/start")
def youtube_start(request: Request):
    """Mint a token_<name>.pickle for one channel.

    `name` matters: it is both the token filename and the category the clip
    router picks between, so pass the channel's niche ("Gaming", "Podcast"),
    not a display name. It also has to match the accounts registry username.
    """
    try:
        _gate(request.query_params.get("key", ""))
        flow = _yt_flow()
    except (PermissionError, FileNotFoundError) as e:
        return _home(str(e), bad=True)
    name = _safe_name(request.query_params.get("name", ""), "")
    url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true",
        prompt="consent",  # without it Google withholds the refresh token on re-auth
        state=_issue_state("youtube", name))
    return RedirectResponse(url, status_code=303)


@app.get("/oauth/youtube/callback")
def youtube_callback(request: Request):
    import pickle

    p = request.query_params
    if p.get("error"):
        return _home(f"Google declined: {p['error']}", bad=True)
    try:
        chosen = _take_state(p.get("state", ""), "youtube")
        flow = _yt_flow()
        flow.fetch_token(code=p.get("code", ""))
    except Exception as e:
        return _home(f"youtube auth failed: {type(e).__name__}: {e}", bad=True)

    creds = flow.credentials
    if not creds.refresh_token:
        return _home("Google returned no refresh token — revoke the app under "
                     "your Google account's third-party access and connect again",
                     bad=True)
    name = chosen
    if not name:
        try:
            from googleapiclient.discovery import build
            yt = build("youtube", "v3", credentials=creds)
            items = yt.channels().list(part="snippet", mine=True).execute().get("items")
            name = _safe_name(items[0]["snippet"]["title"] if items else "", "")
        except Exception:
            name = ""
    name = name or "account"

    os.makedirs(upload_youtube.TOKEN_DIR, exist_ok=True)
    path = os.path.join(upload_youtube.TOKEN_DIR, f"token_{name}.pickle")
    with open(path, "wb") as f:
        pickle.dump(creds, f)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return _home(f"YouTube channel saved as {name} — add a matching "
                 f"'{name}' row in the accounts dashboard so it can receive clips")


# ---------------------------------------------------------------- posting

def clips_available():
    if not os.path.isdir(OUT_DIR):
        return []
    return sorted(f for f in os.listdir(OUT_DIR) if f.endswith(".mp4"))


@app.get("/post", response_class=HTMLResponse)
def post_form(request: Request):
    """The posting surface TikTok's audit inspects.

    Every element here is a review requirement, not decoration: the creator's
    own nickname and avatar, a privacy choice they make themselves, toggles
    that respect what their account disables, and commercial disclosure.
    """
    key = request.query_params.get("key", "")
    account = request.query_params.get("account", "")
    accts = upload_tiktok.available_accounts()
    if not accts:
        return HTMLResponse(page("Post", "<h1>Post a clip</h1>"
                                 "<p class='sub'>No TikTok account connected yet. "
                                 "<a href='/'>Connect one first.</a></p>"))
    account = account if account in accts else accts[0]

    try:
        info = upload_tiktok.creator_info(upload_tiktok.load_credentials(account))
    except Exception as e:
        info, err = {}, f"could not load creator info: {type(e).__name__}: {e}"
    else:
        err = ""

    nickname = info.get("creator_nickname") or account
    avatar = info.get("creator_avatar_url") or ""
    levels = info.get("privacy_level_options") or ["SELF_ONLY"]
    max_sec = info.get("max_video_post_duration_sec")

    opts = "".join(f'<option value="{_esc(l)}">{_esc(l.replace("_", " ").title())}</option>'
                   for l in levels)
    picker = "".join(f'<option value="{_esc(a)}"{" selected" if a == account else ""}>'
                     f'{_esc(a)}</option>' for a in accts)
    clips = "".join(f'<option value="{_esc(c)}">{_esc(c)}</option>'
                    for c in clips_available()) or '<option value="">no clips in out/</option>'

    def toggle(name, label):
        off = bool(info.get(name.replace("disable_", "") + "_disabled"))
        return (f'<div class="check{" off" if off else ""}">'
                f'<input type="checkbox" name="{name}" value="1"'
                f'{" checked disabled" if off else ""}>'
                f'<span>{_esc(label)}'
                + ('<br><span class="note">turned off on this account</span>' if off else "")
                + '</span></div>')

    return page("Post a clip", f"""
<h1>Post a clip</h1>
<p class="sub">Review what will be published, then post it yourself.</p>
{_msg(request.query_params.get("msg", ""), request.query_params.get("bad") == "1")}
{_msg(err, True) if err else ""}
<form method="post" action="/post">
<input type="hidden" name="key" value="{_esc(key)}">
<div class="card"><div class="row">
  <img class="av" src="{_esc(avatar)}" alt="">
  <div><strong>{_esc(nickname)}</strong><br>
  <span class="note">posting to TikTok as @{_esc(account)}</span></div>
</div>
<label for="account">Post as</label>
<select id="account" name="account" onchange="location='/post?key={_esc(key)}&account='+this.value">
{picker}</select></div>

<div class="card">
<label for="clip">Clip</label><select id="clip" name="clip">{clips}</select>
<label for="caption">Caption</label>
<input type="text" id="caption" name="caption" maxlength="2200" placeholder="Write a caption">
{f'<p class="note">This account accepts clips up to {max_sec}s.</p>' if max_sec else ""}
</div>

<div class="card">
<label for="privacy">Who can view this video</label>
<select id="privacy" name="privacy" required>{opts}</select>
<h2>Allow users to</h2>
{toggle("disable_comment", "Comment")}
{toggle("disable_duet", "Duet")}
{toggle("disable_stitch", "Stitch")}
<p class="note">Ticking a box turns that interaction off.</p>
</div>

<div class="card">
<h2 style="margin-top:0">Disclose video content</h2>
<div class="check"><input type="checkbox" name="brand_organic_toggle" value="1">
<span>Your brand — promoting yourself or your own business</span></div>
<div class="check"><input type="checkbox" name="brand_content_toggle" value="1">
<span>Branded content — promoting another brand or a third party.
<br><span class="note">Branded content cannot be posted privately.</span></span></div>
<p class="note">By posting, you agree to TikTok's
<a href="https://www.tiktok.com/legal/page/global/bc-policy/en" rel="noopener"
target="_blank">Branded Content Policy</a> and
<a href="https://www.tiktok.com/legal/page/global/music-usage-confirmation/en"
rel="noopener" target="_blank">Music Usage Confirmation</a>.</p>
</div>

<button type="submit">Post to TikTok</button>
</form>""")


@app.post("/post")
def post_submit(key: str = Form(""), account: str = Form(...), clip: str = Form(...),
                caption: str = Form(""), privacy: str = Form(...),
                disable_comment: str = Form(""), disable_duet: str = Form(""),
                disable_stitch: str = Form(""), brand_organic_toggle: str = Form(""),
                brand_content_toggle: str = Form("")):
    try:
        _gate(key)
    except PermissionError as e:
        return RedirectResponse(f"/post?bad=1&msg={_esc(str(e))}", status_code=303)

    path = os.path.join(OUT_DIR, os.path.basename(clip))
    if not clip or not os.path.exists(path):
        return RedirectResponse("/post?bad=1&msg=clip+not+found", status_code=303)
    try:
        res = upload_tiktok.upload(
            None, path, {"description": caption}, account=account,
            direct=True, privacy=privacy,
            options={"disable_comment": bool(disable_comment),
                     "disable_duet": bool(disable_duet),
                     "disable_stitch": bool(disable_stitch),
                     "brand_organic_toggle": bool(brand_organic_toggle),
                     "brand_content_toggle": bool(brand_content_toggle)})
    except Exception as e:
        return RedirectResponse(f"/post?key={key}&bad=1&msg={_esc(str(e)[:200])}",
                                status_code=303)
    return RedirectResponse(f"/post?key={key}&msg=posted+{_esc(res['publish_id'])}",
                            status_code=303)


# ---------------------------------------------------------------- legal

def _legal(title, body):
    return page(title, f"<h1>{_esc(title)}</h1>{body}"
                       f'<p class="note"><a href="/">Back</a></p>')


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    contact = (f"<p>Questions: <a href='mailto:{_esc(CONTACT)}'>{_esc(CONTACT)}</a></p>"
               if CONTACT else "")
    return _legal("Privacy policy", f"""
<p class="note">Last updated: {time.strftime('%d %B %Y')}</p>
<p>{_esc(BRAND)} publishes short videos to social accounts on behalf of the
person who connects them. This policy covers what that involves.</p>
<h2>What is collected</h2>
<ul>
<li><strong>Account access tokens</strong> issued by TikTok and Instagram when
you connect an account, so videos can be published on your behalf.</li>
<li><strong>Basic profile details</strong> — username, display name, avatar —
shown so you can confirm which account you are posting to.</li>
<li><strong>Publishing records</strong> — which video went to which account and
its resulting view count.</li>
</ul>
<h2>What is not collected</h2>
<p>No passwords, no direct messages, no contacts, no follower lists, and no
data about anyone other than the connected account holder.</p>
<h2>How it is used</h2>
<p>Solely to publish videos you have chosen to publish, and to report back how
they performed. Your data is not sold, rented, or shared with advertisers, and
is not used to train models.</p>
<h2>Where it is stored</h2>
<p>Tokens are stored on a private server, readable only by the service account
that publishes videos. They are transmitted only to the platform that issued
them.</p>
<h2>Retention and deletion</h2>
<p>Tokens are kept until you disconnect. Revoke access in the platform's own
settings, or ask us to remove them, and the stored token is deleted. Publishing
records are kept while the account remains connected.</p>
<h2>Your choices</h2>
<p>You can disconnect at any time, and request a copy or deletion of everything
held about your account.</p>
{contact}""")


@app.get("/terms", response_class=HTMLResponse)
def terms():
    contact = (f"<p>Contact: <a href='mailto:{_esc(CONTACT)}'>{_esc(CONTACT)}</a></p>"
               if CONTACT else "")
    return _legal("Terms of service", f"""
<p class="note">Last updated: {time.strftime('%d %B %Y')}</p>
<h2>What this service does</h2>
<p>{_esc(BRAND)} edits long videos into short clips and publishes them to social
accounts you have connected.</p>
<h2>Your responsibilities</h2>
<ul>
<li>You own the footage you publish, or hold the rights to use it.</li>
<li>You connect only accounts you control.</li>
<li>You follow the rules of each platform you publish to, including their
community guidelines and branded-content disclosure requirements.</li>
</ul>
<h2>Acceptable use</h2>
<p>Do not use this service to publish content that is unlawful, infringing,
misleading, or that breaches a platform's terms. Access may be withdrawn for
any of these.</p>
<h2>Platform rules come first</h2>
<p>Publishing happens through TikTok's and Meta's official APIs and remains
subject to their terms. They may limit, reject, or remove content
independently of this service.</p>
<h2>No warranty</h2>
<p>The service is provided as is. Uploads can fail, platforms change their
APIs, and scheduled posts may not appear. Keep your own copy of anything you
care about.</p>
<h2>Ending it</h2>
<p>Disconnect your accounts at any time, from here or from the platform's own
settings. That ends the service for those accounts immediately.</p>
{contact}""")


if __name__ == "__main__":
    import sys

    if "--selfcheck" in sys.argv:
        from fastapi.testclient import TestClient

        globals()["CONNECT_KEY"] = "s3cret"
        globals()["PUBLIC_ORIGIN"] = "https://clips.example"
        c = TestClient(app, follow_redirects=False)

        # legal pages are public, no key, and say the things review looks for
        for path, must in (("/privacy", "Retention"), ("/terms", "Acceptable use")):
            r = c.get(path)
            assert r.status_code == 200 and must in r.text, (path, r.status_code)
        assert "password" in c.get("/privacy").text.lower()

        assert redirect_uri("tiktok") == "https://clips.example/oauth/tiktok/callback"

        # the gate: no key and a wrong key both refuse to start a flow
        for bad in ("", "nope"):
            r = c.get(f"/oauth/tiktok/start?key={bad}")
            assert r.status_code == 303 and "/?" in r.headers["location"], r.headers
            assert "bad=1" in r.headers["location"], r.headers
        globals()["CONNECT_KEY"] = ""
        r = c.get("/oauth/tiktok/start?key=anything")
        assert "bad=1" in r.headers["location"]          # unset = closed, not open
        globals()["CONNECT_KEY"] = "s3cret"

        # with the key it hands off to TikTok, carrying a state we issued
        globals()["TIKTOK_KEY"] = "ck"
        r = c.get("/oauth/tiktok/start?key=s3cret")
        loc = r.headers["location"]
        assert loc.startswith("https://www.tiktok.com/v2/auth/authorize/"), loc
        assert "video.publish" in loc and "client_key=ck" in loc
        state = loc.split("state=")[1].split("&")[0]
        assert state in _PENDING

        # a callback state must be one we issued, for that platform, once only
        r = c.get(f"/oauth/instagram/callback?code=x&state={state}")
        assert "bad=1" in r.headers["location"]          # wrong platform
        assert state in _PENDING, "a rejected callback consumed the state"
        r = c.get("/oauth/tiktok/callback?code=x&state=forged")
        assert "bad=1" in r.headers["location"]

        # a declined authorisation reports the reason instead of exchanging
        r = c.get("/oauth/tiktok/callback?error=access_denied&error_description=nope")
        assert "bad=1" in r.headers["location"]

        # state expires
        old = _issue_state("tiktok")
        _PENDING[old] = ("tiktok", time.time() - STATE_TTL - 1, None)
        try:
            _take_state(old, "tiktok")
            raise AssertionError("expired state accepted")
        except ValueError:
            pass

        # youtube: no client_secrets file means a readable refusal, not a stack trace
        globals()["CLIENT_SECRETS"] = "/nonexistent/client_secrets.json"
        r = c.get("/oauth/youtube/start?key=s3cret")
        assert "bad=1" in r.headers["location"], r.headers
        assert "client_secrets" in r.headers["location"], r.headers
        assert "youtube" in c.get("/").text.lower()

        # the chosen channel name rides along on the state, not on the URL
        s2 = _issue_state("youtube", "Gaming")
        assert _take_state(s2, "youtube") == "Gaming"

        # the posting screen carries every element the audit checks
        import tempfile
        tmp = tempfile.mkdtemp()
        globals()["OUT_DIR"] = tmp
        open(os.path.join(tmp, "t1_vidA_100.mp4"), "wb").write(b"0")
        upload_tiktok.available_accounts = lambda: ["leo"]
        upload_tiktok.load_credentials = lambda a, now=None: {"access_token": "t"}
        upload_tiktok.creator_info = lambda creds: {
            "creator_nickname": "Leo G", "creator_avatar_url": "https://img/a.jpg",
            "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
            "comment_disabled": True, "max_video_post_duration_sec": 600}
        html = c.get("/post?key=s3cret").text
        assert "Leo G" in html and "https://img/a.jpg" in html      # creator shown
        assert "Who can view this video" in html                    # privacy choice
        assert "Public To Everyone" in html and "Self Only" in html
        assert "Branded content" in html and "Branded Content Policy" in html
        assert "t1_vidA_100.mp4" in html
        assert "turned off on this account" in html                 # honours the account
        assert 'name="disable_comment" value="1" checked disabled' in html
        assert "up to 600s" in html

        # posting is gated too, and a missing clip never reaches the API
        posted = {}
        upload_tiktok.upload = lambda *a, **k: posted.update(k) or {"publish_id": "p1"}
        form = {"account": "leo", "clip": "t1_vidA_100.mp4", "privacy": "SELF_ONLY",
                "caption": "hi", "disable_duet": "1"}
        r = c.post("/post", data=dict(form, key="wrong"))
        assert "bad=1" in r.headers["location"] and not posted
        r = c.post("/post", data=dict(form, key="s3cret", clip="../../etc/passwd"))
        assert "clip+not+found" in r.headers["location"] and not posted
        r = c.post("/post", data=dict(form, key="s3cret"))
        assert "posted+p1" in r.headers["location"], r.headers
        assert posted["privacy"] == "SELF_ONLY" and posted["direct"] is True
        assert posted["options"]["disable_duet"] is True
        assert posted["options"]["disable_comment"] is False
        print("connect.py self-check OK")
    else:
        import uvicorn
        if not CONNECT_KEY:
            print("WARNING: CLIPPER_CONNECT_KEY unset — the connect flow is closed")
        print("redirect URIs to register:")
        for platform in ("youtube", "tiktok", "instagram"):
            print(f"  {redirect_uri(platform)}")
        uvicorn.run(app, host=HOST, port=PORT)
