# PolyU EV bot

- Python 3.11+, standard library only at runtime.
- `python3 -m unittest discover -s tests -v` runs offline tests.
- `python3 -m polyu_ev_bot --check` checks the live source without Telegram.
- Keep the PolyU fetch cache at least 60 seconds, including failure cooldowns.
- Never infer availability from missing cards or unrecognized images.
- Preserve source timestamps and stale-data warnings in every status view.
- Never log a Telegram token, API URL, message content, or chat ID.
- Keep `.env` and credentials out of Git and Docker image layers.
