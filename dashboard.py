"""Account management dashboard — one local page over the accounts table.

    python dashboard.py        ->  http://localhost:8000

Add accounts, edit their fields, sync public stats, and apply the lifecycle
status the rules suggest. Suggestions are never auto-applied: a bot wall or a
misread profile should not promote an account into campaign work by itself.
"""
import json
import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import accounts
import db

app = FastAPI(title="Clipper accounts")
HOST = os.environ.get("CLIPPER_DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLIPPER_DASH_PORT", "8000"))

# Pill backgrounds. Every one carries ink text, not white: white on the old
# saturated set measured 2.5:1 to 3.9:1, under the 4.5:1 the 11px label needs.
# Six hues is past the 2-3 palette limit on purpose, because status is the
# column you scan this table for; the reason is the exception.
# Gold is the accent and goes to campaign_ready alone, the one status that
# means an account can take work. The status word sits inside the pill, so the
# hue is a second signal, never the only one.
STATUS_COLORS = {
    "new": "#B8B0A4", "warming": "#C9A227", "farming": "#64BBA8",
    "campaign_ready": "#FFD24A", "paused": "#8C8FA8", "banned": "#FF7A8A",
}
PILL_INK = "#12100E"


def _conn():
    conn = db.init_db()
    accounts.init(conn)
    return conn


def _esc(v):
    """Escape for both text and attribute context.

    Quotes matter: campaign ids and usernames reach `value="..."` and a JS
    confirm() string, and both come from outside — Clippo's API and the add
    form — so an unescaped quote breaks out of the attribute.
    """
    return (("" if v is None else str(v))
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Clipper accounts</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,">
<style>
 /* Palette: the clip colours, so the console and the clips it ships look
    related. Teal #64BBA8 and gold #FFD24A are edit.py's QUOTE_TEAL and
    PHRASE_COLOR, both sampled off the reference clips. Neutrals are warm
    (#12100E / #FBFAF7) rather than the blue-black this page used to borrow
    from GitHub, which gave it no identity of its own.
    Teal carries links and the primary action; gold is the single accent and
    appears once, on campaign_ready. Every pairing below was measured with
    .claude/skills/antislop-human/contrast-check.py and clears 4.5:1. */
 :root{
   color-scheme:light dark;
   --bg:#12100E; --card:#1C1917; --line:#2A2622;
   --fg:#EDE9E1; --muted:#A39C90;
   --mark:#64BBA8; --accent:#FFD24A; --danger:#FF8FA0;
   --btn:#241F1B; --btn-line:#3A342E; --on-mark:#12100E;
 }
 @media(prefers-color-scheme:light){
   :root{
     --bg:#FBFAF7; --card:#F2EFE9; --line:#E2DCD1;
     --fg:#1C1917; --muted:#6B6459;
     --mark:#0F6B58; --accent:#7A5A00; --danger:#A3121F;
     --btn:#FFFFFF; --btn-line:#D8D1C5; --on-mark:#FFFFFF;
   }
 }
 body{font:14px/1.45 system-ui,sans-serif;margin:0;padding:24px;
      background:var(--bg);color:var(--fg)}
 h1{font-size:18px;margin:0 0 4px} .sub{color:var(--muted);margin:0 0 20px}
 /* Tables are wider than a phone and reflowing eight columns of dense state
    into cards would cost more than it returns here, so each one scrolls
    inside its own box and the page itself never scrolls sideways. */
 .tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
 table{border-collapse:collapse;width:100%;font-size:13px;min-width:720px}
 th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;
       vertical-align:top}
 /* Uppercase with light tracking on the header row only: it separates labels
    from data in a table dense enough that weight alone does not. */
 th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;
    letter-spacing:.04em}
 tr:hover td{background:var(--card)}
 .pill{display:inline-block;padding:2px 9px;border-radius:999px;
       font-size:11px;font-weight:600}
 .muted{color:var(--muted)} .why{color:var(--muted);font-size:12px;margin-top:3px}
 .num{font-variant-numeric:tabular-nums}
 form.inline{display:inline}
 button{font:inherit;padding:6px 12px;min-height:34px;border-radius:6px;
        border:1px solid var(--btn-line);background:var(--btn);color:var(--fg);
        cursor:pointer}
 button:hover{border-color:var(--mark)}
 button.go{border-color:var(--mark);background:var(--mark);color:var(--on-mark);
           font-weight:600}
 button.del{border-color:var(--danger);background:transparent;color:var(--danger)}
 button[aria-busy="true"]{opacity:.65;cursor:progress}
 /* Keyboard users need to see where they are; the ring is the mark colour,
    which measures over 6:1 against both grounds. */
 :focus-visible{outline:2px solid var(--mark);outline-offset:2px;border-radius:4px}
 a{color:var(--mark)}
 a:hover{text-decoration-thickness:2px}
 /* The status reason is a sentence, not a token: without a floor it squeezed
    to one word per line and made every row six lines tall. */
 td:nth-child(2){min-width:190px}
 td:last-child{white-space:nowrap}
 td:last-child form{margin-right:4px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:10px;
       padding:16px;margin-bottom:22px}
 .row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
 label{display:block;font-size:12px;color:var(--muted);margin-bottom:3px}
 .plat{display:inline-flex;gap:4px;align-items:center;margin-right:8px;
       color:var(--fg)}
 input,select{font:inherit;padding:7px 8px;min-height:34px;border-radius:6px;
              border:1px solid var(--btn-line);background:var(--btn);
              color:var(--fg)}
 .err{background:var(--card);border:1px solid var(--danger);
      border-left-width:4px;padding:10px 14px;border-radius:8px;
      margin-bottom:16px;color:var(--fg)}
 /* A thumb is not a cursor: controls grow to the 44px target on a phone. */
 @media(max-width:700px){
   body{padding:16px}
   button,input,select{min-height:44px}
   .row{gap:12px} .row>div{flex:1 1 100%}
 }
</style></head><body>
<h1>Clipper accounts</h1>
<p class="sub">Warm-up, then farming, then campaign ready. Suggestions are
advisory; you apply them.</p>
__ERROR__
<div class="card"><form method="post" action="/add"><div class="row">
  <div><label>Platform</label><select name="platform">__PLATFORMS__</select></div>
  <div><label>Username</label><input name="username" placeholder="@handle" required></div>
  <div><label>Niche</label><input name="niche" placeholder="Entertainment"></div>
  <div><label>Follower target</label><input name="followers_target" type="number" value="10" style="width:110px"></div>
  <div><label>Device label</label><input name="device_label" placeholder="phone-01"></div>
  <div><label>Proxy label</label><input name="proxy_label" placeholder="id-mobile-3"></div>
  <div><button class="go" type="submit">Add account</button></div>
</div></form></div>
<h2 style="font-size:15px;margin:26px 0 8px">Campaigns</h2>
<p class="sub" style="margin:0 0 12px">Destinations are derived from the brief and
your ready accounts. Pin one only when the rule gets it wrong. Pinning a
platform you cannot publish to sizes clips for a surface that never receives them.</p>
<div class="tablewrap"><table><thead><tr>
 <th>Campaign</th><th>Brief wants</th><th>Destinations now</th><th>Pin</th>
</tr></thead><tbody>__CAMPAIGNS__</tbody></table></div>

<h2 style="font-size:15px;margin:26px 0 8px">Accounts</h2>
<div class="tablewrap"><table><thead><tr>
 <th>Account</th><th>Status</th><th>Followers</th><th>Posts</th>
 <th>Health</th><th>Verified</th><th>Device / proxy</th><th>Actions</th>
</tr></thead><tbody>__ROWS__</tbody></table></div>
<p class="sub" style="margin-top:18px">__COUNT__. Stats are read from public
profiles. A healthy new account clears 50 views in 24h; 0 to 5 views across 3 or
more clips signals a shadowban.</p>
<script>
/* Every control here is a form POST, and /sync scrapes a live profile, so the
   page can sit for seconds with nothing to show it is working. Label the
   button that was pressed instead of leaving the click unanswered. It is
   marked busy after submission, never before, so the POST still goes. */
document.addEventListener('submit', function (e) {
  var b = e.target.querySelector('button[type=submit]:focus') ||
          e.target.querySelector('button[type=submit]');
  if (!b) return;
  var was = b.textContent;
  setTimeout(function () {
    b.setAttribute('aria-busy', 'true');
    b.textContent = was.trim() === 'Delete' ? 'Deleting...' : 'Working...';
  }, 0);
});
</script>
</body></html>"""


def _campaign_rows(conn):
    """One row per campaign: what the brief wants, where clips actually go."""
    try:
        import pipeline           # local: pulls the uploader's deps
        import segments
    except Exception as e:
        return (f'<tr><td colspan="4" class="muted" style="padding:20px">'
                f'destination rules unavailable ({_esc(type(e).__name__)})</td></tr>')

    out = []
    for c in db.all_campaigns(conn):
        reqs = json.loads(c["requirements_json"] or "{}")
        pinned = db.campaign_override(conn, c["campaign_id"]) or []
        dests, why = pipeline._destinations(conn, c["campaign_id"], reqs)
        window = segments.duration_window(dests)
        win = f"{window[0]}-{window[1]}s" if window else "no common length"
        boxes = "".join(
            f'<label class="plat">'
            f'<input type="checkbox" name="platforms" value="{p}"'
            f'{" checked" if p in pinned else ""}>{p}</label>'
            for p in accounts.PLATFORMS)
        out.append(f"""<tr>
 <td>{_esc(reqs.get('title') or c['campaign_id'])}<br>
     <span class="muted">{_esc(c['campaign_id'])} &middot; {_esc(c['status'])}</span></td>
 <td class="muted">{_esc('+'.join(reqs.get('platforms_required') or []) or '-')}</td>
 <td><b>{_esc('+'.join(dests))}</b> <span class="muted">{win}</span>
     <div class="why">{_esc(why)}</div></td>
 <td><form method="post" action="/destinations">
     <input type="hidden" name="campaign_id" value="{_esc(c['campaign_id'])}">
     {boxes}
     <button class="go" type="submit">Pin</button>
     <button type="submit" name="clear" value="1">Auto</button>
     </form></td></tr>""")
    return "".join(out) or ('<tr><td colspan="4" class="muted" '
                            'style="padding:20px">No campaigns crawled yet.</td></tr>')


def render(error=""):
    conn = _conn()
    rows = accounts.all_accounts(conn)
    out = []
    for r in rows:
        suggested, why = accounts.evaluate(r)
        color = STATUS_COLORS.get(r["status"], "#8b8b8b")
        apply_btn = ""
        if suggested != r["status"]:
            apply_btn = (
                f'<form class="inline" method="post" action="/status">'
                f'<input type="hidden" name="account_id" value="{r["account_id"]}">'
                f'<input type="hidden" name="status" value="{suggested}">'
                f'<button class="go" type="submit">&rarr; {_esc(suggested)}</button></form> ')
        warm = ""
        if r["status"] == "new":
            warm = (f'<form class="inline" method="post" action="/warmup">'
                    f'<input type="hidden" name="account_id" value="{r["account_id"]}">'
                    f'<button type="submit">Start warm-up</button></form> ')
        views = r["recent_avg_views"]
        health = (f'<span class="num">{views}</span> avg / {r["recent_clip_count"] or 0} clips'
                  if views is not None else '<span class="muted">no data</span>')
        out.append(f"""<tr>
 <td><a href="{_esc(r['profile_url'])}" target="_blank" rel="noreferrer">
     {_esc(r['platform'])}/{_esc(r['username'])}</a><br>
     <span class="muted">{_esc(r['niche'] or '-')}</span></td>
 <td><span class="pill" style="background:{color};color:{PILL_INK}">{_esc(r['status'])}</span>
     <div class="why">{_esc(why)}</div></td>
 <td class="num">{r['followers'] or 0}<span class="muted"> / {r['followers_target'] or 10}</span></td>
 <td class="num">{r['video_count'] or 0}</td>
 <td>{health}</td>
 <td>{'✔ ' + _esc(r['verified_bio_code']) if r['verified_bio_code'] else '<span class="muted">-</span>'}</td>
 <td class="muted">{_esc(r['device_label'] or '-')} / {_esc(r['proxy_label'] or '-')}</td>
 <td>{warm}{apply_btn}
   <form class="inline" method="post" action="/sync">
     <input type="hidden" name="account_id" value="{r['account_id']}">
     <button type="submit">Sync</button></form>
   <form class="inline" method="post" action="/verify">
     <input type="hidden" name="account_id" value="{r['account_id']}">
     <input name="code" placeholder="6-digit" style="width:78px" maxlength="6">
     <button type="submit">Verify</button></form>
   <form class="inline" method="post" action="/delete"
         onsubmit="return confirm('Delete {_esc(r['username'])}?')">
     <input type="hidden" name="account_id" value="{r['account_id']}">
     <button class="del" type="submit">Delete</button></form></td></tr>""")

    body = "".join(out) or ('<tr><td colspan="8" class="muted" '
                            'style="padding:26px">No accounts yet.</td></tr>')
    opts = "".join(f'<option value="{p}">{p}</option>' for p in accounts.PLATFORMS)
    return (PAGE.replace("__CAMPAIGNS__", _campaign_rows(conn))
                .replace("__ROWS__", body)
                .replace("__PLATFORMS__", opts)
                .replace("__COUNT__", f"{len(rows)} account(s)")
                .replace("__ERROR__", f'<div class="err">{_esc(error)}</div>' if error else ""))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return render(request.query_params.get("error", ""))


def _back(error=""):
    return RedirectResponse(f"/?error={error}" if error else "/", status_code=303)


@app.post("/add")
def add_account(platform: str = Form(...), username: str = Form(...),
                niche: str = Form(""), followers_target: int = Form(10),
                device_label: str = Form(""), proxy_label: str = Form("")):
    try:
        accounts.add(_conn(), platform, username, niche=niche or None,
                     followers_target=followers_target,
                     device_label=device_label or None,
                     proxy_label=proxy_label or None)
    except Exception as e:
        return _back(f"{type(e).__name__}: {e}")
    return _back()


@app.post("/status")
def set_status(account_id: int = Form(...), status: str = Form(...)):
    try:
        accounts.update(_conn(), account_id, status=status)
    except Exception as e:
        return _back(f"{type(e).__name__}: {e}")
    return _back()


@app.post("/warmup")
def warmup(account_id: int = Form(...)):
    # Every mutating route reports its failure in the page's own error banner.
    # Without this the operator gets a raw 500 stack page instead, which is the
    # one state the dashboard cannot explain itself in.
    try:
        accounts.start_warmup(_conn(), account_id)
    except Exception as e:
        return _back(f"{type(e).__name__}: {e}")
    return _back()


@app.post("/verify")
def verify(account_id: int = Form(...), code: str = Form("")):
    try:
        accounts.verify(_conn(), account_id, code)
    except Exception as e:
        return _back(f"{type(e).__name__}: {e}")
    return _back()


@app.post("/sync")
def sync(account_id: int = Form(...)):
    try:
        accounts.sync(_conn(), account_id)
    except Exception as e:
        return _back(f"sync failed: {e}")
    return _back()


@app.post("/destinations")
def set_destinations(campaign_id: str = Form(...),
                     platforms: list[str] = Form([]), clear: str = Form("")):
    try:
        db.set_campaign_override(_conn(), campaign_id,
                                 None if clear else platforms)
    except Exception as e:
        return _back(f"{type(e).__name__}: {e}")
    return _back()


@app.post("/delete")
def delete(account_id: int = Form(...)):
    try:
        accounts.delete(_conn(), account_id)
    except Exception as e:
        return _back(f"{type(e).__name__}: {e}")
    return _back()


@app.get("/api/accounts")
def api_accounts():
    conn = _conn()
    out = []
    for r in accounts.all_accounts(conn):
        d = dict(r)
        d["suggested_status"], d["reason"] = accounts.evaluate(r)
        out.append(d)
    return JSONResponse(out)


if __name__ == "__main__":
    import uvicorn
    print(f"dashboard on http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
