import argparse
import logging
import sys
from pathlib import Path

from .bot import Bot, render_status
from .config import Config
from .source import SourceClient, SourceError
from .telegram import Telegram, TelegramError
from .subscriptions import Subscriptions


def main():
    parser = argparse.ArgumentParser(description="PolyU EV charger Telegram bot")
    parser.add_argument("--check", action="store_true", help="Print live PolyU status without a Telegram token")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    source = SourceClient()
    try:
        if args.check:
            print(render_status(source.get(), "status"))
        else:
            config = Config.from_env()
            subscriptions = Subscriptions(Path(config.data_dir) / "subscriptions.sqlite3")
            try:
                Bot(Telegram(config.token), source, config, subscriptions).run()
            finally:
                subscriptions.close()
    except KeyboardInterrupt:
        return 0
    except (ValueError, SourceError, TelegramError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
