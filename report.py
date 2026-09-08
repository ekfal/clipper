"""Failure reports the maintainer can act on without being at the machine.

A traceback pasted out of a terminal loses the things that actually decide the
answer: which ffmpeg, whether cookies were present, how much disk was left,
which style the job ran with. This collects those alongside the error.

Two sinks:
  * always — a JSON file under reports/, printed as one line so a wrapper can
    pick the path out of stdout;
  * optionally — a GitHub issue, which is the one channel a maintainer reading
    this repo can see without access to the box. Set CLIPPER_GITHUB_TOKEN and
    CLIPPER_GITHUB_REPO (owner/name) to enable it.

Nothing secret goes in a report. API keys, cookie contents and OAuth tokens are
never read; only whether they are present, because that is the diagnostic value
and the secret is not.
"""
import getpass
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import traceback

_BASE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.environ.get("CLIPPER_REPORT_DIR", os.path.join(_BASE, "reports"))
GH_TOKEN = os.environ.get("CLIPPER_GITHUB_TOKEN", "")
GH_REPO = os.environ.get("CLIPPER_GITHUB_REPO", "")
MARKER = "clipper-auto-report"


def _ffmpeg_version():
    try:
        import edit
        out = subprocess.run([edit.FFMPEG, "-version"], capture_output=True,
                             text=True, timeout=10).stdout
        return out.splitlines()[0] if out else "unknown"
    except Exception as e:
        return f"unavailable ({type(e).__name__})"


def diagnostics():
    """Everything about the host that changes the answer. No secrets."""
    d = {
        "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": socket.gethostname(),
        "user": getpass.getuser(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "ffmpeg": _ffmpeg_version(),
    }
    try:
        usage = shutil.disk_usage(_BASE)
        d["disk_free_gb"] = round(usage.free / 1e9, 1)
        d["disk_used_pct"] = round(100 * usage.used / usage.total)
    except Exception:
        pass

    # presence, never contents
    try:
        import ai
        import fetch
        import upload_youtube
        d["config"] = {
            "router_base": ai.BASE_URL,
            "router_key_set": bool(ai.API_KEY),
            "yt_cookies_present": bool(fetch.YTDLP_COOKIES),
            "upload_accounts": upload_youtube.available_accounts(),
        }
    except Exception as e:
        d["config"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        import bgm
        import edit
        import segments
        d["style"] = {
            "frame_mode": edit.FRAME_MODE,
            "caption_style": edit.CAPTION_STYLE,
            "hook_style": edit.HOOK_STYLE,
            "hook_seconds": edit.HOOK_DUR,
            "duration_ranges": {k: list(v) for k, v in segments.DURATION_RANGES.items()},
            "bgm_tracks": len(bgm.load_tracks()),
        }
    except Exception as e:
        d["style"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        d["commit"] = subprocess.run(
            ["git", "-C", _BASE, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        d["commit"] = None
    return d


def fingerprint(err_type, message):
    """Stable id for 'the same failure', so repeats update one issue.

    Paths, timestamps and ids differ every run, so the fingerprint is the
    exception type plus the first line with digits and paths flattened.
    """
    import re
    first = (message or "").strip().splitlines()[0] if message else ""
    flat = re.sub(r"/\S+", "<path>", first)
    flat = re.sub(r"\d+", "<n>", flat)[:160]
    return hashlib.sha1(f"{err_type}|{flat}".encode()).hexdigest()[:10]


def build(exc=None, context=None, stage=None):
    """Assemble a report dict from the exception currently being handled."""
    err_type = type(exc).__name__ if exc else "Unknown"
    message = str(exc) if exc else ""
    return {
        "marker": MARKER,
        "stage": stage or "unknown",
        "error": {"type": err_type, "message": message,
                  "traceback": traceback.format_exc() if exc else None},
        "fingerprint": fingerprint(err_type, message),
        "context": context or {},
        "diagnostics": diagnostics(),
    }


def write(report):
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(
        REPORT_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{report['fingerprint']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return path


def _markdown(report):
    e = report["error"]
    d = report["diagnostics"]
    lines = [
        f"**{e['type']}** during `{report['stage']}`",
        "", f"```\n{e['message'][:1500]}\n```", "",
        "| | |", "|---|---|",
        f"| when | {d.get('when')} |",
        f"| host | {d.get('host')} |",
        f"| commit | `{d.get('commit')}` |",
        f"| ffmpeg | {str(d.get('ffmpeg'))[:60]} |",
        f"| disk free | {d.get('disk_free_gb')} GB ({d.get('disk_used_pct')}% used) |",
    ]
    cfg = d.get("config") or {}
    lines += [
        f"| cookies | {'present' if cfg.get('yt_cookies_present') else 'MISSING'} |",
        f"| router key | {'set' if cfg.get('router_key_set') else 'MISSING'} |",
        f"| upload accounts | {', '.join(cfg.get('upload_accounts') or []) or 'none'} |",
    ]
    if report.get("context"):
        lines += ["", "**Context**", "```json",
                  json.dumps(report["context"], indent=2, ensure_ascii=False)[:1200],
                  "```"]
    if e.get("traceback"):
        lines += ["", "<details><summary>Traceback</summary>", "",
                  "```", e["traceback"][-2500:], "```", "", "</details>"]
    lines += ["", f"<!-- {MARKER}:{report['fingerprint']} -->"]
    return "\n".join(lines)


def to_github(report, token=None, repo=None, timeout=20):
    """File or update one issue per distinct failure. Returns a status dict.

    A repeat of a failure already open comments on that issue rather than
    opening another, so a nightly run that keeps hitting the same wall leaves
    one thread instead of a pile.
    """
    import requests

    token = token or GH_TOKEN
    repo = repo or GH_REPO
    if not token or not repo:
        return {"posted": False, "reason": "CLIPPER_GITHUB_TOKEN/REPO not set"}

    fp = report["fingerprint"]
    title = f"[auto] {report['error']['type']} in {report['stage']} ({fp})"
    head = {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json"}
    api = f"https://api.github.com/repos/{repo}"
    try:
        found = requests.get(f"{api}/issues", headers=head, timeout=timeout,
                             params={"state": "open", "labels": "", "per_page": 100})
        found.raise_for_status()
        existing = next((i for i in found.json() if fp in (i.get("title") or "")), None)
        if existing:
            r = requests.post(f"{api}/issues/{existing['number']}/comments",
                              headers=head, timeout=timeout,
                              json={"body": "Happened again.\n\n" + _markdown(report)})
            r.raise_for_status()
            return {"posted": True, "issue": existing["number"], "action": "commented",
                    "url": existing["html_url"]}
        r = requests.post(f"{api}/issues", headers=head, timeout=timeout,
                          json={"title": title, "body": _markdown(report)})
        r.raise_for_status()
        return {"posted": True, "issue": r.json()["number"], "action": "opened",
                "url": r.json()["html_url"]}
    except Exception as e:
        return {"posted": False, "reason": f"{type(e).__name__}: {e}"}


def capture(exc, stage, context=None, github=True):
    """Build, save and (when configured) publish one failure. Never raises."""
    try:
        rep = build(exc, context, stage)
        path = write(rep)
        out = {"report": path, "fingerprint": rep["fingerprint"]}
        if github:
            out["github"] = to_github(rep)
        return out
    except Exception as e:                       # reporting must not mask the bug
        return {"report": None, "error": f"reporter failed: {type(e).__name__}: {e}"}


if __name__ == "__main__":
    # Self-check: fingerprints are stable across runs and blind to paths/ids.
    a = fingerprint("RuntimeError", "ffmpeg failed: /tmp/x8/graph.txt line 42")
    b = fingerprint("RuntimeError", "ffmpeg failed: /tmp/q1/graph.txt line 91")
    assert a == b, "same failure, different paths, should share a fingerprint"
    assert a != fingerprint("ValueError", "ffmpeg failed: /tmp/x/graph.txt line 42")

    d = diagnostics()
    assert d["python"] and "ffmpeg" in d and "style" in d, d
    blob = json.dumps(d)
    for secret in ("Authorization", "cookie=", "refresh_token"):
        assert secret not in blob, f"{secret} leaked into a report"

    try:
        raise RuntimeError("ffmpeg failed: broken pipe")
    except RuntimeError as e:
        rep = build(e, {"content": "https://youtu.be/x"}, "render")
    assert rep["error"]["type"] == "RuntimeError"
    assert "youtu.be" in json.dumps(rep["context"])
    md = _markdown(rep)
    assert MARKER in md and "broken pipe" in md
    assert to_github(rep, token="", repo="")["posted"] is False   # opt-in only
    print("report.py self-check OK")
