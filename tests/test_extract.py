import unittest
from pathlib import Path

from scrapekit.extract import Recipe, generic

SITE = Path(__file__).parent / "site"
BASE = "http://shop.test/"


class TestGeneric(unittest.TestCase):
    def test_index(self):
        r = generic((SITE / "index.html").read_text(), BASE)
        self.assertEqual(r["title"], "Fixture Shop")
        self.assertEqual(r["meta"]["description"], "A tiny shop for scrapekit tests")
        self.assertEqual(r["meta"]["og:type"], "website")
        self.assertIn("http://shop.test/products/3.html", r["links"])  # fragment dropped
        self.assertEqual(sum("index.html?a=1&b=2" in u for u in r["links"]), 1)  # query dedupe
        self.assertFalse(any(u.startswith("mailto") for u in r["links"]))

    def test_json_ld(self):
        r = generic((SITE / "products/2.html").read_text(), BASE + "products/2.html")
        self.assertEqual(r["json_ld"][0]["offers"]["price"], "7.25")

    def test_main_text_skips_nav_and_footer(self):
        r = generic((SITE / "about.html").read_text(), BASE)
        self.assertTrue(r["main_text"].startswith("About us"))
        self.assertIn("polite crawlers", r["main_text"])
        self.assertNotIn("Copyright", r["main_text"])


class TestRecipe(unittest.TestCase):
    def test_item_list(self):
        rec = Recipe({
            "item": "li.product",
            "fields": {
                "name": "a::attr(title)",
                "url": "a::attr(href)",
                "price": {"selector": ".price::text", "regex": r"[\d.]+", "type": "float"},
            },
        })
        items = rec.extract((SITE / "index.html").read_text(), BASE)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0], {"name": "Red Widget", "url": BASE + "products/1.html",
                                    "price": 10.5, "_url": BASE})

    def test_page_record_many_default_match(self):
        rec = Recipe({
            "match": r"/products/\d+\.html$",
            "fields": {
                "name": "h1.name",
                "stock": {"selector": ".stock", "regex": r"\((\d+) available", "type": "int"},
                "tags": {"selector": "ul.tags li", "many": True},
                "sku": {"selector": ".sku", "default": "n/a"},
            },
            "follow": ["nav a"],
        })
        url = BASE + "products/3.html"
        self.assertTrue(rec.applies(url))
        self.assertFalse(rec.applies(BASE))
        [item] = rec.extract((SITE / "products/3.html").read_text(), url)
        self.assertEqual(item["stock"], 12)
        self.assertEqual(item["tags"], ["tag-a", "tag-3"])
        self.assertEqual(item["sku"], "n/a")

    def test_invalid(self):
        with self.assertRaises(ValueError):
            Recipe({"fields": {}})
        with self.assertRaises(ValueError):
            Recipe({"fields": {"x": {"selector": "a", "type": "decimal"}}})


if __name__ == "__main__":
    unittest.main()
