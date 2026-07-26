"""9Router client — the only AI entry point (replaces Groq/Gemini/Ollama chain).

OpenAI-compatible chat completions over plain requests; no SDK dependency.
Config from clipper/.env (NINEROUTER_BASE_URL, NINEROUTER_API_KEY).
"""
import json
import os

import requests

_BASE = os.path.dirname(os.path.abspath(__file__))


def _load_env():
    """Tiny .env loader — no python-dotenv dependency for two variables."""
    path = os.path.join(_BASE, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())


_load_env()
BASE_URL = os.environ.get("NINEROUTER_BASE_URL", "http://localhost:20128/v1")
API_KEY = os.environ.get("NINEROUTER_API_KEY", "")
MODEL = os.environ.get("NINEROUTER_MODEL", "ds/deepseek-v4-pro")
TIMEOUT = 120


def chat_json(system, user, model=MODEL, temperature=0.7):
    """One chat completion that must return a JSON object. Returns parsed dict.
    Raises on transport error or unparseable output — caller decides fallback."""
    resp = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):  # some models still fence despite json mode
        text = text.strip("`")
        text = text[4:] if text.startswith("json") else text
    return json.loads(text)


if __name__ == "__main__":
    # Live check (needs tunnel on PC): list models + one tiny JSON completion.
    r = requests.get(f"{BASE_URL}/models",
                     headers={"Authorization": f"Bearer {API_KEY}"}, timeout=10)
    print("models:", r.status_code, str(r.json())[:200] if r.ok else r.text[:200])
    out = chat_json("Reply as JSON.", 'Return {"ok": true} exactly.')
    assert out.get("ok") is True, out
    print("chat_json OK:", out)
