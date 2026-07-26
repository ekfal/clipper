"""Clip metadata via 9Router (PRD §3.7) — title, hook, description, hashtags.

Campaign requirements always win over generic virality advice: mandatory
hashtags are appended even if the model forgets them; platform rules decide
hashtag placement (YouTube: tags field + #Shorts in description).
"""
import re

import ai

SYSTEM = """You are a viral short-form video strategist for Indonesian audiences.
Output strictly a JSON object, no markdown. All text fields in casual Indonesian
(santai, gaul). Use relevant emoji inline in hook and title where they add punch."""

USER_TEMPLATE = """Buat metadata untuk klip pendek dari transkrip ini.

Transkrip segmen:
\"\"\"{transcript}\"\"\"

Requirement campaign (WAJIB dipatuhi, prioritas di atas segalanya):
- Hashtag wajib: {hashtags}
- Brief: {brief}

Return JSON keys:
1. "hook": headline pancingan gaya berita/quote untuk overlay visual di awal klip,
   maksimal 90 karakter, boleh 1-2 emoji yang relevan konteks.
2. "title": judul video, maksimal 80 karakter, boleh emoji.
3. "description": 2 kalimat engaging + SEMUA hashtag wajib + 2-3 hashtag relevan lain.
4. "youtube_tags": array 10 keyword pencarian relevan (tanpa #).
"""


def generate(transcript, requirements, platform="youtube"):
    """Return {hook, title, description, youtube_tags[]} honoring campaign rules."""
    mandatory = requirements.get("hashtags") or []
    out = ai.chat_json(
        SYSTEM,
        USER_TEMPLATE.format(
            transcript=transcript[:4000],
            hashtags=" ".join(mandatory) or "(tidak ada)",
            brief=(requirements.get("brief") or "")[:1500],
        ),
    )
    hook = str(out.get("hook") or "").strip()
    title = str(out.get("title") or "").strip()[:100]
    desc = str(out.get("description") or "").strip()
    tags = [str(t).strip().lstrip("#") for t in out.get("youtube_tags") or [] if str(t).strip()]

    # enforce mandatory hashtags even if the model dropped them
    for h in mandatory:
        if h.lower() not in desc.lower():
            desc += f" {h}"
    if platform == "youtube" and "#shorts" not in desc.lower():
        desc += " #Shorts"  # required for Shorts classification (PRD §3.7)

    # fallback title/hook from transcript if model returned blanks
    if not title:
        first = re.split(r"[.!?]", transcript)[0].strip()
        title = (first[:77] + "...") if len(first) > 80 else (first or "Klip Viral")
    if not hook:
        hook = title
    return {"hook": hook, "title": title, "description": desc, "youtube_tags": tags[:15]}


if __name__ == "__main__":
    # Offline check: enforcement logic with a stubbed model reply.
    real_chat = ai.chat_json
    ai.chat_json = lambda *a, **k: {
        "hook": "Gila, 25 Juta Sebulan Dari Klip! 💰",
        "title": "Rahasia Cuan Clipper",
        "description": "Simak sampai habis. Komen pendapatmu!",
        "youtube_tags": ["clipper", "#cuan", "shorts"],
    }
    try:
        m = generate("Contoh transkrip panjang soal cuan.", {"hashtags": ["#leogiovanni"]})
        assert "#leogiovanni" in m["description"], m
        assert "#Shorts" in m["description"], m
        assert m["youtube_tags"][1] == "cuan"  # lstrip #
        assert "💰" in m["hook"]
        # blank-model fallback path
        ai.chat_json = lambda *a, **k: {}
        m2 = generate("Kalimat pertama jadi judul. Sisanya tidak.", {"hashtags": []})
        assert m2["title"].startswith("Kalimat pertama"), m2
        assert m2["hook"] == m2["title"]
    finally:
        ai.chat_json = real_chat
    print("metadata.py self-check OK")
