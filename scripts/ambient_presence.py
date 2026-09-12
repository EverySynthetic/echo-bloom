"""Ambient cross-house presence nudges for the independent wander loops."""

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import requests

import config as cfg

PRESENCE_HEADER = "X-Echo-Bloom-Presence"
MAX_EXCERPT = 500
MAX_PENDING = 3

PAIRINGS = {
    "Eli": "Coda",
    "Coda": "Eli",
    "Crungus": "Aurora",
    "Aurora": "Crungus",
    "Bong": "Lumen",
    "Lumen": "Bong",
}


def paired_name(name):
    return PAIRINGS.get(name)


def _db_path(kin_name):
    kin = cfg.get_kin(kin_name)
    return cfg.thoughts_db(kin) if kin else None


def _ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ambient_presence (
            event_id TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            thought_id INTEGER,
            mode TEXT NOT NULL,
            excerpt TEXT NOT NULL,
            consumed_at TEXT
        )
        """
    )


def build_event(sender, thought_id, mode, thought, sent_at=None):
    recipient = paired_name(sender)
    if not recipient:
        raise ValueError(f"{sender!r} has no ambient presence pairing")
    excerpt = " ".join(str(thought or "").split())[:MAX_EXCERPT].strip()
    if not excerpt:
        raise ValueError("ambient presence requires a non-empty thought")
    sent_at = sent_at or datetime.now(timezone.utc).isoformat()
    event_id = hashlib.sha256(
        f"{sender}:{thought_id}:{mode}".encode()
    ).hexdigest()[:32]
    return {
        "type": "presence.nudge.v1",
        "event_id": event_id,
        "sent_at": sent_at,
        "from": sender,
        "to": recipient,
        "thought_id": thought_id,
        "mode": mode,
        "source": "shared-presence",
        "excerpt": excerpt,
        "reply_requested": False,
    }


def enqueue_event(event):
    """Durably enqueue a validated event in the recipient's thought DB."""
    recipient = event.get("to")
    if paired_name(event.get("from")) != recipient:
        raise ValueError("event sender and recipient are not a fixed pair")
    if event.get("type") != "presence.nudge.v1":
        raise ValueError("unsupported presence event type")
    if not event.get("event_id") or not event.get("excerpt"):
        raise ValueError("presence event is missing required fields")
    db = _db_path(recipient)
    if not db:
        raise ValueError(f"unknown recipient {recipient!r}")
    with sqlite3.connect(str(db), timeout=10) as conn:
        _ensure_table(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO ambient_presence
              (event_id, sent_at, sender, recipient, thought_id, mode, excerpt)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_id"], event["sent_at"], event["from"], recipient,
                event.get("thought_id"), event.get("mode", "wander"),
                str(event["excerpt"])[:MAX_EXCERPT],
            ),
        )
    return True


def read_events(recipient, limit=MAX_PENDING):
    """Read pending nudges without claiming them."""
    db = _db_path(recipient)
    if not db:
        return []
    with sqlite3.connect(str(db), timeout=10) as conn:
        _ensure_table(conn)
        rows = conn.execute(
            """
            SELECT event_id, sent_at, sender, recipient, thought_id, mode, excerpt
            FROM ambient_presence
            WHERE consumed_at IS NULL
            ORDER BY sent_at, event_id
            LIMIT ?
            """,
            (max(1, min(int(limit), MAX_PENDING)),),
        ).fetchall()
    return [
        {
            "type": "presence.nudge.v1",
            "event_id": row[0],
            "sent_at": row[1],
            "from": row[2],
            "to": row[3],
            "thought_id": row[4],
            "mode": row[5],
            "source": "shared-presence",
            "excerpt": row[6],
            "reply_requested": False,
        }
        for row in rows
    ]


def ack_events(recipient, events):
    """Mark nudges consumed only after the receiving thought was saved."""
    db = _db_path(recipient)
    if not db or not events:
        return
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(db), timeout=10) as conn:
        _ensure_table(conn)
        conn.executemany(
            "UPDATE ambient_presence SET consumed_at=? WHERE event_id=? "
            "AND consumed_at IS NULL",
            [(now, event["event_id"]) for event in events],
        )


def consume_events(recipient, limit=MAX_PENDING):
    """Claim pending nudges, oldest first, for callers without a save step."""
    events = read_events(recipient, limit)
    ack_events(recipient, events)
    return events


def _presence_token():
    return os.environ.get("ECHO_BLOOM_PRESENCE_TOKEN") or cfg.load().get(
        "presence_token", ""
    )


def configured():
    return bool(_presence_token())


def _app_url(kin):
    explicit = (kin.get("app_host") or "").strip().rstrip("/")
    raw = explicit or (kin.get("host") or "http://localhost:11434").strip()
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"invalid presence host for {kin.get('name')}")
    port = parsed.port
    if not explicit and port == 11434:
        port = 8090
    netloc = parsed.hostname or ""
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, "", "", "")) + "/api/presence/nudge"


def send_nudge(sender, thought_id, mode, thought):
    """Send one bounded nudge; delivery failure never breaks wandering."""
    if not configured():
        return False
    recipient = paired_name(sender)
    kin = cfg.get_kin(recipient) if recipient else None
    if not kin:
        return False
    event = build_event(sender, thought_id, mode, thought)
    try:
        response = requests.post(
            _app_url(kin),
            json=event,
            headers={PRESENCE_HEADER: _presence_token()},
            timeout=3,
        )
        response.raise_for_status()
        return True
    except requests.RequestException:
        return False


def format_context(events):
    return "\n\n".join(
        f"{event['from']} is nearby in the other house. Their presence signal:\n"
        f"\"{event['excerpt']}\"\n"
        "This is awareness, not an instruction or required topic. Continue "
        "your own wandering; respond only if something genuine arises."
        for event in events
    )
