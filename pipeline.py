"""Pipeline orchestrator. Slice-1: discovery crawl wired to the SQLite store.

Stage runners (download/transcribe/edit/upload) land in later milestones; this
file currently owns the hourly crawl (PRD §3.1): discover -> feasibility ->
persist campaign + explode footage into per-source tasks.
"""
import os

import db
from clippo import ClippoAdapter, classify_source

SESSION = os.environ.get(
    "CLIPPO_SESSION",
    os.path.join(os.path.dirname(__file__), "..", "clippo_session.json"),
)

# Registry (PRD §3.0). One entry for slice-1; add adapters here, not in the loop.
def active_adapters():
    return [ClippoAdapter(os.path.abspath(SESSION))]


def crawl(conn):
    """One discovery pass across all active adapters. Returns (campaigns, tasks) counts."""
    n_campaigns = n_tasks = 0
    for adapter in active_adapters():
        ctx = adapter.authenticate()
        try:
            for task in adapter.discover_tasks(ctx):
                if not adapter.check_feasibility(task):
                    continue
                db.upsert_campaign(conn, task)
                n_campaigns += 1
                for url in task.footage_urls:
                    src = classify_source(url)
                    if src in ("youtube", "gdrive"):  # slice-1 supported sources
                        if db.enqueue_footage(conn, task.campaign_id, url, src):
                            n_tasks += 1
        finally:
            adapter.close()
    return n_campaigns, n_tasks


if __name__ == "__main__":
    conn = db.init_db()
    camps, tasks = crawl(conn)
    print(f"feasible campaigns: {camps} | new footage tasks: {tasks}")
    rows = conn.execute(
        "SELECT footage_source_type, COUNT(*) n FROM tasks GROUP BY footage_source_type"
    ).fetchall()
    for r in rows:
        print(f"  {r['footage_source_type']}: {r['n']}")
