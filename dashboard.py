"""Account management dashboard — one local page over the accounts table.

    python dashboard.py        ->  http://localhost:8000

Add accounts, edit their fields, sync public stats, and apply the lifecycle
status the rules suggest. Suggestions are never auto-applied: a bot wall or a
misread profile should not promote an account into campaign work by itself.
"""
import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import accounts
import db

app = FastAPI(title="Clipper accounts")
HOST = os.environ.get("CLIPPER_DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLIPPER_DASH_PORT", "8000"))

STATUS_COLORS = {
    "new": "#8b8b8b", "warming": "#d18616", "farming": "#2f86d1",
    "campaign_ready": "#2ea043", "paused": "#d29922", "banned": "#cf3b3b",
}


def _conn():
    conn = db.init_db()
    accounts.init(conn)
    return conn


def _esc(v):
    return ("" if v is None else str(v)).replace("&", "&amp;").replace("<", "&lt;")


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Clipper accounts</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root{color-scheme:light dark}
 body{font:14px/1.45 system-ui,sans-serif;margin:0;padding:24px;
      background:#0d1117;color:#e6edf3}
 h1{font-size:18px;margin:0 0 4px} .sub{color:#8b949e;margin:0 0 20px}
 table{border-collapse:collapse;width:100%;font-size:13px}
 th,td{padding:8px 10px;border-bottom:1px solid #21262d;text-align:left;
       vertical-align:top}
 th{color:#8b949e;font-weight:600;font-size:12px;text-transform:uppercase;
    letter-spacing:.04em}
 tr:hover td{background:#161b22}
 .pill{display:inline-block;padding:2px 9px;border-radius:999px;color:#fff;
       font-size:11px;font-weight:600}
 .muted{color:#8b949e} .why{color:#8b949e;font-size:12px;margin-top:3px}
 .num{font-variant-numeric:tabular-nums}
 form.inline{display:inline}
 button{font:inherit;padding:4px 10px;border-radius:6px;border:1px solid #30363d;
        background:#21262d;color:#e6edf3;cursor:pointer}
 button:hover{background:#30363d}
 button.go{border-color:#2ea043;background:#1a7f37}
 button.del{border-color:#6e2b2b;background:#3d1d1d;color:#ffb4b4}
 td:last-child{white-space:nowrap}
 td:last-child form{margin-right:4px}
 .card{background:#161b22;border:1px solid #21262d;border-radius:10px;
       padding:16px;margin-bottom:22px}
 .row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
 label{display:block;font-size:12px;color:#8b949e;margin-bottom:3px}
 input,select{font:inherit;padding:6px 8px;border-radius:6px;
              border:1px solid #30363d;background:#0d1117;color:#e6edf3}
 .err{background:#3d1d1d;border:1px solid #6e2b2b;padding:10px 14px;
      border-radius:8px;margin-bottom:16px}
 a{color:#58a6ff}
 @media(prefers-color-scheme:light){
   body{background:#fff;color:#1f2328} tr:hover td{background:#f6f8fa}
   .card{background:#f6f8fa;border-color:#d0d7de}
   th,td{border-color:#d8dee4} input,select,button{background:#fff;color:#1f2328;
     border-color:#d0d7de} button.go{background:#1a7f37;color:#fff}
   button.del{background:#fff;color:#b62324;border-color:#f0b3b3}
   .err{background:#fff1f1;border-color:#ffc1c1}
 }
</style></head><body>
<h1>Clipper accounts</h1>
<p class="sub">Warm-up &rarr; farming &rarr; campaign ready. Suggestions are
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
<table><thead><tr>
 <th>Account</th><th>Status</th><th>Followers</th><th>Posts</th>
 <th>Health</th><th>Verified</th><th>Device / proxy</th><th>Actions</th>
</tr></thead><tbody>__ROWS__</tbody></table>
<p class="sub" style="margin-top:18px">__COUNT__ &middot; stats read from public
profiles &middot; healthy new account: 50+ views in 24h, 0&ndash;5 across 3+ clips
signals a shadowban</p>
</body></html>"""


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
     <span class="muted">{_esc(r['niche'] or '—')}</span></td>
 <td><span class="pill" style="background:{color}">{_esc(r['status'])}</span>
     <div class="why">{_esc(why)}</div></td>
 <td class="num">{r['followers'] or 0}<span class="muted"> / {r['followers_target'] or 10}</span></td>
 <td class="num">{r['video_count'] or 0}</td>
 <td>{health}</td>
 <td>{'✔ ' + _esc(r['verified_bio_code']) if r['verified_bio_code'] else '<span class="muted">—</span>'}</td>
 <td class="muted">{_esc(r['device_label'] or '—')} / {_esc(r['proxy_label'] or '—')}</td>
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
    return (PAGE.replace("__ROWS__", body)
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
    accounts.start_warmup(_conn(), account_id)
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


@app.post("/delete")
def delete(account_id: int = Form(...)):
    accounts.delete(_conn(), account_id)
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
