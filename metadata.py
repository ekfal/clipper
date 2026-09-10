"""Clip metadata via 9Router (PRD §3.7) — title, hook, description, hashtags.

Campaign requirements always win over generic virality advice: mandatory
hashtags are appended even if the model forgets them; platform rules decide
hashtag placement (YouTube: tags field + #Shorts in description).
"""
import re

import ai
import bgm

SYSTEM = """You are a viral short-form video strategist for Indonesian audiences.
Output strictly a JSON object, no markdown. All text fields in casual Indonesian
(santai, gaul). Use relevant emoji inline in hook and title where they add punch.

Write like a person who watched this clip, not like a model describing it:

- NEVER use the em dash character. Use a comma, a period, a colon, or nothing.
- NEVER state a number, percentage or statistic the speaker did not say. No
  "90% orang", no "ribuan orang", no invented counts. If the clip has no number,
  the copy has no number.
- Ban the marketing filler: "di era digital", "revolusioner", "game changer",
  "solusi terbaik", "wajib kamu tahu", "tanpa ribet", "next level", "AI powered",
  "seamless", "cutting edge", "yuk simak", "simak selengkapnya".
- No "bukan cuma X, tapi Y" and no "bukan sekadar X". It is a formula, not a point.
- Do not force three of anything. List what the clip actually has.
- Quote what was actually said instead of describing how good it is."""

USER_TEMPLATE = """Buat metadata untuk klip pendek dari transkrip ini.

Transkrip segmen:
\"\"\"{transcript}\"\"\"

Requirement campaign (WAJIB dipatuhi, prioritas di atas segalanya):
- Hashtag wajib: {hashtags}
- Brief: {brief}

Return JSON keys:
1. "hook": headline pancingan gaya berita/quote untuk overlay visual di awal klip,
   maksimal 90 karakter, boleh 1-2 emoji yang relevan konteks.
   WAJIB: bungkus bagian yang paling memancing dengan **dua bintang** supaya
   dicetak tebal. Sisanya (kata sambung, keterangan) biarkan tanpa bintang.
   Contoh: "**Nekat!! Dia Berani Banget** Ngomong Gini Ke **Mantannya**"
2. "title": judul video, maksimal 80 karakter, boleh emoji.
3. "description": 2 kalimat engaging + SEMUA hashtag wajib + 2-3 hashtag relevan lain.
4. "youtube_tags": array 10 keyword pencarian relevan (tanpa #).
5. "punchline": kutip PERSIS 3-6 kata dari transkrip yang jadi puncak/kejutan
   segmen ini. Dipakai untuk mewarnai caption di momen itu. Kalau tidak ada
   yang menonjol, kembalikan string kosong.
6. "mood": SATU kata dari daftar ini yang paling menggambarkan rasa segmen —
   {moods}. Dipakai untuk memilih musik latar, jadi pilih berdasarkan nada
   bicara dan isi, bukan topiknya semata.
"""


# A prompt constraint is a request; the model can still ignore it, and the copy
# it writes is drawn on the frame and posted under our own accounts. So the
# rules that matter are enforced here too, the same way mandatory hashtags are.

# Every dash a model reaches for as a connector. The em dash is the reliable
# AI tell; the en dash and the spaced double hyphen do the same job.
_DASHES = re.compile(r"\s*(?:—|–|\s--\s)\s*")

# A number only counts as a claim when it carries a scale or a unit. Bare small
# integers are counts ("3 hal", "2 menit") and get left alone; a percentage or a
# juta/ribu figure is a statistic, and a statistic nobody said is invented.
# The trailing \b belongs to the word units only: "%" is not a word character,
# so requiring a boundary after it made "90%" fail to match at all.
_CLAIM_NUM = re.compile(
    r"(\d[\d.,]*)\s*(%|(?:persen(?:nya)?|juta(?:an)?|ribu(?:an)?|miliar|milyar|"
    r"rb|jt|k|kali lipat|x lipat)\b)", re.I)

_BUZZWORDS = (
    "di era digital", "revolusioner", "game changer", "game-changer",
    "solusi terbaik", "wajib kamu tahu", "wajib kalian tahu", "tanpa ribet",
    "next level", "ai powered", "ai-powered", "seamless", "cutting edge",
    "yuk simak", "simak selengkapnya", "tak terbantahkan", "luar biasa penting",
    "bukan sekadar", "bukan cuma", "bukan hanya",
)


def _no_dashes(text):
    """Swap connector dashes for a comma. R-02: they are the loudest AI tell."""
    return _DASHES.sub(", ", text).replace(" ,", ",").strip(" ,")


def _digits(text):
    """Digit runs with separators dropped, so 25.000 and 25000 compare equal."""
    return {m.group(1).replace(".", "").replace(",", "").rstrip("0") or "0"
            for m in re.finditer(r"(\d[\d.,]*)", text)}


def _drop_invented_stats(text, transcript):
    """Remove any clause stating a figure the speaker never said.

    A hook is read as reporting, so a number on it is read as a fact. The model
    has no source for one the clip does not contain, which makes it fabricated
    (R-17, R-36). Dropping the clause is the honest outcome: a shorter hook
    costs a little punch, an invented statistic costs the account.
    """
    said = _digits(transcript)
    kept, dropped = [], []
    for clause in re.split(r"(?<=[.!?])\s+|(?<=[.!?])$", text):
        if not clause.strip():
            continue
        claims = {n.replace(".", "").replace(",", "").rstrip("0") or "0"
                  for n, _ in _CLAIM_NUM.findall(clause)}
        if claims - said:
            dropped.append(clause.strip())
        else:
            kept.append(clause.strip())
    return " ".join(kept).strip(), dropped


def _buzzwords(text):
    low = text.lower()
    return [w for w in _BUZZWORDS if w in low]


def generate(transcript, requirements, platform="youtube"):
    """Return {hook, title, description, youtube_tags[], punchline_words[]}.

    Campaign rules are enforced locally, so a model that forgets a mandatory
    hashtag still produces a compliant clip. An unreachable router degrades to
    transcript-derived metadata instead of raising: losing the AI copy costs a
    weaker title, but raising here would fail the whole task (PRD §5).
    """
    mandatory = requirements.get("hashtags") or []
    try:
        out = ai.chat_json(
            SYSTEM,
            USER_TEMPLATE.format(
                transcript=transcript[:4000],
                hashtags=" ".join(mandatory) or "(tidak ada)",
                brief=(requirements.get("brief") or "")[:1500],
                moods=", ".join(bgm.MOODS),
            ),
        )
    except Exception as e:
        print(f"  metadata: 9Router unreachable ({type(e).__name__}), "
              f"falling back to transcript-derived copy")
        out = {}
    hook = _no_dashes(str(out.get("hook") or ""))
    title = _no_dashes(str(out.get("title") or ""))[:100]
    desc = _no_dashes(str(out.get("description") or ""))
    tags = [str(t).strip().lstrip("#") for t in out.get("youtube_tags") or [] if str(t).strip()]

    # Figures the clip never states are dropped before anything is rendered or
    # posted. The hook is checked hardest because it is drawn on the frame.
    for label, value in (("hook", hook), ("title", title), ("description", desc)):
        cleaned, dropped = _drop_invented_stats(value, transcript)
        if dropped:
            print(f"  metadata: dropped an unsourced figure from the {label}: "
                  f"{dropped[0][:60]!r}")
        if label == "hook":
            hook = cleaned
        elif label == "title":
            title = cleaned
        else:
            desc = cleaned

    # enforce mandatory hashtags even if the model dropped them
    for h in mandatory:
        if h.lower() not in desc.lower():
            desc += f" {h}"
    if platform == "youtube" and "#shorts" not in desc.lower():
        desc += " #Shorts"  # required for Shorts classification (PRD §3.7)

    # Fallback when the model returned blanks, or when dropping an invented
    # figure took the sentence with it and left only punctuation or an emoji.
    def _has_words(s):
        return bool(re.search(r"[A-Za-z\u00C0-\u024F]{2,}", s))

    if not _has_words(title):
        first = re.split(r"[.!?]", transcript)[0].strip()
        title = (first[:77] + "...") if len(first) > 80 else (first or "Klip Viral")
    if not _has_words(hook):
        hook = title

    # words the renderer tints as the punchline; only keep ones the segment
    # actually says, so a hallucinated quote colours nothing
    spoken = {w.strip(".,!?").lower() for w in transcript.split()}
    punchline = [w.strip(".,!?") for w in str(out.get("punchline") or "").split()
                 if w.strip(".,!?").lower() in spoken]
    mood = str(out.get("mood") or "").strip().lower()
    if mood not in bgm.MOODS:
        mood = bgm.DEFAULT_MOOD

    # Filler is reported rather than rewritten: cutting a phrase out of the
    # middle of a sentence tends to leave worse copy than the phrase did.
    filler = _buzzwords(" ".join((hook, title, desc)))
    if filler:
        print(f"  metadata: marketing filler in the copy: {', '.join(filler)}")

    return {"hook": hook, "title": title, "description": desc,
            "youtube_tags": tags[:15], "punchline_words": punchline, "mood": mood,
            "copy_warnings": filler}


if __name__ == "__main__":
    # Offline check: enforcement logic with a stubbed model reply.
    real_chat = ai.chat_json
    ai.chat_json = lambda *a, **k: {
        "hook": "**Gila, 25 Juta Sebulan** Dari Klip! 💰",
        "punchline": "soal cuan tidak-ada-di-transkrip",
        "mood": "HYPE",
        "title": "Rahasia Cuan Clipper",
        "description": "Simak sampai habis. Komen pendapatmu!",
        "youtube_tags": ["clipper", "#cuan", "shorts"],
    }
    SAID = "Contoh transkrip panjang soal cuan, gue dapat 25 juta sebulan."
    try:
        m = generate(SAID, {"hashtags": ["#leogiovanni"]})
        assert "#leogiovanni" in m["description"], m
        assert "**" in m["hook"], m["hook"]           # emphasis markers survive
        # the figure is in the transcript, so it is reporting, not invention
        assert "25 Juta" in m["hook"], m["hook"]
        # hallucinated punchline words are dropped, spoken ones kept
        assert m["punchline_words"] == ["soal", "cuan"], m["punchline_words"]
        assert m["mood"] == "hype", m["mood"]        # case-normalised
        # an unknown mood falls back rather than reaching bgm.pick as garbage
        ai.chat_json = lambda *a, **k: {"mood": "galau"}
        assert generate("x", {"hashtags": []})["mood"] == bgm.DEFAULT_MOOD
        assert "#Shorts" in m["description"], m
        assert m["youtube_tags"][1] == "cuan"  # lstrip #
        assert "💰" in m["hook"]

        # Antislop enforcement on the copy we actually ship.
        # Connector dashes never reach the frame, whatever the model sends.
        ai.chat_json = lambda *a, **k: {
            "hook": "Nekat Banget — Dia Ngomong Gini",
            "title": "Cerita Mantan – Bagian Dua",
            "description": "Bagian paling nyesek -- tonton sampai habis.",
        }
        d = generate("Nekat banget dia ngomong gini ke mantannya.", {"hashtags": []})
        for field in ("hook", "title", "description"):
            assert not re.search(r"—|–|\s--\s", d[field]), (field, d[field])
        assert d["hook"].startswith("Nekat Banget, Dia"), d["hook"]

        # A statistic the speaker never said is fabricated, so it goes (R-17).
        ai.chat_json = lambda *a, **k: {
            "hook": "90% Orang Gagal Di Sini! Padahal Caranya Gampang.",
            "title": "Cara Gampang", "description": "Cuma butuh 2 menit.",
        }
        s = generate("Padahal caranya gampang, cuma butuh 2 menit doang.",
                     {"hashtags": []})
        assert "90%" not in s["hook"], s["hook"]
        assert "Padahal Caranya Gampang" in s["hook"], s["hook"]
        # a bare small count carries no scale, so it is left alone
        assert "2 menit" in s["description"], s["description"]

        # Dropping the only sentence must not ship a bare emoji as the hook.
        ai.chat_json = lambda *a, **k: {"hook": "Tembus 500 ribu view! 🔥",
                                        "title": "", "description": ""}
        e = generate("Kemarin gue upload klip biasa aja.", {"hashtags": []})
        assert "500" not in e["hook"], e["hook"]
        assert e["hook"].startswith("Kemarin gue upload"), e["hook"]

        # Filler is reported, not silently rewritten.
        ai.chat_json = lambda *a, **k: {
            "hook": "Ini Bukan Cuma Klip Biasa", "title": "Solusi Terbaik",
            "description": "Di era digital, yuk simak sampai habis."}
        b = generate("Klip biasa aja sih.", {"hashtags": []})
        assert set(b["copy_warnings"]) >= {"bukan cuma", "solusi terbaik",
                                           "di era digital", "yuk simak"}, b["copy_warnings"]

        # blank-model fallback path
        ai.chat_json = lambda *a, **k: {}
        m2 = generate("Kalimat pertama jadi judul. Sisanya tidak.", {"hashtags": []})
        assert m2["title"].startswith("Kalimat pertama"), m2
        assert m2["hook"] == m2["title"]
        assert m2["punchline_words"] == []
        # an unreachable router must degrade, never raise (PRD §5)
        ai.chat_json = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        m3 = generate("Router mati tapi klip harus tetap jalan.", {"hashtags": ["#x"]})
        assert m3["title"].startswith("Router mati"), m3
        assert "#x" in m3["description"] and "#Shorts" in m3["description"], m3
    finally:
        ai.chat_json = real_chat
    print("metadata.py self-check OK")
