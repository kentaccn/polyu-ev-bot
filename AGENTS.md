# PolyU EV bot

- Python 3.11+, standard library only at runtime.
- `python3 -m unittest discover -s tests -v` runs offline tests.
- `python3 -m polyu_ev_bot --check` checks the live source without Telegram.
- Keep the PolyU fetch cache at least 60 seconds, including failure cooldowns.
- Never infer availability from missing cards or unrecognized images.
- Preserve source timestamps and stale-data warnings in every status view.
- Never log a Telegram token, API URL, message content, or chat ID.
- Keep `.env` and credentials out of Git and Docker image layers.
- Docker Compose runs independent `collector` and `bot` services; preserve their shared data volume.
- Archive all fetched pages, statuses and failed attempts. Do not prune history automatically.
- Never invent a session start, last-empty time, vehicle power or battery state.
- Gaps, failures, unknown statuses and maintenance break session-start inference.
- The four-hour limit is a user-provided forecasting assumption, not a departure guarantee.
