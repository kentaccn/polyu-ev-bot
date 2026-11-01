from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zlib

from polyu_ev_bot.bot import Bot, render_status
from polyu_ev_bot.config import Config
from polyu_ev_bot.history import History, RecordedSource, MAX_SESSION_SECONDS
from polyu_ev_bot.source import Charger, HKT, SourceClient, SourceError, parse_page
from polyu_ev_bot import healthcheck

BASE = datetime(2026, 11, 1, 12, 0, tzinfo=HKT)
RAW = (Path(__file__).parent / "fixtures" / "status.html").read_bytes()


def snapshot(minute, status="Occupied", service="Normal"):
    date = BASE + timedelta(minutes=minute)
    original = parse_page(RAW.decode(), date)
    chargers = tuple(Charger(c.number, c.level, status) for c in original.chargers)
    return replace(original, updated_at=date, fetched_at=date, chargers=chargers, service=service)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "history.sqlite3"
        self.history = History(self.path)

    def tearDown(self):
        self.history.close()
        self.folder.cleanup()

    def record(self, minute, status="Occupied", service="Normal"):
        self.history.record(snapshot(minute, status, service), RAW)

    def test_records_every_unchanged_sample_and_exact_original_page(self):
        self.record(0)
        self.record(1)
        self.assertEqual(self.history.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 24)
        self.assertEqual(self.history.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 2)
        blob = self.history.db.execute("SELECT page_zlib FROM samples LIMIT 1").fetchone()[0]
        self.assertEqual(zlib.decompress(blob), RAW)

    def test_last_empty_and_start_bracket_from_observed_transition(self):
        self.record(0, "Available")
        self.record(1)
        state = self.history.state(1)
        self.assertEqual(state["last_empty"], BASE.timestamp())
        self.assertEqual(state["start_lower"], BASE.timestamp())
        self.assertEqual(state["start_upper"], (BASE + timedelta(minutes=1)).timestamp())
        details = "\n".join(self.history.describe(snapshot(2).chargers[0], snapshot(2), snapshot(2).fetched_at.timestamp()))
        self.assertIn("01 Nov 12:00", details)
        self.assertIn("4h limit estimate: 01 Nov 16:00–16:01", details)

    def test_cold_start_does_not_invent_start_or_last_empty(self):
        self.record(0)
        state = self.history.state(1)
        self.assertIsNone(state["last_empty"])
        self.assertIsNone(state["start_lower"])
        details = "\n".join(self.history.describe(snapshot(0).chargers[0], snapshot(0), BASE.timestamp()))
        self.assertIn("not recorded yet", details)
        self.assertIn("start unknown", details)
        self.assertIn("by 01 Nov 16:00", details)

    def test_full_is_a_capacity_summary_and_still_supports_history(self):
        current = snapshot(0, service="Full")
        self.assertIsNone(current.warning(current.fetched_at))
        self.history.record(current, RAW)
        self.assertEqual(self.history.state(1)["status"], "Occupied")
        self.assertEqual(self.history.state(1)["occupied_since"], BASE.timestamp())

    def test_long_gap_breaks_session_anchor(self):
        self.record(0, "Available")
        self.record(1)
        self.record(6)
        state = self.history.state(1)
        self.assertIsNone(state["start_lower"])
        self.assertEqual(state["occupied_since"], snapshot(6).fetched_at.timestamp())

    def test_failed_attempt_breaks_anchors_even_for_short_gap(self):
        self.record(0, "Available")
        self.record(1)
        self.history.record(error="Upstream offline", now=snapshot(2).fetched_at.timestamp())
        self.record(3)
        self.assertIsNone(self.history.state(1)["start_lower"])
        self.record(4, "Available")
        self.assertEqual(self.history.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_service_outage_and_unknown_status_break_anchors(self):
        for status, service in [("Unknown", "Normal"), ("Occupied", "Maintenance")]:
            with self.subTest(status=status):
                self.record(0, "Available")
                self.record(1)
                self.record(2, status, service)
                self.record(3)
                self.assertIsNone(self.history.state(1)["start_lower"])

    def test_repeated_or_regressing_timestamp_does_not_extend_session(self):
        self.record(1, "Available")
        self.record(2)
        before = dict(self.history.state(1))
        self.record(2)
        self.record(1)
        self.assertEqual(dict(self.history.state(1)), before)
        self.assertEqual(self.history.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 4)

    def test_completed_session_uses_midpoints_of_observation_bounds(self):
        self.record(0, "Available")
        for minute in range(1, 61):
            self.record(minute)
        self.record(61, "Available")
        row = self.history.db.execute("SELECT * FROM sessions WHERE charger=1").fetchone()
        self.assertEqual(row["duration"], 3600)
        self.assertEqual(row["end_lower"], snapshot(60).fetched_at.timestamp())
        self.assertEqual(self.history.state(1)["last_empty"], snapshot(61).fetched_at.timestamp())

    def test_censored_session_is_not_used_for_training(self):
        self.record(0)
        self.record(1, "Available")
        self.assertEqual(self.history.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_overdue_charger_never_becomes_available_by_prediction(self):
        self.record(0, "Available")
        for minute in range(1, 243):
            self.record(minute)
        current = snapshot(242)
        text = render_status(current, "level1", current.fetched_at, self.history)
        self.assertIn("4h estimate passed; still occupied", text)
        self.assertIn("No. 1 — Occupied", text)

    def train(self, charger, durations):
        with self.history.db:
            self.history.db.executemany("""INSERT INTO sessions
                (charger,start_lower,start_upper,end_lower,end_upper,duration) VALUES (?,0,60,?,?,?)""",
                [(charger, d, d + 60, d) for d in durations])

    def test_empirical_forecast_requires_enough_samples_and_conditions_on_elapsed_time(self):
        self.assertIsNone(self.history._empirical_window(1, 0, 100, 14400))
        self.train(1, [60, 90, 1800, 3600, 5400, 7200, 9000])
        window = self.history._empirical_window(1, 0, 1000, 14400)
        self.assertEqual(window[2:], (5, "this charger"))
        self.assertGreater(window[0], 1000)
        self.assertIsNone(self.history._empirical_window(1, 0, 8000, 14400))

    def test_pooled_forecast_and_four_hour_cap(self):
        self.train(2, [18000] * 10)
        window = self.history._empirical_window(1, 0, 1000, MAX_SESSION_SECONDS)
        self.assertEqual(window, (14400, 14400, 10, "all chargers"))

    def test_restart_retains_state_and_samples(self):
        self.record(0, "Available")
        self.record(1)
        self.history.close()
        self.history = History(self.path)
        self.record(2)
        self.assertEqual(self.history.state(1)["start_lower"], BASE.timestamp())
        self.assertEqual(self.history.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 3)

    def test_recorded_source_rejects_missing_failed_or_stopped_collector(self):
        with self.assertRaises(SourceError):
            self.history.latest()
        self.record(0)
        with patch("polyu_ev_bot.history.time.time", return_value=BASE.timestamp() + 100):
            self.assertEqual(len(RecordedSource(self.history).get().chargers), 12)
        with patch("polyu_ev_bot.history.time.time", return_value=BASE.timestamp() + 151):
            with self.assertRaises(SourceError):
                self.history.latest()
        self.history.record(error="offline")
        with self.assertRaises(SourceError):
            self.history.latest()

    def test_stale_snapshots_archived_but_not_used_for_predictions(self):
        self.record(0, "Available")
        stale = replace(snapshot(1), fetched_at=snapshot(10).fetched_at)
        self.history.record(stale, RAW)
        self.assertEqual(self.history.state(1)["status"], "Unknown")
        details = self.history.describe(stale.chargers[0], stale, stale.fetched_at.timestamp())
        self.assertIn("stale", details[-1])

    def test_status_fits_telegram_limit_with_history_for_all_chargers(self):
        self.record(0, "Available")
        self.record(1)
        self.train(1, [3600] * 10)
        current = snapshot(2)
        for view in ("all", "predict", "level1"):
            text = render_status(current, view, current.fetched_at, self.history)
            self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)

    def test_history_command_reports_counts_without_personal_data(self):
        self.record(0)
        api = Mock()
        Bot(api, Mock(), Config("unused"), history=self.history).handle(
            {"message": {"chat": {"id": 123}, "text": "/history"}})
        self.assertIn("Charger observations: 12", api.call.call_args.kwargs["text"])
        self.assertNotIn("reply_markup", api.call.call_args.kwargs)

    def test_health_is_collector_liveness_not_upstream_availability(self):
        self.history.record(error="network outage", now=BASE.timestamp())
        with patch.dict("os.environ", {"BOT_DATA_DIR": self.folder.name}), patch("polyu_ev_bot.healthcheck.time.time", return_value=BASE.timestamp() + 60):
            self.assertEqual(healthcheck.main(), 0)
        with patch.dict("os.environ", {"BOT_DATA_DIR": self.folder.name}), patch("polyu_ev_bot.healthcheck.time.time", return_value=BASE.timestamp() + 151):
            self.assertEqual(healthcheck.main(), 1)

    @patch("polyu_ev_bot.source.urlopen")
    def test_source_client_archives_real_fetch_and_failure_once_each(self, opener):
        opener.return_value.__enter__.return_value.read.return_value = RAW
        source = SourceClient(self.history)
        source.get()
        source.get()
        self.assertEqual(self.history.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 1)
        source.next_fetch = 0
        opener.return_value.__enter__.return_value.read.return_value = b"bad page"
        with self.assertRaises(SourceError):
            source.get()
        row = self.history.db.execute("SELECT * FROM samples ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIn("missing", row["error"])
        self.assertEqual(zlib.decompress(row["page_zlib"]), b"bad page")


if __name__ == "__main__":
    unittest.main()
