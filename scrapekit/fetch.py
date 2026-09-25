"""Polite HTTP fetching: robots.txt, per-domain rate limit, retries."""
from __future__ import annotations

import random
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from email.message import Message
from urllib.parse import urlsplit

from . import __version__

DEFAULT_UA = f"scrapekit/{__version__} (+https://github.com/sakshamchitkara-dotcom/scrapekit)"
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def is_html(self) -> bool:
        return self.content_type in ("text/html", "application/xhtml+xml", "")

    @property
    def text(self) -> str:
        m = Message()
        m["content-type"] = self.headers.get("content-type", "")
        charset = m.get_content_charset() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class RobotsDisallowed(Exception):
    pass


class Fetcher:
    def __init__(self, user_agent: str = DEFAULT_UA, delay: float = 1.0, retries: int = 3,
                 backoff: float = 0.5, timeout: float = 15.0, max_bytes: int = 5_000_000,
                 respect_robots: bool = True):
        self.user_agent = user_agent
        self.delay = delay
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.respect_robots = respect_robots
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._next_slot: dict[str, float] = {}
        self._lock = threading.Lock()
        self._robots_locks: dict[str, threading.Lock] = {}

    # -------------------------------------------------------------- robots
    def robots(self, url: str) -> urllib.robotparser.RobotFileParser:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self._lock:
            lock = self._robots_locks.setdefault(origin, threading.Lock())
        with lock:
            if origin not in self._robots:
                rp = urllib.robotparser.RobotFileParser(origin + "/robots.txt")
                try:
                    r = self._request(origin + "/robots.txt")
                    if r.status in (401, 403):
                        rp.disallow_all = True
                    elif r.status >= 400:
                        rp.allow_all = True
                    else:
                        rp.parse(r.text.splitlines())
                except (urllib.error.URLError, OSError):
                    rp.disallow_all = True  # can't tell: be conservative
                self._robots[origin] = rp
            return self._robots[origin]

    def allowed(self, url: str) -> bool:
        return not self.respect_robots or self.robots(url).can_fetch(self.user_agent, url)

    def _domain_delay(self, url: str) -> float:
        d = self.delay
        if self.respect_robots:
            cd = self.robots(url).crawl_delay(self.user_agent)
            if cd:
                d = max(d, float(cd))
        return d

    # ---------------------------------------------------------- rate limit
    def _wait_turn(self, url: str):
        dom = urlsplit(url).netloc
        delay = self._domain_delay(url)
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(dom, 0.0))
            self._next_slot[dom] = slot + delay
        if slot > now:
            time.sleep(slot - now)

    # --------------------------------------------------------------- fetch
    def _request(self, url: str) -> Response:
        req = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read(self.max_bytes + 1)[: self.max_bytes]
                headers = {k.lower(): v for k, v in r.headers.items()}
                return Response(url, r.geturl(), r.status, headers, body)
        except urllib.error.HTTPError as e:
            with e:
                headers = {k.lower(): v for k, v in (e.headers or {}).items()}
                return Response(url, url, e.code, headers, e.read() if e.fp else b"")

    def fetch(self, url: str) -> Response:
        """GET with robots check, rate limit and exponential-backoff retries.

        Raises RobotsDisallowed, or the last network error once retries run out.
        Returns non-retryable HTTP errors (e.g. 404) as a Response.
        """
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        for attempt in range(self.retries + 1):
            self._wait_turn(url)
            wait = self.backoff * (2 ** attempt) * (1 + random.random() * 0.1)
            try:
                resp = self._request(url)
            except (urllib.error.URLError, OSError):
                if attempt == self.retries:
                    raise
            else:
                if resp.status not in RETRY_STATUS or attempt == self.retries:
                    return resp
                ra = resp.headers.get("retry-after", "")
                if ra.isdigit():
                    wait = max(wait, min(float(ra), 60.0))
            time.sleep(wait)
        raise AssertionError("unreachable")
