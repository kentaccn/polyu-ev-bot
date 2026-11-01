import argparse
import logging
import os
import sys
from pathlib import Path

from .bot import Bot, render_status
from .config import Config, load_env
from .collector import collect_forever
from .history import History, RecordedSource
from .source import SourceClient, SourceError
from .telegram import Telegram, TelegramError
from .subscriptions import Subscriptions


def main():
    parser = argparse.ArgumentParser(description="PolyU EV charger Telegram bot")
    parser.add_argument("--check", action="store_true", help="Print live PolyU status without a Telegram token")
    parser.add_argument("--collect", action="store_true", help="Collect history every minute without Telegram")
    parser.add_argument("--history-check", action="store_true", help="Print stored status and history without a token")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    source = SourceClient()
    try:
        if args.check:
            print(render_status(source.get(), "status"))
        elif args.collect or args.history_check:
            load_env()
            history = History(Path(os.environ.get("BOT_DATA_DIR", "data")) / "history.sqlite3")
            try:
                if args.collect:
                    collect_forever(history)
                else:
                    print(history.summary())
                    print(render_status(history.latest(), "all", history=history))
            finally:
                history.close()
        else:
            config = Config.from_env()
            subscriptions = Subscriptions(Path(config.data_dir) / "subscriptions.sqlite3")
            history = History(Path(config.data_dir) / "history.sqlite3")
            try:
                Bot(Telegram(config.token), RecordedSource(history), config, subscriptions, history).run()
            finally:
                subscriptions.close()
                history.close()
    except KeyboardInterrupt:
        return 0
    except (ValueError, SourceError, TelegramError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
