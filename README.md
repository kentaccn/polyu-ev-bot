<img src="assets/polyu-logo.png" alt="The Hong Kong Polytechnic University" width="400">

# PolyU EV Charger Telegram Bot

Check the 12 EV chargers at the PolyU carpark, see their last-seen-empty times and estimated release times, or subscribe to status summaries for your floor.

An unofficial community project. The PolyU logo belongs to the university; see [asset attribution](assets/README.md).

**Bot:** [@polyu_evcharger_bot](https://t.me/polyu_evcharger_bot)

**Source:** [EVCharger@PolyU](https://www.polyu.edu.hk/greencampus/evcharger/index.php)

## Telegram menu

| Command | What it does |
| --- | --- |
| `/start`, `/menu` | Open the persistent button menu |
| `/all`, `/status` | All chargers across all three levels |
| `/available` | Available chargers only |
| `/predict` | Last-empty times and release estimates for every charger |
| `/history` | Collection start, observation count and completed sessions |
| `/level1` | Level 1: chargers 1–4 |
| `/level2` | Level 2: chargers 5–8 |
| `/level3` | Level 3: chargers 9–12 |
| `/subscribe` | Choose a floor and notification frequency |
| `/subscriptions` | View or change your subscription |
| `/unsubscribe` | Stop notifications and delete your subscription |
| `/help`, `/privacy` | Usage and data handling |

Commands and Telegram’s Menu button are registered automatically on startup. Navigation stays in the persistent keyboard at the bottom. Status replies and scheduled updates include only a Refresh button, so the full menu is not repeated under each message. Subscription setup shows floor and frequency choices when requested.

### Notifications

1. Open a private chat with the bot and send `/subscribe`.
2. Choose **Level 1**, **Level 2**, **Level 3**, or **All floors**.
3. Choose **every 1, 5, 15, 30, or 60 minutes**. This final tap enables notifications.

Each user has one subscription. Choosing again replaces their previous floor and frequency. A status summary is sent each interval, even when nothing changes. The first notification arrives after the selected interval. `/unsubscribe` stops it immediately when processed.

Settings persist in SQLite across restarts. Missed intervals are skipped; the bot sends one update after recovery, then resumes the interval. Network outages and Telegram rate limits can delay delivery. If PolyU fails or its update becomes stale, the bot sends one warning and suppresses repeated warnings until fresh data returns. Users who block the bot are removed from subscriptions. Subscriptions are available in private chats, so group members cannot subscribe others.

## Docker on the Mac Mini

Docker Compose runs two independent services: **collector** fetches PolyU every minute; **bot** answers Telegram using the stored data. Telegram downtime does not interrupt collection. No inbound ports are exposed. Both services restart automatically while Docker is running.

```sh
git clone https://github.com/kentaccn/polyu-ev-bot.git
cd polyu-ev-bot
cp .env.example .env
chmod 600 .env
```

Create a dedicated bot with [@BotFather](https://t.me/BotFather) and set `TELEGRAM_BOT_TOKEN` in `.env`. Optional `TELEGRAM_ALLOWED_CHAT_IDS` restricts access to specific comma-separated chat IDs; empty permits anyone. The token goes only to the bot service, not the collector. Docker images contain no credentials or runtime database.

```sh
docker compose up -d --build
docker compose ps
docker compose logs --tail 20 collector bot
```

The named `polyu-ev-bot_polyu-ev-data` volume stores `history.sqlite3` and `subscriptions.sqlite3`. It survives container updates, replacement and `docker compose down`. **Do not use `down -v`** unless you intend to delete all history and subscriptions. Container logs rotate; historical observations in SQLite are retained without automatic pruning. Back up both databases with SQLite's online backup API, rather than copying a live WAL database file alone.

```sh
# Update code and restart the two services, preserving data
git pull
docker compose up -d --build

# Show the stored status, prediction details and collection progress
docker compose exec collector python -m polyu_ev_bot --history-check
```

The collector health check verifies that fetch attempts are being archived, including failed attempts during a PolyU outage. The bot refuses to call old cached data current if the collector stops. Run only one bot instance per token. Docker Desktop must be running on the Mini; enable its start-at-login setting and keep the host awake for uninterrupted service. Container restart policy resumes services when the Docker engine restarts.

### Without Docker

Python **3.11+** with timezone data is sufficient; there are no third-party runtime dependencies. Run these as separate supervised processes from the same folder and with the same `BOT_DATA_DIR` (default `data`):

```sh
python3 -m polyu_ev_bot --collect  # Always-on history collector; no bot token required
python3 -m polyu_ev_bot            # Telegram bot reads the collector database
```

`python3 -m polyu_ev_bot --check` performs a separate live-source check without a token. `--history-check` reads stored observations and forecasts. A new macOS installation can use `python3 scripts/install_launchd.py` to install both processes as LaunchAgents; do not run these alongside Docker with the same token. Existing older single-process LaunchAgents should be stopped and disabled before switching to Docker.

## History and predictions

Every fetch attempt is recorded. Successful polls save source time, fetch time, service state, all 12 charger statuses, and the exact fetched HTML compressed with zlib plus a SHA-256 digest. Failed polls save their error and the bounded response body when available. Unchanged snapshots are retained too. No vehicle identity, battery state, charging power, or Telegram messages are collected. All timestamps are HKT in the bot, Unix timestamps in SQLite.

Each charger shows its **last seen empty** time. Occupied-to-available transitions close a session; available-to-occupied transitions bracket its start between observations. The actual transition time is only known to approximately the polling interval. Downtime, errors, maintenance and unknown statuses break start-time inference. Repeated or regressing source timestamps cannot extend a session.

- **Observed start:** show the four-hour limit window from the observed start bracket.
- **Already occupied at collection startup:** show a low-confidence upper estimate of first observation + four hours, assuming the same session and that the limit is followed. The actual start is unknown and it could be free much sooner.
- **Enough completed sessions:** use the 25th–75th percentile of observed durations longer than the session's elapsed age, from this charger's latest 200 completed sessions (at least 5 matching samples), or all chargers' latest 2,000 completed sessions (at least 10). The range is capped by the assumed four-hour upper time. This is a heuristic empirical range, not a calibrated confidence interval.
- **Four hours passed:** keep the charger Occupied and report that the estimate has passed. Never turn a prediction into an Available status.
- **Stale or missing data:** suppress current free-time estimates.

The **four-hour maximum is a user-provided assumption**. It does not prove a vehicle has moved; overstays, immediate reuse between polls and delayed source updates can make estimates wrong. Sessions without an observed start or end are not used to train the simple duration model. Historical observations before this collector began are unavailable; the public page does not supply them.

The page also does not identify which car accepts **7 kW or 11 kW**, nor its battery size or state of charge. Those cannot be inferred reliably from occupancy alone. At an assumed 28 kWh needed, ideal charge times would be 4h at 7 kW or about 2h33m at 11 kW; that example is not used as an invented per-car ETA. Predictions instead learn from actual observed occupancy.

## Data accuracy

PolyU’s page refreshes once a minute; the collector waits at least 60 seconds between fetch attempts, including failures. All users and subscription deliveries share its stored observations. Refresh reads the latest collected snapshot.

The parser reads each charger card and its status image:

| PolyU image | Meaning |
| --- | --- |
| `img/0.jpg` | 🟢 Available |
| `img/1.jpg` | 🟠 Occupied |
| `img/2.jpg` | 🔧 Not in service |
| Any other filename | ❔ Unknown |

The image labels were visually verified on 30 October 2026. Missing or duplicate chargers, a missing timestamp, changed floor assignments, and a changed grid fail closed. Unknown images are never counted as available. Normal and Full are valid capacity summaries (Full was verified against the live page on 1 November 2026). A source update older than five minutes, implausibly in the future, or another service state produces a warning. Available-only results are suppressed when the data cannot be trusted. A failed refresh never silently serves the previous successful snapshot.

All times are HKT. Source time and fetch time are shown separately. Availability can change before you arrive. This is an unofficial convenience bot; it does not reserve a charger.

## Privacy

Message content is not saved. Subscriptions store only chat ID, selected floor, interval, next scheduled delivery, and whether an outage warning was sent. `/unsubscribe` deletes that record. Runtime logs omit chat IDs, message text, and Telegram credentials. Telegram details are never sent to PolyU. `.env`, logs, and both databases are excluded from Git and Docker build context.

## Test

```sh
python3 -m unittest discover -s tests -v
```

Offline tests cover historical raw-page retention, session boundaries, observation gaps, cold starts, empirical estimates, the four-hour assumption, message lengths, and source parsing and failure handling, stale data, menus and commands, callback routing, access restrictions, credential redaction, subscription persistence, chosen-floor delivery, frequencies, outage recovery, blocked users, and rate limiting. CI runs on Python 3.11 and 3.13. `--check` is the separate live-source smoke test.
