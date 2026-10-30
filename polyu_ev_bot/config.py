"""Small, non-expanding .env loader. Environment variables take precedence."""

from dataclasses import dataclass
import os
from pathlib import Path
import re


def load_env(path=Path(".env")):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z_][A-Z_0-9]*", key.strip()):
            raise ValueError("Invalid .env assignment; use KEY=value.")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Config:
    token: str
    allowed_chat_ids: frozenset[int] = frozenset()
    data_dir: str = "data"

    @classmethod
    def from_env(cls):
        load_env()
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Set TELEGRAM_BOT_TOKEN in .env using the token from @BotFather.")
        try:
            ids = frozenset(int(x.strip()) for x in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if x.strip())
        except ValueError:
            raise ValueError("TELEGRAM_ALLOWED_CHAT_IDS must contain comma-separated numeric chat IDs.") from None
        return cls(token, ids, os.environ.get("BOT_DATA_DIR", "data"))
