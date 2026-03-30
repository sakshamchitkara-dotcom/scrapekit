import time
import unittest

from scrapekit.fetch import Fetcher, RobotsDisallowed
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

    def test_unreachable_host_disallowed_conservatively(self):
        f = Fetcher(delay=0, retries=0, timeout=1)
        self.assertFalse(f.allowed("http://127.0.0.1:9/x"))


if __name__ == "__main__":
    unittest.main()
