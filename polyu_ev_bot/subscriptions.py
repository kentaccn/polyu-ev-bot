"""Persist one opt-in charger subscription per private Telegram chat."""

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time

FREQUENCIES = (1, 5, 15, 30, 60)
FLOORS = ("all", "1", "2", "3")


@dataclass(frozen=True)
class Subscription:
    chat_id: int
    floor: str
    minutes: int
    next_at: float
    unavailable_notified: bool

    @property
    def view(self):
        return "all" if self.floor == "all" else "level" + self.floor

    @property
    def label(self):
        return "All floors" if self.floor == "all" else "Level " + self.floor


class Subscriptions:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
            chat_id INTEGER PRIMARY KEY,
            floor TEXT NOT NULL,
            minutes INTEGER NOT NULL,
            next_at REAL NOT NULL,
            unavailable_notified INTEGER NOT NULL DEFAULT 0
        )""")
        self.db.commit()

    @staticmethod
    def row(row):
        return Subscription(**dict(row)) if row else None

    def get(self, chat_id):
        return self.row(self.db.execute("SELECT * FROM subscriptions WHERE chat_id=?", (chat_id,)).fetchone())

    def set(self, chat_id, floor, minutes, now=None):
        if floor not in FLOORS or minutes not in FREQUENCIES:
            raise ValueError("Invalid subscription settings")
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("""INSERT INTO subscriptions VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(chat_id) DO UPDATE SET floor=excluded.floor, minutes=excluded.minutes,
                next_at=excluded.next_at, unavailable_notified=0""", (chat_id, floor, minutes, now + minutes * 60))

    def delete(self, chat_id):
        with self.db:
            self.db.execute("DELETE FROM subscriptions WHERE chat_id=?", (chat_id,))

    def due(self, now=None):
        now = time.time() if now is None else now
        return [self.row(row) for row in self.db.execute(
            "SELECT * FROM subscriptions WHERE next_at<=? ORDER BY next_at LIMIT 20", (now,))]

    def reschedule(self, chat_id, unavailable=False, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("UPDATE subscriptions SET next_at=? + minutes*60, unavailable_notified=? WHERE chat_id=?",
                            (now, int(unavailable), chat_id))

    def defer(self, chat_id, seconds):
        with self.db:
            self.db.execute("UPDATE subscriptions SET next_at=? WHERE chat_id=?", (time.time() + seconds, chat_id))

    def poll_timeout(self):
        deadline = self.db.execute("SELECT MIN(next_at) FROM subscriptions").fetchone()[0]
        return 25 if deadline is None else max(1, min(25, int(deadline - time.time())))

    def close(self):
        self.db.close()
