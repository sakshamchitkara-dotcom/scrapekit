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
            for argv in (("stats", "--run", 99), ("export", "--run", 99), ("diff", "--old", 99),
                         ("diff", "--old", 1, "--new", 99)):
                with self.subTest(argv=argv), self.assertRaisesRegex(SystemExit, "no run 99"):
                    run(*argv, "--db", db)

    def test_diff_webhook_failure_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp, FixtureServer() as srv:
            db = os.path.join(tmp, "w.db")
            for price in ("10", "12"):
                srv.mutable = f"<html><body><p>price {price}</p></body></html>"
                run("crawl", srv.url + "mutable", "--db", db, "--delay", "0", "--max-depth", "0")
            code, out = run("diff", "--db", db, "--webhook", srv.url + "no-such-hook")  # 501 to POST
            self.assertEqual(code, 2)
            self.assertIn("1 changed", out)  # the report still prints
            self.assertEqual(run("diff", "--db", db, "--webhook", "http://127.0.0.1:9/hook")[0], 2)

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

    def test_domain_delay_option(self):
        from scrapekit.cli import build_parser
        a = build_parser().parse_args(["crawl", "http://x/", "--domain-delay", "Slow.example=2.5",
                                       "--domain-delay", "127.0.0.1:8000=0"])
        self.assertEqual(a.domain_delay, [("slow.example", 2.5), ("127.0.0.1:8000", 0.0)])
        for bad in ("nohost", "=1", "x=-1", "x=soon"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                build_parser().parse_args(["crawl", "--domain-delay", bad])

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

    def test_lint(self):
        code, out = run("lint", *sorted(Path(RECIPE).parent.glob("*.json")))
        self.assertEqual(code, 0)
        self.assertEqual(out.count(": ok ("), 4)
        bad = {"name": "bad", "folow": ["a"], "item": "li >", "paginate": {"selector": "a", "max_pages": 0},
               "fields": {"t": {"selecter": "h1", "selector": "h1"}}}
        with tempfile.TemporaryDirectory() as tmp:
            def lint(spec):
                path = os.path.join(tmp, "r.json")
                Path(path).write_text(json.dumps(spec))
                return run("lint", path)
            code, out = lint(bad)
            self.assertEqual(code, 1)
            self.assertIn("warning: unknown key 'folow'", out)
            self.assertIn("warning: fields.t: unknown key 'selecter'", out)
            self.assertIn("error: paginate.max_pages must be a positive integer, got 0", out)
            del bad["paginate"]
            self.assertIn("error: item: empty selector or dangling combinator in 'li >'", lint(bad)[1])
            for spec, msg in (({**bad, "item": None, "follow": "a"}, "follow must be a list"),
                              ({"fields": {"x": {"selector": "p", "regex": "("}}}, "error: "),
                              ({"fields": {"x": {"selector": "p", "process": ["prize"]}}},
                               "unknown process step 'prize'"),
                              ([], "recipe must be an object")):
                with self.subTest(msg=msg):
                    code, out = lint(spec)
                    self.assertEqual(code, 1)
                    self.assertIn(msg, out)

    def test_lint_against_url(self):
        with FixtureServer() as srv:
            code, out = run("lint", RECIPE, "--url", srv.url + "products/1.html")
            self.assertEqual(code, 0)
            self.assertIn("HTTP 200, 1 items", out)
            self.assertRegex(out, r"name +1/1\n")
            _, out = run("lint", RECIPE, "--url", srv.url + "about.html")
            self.assertIn("does not match the recipe's `match`", out)
            code, out = run("lint", RECIPE, "--url", srv.url + "private/secret.html")
            self.assertEqual(code, 1)
            self.assertIn("disallowed by robots.txt", out)

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
