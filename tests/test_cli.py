import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scrapekit.cli import main
from scrapekit.extract import Recipe
from tests.server import FixtureServer, ProxyServer

RECIPE = str(Path(__file__).parent.parent / "recipes" / "fixture.json")


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main([str(a) for a in argv])
    return code, out.getvalue()


class TestCli(unittest.TestCase):
    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp, FixtureServer() as srv:
            db = os.path.join(tmp, "s.db")
            code, out = run("crawl", srv.url, "--recipe", RECIPE, "--db", db, "--delay", "0",
                            "--max-depth", "3")
            self.assertEqual(code, 0)
            self.assertIn("items=3", out)
            self.assertIn("skipped=1", out)  # robots-disallowed page
            self.assertRegex(out, r"http 200=9 \| [\d.]+ KB \| p50 [\d.]+ ms")

            _, out = run("export", "--db", db)
            names = sorted(json.loads(l)["name"] for l in out.splitlines())
            self.assertEqual(names, ["Blue Gadget", "Green Gizmo", "Red Widget"])
            csv_path = os.path.join(tmp, "o.csv")
            run("export", "--db", db, "--format", "csv", "-o", csv_path)
            self.assertTrue(Path(csv_path).read_text().startswith("name,price,stock,tags,_url"))

            run("crawl", srv.url, "--recipe", RECIPE, "--db", db, "--delay", "0", "--max-depth", "3")
            code, out = run("diff", "--db", db, "--exit-code")
            self.assertEqual(code, 0)
            self.assertIn("0 added, 0 removed, 0 changed", out)

            _, out = run("stats", "--db", db)  # latest run: a re-crawl, so all revalidated
            self.assertIn("run 2: " + srv.url, out)
            self.assertIn("http 304=9 | 0 B |", out)
            _, out = run("stats", "--db", db, "--run", 1, "--json")
            self.assertEqual(json.loads(out)["status_codes"], {"200": 9})
            with self.assertRaises(SystemExit):
                run("stats", "--db", db, "--run", 99)

    def test_paginated_listing_recipe(self):
        recipe = Path(RECIPE).with_name("fixture_list.json")
        with tempfile.TemporaryDirectory() as tmp, FixtureServer() as srv:
            db = os.path.join(tmp, "s.db")
            code, out = run("crawl", srv.url + "list/page1.html", "--recipe", recipe, "--db", db,
                            "--delay", "0", "--max-depth", "0")
            self.assertEqual(code, 0)
            self.assertIn("done=4", out)
            self.assertIn("items=8", out)
            _, out = run("export", "--db", db)
        rows = [json.loads(l) for l in out.splitlines()]
        self.assertEqual(rows[-1], {"name": "Item 4-b", "price": 1004.0, "listed": "2026-01-14",
                                    "_url": srv.url + "list/page4.html"})

    def test_bundled_recipes_load(self):
        for path in Path(RECIPE).parent.glob("*.json"):
            with self.subTest(recipe=path.name):
                self.assertTrue(Recipe.load(path).fields)

    def test_proxy(self):
        # .invalid never resolves, so these only succeed if they go through the proxy
        with tempfile.TemporaryDirectory() as tmp, ProxyServer() as proxy:
            _, out = run("extract", "http://scrapekit.invalid/a.html", "--proxy", proxy.url)
            self.assertEqual(json.loads(out)["main_text"], "http://scrapekit.invalid/a.html")
            db = os.path.join(tmp, "p.db")
            code, out = run("crawl", "http://scrapekit.invalid/", "--db", db, "--delay", "0",
                            "--max-depth", "0", "--proxy", proxy.url)
            self.assertEqual(code, 0)
            self.assertIn("done=1 failed=0", out)
            with sqlite3.connect(db) as conn:
                self.assertNotIn("proxy", conn.execute("SELECT config FROM runs").fetchone()[0])
            conn.close()
        self.assertEqual(proxy.log, ["http://scrapekit.invalid/robots.txt",
                                     "http://scrapekit.invalid/a.html",
                                     "http://scrapekit.invalid/robots.txt",
                                     "http://scrapekit.invalid/"])

    def test_extract(self):
        with FixtureServer() as srv:
            _, out = run("extract", srv.url + "products/1.html")
            self.assertEqual(json.loads(out)["json_ld"][0]["name"], "Red Widget")
            _, out = run("extract", srv.url + "products/1.html", "--recipe", RECIPE)
            self.assertEqual(json.loads(out)[0]["stock"], 5)
            with self.assertRaises(SystemExit):
                run("extract", srv.url + "private/secret.html")


if __name__ == "__main__":
    unittest.main()
