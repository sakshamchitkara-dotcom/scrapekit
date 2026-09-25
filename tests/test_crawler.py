import os
import sqlite3
import tempfile
import unittest

from scrapekit.crawler import CrawlConfig, Crawler, discover_sitemap
from scrapekit.fetch import Fetcher
from scrapekit.diff import diff_runs
from scrapekit.extract import Recipe
from scrapekit.store import Store
from tests.server import FixtureServer

RECIPE = {
    "name": "products",
    "match": r"/products/\d+\.html$",
    "fields": {"name": "h1.name", "price": {"selector": ".price", "regex": r"[\d.]+", "type": "float"}},
}


class CrawlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "c.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def crawl(self, srv, recipe=None, **kw):
        cfg = CrawlConfig(delay=0, retries=0, **kw)
        return Crawler(self.store, cfg, Recipe(recipe) if recipe else None).run(srv.url)

    def test_full_crawl_polite_and_scoped(self):
        with FixtureServer() as srv:
            run = self.crawl(srv, RECIPE, max_depth=3)
            fetched = [p for p in srv.paths() if p != "/robots.txt"]
        self.assertNotIn("/private/secret.html", fetched)  # robots.txt
        self.assertEqual(len(fetched), len(set(fetched)), "each URL fetched once")
        self.assertIn("/products/deep/level2.html", fetched)
        self.assertNotIn("/products/deep/level3.html", fetched)  # depth 4 > max_depth
        self.assertEqual(sum(p.startswith("/index.html?") for p in fetched), 1)  # query-order dedupe
        items = sorted(self.store.items(run), key=lambda i: i["name"])
        self.assertEqual([(i["name"], i["price"]) for i in items],
                         [("Blue Gadget", 7.25), ("Green Gizmo", 3.0), ("Red Widget", 10.5)])
        states = dict(self.store.db.execute(
            "SELECT url, state FROM frontier WHERE run_id=?", (run,)).fetchall())
        self.assertEqual(states[srv.url + "private/secret.html"], "skipped")

    def test_sitemap_seeds(self):
        with FixtureServer() as srv:
            urls = discover_sitemap(Fetcher(delay=0, retries=0), srv.url, 50)
            self.assertEqual(urls, [srv.url + "about.html", srv.url + "products/deep/level3.html",
                                    "https://example.org/off-site.html", srv.url + "orphan.html"])
            self.assertIn("/missing-sitemap.xml", srv.paths())  # 404 tolerated
            run = self.crawl(srv, max_depth=0, sitemap=True)
        done = {u for u, in self.store.db.execute(
            "SELECT url FROM frontier WHERE run_id=? AND state='done'", (run,))}
        self.assertEqual(done, {srv.url, srv.url + "orphan.html", srv.url + "about.html",
                                srv.url + "products/deep/level3.html"})  # off-site dropped

    def test_pagination_keeps_depth_and_honors_limit(self):
        spec = {"item": "li.item", "follow": [],
                "fields": {"name": ".name", "price": {"selector": ".price", "process": ["price"]}}}
        for limit, want_pages in ((2, 3), (None, 4)):
            with self.subTest(limit=limit), FixtureServer() as srv:
                rec = Recipe({**spec, "paginate": {"selector": "a.next", "max_pages": limit}})
                cfg = CrawlConfig(delay=0, retries=0, max_depth=0)
                run = Crawler(self.store, cfg, rec).run(srv.url + "list/page1.html")
                fetched = [p for p in srv.paths() if p != "/robots.txt"]
            self.assertEqual(fetched, [f"/list/page{i}.html" for i in range(1, want_pages + 1)])
            items = self.store.items(run)
            self.assertEqual(len(items), 2 * want_pages)
            self.assertEqual(items[1]["price"], 1001.0)  # "€1.001,00"

    def test_recrawl_revalidates_and_reuses_unchanged_pages(self):
        def statuses(run):
            return sorted(r[0] for r in self.store.db.execute(
                "SELECT status FROM pages WHERE run_id=?", (run,)))
        with FixtureServer() as srv:
            first = self.crawl(srv, RECIPE, max_depth=3)
            second = self.crawl(srv, RECIPE, max_depth=3)
            self.assertEqual(set(statuses(second)), {304})
            self.assertEqual(statuses(first), [200] * len(statuses(second)))  # same pages found
            by_name = sorted(self.store.items(first), key=lambda i: i["name"])
            self.assertEqual(sorted(self.store.items(second), key=lambda i: i["name"]), by_name)
            self.assertEqual(diff_runs(self.store, first, second)["changed"], [])
            # a different recipe can't reuse cached items, so it refetches in full
            third = self.crawl(srv, {**RECIPE, "name": "other"}, max_depth=3)
            self.assertEqual(set(statuses(third)), {200})
            # opting out
            fourth = self.crawl(srv, {**RECIPE, "name": "other"}, max_depth=3, conditional=False)
            self.assertEqual(set(statuses(fourth)), {200})

    def test_etag_change_is_refetched(self):
        with FixtureServer() as srv:
            seed = srv.url + "mutable"
            cfg = CrawlConfig(delay=0, retries=0, max_depth=0)
            runs = [Crawler(self.store, cfg).run(seed) for _ in range(2)]
            srv.mutable = "<html><title>v2</title><body><p>price 12</p></body></html>"
            runs.append(Crawler(self.store, cfg).run(seed))
        got = [self.store.db.execute("SELECT status, title FROM pages WHERE run_id=?", (r,)).fetchone()
               for r in runs]
        self.assertEqual([tuple(g) for g in got], [(200, "v1"), (304, "v1"), (200, "v2")])
        self.assertEqual(len(diff_runs(self.store, runs[1], runs[2])["changed"]), 1)

    def test_old_database_is_migrated(self):
        path = os.path.join(self.tmp.name, "old.db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE pages (run_id INTEGER, url TEXT, status INTEGER, fetched_at REAL,"
                   " title TEXT, hash TEXT, content TEXT, PRIMARY KEY (run_id, url))")
        db.execute("INSERT INTO pages VALUES (1, 'http://a/', 200, 0, 't', 'h', 'c')")
        db.commit()
        db.close()
        st = Store(path)
        cols = {r[1] for r in st.db.execute("PRAGMA table_info(pages)")}
        self.assertTrue({"etag", "last_modified", "links", "recipe_fp"} <= cols)
        self.assertIsNone(st.previous_page("http://a/", 2, ""))  # no validators: full fetch
        st.close()

    def test_max_pages(self):
        with FixtureServer() as srv:
            run = self.crawl(srv, max_pages=3, workers=2)
        self.assertEqual(self.store.count(run, "done", "failed"), 3)

    def test_resume_after_interrupt(self):
        with FixtureServer() as srv:
            cfg = CrawlConfig(delay=0, retries=0, max_depth=3, workers=1)
            c = Crawler(self.store, cfg)
            orig, n = c._record, [0]

            def boom(*a):
                orig(*a)
                n[0] += 1
                if n[0] == 3:
                    raise KeyboardInterrupt
            c._record = boom
            with self.assertRaises(KeyboardInterrupt):
                c.run(srv.url)
            self.assertIsNotNone(self.store.unfinished_run())
            before = len(srv.paths())
            run = Crawler(self.store, cfg).run(resume=True)
            refetched = srv.paths()[before:]
        self.assertIsNone(self.store.unfinished_run())
        self.assertNotIn("/", refetched)  # seed not fetched again
        self.assertEqual(self.store.count(run, "done"), len(self.store.pages(run)))
        self.assertGreater(self.store.count(run, "done"), 3)


if __name__ == "__main__":
    unittest.main()
