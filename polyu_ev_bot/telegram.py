"""Minimal Telegram API client; exceptions never include credential-bearing URLs."""

import json
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(Exception):
    def __init__(self, code=0, retry_after=5):
        super().__init__(f"Telegram request failed (code {code}).")
        self.code = code
        self.retry_after = max(1, int(retry_after))


class Telegram:
    def __init__(self, token):
        self._base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **payload):
        request = Request(self._base + method, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=40) as response:
                raw = response.read(4_194_305)
        except HTTPError as exc:
            try:
                body = json.loads(exc.read(65_536))
                retry_after = body.get("parameters", {}).get("retry_after", 5)
            except (ValueError, OSError, AttributeError, TypeError):
                retry_after = 5
            finally:
                exc.close()
            raise TelegramError(exc.code, retry_after) from None
        except (URLError, OSError, TimeoutError, ValueError, HTTPException):
            raise TelegramError() from None
        try:
            body = json.loads(raw)
            if not body.get("ok"):
                raise TelegramError(body.get("error_code", 0), body.get("parameters", {}).get("retry_after", 5))
            return body["result"]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise TelegramError() from None
