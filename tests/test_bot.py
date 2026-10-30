from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from polyu_ev_bot.bot import Bot, COMMANDS, REPLY_MENU, inline_menu, render_status
from polyu_ev_bot.config import Config, load_env
from polyu_ev_bot.source import HKT, SourceClient, SourceError, parse_page
from polyu_ev_bot.telegram import Telegram, TelegramError

NOW = datetime(2026, 10, 30, 19, 0, tzinfo=HKT)
FIXTURE = Path(__file__).parent / "fixtures" / "status.html"


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.html = FIXTURE.read_text()

    def test_real_page_structure_and_icon_mapping(self):
        snapshot = parse_page(self.html, NOW)
        self.assertEqual(len(snapshot.chargers), 12)
        self.assertEqual(snapshot.updated_at, NOW)
        self.assertEqual(snapshot.service, "Normal")
        self.assertEqual([c.status for c in snapshot.chargers[:4]],
                         ["Occupied", "Available", "Not in service", "Unknown"])
        self.assertEqual([c.level for c in snapshot.chargers], [1]*4 + [2]*4 + [3]*4)
        self.assertIsNone(snapshot.warning(NOW))

    def test_missing_or_duplicate_card_fails_closed(self):
        for html in [self.html.replace("No. 12", "No. 11"), self.html.replace("No. 12", "No. X")]:
            with self.subTest(html=html[-50:]), self.assertRaises(SourceError):
                parse_page(html)

    def test_missing_timestamp_and_grid_fail_closed(self):
        for html in [self.html.replace("Updated on:", "Time:"), self.html.replace("evno_css", "changed")]:
            with self.assertRaises(SourceError):
                parse_page(html)

    def test_invalid_time_fails_closed(self):
        with self.assertRaises(SourceError):
            parse_page(self.html.replace("07:00pm", "00:99pm"))

    def test_image_query_parameters_and_nested_labels(self):
        snapshot = parse_page(self.html.replace('src="img/0.jpg"', 'src="/img/0.jpg?v=123"'))
        self.assertEqual(snapshot.chargers[1].status, "Available")

    def test_source_age_and_outage_warnings(self):
        snapshot = parse_page(self.html, NOW)
        self.assertIn("over 5 minutes", snapshot.warning(NOW + timedelta(minutes=6)))
        self.assertIn("future", snapshot.warning(NOW - timedelta(minutes=3)))
        self.assertIn("Maintenance", parse_page(self.html.replace(">Normal<", ">Maintenance<")).warning(NOW))

    @patch("polyu_ev_bot.source.urlopen")
    def test_cache_is_shared_and_lasts_one_minute(self, opener):
        opener.return_value.__enter__.return_value.read.return_value = self.html.encode()
        source = SourceClient()
        with patch("polyu_ev_bot.source.time.monotonic", return_value=100):
            first = source.get()
        with patch("polyu_ev_bot.source.time.monotonic", return_value=159):
            self.assertIs(source.get(), first)
        self.assertEqual(opener.call_count, 1)
        with patch("polyu_ev_bot.source.time.monotonic", return_value=160):
            source.get()
        self.assertEqual(opener.call_count, 2)

    @patch("polyu_ev_bot.source.urlopen")
    def test_failed_refresh_never_serves_old_availability(self, opener):
        opener.return_value.__enter__.return_value.read.return_value = self.html.encode()
        source = SourceClient()
        with patch("polyu_ev_bot.source.time.monotonic", return_value=100):
            source.get()
        opener.side_effect = URLError("offline")
        with patch("polyu_ev_bot.source.time.monotonic", return_value=161):
            with self.assertRaises(SourceError):
                source.get()
        with patch("polyu_ev_bot.source.time.monotonic", return_value=162):
            with self.assertRaises(SourceError):
                source.get()
        self.assertEqual(opener.call_count, 2)

    @patch("polyu_ev_bot.source.urlopen")
    def test_oversized_page_is_rejected(self, opener):
        opener.return_value.__enter__.return_value.read.return_value = b"x" * 1_048_577
        with self.assertRaises(SourceError):
            SourceClient().get()


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.source = Mock()
        self.source.get.return_value = parse_page(FIXTURE.read_text(), NOW)
        self.bot = Bot(self.api, self.source, Config("unused"))
        self.bot.username = "ExampleBot"

    def update(self, command, chat_id=123):
        return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": command}}

    def test_setup_registers_all_level_commands_and_menu(self):
        self.api.call.side_effect = [{"username": "ExampleBot"}, {"url": ""}, True, True]
        self.bot.setup()
        registered = {c["command"] for c in self.api.call.call_args_list[2].kwargs["commands"]}
        self.assertTrue({"all", "level1", "level2", "level3"} <= registered)
        self.assertEqual(self.api.call.call_args_list[3].kwargs["menu_button"], {"type": "commands"})

    def test_existing_webhook_is_not_removed(self):
        self.api.call.side_effect = [{"username": "ExampleBot"}, {"url": "https://example.org/webhook"}]
        with self.assertRaises(ValueError):
            self.bot.setup()
        self.assertEqual(self.api.call.call_count, 2)

    def test_start_shows_persistent_menu(self):
        self.bot.handle(self.update("/start"))
        self.assertEqual(self.api.call.call_args.kwargs["reply_markup"], REPLY_MENU)

    def test_all_and_each_level_command(self):
        for command, expected in [("all", range(1, 13)), ("status", range(1, 13)),
                                  ("level1", range(1, 5)), ("level2", range(5, 9)), ("level3", range(9, 13))]:
            with self.subTest(command=command):
                self.bot.handle(self.update("/" + command))
                text = self.api.call.call_args.kwargs["text"]
                for number in range(1, 13):
                    self.assertEqual(f"No. {number} —" in text, number in expected)

    def test_available_filter_excludes_unknown_and_occupied(self):
        text = render_status(self.source.get(), "available", NOW)
        self.assertIn("No. 2 — Available", text)
        self.assertNotIn("No. 1 —", text)
        self.assertNotIn("No. 4 —", text)

    def test_stale_available_filter_never_lists_chargers_as_available(self):
        text = render_status(self.source.get(), "available", NOW + timedelta(minutes=6))
        self.assertIn("cannot be confirmed", text)
        self.assertNotIn("No. 2 —", text)

    def test_callback_acknowledges_then_edits_same_message(self):
        self.bot.handle({"callback_query": {"id": "q", "data": "level2", "message": {"message_id": 42, "chat": {"id": 123}}}})
        self.assertEqual([c.args[0] for c in self.api.call.call_args_list], ["answerCallbackQuery", "editMessageText"])
        self.assertEqual(self.api.call.call_args.kwargs["message_id"], 42)
        self.assertIn("No. 5 —", self.api.call.call_args.kwargs["text"])

    def test_unavailable_source_gets_error_and_retry_menu(self):
        self.source.get.side_effect = SourceError("offline")
        self.bot.handle(self.update("/all"))
        self.assertIn("temporarily unavailable", self.api.call.call_args.kwargs["text"])
        self.assertEqual(self.api.call.call_args.kwargs["reply_markup"], inline_menu("all"))

    def test_allowlist_rejects_unauthorized_commands_and_callbacks(self):
        self.bot.config = Config("unused", frozenset({456}))
        self.bot.handle(self.update("/all"))
        self.api.call.assert_not_called()
        self.bot.handle({"callback_query": {"id": "q", "data": "all", "message": {"chat": {"id": 123}}}})
        self.assertEqual(self.api.call.call_count, 1)
        self.source.get.assert_not_called()

    def test_other_bots_commands_are_ignored(self):
        self.bot.handle(self.update("/all@OtherBot"))
        self.api.call.assert_not_called()
        self.bot.handle(self.update("/all@ExampleBot"))
        self.api.call.assert_called_once()

    def test_keyboard_level_button(self):
        self.bot.handle(self.update("🏢 Level 3"))
        self.assertIn("No. 9 —", self.api.call.call_args.kwargs["text"])
        self.assertNotIn("No. 1 —", self.api.call.call_args.kwargs["text"])

    def test_callback_data_fits_telegram_limits(self):
        for view in [c for c, _ in COMMANDS if c in {"all", "status", "available", "level1", "level2", "level3"}]:
            for row in inline_menu(view)["inline_keyboard"]:
                for button in row:
                    if "callback_data" in button:
                        self.assertLessEqual(len(button["callback_data"].encode()), 64)


class ConfigAndApiTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_env_is_literal_and_does_not_override_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("# comment\nTELEGRAM_BOT_TOKEN='123:literal$TOKEN'\nTELEGRAM_ALLOWED_CHAT_IDS=-123,456\n")
            load_env(path)
            self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "123:literal$TOKEN")
            os.environ["TELEGRAM_BOT_TOKEN"] = "already-set"
            load_env(path)
            self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "already-set")

    @patch("polyu_ev_bot.config.load_env")
    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "invalid"}, clear=True)
    def test_missing_token_has_actionable_error(self, loader):
        with self.assertRaisesRegex(ValueError, "@BotFather"):
            Config.from_env()

    @patch("polyu_ev_bot.telegram.urlopen")
    def test_network_error_never_exposes_token(self, opener):
        token = "123:secret-token-must-not-appear"
        opener.side_effect = URLError("https://api.telegram.org/bot" + token)
        with self.assertRaises(TelegramError) as caught:
            Telegram(token).call("getMe")
        self.assertNotIn(token, str(caught.exception))

    @patch("polyu_ev_bot.telegram.urlopen")
    def test_rate_limit_retains_retry_after(self, opener):
        opener.side_effect = HTTPError("secret-url", 429, "Too Many Requests", {}, io.BytesIO(json.dumps({"parameters": {"retry_after": 42}}).encode()))
        with self.assertRaises(TelegramError) as caught:
            Telegram("unused").call("getUpdates")
        self.assertEqual(caught.exception.retry_after, 42)
        self.assertNotIn("secret-url", str(caught.exception))

    @patch("polyu_ev_bot.telegram.urlopen")
    def test_polling_retries_same_update_after_rate_limit(self, opener):
        api = Mock()
        source = Mock()
        bot = Bot(api, source, Config("unused"))
        bot.setup = Mock()
        api.call.side_effect = [[{"update_id": 7, "message": {"chat": {"id": 1}, "text": "/menu"}}],
                                TelegramError(429, 1), [], KeyboardInterrupt()]
        with patch("polyu_ev_bot.bot.time.sleep"), self.assertRaises(KeyboardInterrupt):
            bot.run()
        polls = [c for c in api.call.call_args_list if c.args[0] == "getUpdates"]
        self.assertEqual([c.kwargs["offset"] for c in polls], [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
