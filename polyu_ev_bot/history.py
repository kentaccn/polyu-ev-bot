"""Append-only source observations and cautious occupancy-based forecasts."""

from datetime import datetime
import hashlib
from pathlib import Path
import sqlite3
from statistics import quantiles
import time
import zlib

from .source import Charger, HKT, Snapshot, SourceError

MAX_SESSION_SECONDS = 4 * 3600  # User-provided charging-limit assumption.
MAX_OBSERVATION_GAP = 150


def stamp(value):
    return datetime.fromtimestamp(value, HKT).strftime("%d %b %H:%M")


class History:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=15)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY, fetched_at REAL NOT NULL,
                source_at REAL, service TEXT, error TEXT,
                page_zlib BLOB, page_sha256 TEXT
            );
            CREATE TABLE IF NOT EXISTS observations (
                sample_id INTEGER NOT NULL REFERENCES samples(id),
                charger INTEGER NOT NULL, level INTEGER NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY (sample_id, charger)
            );
            CREATE TABLE IF NOT EXISTS charger_state (
                charger INTEGER PRIMARY KEY, status TEXT NOT NULL, seen_at REAL NOT NULL,
                last_empty REAL, occupied_since REAL, start_lower REAL, start_upper REAL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY, charger INTEGER NOT NULL,
                start_lower REAL NOT NULL, start_upper REAL NOT NULL,
                end_lower REAL NOT NULL, end_upper REAL NOT NULL, duration REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_charger ON sessions(charger, id);
        """)
        self.db.commit()

    def record(self, snapshot=None, raw=None, error=None, now=None):
        now = time.time() if now is None else now
        if snapshot:
            now = snapshot.fetched_at.timestamp()
        compressed = zlib.compress(raw) if raw is not None else None
        digest = hashlib.sha256(raw).hexdigest() if raw is not None else None
        with self.db:
            cursor = self.db.execute("""INSERT INTO samples
                (fetched_at, source_at, service, error, page_zlib, page_sha256)
                VALUES (?, ?, ?, ?, ?, ?)""", (now, snapshot.updated_at.timestamp() if snapshot else None,
                snapshot.service if snapshot else None, error, compressed, digest))
            if snapshot is None:
                self._invalidate_sessions()
                return
            self.db.executemany("INSERT INTO observations VALUES (?, ?, ?, ?)",
                                [(cursor.lastrowid, c.number, c.level, c.status) for c in snapshot.chargers])
            # Archive stale/unhealthy snapshots, but never use them to infer transitions.
            if snapshot.warning(snapshot.fetched_at):
                self._invalidate_sessions()
                return
            observed = snapshot.updated_at.timestamp()
            for charger in snapshot.chargers:
                self._observe(charger, observed)

    def _invalidate_sessions(self):
        self.db.execute("UPDATE charger_state SET status='Unknown', occupied_since=NULL, start_lower=NULL, start_upper=NULL")

    def _observe(self, charger, observed):
        previous = self.state(charger.number)
        if previous and observed <= previous["seen_at"]:
            return  # Repeated/regressing source timestamps cannot extend a session.
        continuous = previous and 0 < observed - previous["seen_at"] <= MAX_OBSERVATION_GAP
        last_empty = previous["last_empty"] if previous else None
        occupied_since = lower = upper = None
        if charger.status == "Available":
            last_empty = observed
            if continuous and previous["status"] == "Occupied" and previous["start_lower"] is not None:
                lower_start, upper_start = previous["start_lower"], previous["start_upper"]
                duration = (previous["seen_at"] + observed - lower_start - upper_start) / 2
                self.db.execute("""INSERT INTO sessions
                    (charger, start_lower, start_upper, end_lower, end_upper, duration)
                    VALUES (?, ?, ?, ?, ?, ?)""", (charger.number, lower_start, upper_start,
                    previous["seen_at"], observed, duration))
        elif charger.status == "Occupied":
            occupied_since = observed
            if continuous and previous["status"] == "Available":
                lower, upper = previous["seen_at"], observed
            elif continuous and previous["status"] == "Occupied":
                occupied_since = previous["occupied_since"]
                lower, upper = previous["start_lower"], previous["start_upper"]
        self.db.execute("""INSERT INTO charger_state VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(charger) DO UPDATE SET status=excluded.status, seen_at=excluded.seen_at,
            last_empty=excluded.last_empty, occupied_since=excluded.occupied_since,
            start_lower=excluded.start_lower, start_upper=excluded.start_upper""",
            (charger.number, charger.status, observed, last_empty, occupied_since, lower, upper))

    def state(self, number):
        return self.db.execute("SELECT * FROM charger_state WHERE charger=?", (number,)).fetchone()

    def latest(self):
        sample = self.db.execute("SELECT * FROM samples ORDER BY id DESC LIMIT 1").fetchone()
        if sample is None or sample["error"] or sample["source_at"] is None:
            raise SourceError("The history collector has no current successful observation.")
        if time.time() - sample["fetched_at"] > MAX_OBSERVATION_GAP:
            raise SourceError("The history collector has stopped updating.")
        rows = self.db.execute("SELECT * FROM observations WHERE sample_id=? ORDER BY charger", (sample["id"],)).fetchall()
        return Snapshot(tuple(Charger(r["charger"], r["level"], r["status"]) for r in rows),
                        datetime.fromtimestamp(sample["source_at"], HKT),
                        datetime.fromtimestamp(sample["fetched_at"], HKT), sample["service"])

    def _empirical_window(self, number, start, now, deadline):
        age = now - start
        local = [r[0] for r in self.db.execute("SELECT duration FROM sessions WHERE charger=? ORDER BY id DESC LIMIT 200", (number,))]
        groups = [(local, 5, "this charger")]
        pooled = [r[0] for r in self.db.execute("SELECT duration FROM sessions ORDER BY id DESC LIMIT 2000")]
        groups.append((pooled, 10, "all chargers"))
        for durations, minimum, label in groups:
            # Condition on the car still being present; do not predict a past departure.
            survivors = [d for d in durations if d > age]
            if len(survivors) >= minimum:
                quartiles = quantiles(survivors, n=4, method="inclusive")
                low, high = min(start + quartiles[0], deadline), min(start + quartiles[2], deadline)
                return max(low, now), max(high, now), len(survivors), label
        return None

    def describe(self, charger, snapshot, now=None):
        now = time.time() if now is None else now
        state = self.state(charger.number)
        if not state:
            return ["Last seen empty: not recorded yet"] + (["Free estimate: collecting history"] if charger.status == "Occupied" else [])
        last_empty = "not recorded yet" if state["last_empty"] is None else stamp(state["last_empty"])
        lines = ["Last seen empty: " + last_empty]
        if charger.status != "Occupied":
            return lines
        if snapshot.warning(datetime.fromtimestamp(now, HKT)) or state["status"] != "Occupied" or abs(snapshot.updated_at.timestamp() - state["seen_at"]) > MAX_OBSERVATION_GAP:
            return lines + ["Free estimate unavailable: observation gap / stale data"]
        first = state["occupied_since"]
        if first is None:
            return lines + ["Free estimate unavailable: session start unknown"]
        if state["start_lower"] is None:
            deadline = first + MAX_SESSION_SECONDS
            if now >= deadline:
                return lines + ["Observed occupied ≥4h; no reliable free-time estimate"]
            return lines + [f"Occupied since at least {stamp(first)}",
                            f"4h upper estimate: by {stamp(deadline)} (start unknown)"]
        lower, upper = state["start_lower"], state["start_upper"]
        deadline = upper + MAX_SESSION_SECONDS
        lines.append(f"Occupied from ~{stamp(upper)}")
        if now >= deadline:
            return lines + ["4h estimate passed; still occupied"]
        window = self._empirical_window(charger.number, (lower + upper) / 2, now, deadline)
        if window:
            low, high, count, label = window
            lines.append(f"Est. free: {stamp(low)}–{datetime.fromtimestamp(high, HKT):%H:%M} (history, n={count}, {label})")
        else:
            lines.append("Est. free: not enough completed sessions yet")
        lines.append(f"4h limit estimate: {stamp(lower + MAX_SESSION_SECONDS)}–{datetime.fromtimestamp(deadline, HKT):%H:%M}")
        return lines

    def summary(self):
        counts = self.db.execute("SELECT COUNT(*), MIN(fetched_at), MAX(fetched_at), SUM(error IS NOT NULL) FROM samples").fetchone()
        if not counts[0]:
            return "📈 History collection is starting. No observations yet."
        sessions = self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        observations = self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        return (f"📈 PolyU charger history\n\nRecording since: {stamp(counts[1])} HKT\n"
                f"Latest collection: {stamp(counts[2])} HKT\n"
                f"Fetch attempts: {counts[0]} · failures: {counts[3] or 0}\n"
                f"Charger observations: {observations}\nComplete observed sessions: {sessions}\n\n"
                "Every successful poll stores all 12 statuses and the original page. "
                "Collection runs every minute, even without subscribers.\n\n"
                "Use /predict, /all or a floor command for last-seen-empty times and estimates. "
                "Earlier history before collection began is unavailable.")

    def close(self):
        self.db.close()


class RecordedSource:
    """The Telegram process reads the collector's snapshots without fetching PolyU."""

    def __init__(self, history):
        self.history = history

    def get(self):
        return self.history.latest()
