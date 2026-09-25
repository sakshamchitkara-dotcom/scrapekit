"""Polite HTTP fetching: robots.txt, per-domain rate limit, retries."""
from __future__ import annotations

import random
import re
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from email.message import Message
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from . import __version__

DEFAULT_UA = f"scrapekit/{__version__} (+https://github.com/sakshamchitkara-dotcom/scrapekit)"
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)""", re.I)


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    elapsed: float = 0.0  # seconds for the final attempt, excluding rate-limit waits

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
        charset = m.get_content_charset()
        if not charset:  # <meta charset> / http-equiv, which must sit in the first 1024 bytes
            found = _META_CHARSET.search(self.body[:1024])
            charset = found.group(1).decode() if found else "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def retry_after(value: str) -> float | None:
    """Seconds to wait from a Retry-After header: delay-seconds or an HTTP-date."""
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return max(0.0, when.timestamp() - time.time())


class RobotsDisallowed(Exception):
    """url is off limits. reason says why: "robots.txt" (the rules disallow it) or why
    robots.txt couldn't be read, since that blocks the whole origin."""

    def __init__(self, url: str, reason: str = "robots.txt"):
        super().__init__(url)
        self.url, self.reason = url, reason


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only if robots.txt allows its target."""

    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.fetcher.allowed(newurl):
            fp.close()
            raise RobotsDisallowed(newurl, self.fetcher.robots_problem(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    def __init__(self, user_agent: str = DEFAULT_UA, delay: float = 1.0, retries: int = 3,
                 backoff: float = 0.5, timeout: float = 15.0, max_bytes: int = 5_000_000,
                 respect_robots: bool = True, proxy: str | None = None,
                 domain_delays: dict[str, float] | None = None):
        self.user_agent = user_agent
        self.delay = delay
        self.domain_delays = {k.lower(): v for k, v in (domain_delays or {}).items()}
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.respect_robots = respect_robots
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._robots_errors: dict[str, str] = {}  # origin -> why robots.txt blocks everything
        self._next_slot: dict[str, float] = {}
        self._lock = threading.Lock()
        self._robots_locks: dict[str, threading.Lock] = {}
        # Without an explicit proxy, urllib uses HTTP(S)_PROXY / NO_PROXY from the environment.
        proxies = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
        # robots.txt itself is fetched without the redirect check (it would recurse)
        self._plain = urllib.request.build_opener(*proxies)
        self._checked = urllib.request.build_opener(_CheckedRedirect(self), *proxies)

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
                    if r.status in (401, 403) or r.status >= 500:
                        rp.disallow_all = True  # forbidden or unknown: be conservative
                        self._robots_errors[origin] = f"robots.txt HTTP {r.status}"
                    elif r.status >= 400:
                        rp.allow_all = True
                    else:
                        rp.parse(r.text.splitlines())
                except (urllib.error.URLError, OSError) as e:
                    rp.disallow_all = True  # can't tell: be conservative
                    self._robots_errors[origin] = f"robots.txt unreachable: {getattr(e, 'reason', e)}"
                self._robots[origin] = rp
            return self._robots[origin]

    def sitemap_locations(self, url: str) -> list[str]:
        """Sitemaps named by robots.txt `Sitemap:` lines, then the conventional /sitemap.xml."""
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        found = [urljoin(origin + "/", s.strip()) for s in (self.robots(url).site_maps() or [])]
        return list(dict.fromkeys(found + [origin + "/sitemap.xml"]))

    def robots_problem(self, url: str) -> str:
        """Why url is disallowed: robots.txt's rules, or why robots.txt couldn't be read."""
        p = urlsplit(url)
        return self._robots_errors.get(f"{p.scheme}://{p.netloc}", "robots.txt")

    def allowed(self, url: str) -> bool:
        return not self.respect_robots or self.robots(url).can_fetch(self.user_agent, url)

    def _domain_delay(self, url: str) -> float:
        """--delay, or the host's own override (by host:port, then host); never below Crawl-delay."""
        p = urlsplit(url)
        d = self.domain_delays.get(p.netloc.lower(), self.domain_delays.get(p.hostname or "", self.delay))
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
    def _request(self, url: str, headers: dict | None = None, opener=None) -> Response:
        req = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            **(headers or {}),
        })
        t0 = time.monotonic()
        try:
            with (opener or self._plain).open(req, timeout=self.timeout) as r:
                body = r.read(self.max_bytes + 1)[: self.max_bytes]
                headers = {k.lower(): v for k, v in r.headers.items()}
                return Response(url, r.geturl(), r.status, headers, body, time.monotonic() - t0)
        except urllib.error.HTTPError as e:
            with e:
                headers = {k.lower(): v for k, v in (e.headers or {}).items()}
                body = e.read() if e.fp else b""
                return Response(url, url, e.code, headers, body, time.monotonic() - t0)

    def fetch(self, url: str, etag: str | None = None, last_modified: str | None = None) -> Response:
        """GET with robots check, rate limit and exponential-backoff retries.

        Pass a previous response's ETag / Last-Modified to make the request
        conditional; an unchanged page then comes back as status 304 with no body.
        Redirects are followed only to URLs robots.txt allows.
        Raises RobotsDisallowed, or the last network error once retries run out.
        Returns non-retryable HTTP errors (e.g. 404) as a Response.
        """
        if not self.allowed(url):
            raise RobotsDisallowed(url, self.robots_problem(url))
        cond = {}
        if etag:
            cond["If-None-Match"] = etag
        if last_modified:
            cond["If-Modified-Since"] = last_modified
        for attempt in range(self.retries + 1):
            self._wait_turn(url)
            wait = self.backoff * (2 ** attempt) * (1 + random.random() * 0.1)
            try:
                resp = self._request(url, cond, self._checked)
            except (urllib.error.URLError, OSError):
                if attempt == self.retries:
                    raise
            else:
                if resp.status not in RETRY_STATUS or attempt == self.retries:
                    return resp
                ra = retry_after(resp.headers.get("retry-after", ""))
                if ra is not None:
                    wait = max(wait, min(ra, 60.0))
            time.sleep(wait)
        raise AssertionError("unreachable")
