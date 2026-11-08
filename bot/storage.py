"""SQLite: пользователи, последние известные графики, отправленные напоминания."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .poe import DaySchedule

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id  INTEGER PRIMARY KEY,
    queue    TEXT,
    notify   INTEGER NOT NULL DEFAULT 1,
    remind   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS schedules (
    day        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS reminders (
    chat_id   INTEGER NOT NULL,
    day       TEXT NOT NULL,
    start_min INTEGER NOT NULL,
    PRIMARY KEY (chat_id, day, start_min)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class User:
    chat_id: int
    queue: str | None
    notify: bool
    remind: bool


class Storage:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- users ---
    def get_user(self, chat_id: int) -> User:
        row = self.db.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO users(chat_id) VALUES (?)", (chat_id,))
            self.db.commit()
            return User(chat_id, None, True, True)
        return User(row["chat_id"], row["queue"], bool(row["notify"]), bool(row["remind"]))

    def set_queue(self, chat_id: int, queue: str) -> None:
        self.get_user(chat_id)
        self.db.execute("UPDATE users SET queue=? WHERE chat_id=?", (queue, chat_id))
        self.db.commit()

    def toggle(self, chat_id: int, column: str) -> bool:
        assert column in ("notify", "remind")
        self.get_user(chat_id)
        self.db.execute(f"UPDATE users SET {column} = 1 - {column} WHERE chat_id=?", (chat_id,))
        self.db.commit()
        return getattr(self.get_user(chat_id), column)

    def users_with_queue(self) -> list[User]:
        rows = self.db.execute("SELECT * FROM users WHERE queue IS NOT NULL").fetchall()
        return [User(r["chat_id"], r["queue"], bool(r["notify"]), bool(r["remind"])) for r in rows]

    def delete_user(self, chat_id: int) -> None:
        self.db.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
        self.db.commit()

    # --- schedules ---
    def save_schedule(self, s: DaySchedule) -> None:
        payload = json.dumps({"queues": s.queues, "no_outages": s.no_outages, "note": s.note},
                             ensure_ascii=False)
        self.db.execute(
            "INSERT INTO schedules(day, payload) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET payload=excluded.payload, fetched_at=datetime('now')",
            (s.day.isoformat(), payload),
        )
        self.db.commit()

    def get_schedule(self, day: date) -> DaySchedule | None:
        row = self.db.execute("SELECT payload FROM schedules WHERE day=?", (day.isoformat(),)).fetchone()
        if row is None:
            return None
        p = json.loads(row["payload"])
        return DaySchedule(day=day, queues=p["queues"], no_outages=p["no_outages"], note=p.get("note", ""))

    # --- reminders ---
    def mark_reminded(self, chat_id: int, day: date, start_min: int) -> bool:
        """True, если напоминание ещё не отправлялось (и теперь помечено)."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO reminders VALUES (?, ?, ?)", (chat_id, day.isoformat(), start_min)
        )
        self.db.commit()
        return cur.rowcount == 1

    def cleanup(self, before: date) -> None:
        self.db.execute("DELETE FROM reminders WHERE day < ?", (before.isoformat(),))
        self.db.execute("DELETE FROM schedules WHERE day < ?", (before.isoformat(),))
        self.db.commit()

    # --- meta ---
    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()
