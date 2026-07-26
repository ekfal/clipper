"""ClippoAdapter — concrete task-source adapter for Clippo.id.

Method signatures follow the PRD §3.0 contract (authenticate / discover_tasks /
check_feasibility / submit_clip) so extracting a TaskSourceAdapter ABC later is
add-an-ABC, not a restructure. No ABC yet (only one implementation — YAGNI).

Discovery hits Clippo's JSON API (SPA backend) with the reused Playwright
session cookies — no DOM scraping. submit_clip is a stub for slice-1 (Clippo
accepts only TikTok/IG URLs; wired once those platforms are live).
"""
import html
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

API_BASE = "https://app.clippo.id/api/proxy"
SOURCE = "clippo"

# platformSupported enum, decoded from campaign briefs during recon.
PLATFORM_ENUM = {0: "tiktok", 1: "instagram"}
STATUS_ACTIVE = 3  # observed value for live campaigns


@dataclass
class Task:
    """Normalized task — identical shape regardless of source platform (PRD §3.0)."""
    campaign_id: str
    source_platform: str
    footage_urls: list = field(default_factory=list)
    requirements: dict = field(default_factory=dict)
    budget_remaining: int = 0
    platform_specific_data: dict = field(default_factory=dict)
    status: str = ""  # DB campaign status (we store "active"/"inactive")


def classify_source(url: str) -> str:
    """Route footage URL to a downloader family. fetch.py (M2) consumes this."""
    host = urlparse(url).netloc.lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "drive.google.com" in host:
        return "gdrive"
    if "cdn.discordapp.com" in host:
        return "discord"
    return "unknown"


def _strip_html(s: str) -> str:
    if not s:
        return ""
    text = re.sub(r"<[^>]+>", " ", s)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize(c: dict) -> Task:
    """Map one raw Clippo campaign JSON object to a Task."""
    platforms = [PLATFORM_ENUM.get(p, str(p)) for p in (c.get("platformSupported") or [])]
    return Task(
        campaign_id=c["id"],
        source_platform=SOURCE,
        footage_urls=list(c.get("assetVideoUrls") or []),
        requirements={
            "title": c.get("title"),
            "brief": _strip_html(c.get("requirementsV2")),
            "hashtags": c.get("hashtags") or [],
            "platforms_required": platforms,
            "languages": c.get("languages") or [],
            "duration_type": c.get("durationType"),
            "rate_per_kview": c.get("ratePerKView"),
        },
        budget_remaining=c.get("budgetRemaining") or 0,
        platform_specific_data={"clip_batch_count": c.get("clipBatchCount")},
        status="active" if c.get("status") == STATUS_ACTIVE else "inactive",
    )


class ClippoAdapter:
    def __init__(self, storage_state_path: str):
        self.storage_state_path = storage_state_path
        self._pw = None
        self._browser = None
        self._ctx = None

    def authenticate(self):
        """Load the reused Playwright storage_state; return an API-capable context."""
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(storage_state=self.storage_state_path)
        return self._ctx

    # In-page fetch: the /api/proxy endpoint 403s on raw context.request (missing
    # SPA headers/referer). Running fetch() inside the page replays same-origin
    # credentials + headers exactly as the app does.
    _FETCH_JS = """async (url) => {
        const r = await fetch(url, {credentials: 'include'});
        return {ok: r.ok, status: r.status, body: r.ok ? await r.json() : null};
    }"""

    def discover_tasks(self, ctx, limit: int = 15, max_pages: int = 20) -> list:
        """Crawl all active campaigns via JSON API, return normalized Tasks."""
        page = ctx.new_page()
        page.goto("https://app.clippo.id/campaigns", wait_until="domcontentloaded", timeout=60000)
        tasks, pg = [], 1
        while pg <= max_pages:
            url = f"{API_BASE}/campaigns?limit={limit}&orderBy=recommended&page={pg}"
            res = page.evaluate(self._FETCH_JS, url)
            if not res["ok"]:
                raise RuntimeError(f"campaigns API {res['status']} on page {pg}")
            payload = res["body"].get("data", {})
            for c in payload.get("data", []):
                tasks.append(_normalize(c))
            if pg >= (payload.get("totalPage") or 1):
                break
            pg += 1
        page.close()
        return tasks

    def check_feasibility(self, task: Task) -> bool:
        """Active + budget left + at least one footage source we can fetch.
        Slice-1 does NOT gate on platforms_required: we emit YT clips for
        pipeline validation regardless of the campaign's TikTok/IG requirement.
        """
        if task.status != "active":
            return False
        if task.budget_remaining <= 0:
            return False
        return any(classify_source(u) in ("youtube", "gdrive") for u in task.footage_urls)

    def submit_clip(self, ctx, campaign_id: str, clip_urls: list):
        """PRD §3.11 — fill Clippo 10-slot form. Deferred (slice-1 = YT-only,
        Clippo takes TikTok/IG URLs). Implement at M5 once those are live."""
        raise NotImplementedError("submit_clip deferred to M5 (needs TikTok/IG clips)")

    def close(self):
        for obj in (self._ctx, self._browser):
            try:
                obj and obj.close()
            except Exception:
                pass
        if self._pw:
            self._pw.stop()


if __name__ == "__main__":
    # Offline self-check: normalization + feasibility, no network.
    raw = {
        "id": "c123", "title": "Demo", "platformSupported": [0, 1],
        "budgetRemaining": 500, "status": 3, "hashtags": ["#a"],
        "requirementsV2": "<p>Platforms: TikTok &amp; IG</p>", "durationType": 1,
        "assetVideoUrls": ["https://youtu.be/x", "https://drive.google.com/drive/folders/y"],
        "clipBatchCount": 5, "languages": [0], "ratePerKView": 1000,
    }
    t = _normalize(raw)
    assert t.campaign_id == "c123"
    assert t.requirements["hashtags"] == ["#a"]
    assert t.requirements["platforms_required"] == ["tiktok", "instagram"]
    assert t.requirements["brief"] == "Platforms: TikTok & IG"
    assert t.status == "active"
    assert classify_source("https://youtu.be/x") == "youtube"
    assert classify_source("https://drive.google.com/drive/folders/y") == "gdrive"
    a = ClippoAdapter("dummy.json")
    assert a.check_feasibility(t) is True
    t.budget_remaining = 0
    assert a.check_feasibility(t) is False
    print("clippo.py self-check OK")
