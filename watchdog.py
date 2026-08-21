"""Failure router — reads the DB, decides who deals with each failure.

No LLM. Sorting a known error into one of three buckets is a lookup table, and
a table cannot hallucinate "all clear" over a real outage. The unmatched
bucket is the only place a model would ever earn its tokens here.

    TRANSIENT   quota, rate limit, network — requeue the task, stay quiet
    NEEDS_HUMAN expired session/token/cookies — nobody can fix this in code
    CODE_BUG    anything the table does not recognise -> dev agent ticket

One fingerprint gets one ticket. A recurring failure bumps a counter instead
of opening a second ticket, so a crash loop cannot spam the channel or hand
the dev agent the same job fifty times.
"""
import hashlib
import json
import os
import re
import sys

import db
import notify

MAX_ATTEMPTS = int(os.environ.get("CLIPPER_MAX_FIX_ATTEMPTS", "1"))
MAX_TICKETS_PER_DAY = int(os.environ.get("CLIPPER_MAX_TICKETS_PER_DAY", "3"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    fingerprint TEXT PRIMARY KEY,
    kind        TEXT,      -- TRANSIENT | NEEDS_HUMAN | CODE_BUG
    stage       TEXT,
    error       TEXT,
    task_id     INTEGER,
    seen        INTEGER DEFAULT 1,
    attempts    INTEGER DEFAULT 0,
    status      TEXT,      -- open | escalated | closed
    first_seen  TEXT,
    last_seen   TEXT
);
"""

# First match wins, so the specific patterns lead. Every entry carries the
# sentence a human needs to act on, because "NEEDS_HUMAN" alone at 3am is not
# an instruction.
RULES = [
    (r"uploadLimitExceeded|quotaExceeded|dailyLimitExceeded", "TRANSIENT",
     "YouTube daily quota reached — retries on the next run"),
    (r"\b429\b|rate.?limit|Too Many Requests", "TRANSIENT",
     "rate limited — retries on the next run"),
    (r"too many accesses|timed?.out|TimeoutError|Connection|Temporary failure|502|503",
     "TRANSIENT", "network hiccup — retries on the next run"),
    (r"invalid_grant|refresh_token|token has been expired|Token has been revoked",
     "NEEDS_HUMAN", "a YouTube OAuth token died — re-run the OAuth flow and "
                    "drop the new token_<account>.pickle into the token dir"),
    (r"storage_state|\b401\b|\b403\b|login|sign in|session", "NEEDS_HUMAN",
     "the Clippo session expired — log in again and replace clippo_session.json"),
    (r"cookies|360p|Sign in to confirm", "NEEDS_HUMAN",
     "YouTube cookies are stale — export a fresh cookies.txt from a private "
     "window and replace the old one"),
    (r"no campaign_ready|daily post limit", "TRANSIENT",
     "no account has a free slot today — resumes tomorrow"),
]


def init(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def classify(error):
    """(kind, advice) for one error string."""
    for pattern, kind, advice in RULES:
        if re.search(pattern, error or "", re.I):
            return kind, advice
    return "CODE_BUG", "unrecognised failure — needs a look"


def fingerprint(stage, error):
    """Stable id for 'this same failure again'.

    Digits are stripped first so that ids, timestamps and byte counts inside a
    message do not turn one recurring fault into a hundred distinct ones.
    """
    core = re.sub(r"\d+", "#", (error or "").split("\n")[0])[:200]
    return hashlib.sha1(f"{stage}|{core}".encode("utf-8")).hexdigest()[:8]


def _split(error):
    """pipeline stores 'STAGE: ExcType: message' — recover the stage."""
    head, sep, rest = (error or "").partition(": ")
    return (head, rest) if sep and head.isupper() else ("", error or "")


def scan(conn, notifier=notify.send):
    """Route every FAILED task that has not been routed yet.

    Returns a summary dict. Requeues transient failures, pings a human for the
    ones only a human can fix, and opens dev tickets for the rest.
    """
    init(conn)
    summary = {"TRANSIENT": 0, "NEEDS_HUMAN": 0, "CODE_BUG": 0,
               "requeued": 0, "tickets": 0}
    tickets_today = conn.execute(
        "SELECT COUNT(*) FROM incidents WHERE status='open' AND kind='CODE_BUG' "
        "AND date(first_seen)=date('now')").fetchone()[0]

    for row in conn.execute(
            "SELECT task_id, error FROM tasks WHERE status='FAILED'").fetchall():
        stage, msg = _split(row["error"])
        kind, advice = classify(row["error"])
        fp = fingerprint(stage, msg)
        seen = conn.execute("SELECT * FROM incidents WHERE fingerprint=?",
                            (fp,)).fetchone()
        summary[kind] += 1

        if seen:
            conn.execute("UPDATE incidents SET seen=seen+1, last_seen=?, task_id=? "
                         "WHERE fingerprint=?", (db._now(), row["task_id"], fp))
        else:
            conn.execute(
                """INSERT INTO incidents (fingerprint, kind, stage, error, task_id,
                                          status, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (fp, kind, stage, (row["error"] or "")[:500], row["task_id"],
                 "open", db._now(), db._now()))
        conn.commit()

        if kind == "TRANSIENT":
            db.set_task_status(conn, row["task_id"], "DISCOVERED", None)
            conn.execute("UPDATE incidents SET status='closed' WHERE fingerprint=?", (fp,))
            conn.commit()
            summary["requeued"] += 1
            continue

        if seen:          # already routed once; the counter is the whole update
            continue

        if kind == "NEEDS_HUMAN":
            notifier(f"task {row['task_id']} failed in {stage or 'crawl'}\n"
                     f"```{(row['error'] or '')[:600]}```\n{advice}",
                     title=f"Human needed [{fp}]", tag="@here")
        else:
            if tickets_today >= MAX_TICKETS_PER_DAY:
                conn.execute("UPDATE incidents SET status='escalated' "
                             "WHERE fingerprint=?", (fp,))
                conn.commit()
                notifier(f"ticket cap ({MAX_TICKETS_PER_DAY}/day) reached, holding "
                         f"[{fp}] — {advice}", title="Dev queue paused")
                continue
            tickets_today += 1
            summary["tickets"] += 1
            notifier(f"{stage or 'crawl'}: {advice}\n```{(row['error'] or '')[:600]}```",
                     title=f"Dev ticket [{fp}]")
    return summary


def open_tickets(conn):
    """Work waiting for the dev agent, as plain records.

    An incident that already used up its attempts is escalated instead of
    handed out again — a fix that failed twice is a conversation, not a retry.
    """
    init(conn)
    out = []
    for r in conn.execute(
            "SELECT * FROM incidents WHERE status='open' AND kind='CODE_BUG' "
            "ORDER BY last_seen").fetchall():
        if r["attempts"] >= MAX_ATTEMPTS:
            conn.execute("UPDATE incidents SET status='escalated' WHERE fingerprint=?",
                         (r["fingerprint"],))
            conn.commit()
            continue
        out.append({
            "fingerprint": r["fingerprint"], "kind": r["kind"], "stage": r["stage"],
            "error": notify.redact(r["error"]), "task_id": r["task_id"],
            "seen": r["seen"], "attempts": r["attempts"],
            "branch": f"fix/{r['fingerprint']}",
        })
    return out


def mark_attempt(conn, fp):
    init(conn)
    conn.execute("UPDATE incidents SET attempts=attempts+1 WHERE fingerprint=?", (fp,))
    conn.commit()


def close(conn, fp):
    init(conn)
    conn.execute("UPDATE incidents SET status='closed' WHERE fingerprint=?", (fp,))
    conn.commit()


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        import tempfile

        c = db.init_db(os.path.join(tempfile.mkdtemp(), "w.sqlite"))
        sent = []

        def fake(text, title=None, tag=None):
            sent.append((title or "", text))
            return True

        assert classify("uploadLimitExceeded: quota")[0] == "TRANSIENT"
        assert classify("HttpError 429 Too Many Requests")[0] == "TRANSIENT"
        assert classify("invalid_grant: Token has been expired")[0] == "NEEDS_HUMAN"
        assert classify("Error 403 while loading storage_state")[0] == "NEEDS_HUMAN"
        assert classify("ffmpeg failed: filter graph broken")[0] == "CODE_BUG"

        # ids ignore the numbers inside a message, so one fault is one incident
        assert fingerprint("EDITING", "ffmpeg failed on frame 129") \
            == fingerprint("EDITING", "ffmpeg failed on frame 8842")
        assert fingerprint("EDITING", "x") != fingerprint("UPLOADING", "x")

        for tid, err in (
                (1, "UPLOADING: HttpError: uploadLimitExceeded"),
                (2, "TRANSCRIBING: RuntimeError: invalid_grant bad token"),
                (3, "EDITING: RuntimeError: ffmpeg failed: no such filter"),
        ):
            c.execute("INSERT INTO tasks (task_id, campaign_id, footage_url, status, error)"
                      " VALUES (?,?,?,?,?)", (tid, "c1", f"http://x/{tid}", "FAILED", err))
        c.commit()

        s = scan(c, notifier=fake)
        assert s["requeued"] == 1, s
        assert c.execute("SELECT status FROM tasks WHERE task_id=1").fetchone()[0] \
            == "DISCOVERED"                                   # transient requeued
        titles = " | ".join(t for t, _ in sent)
        assert "Human needed" in titles and "Dev ticket" in titles, titles
        assert len(sent) == 2, sent                           # transient stayed quiet
        assert "re-run the OAuth flow" in " ".join(b for _, b in sent)

        # the same failure again bumps the counter, it does not notify twice
        sent.clear()
        scan(c, notifier=fake)
        assert sent == [], sent
        assert c.execute("SELECT seen FROM incidents WHERE kind='CODE_BUG'"
                         ).fetchone()[0] == 2

        tickets = open_tickets(c)
        assert len(tickets) == 1 and tickets[0]["stage"] == "EDITING", tickets
        assert tickets[0]["branch"] == "fix/" + tickets[0]["fingerprint"]
        mark_attempt(c, tickets[0]["fingerprint"])
        assert open_tickets(c) == []                          # one attempt, then escalate
        assert c.execute("SELECT status FROM incidents WHERE kind='CODE_BUG'"
                         ).fetchone()[0] == "escalated"

        # secrets never reach the ticket payload
        c.execute("UPDATE incidents SET status='open', attempts=0, "
                  "error='boom refresh_token: 1//supersecret' WHERE kind='CODE_BUG'")
        c.commit()
        assert "supersecret" not in json.dumps(open_tickets(c))
        print("watchdog.py self-check OK")
    elif "--tickets" in sys.argv:
        print(json.dumps(open_tickets(db.init_db()), indent=2))
    else:
        print(json.dumps(scan(db.init_db()), indent=2))
