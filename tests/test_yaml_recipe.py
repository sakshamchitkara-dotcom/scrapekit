import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scrapekit.extract import Recipe

ROOT = Path(__file__).parent.parent
SITE = ROOT / "tests" / "site"

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

YAML_RECIPE = r"""
name: fixture-products
match: /products/\d+\.html$
fields:
  name: h1.name
  price: {selector: .price, regex: '[\d.]+', type: float}
  stock: {selector: .stock, regex: '\((\d+) available', type: int}
  tags: {selector: ul.tags li, many: true}
"""


class TestYamlRecipe(unittest.TestCase):
    def write(self, tmp, name="r.yaml"):
        path = Path(tmp) / name
        path.write_text(YAML_RECIPE)
        return path

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
    def test_yaml_matches_json_recipe(self):
        url = "http://shop.test/products/3.html"
        html = (SITE / "products/3.html").read_text()
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("r.yaml", "r.yml"):
                got = Recipe.load(self.write(tmp, name)).extract(html, url)
                want = Recipe.load(ROOT / "recipes" / "fixture.json").extract(html, url)
                self.assertEqual(got, want)
                self.assertEqual(got[0]["stock"], 12)

    def test_missing_pyyaml_explains_install(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {"yaml": None}):
            with self.assertRaises(SystemExit) as cm:
                Recipe.load(self.write(tmp))
        self.assertIn("scrapekit[yaml]", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
