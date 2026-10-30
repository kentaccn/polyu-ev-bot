from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from polyu_ev_bot.bot import Bot, floor_menu, frequency_menu, subscription_menu
from polyu_ev_bot.config import Config
from polyu_ev_bot.source import SourceClient, SourceError
from polyu_ev_bot.subscriptions import Subscriptions
from polyu_ev_bot.telegram import TelegramError


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "subscriptions.sqlite3"
        self.store = Subscriptions(self.path)
        self.api = Mock()
        self.source = Mock()
        self.source.get.return_value.warning.return_value = None
        self.bot = Bot(self.api, self.source, Config("unused"), self.store)

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def update(self, action, chat_type="private"):
        self.bot.handle({"message": {"chat": {"id": 123, "type": chat_type}, "text": "/" + action}})

    def test_floor_then_frequency_menu_does_not_subscribe_until_selection(self):
        self.update("subscribe")
        self.assertEqual(self.api.call.call_args.kwargs["reply_markup"], floor_menu())
        self.update("sub:floor:2")
        self.assertEqual(self.api.call.call_args.kwargs["reply_markup"], frequency_menu("2"))
        self.assertIsNone(self.store.get(123))
        self.update("sub:set:2:5")
        sub = self.store.get(123)
        self.assertEqual((sub.floor, sub.minutes, sub.view), ("2", 5, "level2"))

    def test_settings_survive_restart(self):
        self.store.set(123, "all", 15, now=100)
        self.store.close()
        self.store = Subscriptions(self.path)
        sub = self.store.get(123)
        self.assertEqual((sub.floor, sub.minutes, sub.next_at), ("all", 15, 1000))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_change_replaces_existing_subscription(self):
        self.store.set(123, "all", 5, now=100)
        self.store.set(123, "3", 1, now=200)
        self.assertEqual(len(self.store.due(now=10000)), 1)
        self.assertEqual(self.store.get(123).view, "level3")
        self.assertEqual(self.store.get(123).next_at, 260)

    def test_valid_frequencies_and_due_times(self):
        for chat_id, minutes in enumerate((1, 5, 15, 30, 60)):
            self.store.set(chat_id, "1", minutes, now=100)
        self.assertEqual(self.store.due(now=159), [])
        self.assertEqual([s.minutes for s in self.store.due(now=160)], [1])
        with self.assertRaises(ValueError):
            self.store.set(999, "4", 1)
        with self.assertRaises(ValueError):
            self.store.set(999, "all", 0)

    def test_invalid_callback_does_not_create_subscription(self):
        for action in ("sub:set:4:5", "sub:set:all:2", "sub:set:all:bad", "sub:set:all:5:extra"):
            self.update(action)
        self.assertIsNone(self.store.get(123))

    def test_unsubscribe_deletes_settings_and_stops_delivery(self):
        self.store.set(123, "1", 1, now=0)
        self.update("unsubscribe")
        self.assertIsNone(self.store.get(123))
        self.api.reset_mock()
        self.bot.notify_due()
        self.api.call.assert_not_called()

    def test_group_cannot_subscribe_members(self):
        self.update("sub:set:all:1", "supergroup")
        self.assertIsNone(self.store.get(123))
        self.assertIn("private chat", self.api.call.call_args.kwargs["text"])

    @patch("polyu_ev_bot.bot.render_status", return_value="Floor 2 snapshot")
    def test_scheduled_delivery_uses_selected_floor_and_advances_schedule(self, render):
        self.store.set(123, "2", 1, now=0)
        with patch("polyu_ev_bot.subscriptions.time.time", return_value=60):
            self.bot.notify_due()
        self.assertEqual(render.call_args.args[1], "level2")
        self.assertIn("Floor 2 snapshot", self.api.call.call_args.kwargs["text"])
        self.assertEqual(self.store.get(123).next_at, 120)

    def test_source_outage_notifies_once_then_recovers(self):
        self.store.set(123, "all", 1, now=0)
        self.source.get.side_effect = SourceError("offline")
        with patch("polyu_ev_bot.subscriptions.time.time", return_value=60):
            self.bot.notify_due()
        self.assertIn("unavailable", self.api.call.call_args.kwargs["text"])
        self.api.reset_mock()
        with patch("polyu_ev_bot.subscriptions.time.time", return_value=120):
            self.bot.notify_due()
        self.api.call.assert_not_called()
        self.source.get.side_effect = None
        with patch("polyu_ev_bot.subscriptions.time.time", return_value=180), patch("polyu_ev_bot.bot.render_status", return_value="Recovered snapshot"):
            self.bot.notify_due()
        self.assertIn("Recovered snapshot", self.api.call.call_args.kwargs["text"])
        self.assertFalse(self.store.get(123).unavailable_notified)

    def test_blocked_bot_removes_subscription(self):
        self.store.set(123, "all", 1, now=0)
        self.source.get.side_effect = SourceError("offline")
        self.api.call.side_effect = TelegramError(403)
        self.bot.notify_due()
        self.assertIsNone(self.store.get(123))

    def test_rate_limit_preserves_subscription_and_defers(self):
        self.store.set(123, "all", 1, now=0)
        self.source.get.side_effect = SourceError("offline")
        self.api.call.side_effect = TelegramError(429, 42)
        with patch("polyu_ev_bot.subscriptions.time.time", return_value=100):
            with self.assertRaises(TelegramError):
                self.bot.notify_due()
        self.assertEqual(self.store.get(123).next_at, 142)

    def test_restart_skips_missed_intervals_without_burst(self):
        self.store.set(123, "all", 1, now=0)
        self.source.get.side_effect = SourceError("offline")
        with patch("polyu_ev_bot.subscriptions.time.time", return_value=10000):
            self.bot.notify_due()
            self.bot.notify_due()
        self.assertEqual(self.api.call.call_count, 1)
        self.assertEqual(self.store.get(123).next_at, 10060)

    def test_allowlist_change_removes_disallowed_subscriptions(self):
        self.store.set(123, "all", 1, now=0)
        self.bot.config = Config("unused", frozenset({456}))
        self.bot.notify_due()
        self.assertIsNone(self.store.get(123))
        self.api.call.assert_not_called()

    def test_all_subscription_buttons_fit_callback_limit(self):
        for markup in (floor_menu(), frequency_menu("all"), subscription_menu(True)):
            for row in markup["inline_keyboard"]:
                for button in row:
                    self.assertLessEqual(len(button["callback_data"].encode()), 64)


if __name__ == "__main__":
    unittest.main()
