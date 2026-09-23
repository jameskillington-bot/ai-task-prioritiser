"""SQLite state so each ticket is processed and each journey claimed once."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import ClaimStatus, Journey, Ticket

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, seen_at TEXT);
CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS journeys (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    data TEXT NOT NULL,
    status TEXT NOT NULL,
    delay TEXT,
    compensation TEXT,
    amount REAL,
    operator TEXT,
    claim_reference TEXT,
    arrival_override TEXT,
    notes TEXT,
    updated_at TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def message_seen(self, msg_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM messages WHERE id=?", (msg_id,)).fetchone() is not None

    def mark_message(self, msg_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO messages VALUES (?, ?)", (msg_id, datetime.now().isoformat()))
        self.db.commit()

    def add_ticket(self, ticket: Ticket, journeys: list[Journey]) -> int:
        tid = ticket.ticket_id()
        self.db.execute("INSERT OR REPLACE INTO tickets VALUES (?, ?)", (tid, ticket.model_dump_json()))
        added = 0
        for j in journeys:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO journeys (id, ticket_id, data, status, updated_at) VALUES (?,?,?,?,?)",
                (j.journey_id(), tid, j.model_dump_json(), ClaimStatus.awaiting_travel.value, datetime.now().isoformat()),
            )
            added += cur.rowcount
        self.db.commit()
        return added

    def ticket(self, tid: str) -> Ticket:
        row = self.db.execute("SELECT data FROM tickets WHERE id=?", (tid,)).fetchone()
        return Ticket.model_validate_json(row["data"])

    def journeys(self, *statuses: ClaimStatus) -> list[sqlite3.Row]:
        q = "SELECT * FROM journeys"
        if statuses:
            q += f" WHERE status IN ({','.join('?' * len(statuses))})"
        return self.db.execute(q + " ORDER BY id", [s.value for s in statuses]).fetchall()

    def journey(self, jid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM journeys WHERE id=?", (jid,)).fetchone()

    def claimed_total(self, ticket_id: str, exclude: str) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM journeys WHERE ticket_id=? AND id<>? AND status IN (?,?,?)",
            (ticket_id, exclude, ClaimStatus.submitted.value, ClaimStatus.eligible.value, ClaimStatus.needs_review.value),
        ).fetchone()
        return float(row[0])

    def update(self, jid: str, **fields) -> None:
        for k, v in list(fields.items()):
            if isinstance(v, ClaimStatus):
                fields[k] = v.value
            elif hasattr(v, "model_dump_json"):
                fields[k] = v.model_dump_json()
            elif isinstance(v, (list, dict)):
                fields[k] = json.dumps(v)
        fields["updated_at"] = datetime.now().isoformat()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE journeys SET {cols} WHERE id=?", [*fields.values(), jid])
        self.db.commit()
