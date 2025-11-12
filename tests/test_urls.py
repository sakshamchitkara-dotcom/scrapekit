import unittest

from scrapekit.urls import normalize, same_domain


class TestNormalize(unittest.TestCase):
    def test_canonical_forms(self):
        cases = {
            "HTTP://Example.COM:80/a/./b/../c?b=2&a=1#frag": "http://example.com/a/c?a=1&b=2",
            "https://example.com": "https://example.com/",
            "https://example.com:8443/x": "https://example.com:8443/x",
            "https://example.com/a%7Eb": "https://example.com/a~b",
            "https://example.com/a b": "https://example.com/a%20b",
        }
        for raw, want in cases.items():
            self.assertEqual(normalize(raw), want, raw)

    def test_relative_and_rejects(self):
        self.assertEqual(normalize("../x.html", "http://h/a/b/c.html"), "http://h/a/x.html")
        self.assertIsNone(normalize("mailto:a@b.c"))
        self.assertIsNone(normalize("javascript:void(0)", "http://h/"))

    def test_same_domain(self):
        self.assertTrue(same_domain("http://h:8000/a", "http://h:8000/"))
        self.assertFalse(same_domain("http://other/a", "http://h/"))


if __name__ == "__main__":
    unittest.main()
