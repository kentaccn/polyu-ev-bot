"""Check collection liveness, including archived upstream failures."""

import os
from pathlib import Path
import sqlite3
import sys
import time


def main():
    path = (Path(os.environ.get("BOT_DATA_DIR", "data")) / "history.sqlite3").resolve()
    if not path.exists():
        return 1
    try:
        db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute("SELECT MAX(fetched_at) FROM samples").fetchone()
        finally:
            db.close()
        return 0 if row and row[0] and 0 <= time.time() - row[0] <= 150 else 1
    except sqlite3.Error:
        return 1


if __name__ == "__main__":
    sys.exit(main())
