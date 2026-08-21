"""Discord notifier — the one way the pipeline reaches a human.

Everything sent through here is redacted first. Tracebacks and yt-dlp output
routinely carry access tokens, cookie values and signed URLs, and this text
lands in a chat channel that outlives the run, so redaction is not optional.

CLIPPER_DISCORD_WEBHOOK is the target. With no webhook configured the message
is printed instead, so a fresh box degrades to logging rather than crashing.
"""
import json
import os
import re
import urllib.error
import urllib.request

WEBHOOK = os.environ.get("CLIPPER_DISCORD_WEBHOOK", "")
MAX_LEN = 1900  # Discord rejects messages over 2000 characters

# Ordered most specific first: a bare long token would otherwise swallow the
# labelled forms before they match.
_SECRETS = [
    re.compile(r"(Bearer\s+)[\w\-.~+/=]+", re.I),
    re.compile(r"((?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret"
               r"|password|cookie|set-cookie)\"?\s*[:=]\s*\"?)[^\s\"',;)]+", re.I),
    re.compile(r"(AIza)[\w\-]{30,}"),                      # Google API keys
    re.compile(r"(ya29\.)[\w\-.]{20,}"),                   # Google OAuth tokens
    re.compile(r"([?&](?:key|token|sig|signature|auth)=)[^&\s]+", re.I),
    re.compile(r"(https://discord(?:app)?\.com/api/webhooks/)\S+", re.I),
]


def redact(text):
    """Blank out anything that looks like a credential. Never returns None."""
    out = "" if text is None else str(text)
    for pat in _SECRETS:
        out = pat.sub(lambda m: m.group(1) + "[REDACTED]", out)
    return out


def send(text, title=None, tag=None):
    """Post one message. Returns True when Discord accepted it.

    Never raises: a notifier that takes the pipeline down with it is worse
    than a missed message.
    """
    body = redact(text)
    if title:
        body = f"**{redact(title)}**\n{body}"
    if tag:
        body = f"{tag} {body}"
    if len(body) > MAX_LEN:
        body = body[:MAX_LEN - 3] + "..."

    if not WEBHOOK:
        print(f"[notify] {body}")
        return False
    req = urllib.request.Request(
        WEBHOOK,
        data=json.dumps({"content": body}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError) as e:
        print(f"[notify] webhook failed ({type(e).__name__}), message was: {body}")
        return False


if __name__ == "__main__":
    # Self-check: redaction, offline, no webhook needed.
    assert redact("Authorization: Bearer ya29.abcDEF-123_x") \
        == "Authorization: Bearer [REDACTED]"
    assert redact('{"refresh_token": "1//0abcdefghij"}') \
        == '{"refresh_token": "[REDACTED]"}'
    assert redact("key=AIzaSyD9aBcDeFgHiJkLmNoPqRsTuVwXyZ012345") \
        .endswith("[REDACTED]")
    assert "sig=" in redact("https://x/v?sig=deadbeef&id=7")
    assert "deadbeef" not in redact("https://x/v?sig=deadbeef&id=7")
    assert "id=7" in redact("https://x/v?sig=deadbeef&id=7")  # only the secret goes
    assert redact("https://discord.com/api/webhooks/123/abc") \
        == "https://discord.com/api/webhooks/[REDACTED]"
    assert redact(None) == "" and redact(0) == "0"
    plain = "ffmpeg failed: no such file"
    assert redact(plain) == plain                       # ordinary text untouched

    real = globals()["WEBHOOK"]
    globals()["WEBHOOK"] = ""
    try:
        assert send("hello", title="t", tag="[TEST]") is False   # prints, no crash
    finally:
        globals()["WEBHOOK"] = real
    print("notify.py self-check OK")
