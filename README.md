# PolyU EV Charger Telegram Bot

Check the 12 EV chargers at the PolyU carpark, or subscribe to status summaries for your floor.

**Bot:** [@polyu_evcharger_bot](https://t.me/polyu_evcharger_bot)

**Source:** [EVCharger@PolyU](https://www.polyu.edu.hk/greencampus/evcharger/index.php)

## Telegram menu

| Command | What it does |
| --- | --- |
| `/start`, `/menu` | Open the persistent button menu |
| `/all`, `/status` | All chargers across all three levels |
| `/available` | Available chargers only |
| `/level1` | Level 1: chargers 1–4 |
| `/level2` | Level 2: chargers 5–8 |
| `/level3` | Level 3: chargers 9–12 |
| `/subscribe` | Choose a floor and notification frequency |
| `/subscriptions` | View or change your subscription |
| `/unsubscribe` | Stop notifications and delete your subscription |
| `/help`, `/privacy` | Usage and data handling |

Commands and Telegram’s Menu button are registered automatically on startup. Status messages include inline floor filters, Refresh, subscription settings, and the official page link.

### Notifications

1. Open a private chat with the bot and send `/subscribe`.
2. Choose **Level 1**, **Level 2**, **Level 3**, or **All floors**.
3. Choose **every 1, 5, 15, 30, or 60 minutes**. This final tap enables notifications.

Each user has one subscription. Choosing again replaces their previous floor and frequency. A status summary is sent each interval, even when nothing changes. The first notification arrives after the selected interval. `/unsubscribe` stops it immediately when processed.

Settings persist in SQLite across restarts. Missed intervals are skipped; the bot sends one update after recovery, then resumes the interval. Network outages and Telegram rate limits can delay delivery. If PolyU fails or its update becomes stale, the bot sends one warning and suppresses repeated warnings until fresh data returns. Users who block the bot are removed from subscriptions. Subscriptions are available in private chats, so group members cannot subscribe others.

## Run locally

Requires Python **3.11+** with timezone data. There are **no third-party runtime dependencies**.

```sh
git clone https://github.com/kentaccn/polyu-ev-bot.git
cd polyu-ev-bot
cp .env.example .env
chmod 600 .env
```

Create a dedicated bot with [@BotFather](https://t.me/BotFather) and set `TELEGRAM_BOT_TOKEN` in `.env`. Keep the token private. Environment variables override `.env`. Optional `TELEGRAM_ALLOWED_CHAT_IDS` restricts access to a comma-separated list of chat IDs; empty permits anyone. `BOT_DATA_DIR` defaults to `data` and holds the subscription database.

```sh
python3 -m polyu_ev_bot --check   # Check the real PolyU page; no token needed
python3 -m polyu_ev_bot           # Start the Telegram bot
```

Keep exactly one polling instance per bot token. No inbound port, webhook, browser, or public server is needed. Startup refuses an existing webhook rather than deleting it.

### Keep running on macOS

The installer creates a new LaunchAgent for this checkout. It requires an active user login session and refuses to overwrite an existing service configuration.

```sh
python3 scripts/install_launchd.py
launchctl print "gui/$(id -u)/com.kenta.polyu-ev-bot"
```

Logs are in `data/bot.out.log` and `data/bot.err.log`. After updating the code:

```sh
launchctl kickstart -k "gui/$(id -u)/com.kenta.polyu-ev-bot"
```

Stop the service with `launchctl bootout "gui/$(id -u)/com.kenta.polyu-ev-bot"`. LaunchAgents resume after login, rather than before login after a reboot. Keep the host awake for uninterrupted notifications.

### Docker

```sh
docker build -t polyu-ev-bot .
docker run -d --name polyu-ev-bot --restart unless-stopped \
  --env-file .env -v polyu-ev-data:/app/data polyu-ev-bot
```

The image runs as an unprivileged user. The named volume keeps subscriptions through container replacement. Do not run Docker and the macOS service simultaneously with the same token.

## Data accuracy

PolyU’s page refreshes once a minute; the bot shares a 60-second cache across users and subscription deliveries. Refresh can reuse that cache. Failed requests also have a 60-second cooldown.

The parser reads each charger card and its status image:

| PolyU image | Meaning |
| --- | --- |
| `img/0.jpg` | 🟢 Available |
| `img/1.jpg` | 🟠 Occupied |
| `img/2.jpg` | 🔧 Not in service |
| Any other filename | ❔ Unknown |

The image labels were visually verified on 30 October 2026. Missing or duplicate chargers, a missing timestamp, changed floor assignments, and a changed grid fail closed. Unknown images are never counted as available. A source update older than five minutes, implausibly in the future, or a service state other than Normal produces a warning. Available-only results are suppressed when the data cannot be trusted. A failed refresh never silently serves the previous successful snapshot.

All times are HKT. Source time and fetch time are shown separately. Availability can change before you arrive. This is an unofficial convenience bot; it does not reserve a charger.

## Privacy

Message content is not saved. Subscriptions store only chat ID, selected floor, interval, next scheduled delivery, and whether an outage warning was sent. `/unsubscribe` deletes that record. Runtime logs omit chat IDs, message text, and Telegram credentials. Telegram details are never sent to PolyU. `.env`, logs, and the database are excluded from Git and Docker build context.

## Test

```sh
python3 -m unittest discover -s tests -v
```

Offline tests cover source parsing and failure handling, stale data, menus and commands, callback routing, access restrictions, credential redaction, subscription persistence, chosen-floor delivery, frequencies, outage recovery, blocked users, and rate limiting. CI runs on Python 3.11 and 3.13. `--check` is the separate live-source smoke test.
