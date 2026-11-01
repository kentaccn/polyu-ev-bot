"""Read PolyU's server-rendered page; fail closed if its structure changes."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import PurePosixPath
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SOURCE_URL = "https://www.polyu.edu.hk/greencampus/evcharger/index.php"
HKT = ZoneInfo("Asia/Hong_Kong")
MAX_PAGE_BYTES = 1_048_576
STATUS_IMAGES = {"0.jpg": "Available", "1.jpg": "Occupied", "2.jpg": "Not in service"}


class SourceError(Exception):
    """The source could not provide a trustworthy snapshot."""


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def text(self):
        return " ".join(c if isinstance(c, str) else c.text() for c in self.children)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()


class PageParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


@dataclass(frozen=True)
class Charger:
    number: int
    level: int
    status: str


@dataclass(frozen=True)
class Snapshot:
    chargers: tuple[Charger, ...]
    updated_at: datetime
    fetched_at: datetime
    service: str

    def warning(self, now=None):
        now = now or datetime.now(HKT)
        if self.updated_at > now + timedelta(minutes=2):
            return "PolyU's update time is in the future. Availability may be unreliable."
        if now - self.updated_at > timedelta(minutes=5):
            return "PolyU's data is over 5 minutes old. Availability may have changed."
        if self.service.casefold() not in {"normal", "full"}:
            return f"PolyU reports: {self.service}. Availability may be unreliable."
        return None


def parse_page(html, fetched_at=None):
    parser = PageParser()
    parser.feed(html)
    text = " ".join(parser.root.text().split())
    stamp = re.search(r"Updated on:\s*(\d{1,2}:\d{2}[ap]m,\s*\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", text, re.I)
    if not stamp:
        raise SourceError("PolyU's update timestamp is missing.")
    # Parse English month names explicitly, independent of the host locale.
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(am|pm),\s*(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", stamp[1], re.I)
    try:
        hour, minute, period, day, month, year = match.groups()
        month_num = "jan feb mar apr may jun jul aug sep oct nov dec".split().index(month.lower()) + 1
        if not 1 <= int(hour) <= 12:
            raise ValueError("Invalid hour")
        updated = datetime(int(year), month_num, int(day), int(hour) % 12 + (12 if period.lower() == "pm" else 0), int(minute), tzinfo=HKT)
    except (AttributeError, ValueError) as exc:
        raise SourceError("PolyU's update timestamp is invalid.") from exc

    breadcrumbs = [n for n in parser.root.walk() if n.has_class("breadcrumb")]
    service = "Unknown"
    if breadcrumbs:
        for n in breadcrumbs[0].walk():
            if n.tag == "b":
                service = " ".join(n.text().split()) or "Unknown"
                break

    grids = [n for n in parser.root.walk() if n.has_class("evno_css")]
    if len(grids) != 1:
        raise SourceError("PolyU's charger grid is missing or has changed.")
    chargers = []
    level = None
    for card in grids[0].children:
        if not isinstance(card, Node):
            continue
        label = " ".join(card.text().split())
        floor = re.fullmatch(r"Level\s+([1-3]),\s*Carpark", label)
        if floor:
            level = int(floor[1])
            continue
        if not card.has_class("imgmargin_123"):
            continue
        number = re.search(r"\bNo\.\s*(\d+)\b", label)
        images = [n for n in card.walk() if n.tag == "img"]
        if number is None or level is None or len(images) != 1:
            raise SourceError("A charger card has changed format.")
        filename = PurePosixPath(urlsplit(images[0].attrs.get("src", "")).path).name
        chargers.append(Charger(int(number[1]), level, STATUS_IMAGES.get(filename, "Unknown")))
    if sorted(c.number for c in chargers) != list(range(1, 13)):
        raise SourceError("Expected exactly 12 unique chargers; PolyU's page may have changed.")
    if any(c.level != (c.number - 1) // 4 + 1 for c in chargers):
        raise SourceError("PolyU's charger locations have changed.")
    return Snapshot(tuple(sorted(chargers, key=lambda c: c.number)), updated, fetched_at or datetime.now(HKT), service)


class SourceClient:
    """Share a 60-second cache between all chats, matching the source cadence."""

    def __init__(self, history=None):
        self.history = history
        self.snapshot = None
        self.next_fetch = 0.0
        self.last_error = None

    def get(self):
        if time.monotonic() < self.next_fetch:
            if self.last_error:
                raise SourceError(self.last_error)
            return self.snapshot
        raw = None
        try:
            request = Request(SOURCE_URL, headers={"User-Agent": "PolyU-EV-Telegram-Bot/0.1", "Cache-Control": "no-cache"})
            with urlopen(request, timeout=15) as response:
                raw = response.read(MAX_PAGE_BYTES + 1)
            if len(raw) > MAX_PAGE_BYTES:
                raise SourceError("PolyU returned an unexpectedly large page.")
            snapshot = parse_page(raw.decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, HTTPException, UnicodeError, ValueError, RecursionError, SourceError) as exc:
            self.last_error = str(exc) if isinstance(exc, SourceError) else "Could not read PolyU's status page."
            if self.history:
                self.history.record(raw=raw[:MAX_PAGE_BYTES] if raw is not None else None, error=self.last_error)
            self.next_fetch = time.monotonic() + 60
            raise SourceError(self.last_error) from None
        self.snapshot = snapshot
        if self.history:
            self.history.record(snapshot, raw)
        self.last_error = None
        self.next_fetch = time.monotonic() + 60
        return snapshot
