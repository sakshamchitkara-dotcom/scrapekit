import csv
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from scrapekit.crawler import CrawlConfig, Crawler
from scrapekit.diff import diff_runs, format_report, has_changes, pick_runs, post_webhook
from scrapekit.export import to_csv, to_jsonl, to_sqlite
from scrapekit.store import Store
from tests.server import FixtureServer

ROWS = [{"name": "a", "price": 1.5, "tags": ["x", "y"]}, {"name": "b", "extra": None}]


class TestDiff(unittest.TestCase):
    def test_detects_change_between_runs(self):
        with tempfile.TemporaryDirectory() as tmp, FixtureServer() as srv:
            store = Store(os.path.join(tmp, "d.db"))
            cfg = CrawlConfig(delay=0, retries=0, max_depth=0)
            r1 = Crawler(store, cfg).run(srv.url + "mutable")
            r2 = Crawler(store, cfg).run(srv.url + "mutable")
            self.assertFalse(has_changes(diff_runs(store, r1, r2)))
            srv.mutable = srv.mutable.replace("price 10", "price 12")
            r3 = Crawler(store, cfg).run(srv.url + "mutable")
            self.assertEqual(pick_runs(store), (r2, r3))
            d = diff_runs(store, *pick_runs(store))
            store.close()
        self.assertEqual([c["url"] for c in d["changed"]], [srv.url + "mutable"])
        self.assertIn("-price 10", d["changed"][0]["diff"])
        self.assertIn("+price 12", d["changed"][0]["diff"])
        self.assertIn("1 changed", format_report(d))

    def test_webhook(self):
        got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass
        httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.handle_request, daemon=True).start()
        d = {"old_run": 1, "new_run": 2, "added": ["http://x/"], "removed": [], "changed": []}
        status = post_webhook(f"http://127.0.0.1:{httpd.server_address[1]}/hook", d)
        httpd.server_close()
        self.assertEqual(status, 204)
        self.assertEqual(got[0]["added"], ["http://x/"])
        self.assertIn("1 added", got[0]["text"])


class TestExport(unittest.TestCase):
    def test_jsonl(self):
        buf = io.StringIO()
        to_jsonl(ROWS, buf)
        self.assertEqual([json.loads(l) for l in buf.getvalue().splitlines()], ROWS)

    def test_csv(self):
        buf = io.StringIO()
        to_csv(ROWS, buf)
        rows = list(csv.DictReader(io.StringIO(buf.getvalue())))
        self.assertEqual(list(rows[0]), ["name", "price", "tags", "extra"])
        self.assertEqual(rows[0]["tags"], '["x", "y"]')

    def test_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "o.db")
            to_sqlite(ROWS, p)
            db = sqlite3.connect(p)
            self.assertEqual(db.execute("SELECT name, price FROM items").fetchall(),
                             [("a", 1.5), ("b", None)])
            db.close()


if __name__ == "__main__":
    unittest.main()
