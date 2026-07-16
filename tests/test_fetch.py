import time
import unittest
import urllib.error
from email.utils import formatdate

from scrapekit.fetch import Fetcher, Response, RobotsDisallowed, retry_after
from tests.server import FixtureServer


class TestFetch(unittest.TestCase):
    def test_robots_and_user_agent(self):
        with FixtureServer() as srv:
            f = Fetcher(user_agent="testbot/1.0", delay=0)
            self.assertEqual(f.fetch(srv.url + "about.html").status, 200)
            with self.assertRaises(RobotsDisallowed):
                f.fetch(srv.url + "private/secret.html")
            self.assertNotIn("/private/secret.html", srv.paths())
            self.assertEqual(srv.paths().count("/robots.txt"), 1)  # cached
            self.assertTrue(all(ua == "testbot/1.0" for _, ua in srv.log))

    def test_retry_with_backoff(self):
        with FixtureServer() as srv:
            f = Fetcher(delay=0, retries=3, backoff=0.05)
            t = time.monotonic()
            r = f.fetch(srv.url + "flaky")
            self.assertEqual(r.status, 200)
            self.assertEqual(srv.paths().count("/flaky"), 3)
            self.assertGreaterEqual(time.monotonic() - t, 0.05 + 0.1)  # 0.05 then 0.1

    def test_retries_exhausted_returns_last_status(self):
        with FixtureServer() as srv:
            r = Fetcher(delay=0, retries=1, backoff=0.01).fetch(srv.url + "flaky")
            self.assertEqual(r.status, 503)

    def test_redirect_target_checked_against_robots(self):
        with FixtureServer() as srv:
            f = Fetcher(delay=0, retries=0)
            r = f.fetch(srv.url + "redirect/about.html")
            self.assertEqual((r.status, r.final_url), (200, srv.url + "about.html"))
            with self.assertRaises(RobotsDisallowed):
                f.fetch(srv.url + "redirect/private/secret.html")
            self.assertNotIn("/private/secret.html", srv.paths())

    def test_retry_after_is_honored(self):
        for kind in ("seconds", "http-date"):
            with self.subTest(kind), FixtureServer() as srv:
                srv.retry_after = "1" if kind == "seconds" else formatdate(time.time() + 2, usegmt=True)
                t = time.monotonic()
                r = Fetcher(delay=0, retries=2, backoff=0.01).fetch(srv.url + "limited")
                self.assertEqual(r.status, 200)
                self.assertEqual(srv.paths().count("/limited"), 2)
                self.assertGreaterEqual(time.monotonic() - t, 0.9)  # not the 10 ms backoff

    def test_retry_after_parsing(self):
        self.assertEqual(retry_after("120"), 120.0)
        self.assertEqual(retry_after("Wed, 21 Oct 2015 07:28:00 GMT"), 0.0)  # in the past
        self.assertAlmostEqual(retry_after(formatdate(time.time() + 30, usegmt=True)), 30, delta=2)
        self.assertIsNone(retry_after(""))
        self.assertIsNone(retry_after("soon"))

    def test_network_error_raised_after_retries(self):
        f = Fetcher(delay=0, retries=2, backoff=0.01, timeout=1, respect_robots=False)
        with self.assertRaises(urllib.error.URLError):
            f.fetch("http://127.0.0.1:9/nothing-listens-here")

    def test_404_not_retried(self):
        with FixtureServer() as srv:
            r = Fetcher(delay=0, retries=3).fetch(srv.url + "nope.html")
            self.assertEqual(r.status, 404)
            self.assertEqual(srv.paths().count("/nope.html"), 1)

    def test_rate_limit_per_domain(self):
        with FixtureServer() as srv:
            f = Fetcher(delay=0.2)
            t = time.monotonic()
            for _ in range(3):
                f.fetch(srv.url)
            self.assertGreaterEqual(time.monotonic() - t, 0.4)

    def test_robots_5xx_disallows_whole_origin(self):
        for code in (500, 503):
            with FixtureServer() as srv:
                srv.robots_status = code
                f = Fetcher(delay=0, retries=0)
                self.assertFalse(f.allowed(srv.url))
                with self.assertRaises(RobotsDisallowed):
                    f.fetch(srv.url + "about.html")
                self.assertEqual(srv.paths(), ["/robots.txt"])  # nothing else requested

    def test_robots_404_allows_all(self):
        with FixtureServer() as srv:
            srv.robots_status = 404
            f = Fetcher(delay=0, retries=0)
            self.assertEqual(f.fetch(srv.url + "private/secret.html").status, 200)

    def test_conditional_requests(self):
        with FixtureServer() as srv:
            f = Fetcher(delay=0, retries=0)
            r = f.fetch(srv.url + "about.html")  # static file: Last-Modified
            lm = r.headers["last-modified"]
            self.assertEqual(f.fetch(srv.url + "about.html", last_modified=lm).status, 304)
            r = f.fetch(srv.url + "mutable")  # dynamic: ETag
            etag = r.headers["etag"]
            nm = f.fetch(srv.url + "mutable", etag=etag)
            self.assertEqual((nm.status, nm.body), (304, b""))
            srv.mutable = "<html><title>v2</title></html>"
            self.assertEqual(f.fetch(srv.url + "mutable", etag=etag).status, 200)

    def test_charset_from_header_then_meta(self):
        def text(ctype, body):
            return Response("u", "u", 200, {"content-type": ctype}, body).text
        latin = "<meta charset='ISO-8859-1'><p>café</p>".encode("latin-1")
        self.assertIn("café", text("text/html", latin))
        http_equiv = ('<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">'
                      "<p>€5</p>").encode("cp1252")
        self.assertIn("€5", text("text/html", http_equiv))
        self.assertIn("café", text("text/html; charset=latin-1", "<p>café</p>".encode("latin-1")))
        self.assertIn("café", text("text/html", "<p>café</p>".encode()))  # default utf-8
        self.assertIn("caf", text("text/html", b"<meta charset=bogus><p>caf\xc3\xa9</p>"))

    def test_unreachable_host_disallowed_conservatively(self):
        f = Fetcher(delay=0, retries=0, timeout=1)
        self.assertFalse(f.allowed("http://127.0.0.1:9/x"))


if __name__ == "__main__":
    unittest.main()
