"""Telegram commands, persistent menu, and charger status replies."""

from datetime import datetime
import logging
import time

from .source import HKT, SOURCE_URL, SourceError
from .telegram import TelegramError
from .subscriptions import FLOORS, FREQUENCIES

LOG = logging.getLogger(__name__)
ICONS = {"Available": "🟢", "Occupied": "🟠", "Not in service": "🔧", "Unknown": "❔"}
COMMANDS = [
    ("all", "All chargers on levels 1, 2 and 3"),
    ("status", "All 12 chargers"),
    ("available", "Available chargers only"),
    ("level1", "Level 1: chargers 1–4"),
    ("level2", "Level 2: chargers 5–8"),
    ("level3", "Level 3: chargers 9–12"),
    ("subscribe", "Choose floors and notification frequency"),
    ("subscriptions", "View or change your notifications"),
    ("unsubscribe", "Stop notifications and delete your settings"),
    ("menu", "Open the charger menu"),
    ("help", "How to use this bot"),
    ("privacy", "Privacy and data use"),
]
LABELS = {
    "⚡ All chargers": "status", "🟢 Available": "available",
    "🏢 Level 1": "level1", "🏢 Level 2": "level2", "🏢 Level 3": "level3",
    "ℹ️ Help": "help", "📋 Menu": "menu",
    "🔔 Notifications": "subscriptions",
}
VIEWS = {"all", "status", "available", "level1", "level2", "level3"}
REPLY_MENU = {
    "keyboard": [[{"text": "⚡ All chargers"}, {"text": "🟢 Available"}],
                 [{"text": f"🏢 Level {i}"} for i in range(1, 4)],
                 [{"text": "🔔 Notifications"}],
                 [{"text": "📋 Menu"}, {"text": "ℹ️ Help"}]],
    "resize_keyboard": True, "is_persistent": True,
}


def status_controls(view="status"):
    return {"inline_keyboard": [[{"text": "🔄 Refresh", "callback_data": view}]]}


def floor_menu():
    return {"inline_keyboard": [
        [{"text": "All floors", "callback_data": "sub:floor:all"}],
        [{"text": f"Level {i}", "callback_data": f"sub:floor:{i}"} for i in range(1, 4)],
        [{"text": "⬅️ My notifications", "callback_data": "subscriptions"}],
    ]}


def frequency_menu(floor):
    return {"inline_keyboard": [
        [{"text": f"Every {minutes} min", "callback_data": f"sub:set:{floor}:{minutes}"} for minutes in FREQUENCIES[:3]],
        [{"text": f"Every {minutes} min", "callback_data": f"sub:set:{floor}:{minutes}"} for minutes in FREQUENCIES[3:]],
        [{"text": "⬅️ Choose floor", "callback_data": "subscribe"}],
    ]}


def subscription_menu(active=False):
    rows = [[{"text": "Change floors / frequency" if active else "🔔 Set up notifications", "callback_data": "subscribe"}]]
    if active:
        rows.append([{"text": "🔕 Stop notifications", "callback_data": "unsubscribe"}])
    rows.append([{"text": "⚡ All chargers", "callback_data": "all"}])
    return {"inline_keyboard": rows}


def render_status(snapshot, view, now=None):
    now = now or datetime.now(HKT)
    warning = snapshot.warning(now)
    lines = ["⚡ PolyU EV chargers"]
    if warning:
        lines += [f"⚠️ {warning}", "Showing the last status reported by PolyU."]
    available = sum(c.status == "Available" for c in snapshot.chargers)
    occupied = sum(c.status == "Occupied" for c in snapshot.chargers)
    offline = sum(c.status == "Not in service" for c in snapshot.chargers)
    unknown = sum(c.status == "Unknown" for c in snapshot.chargers)
    lines += [f"Reported: {available}/12 available · {occupied} occupied · {offline} not in service"]
    if unknown:
        lines.append(f"❔ {unknown} unknown — check the official page")
    selected = snapshot.chargers
    if view == "available":
        if warning:
            selected = ()
            lines += ["", "A current available-only list cannot be confirmed. Check All chargers or the official page."]
        else:
            selected = tuple(c for c in selected if c.status == "Available")
            if not selected:
                lines += ["", "No chargers are currently reported available."]
    elif view.startswith("level"):
        selected = tuple(c for c in selected if c.level == int(view[-1]))
    for level in range(1, 4):
        chargers = [c for c in selected if c.level == level]
        if chargers:
            lines += ["", f"🏢 Level {level}, Carpark"]
            lines.extend(f"{ICONS[c.status]} No. {c.number} — {c.status}" for c in chargers)
    lines += ["", f"PolyU updated: {snapshot.updated_at:%d %b %Y, %H:%M} HKT",
              f"Page fetched: {snapshot.fetched_at:%H:%M:%S} HKT",
              "PolyU updates about once a minute. Refresh shares a 60-second cache.",
              "Availability can change before you arrive."]
    return "\n".join(lines)


class Bot:
    def __init__(self, api, source, config, subscriptions=None):
        self.api, self.source, self.config = api, source, config
        self.subscriptions = subscriptions
        self.username = ""

    def setup(self):
        identity = self.api.call("getMe")
        self.username = identity["username"]
        if self.api.call("getWebhookInfo").get("url"):
            raise ValueError("This bot has an active webhook. Use a dedicated bot or remove its webhook before long polling.")
        self.api.call("setMyCommands", commands=[{"command": name, "description": description} for name, description in COMMANDS])
        self.api.call("setChatMenuButton", menu_button={"type": "commands"})
        LOG.info("Bot ready: @%s", self.username)

    def send(self, chat_id, text, markup=None):
        payload = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if markup is not None:
            payload["reply_markup"] = markup
        return self.api.call("sendMessage", **payload)

    def handle(self, update):
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        allowed = chat_id is not None and (not self.config.allowed_chat_ids or chat_id in self.config.allowed_chat_ids)
        if callback:
            try:
                self.api.call("answerCallbackQuery", callback_query_id=callback["id"],
                              text="Checking status…" if allowed else "This chat is not enabled.")
            except TelegramError as exc:
                if exc.code != 400:  # An expired callback must not block the response.
                    raise
        if not allowed:
            return
        if callback:
            action = callback.get("data", "")
            if action not in VIEWS | {"menu", "subscribe", "subscriptions", "unsubscribe"} and not action.startswith("sub:"):
                return
        else:
            text = message.get("text", "").strip()
            if not text:
                return
            action = LABELS.get(text)
            if action is None:
                command = text.split()[0]
                if "@" in command:
                    command, target = command.split("@", 1)
                    if target.casefold() != self.username.casefold():
                        return
                action = command.lstrip("/").lower()
        if action in {"subscribe", "subscriptions", "unsubscribe"} or action.startswith("sub:"):
            self.handle_subscription(chat_id, action, message.get("chat", {}).get("type", "private"))
        elif action in {"start", "menu"}:
            self.send(chat_id, "⚡ PolyU EV charger menu\n\nChoose all chargers, available chargers, or a carpark level.\n🟢 Available · 🟠 Occupied · 🔧 Not in service\n\n🔔 Notifications: choose your floor and frequency with /subscribe.\n\nUse the buttons below or Telegram’s Menu button.", REPLY_MENU)
        elif action == "help":
            self.send(chat_id, "Use /all, /available, /level1, /level2, or /level3.\n\n/subscribe — choose Level 1, 2, 3 or all floors, then a frequency of 1, 5, 15, 30 or 60 minutes. Notifications send a status summary every interval, even if unchanged.\n/subscriptions — view or change settings\n/unsubscribe — stop and delete settings\n\nAll chats share a one-minute source cache. Times are HKT; old source data triggers a warning. Delivery may be delayed by network outages or Telegram limits; missed intervals are not replayed.\n\nUse /menu to restore the keyboard. This is an unofficial bot, not a booking service.")
        elif action == "privacy":
            self.send(chat_id, "The bot processes your Telegram chat ID and commands to reply. If you subscribe, it saves your chat ID, chosen floor, notification interval and next delivery time in a local database. It does not save message contents. /unsubscribe deletes your subscription record.\n\nTelegram handles messages under its own policy. Only the public charger page is requested from PolyU; your Telegram details are not sent to PolyU. Operators may restrict access by chat ID.")
        elif action in VIEWS:
            try:
                text = render_status(self.source.get(), action)
            except SourceError:
                text = f"⚠️ PolyU status is temporarily unavailable.\n\nAvailability could not be verified. Try Refresh in a minute or check the official page:\n{SOURCE_URL}"
                LOG.warning("PolyU source unavailable; no cached availability served.")
            if callback and message.get("message_id"):
                try:
                    self.api.call("editMessageText", chat_id=chat_id, message_id=message["message_id"],
                                  text=text, reply_markup=status_controls(action),
                                  link_preview_options={"is_disabled": True})
                except TelegramError as exc:
                    if exc.code != 400:
                        raise
                    # Identical cached text or an uneditable old message: send a usable response.
                    self.send(chat_id, text, status_controls(action))
            else:
                self.send(chat_id, text, status_controls(action))
        else:
            self.send(chat_id, "Choose a button below, or use /menu to open the charger menu.", REPLY_MENU)

    def handle_subscription(self, chat_id, action, chat_type):
        if chat_type != "private":
            self.send(chat_id, f"Open https://t.me/{self.username} in a private chat to choose your personal notifications.")
            return
        if self.subscriptions is None:
            self.send(chat_id, "Notifications are not enabled on this instance.")
            return
        if action == "subscribe":
            self.send(chat_id, "🔔 Subscribe to PolyU charger status\n\nStep 1 of 2: choose a floor, or all floors. You’ll choose the frequency next.", floor_menu())
        elif action.startswith("sub:floor:"):
            floor = action.removeprefix("sub:floor:")
            if floor not in FLOORS:
                return
            label = "All floors" if floor == "all" else "Level " + floor
            self.send(chat_id, f"🔔 {label}\n\nStep 2 of 2: how often should I send a status summary?\n\nTapping a frequency enables notifications, even when status is unchanged. You can stop with /unsubscribe.", frequency_menu(floor))
        elif action.startswith("sub:set:"):
            parts = action.split(":")
            if len(parts) != 4 or parts[2] not in FLOORS or parts[3] not in {str(x) for x in FREQUENCIES}:
                return
            self.subscriptions.set(chat_id, parts[2], int(parts[3]))
            sub = self.subscriptions.get(chat_id)
            self.send(chat_id, f"✅ Notifications enabled\n\n{sub.label} · every {sub.minutes} minute(s)\nFirst notification in {sub.minutes} minute(s).\n\nSource: {SOURCE_URL}\n\n/subscriptions to change settings\n/unsubscribe to stop", subscription_menu(True))
        elif action == "unsubscribe":
            self.subscriptions.delete(chat_id)
            self.send(chat_id, "🔕 Notifications stopped. Your subscription settings have been deleted.", subscription_menu())
        elif action == "subscriptions":
            sub = self.subscriptions.get(chat_id)
            text = "🔕 You have no active notifications.\n\nChoose a floor and how often to receive status updates."
            if sub:
                next_at = datetime.fromtimestamp(sub.next_at, HKT)
                text = f"🔔 Your PolyU subscription\n\n{sub.label} · every {sub.minutes} minute(s)\nNext scheduled: {next_at:%d %b, %H:%M} HKT\n\nA status summary is sent every interval, even if unchanged.\nSource: {SOURCE_URL}"
            self.send(chat_id, text, subscription_menu(bool(sub)))

    def notify_due(self):
        if self.subscriptions is None:
            return
        for sub in self.subscriptions.due():
            if self.config.allowed_chat_ids and sub.chat_id not in self.config.allowed_chat_ids:
                self.subscriptions.delete(sub.chat_id)
                continue
            unavailable = False
            try:
                snapshot = self.source.get()
                unavailable = bool(snapshot.warning())
                text = f"🔔 Scheduled update · {sub.label}\n\n" + render_status(snapshot, sub.view)
            except SourceError:
                unavailable = True
                text = "🔔 PolyU status is temporarily unavailable.\n\nI’ll keep checking and send your next status summary when data returns. You won’t receive repeated outage notices."
            if unavailable and sub.unavailable_notified:
                self.subscriptions.reschedule(sub.chat_id, unavailable=True)
                continue
            try:
                self.send(sub.chat_id, text, status_controls(sub.view))
            except TelegramError as exc:
                if exc.code in {400, 403}:
                    self.subscriptions.delete(sub.chat_id)
                elif exc.code == 429:
                    self.subscriptions.defer(sub.chat_id, exc.retry_after)
                    raise
                elif exc.code in {401, 404, 409}:
                    raise
                else:
                    self.subscriptions.defer(sub.chat_id, 60)
                continue
            self.subscriptions.reschedule(sub.chat_id, unavailable=unavailable)

    def run(self):
        self.setup()
        offset = 0
        while True:
            try:
                self.notify_due()
                timeout = self.subscriptions.poll_timeout() if self.subscriptions else 25
                updates = self.api.call("getUpdates", offset=offset, timeout=timeout,
                                        allowed_updates=["message", "callback_query"])
                for update in updates:
                    try:
                        self.handle(update)
                    except TelegramError as exc:
                        if exc.code not in {400, 403}:
                            raise
                        LOG.warning("Could not reply to an update (Telegram code %s).", exc.code)
                    offset = update["update_id"] + 1
            except TelegramError as exc:
                if exc.code in {401, 404, 409}:
                    raise  # Bad credentials or another polling process needs operator action.
                LOG.warning("Telegram unavailable (code %s); retrying.", exc.code)
                time.sleep(exc.retry_after)
