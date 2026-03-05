# scrapekit

A polite, resumable web crawler with declarative extraction recipes and change
detection. **Pure Python standard library** (3.10+), no required dependencies.

- **Polite crawling**: honors `robots.txt` (including `Crawl-delay`), rate-limits
  per domain, sends a configurable User-Agent, and retries 429/5xx/network errors
  with exponential backoff and jitter (honoring `Retry-After`).
- **Scoped and bounded**: same-domain by default, max depth, max pages, URL
  normalization (case, default ports, dot segments, fragments, query order,
  percent-encoding), deduplication, `rel="nofollow"` respected.
- **Concurrent and resumable**: thread pool fetches, and the frontier lives in SQLite
  and is committed after every page, so an interrupted crawl continues with `--resume`.
- **Extraction**: JSON/YAML recipes built on a small CSS selector engine that runs on
  `html.parser`, plus generic extractors for title, meta tags, links, JSON-LD and
  readability-style main text.
- **Change detection**: content hashes per page, unified diffs between runs, alerts to
  stdout or a webhook (Slack-compatible JSON).
- **Outputs**: JSONL, CSV, SQLite.

## Install

```bash
pip install -e .            # or: pip install -e '.[yaml]' for YAML recipes
scrapekit --help            # or: python -m scrapekit --help
```

## Usage

```bash
# Crawl with a recipe (data goes to scrapekit.db by default)
scrapekit -v crawl https://books.toscrape.com/ --recipe recipes/books_toscrape.json \
    --max-pages 20 --max-depth 3 --delay 1 --workers 4

# Resume the latest unfinished crawl (after Ctrl-C, a crash, etc.)
scrapekit crawl --resume

# One page: generic extraction (title/meta/links/json_ld/main_text) or a recipe
scrapekit extract https://example.com/
scrapekit extract https://books.toscrape.com/catalogue/sharp-objects_997/index.html \
    --recipe recipes/books_toscrape.json

# Changes between the latest run and the previous run of the same seed
scrapekit diff                        # human-readable report
scrapekit diff --json --exit-code     # machine-readable, exit 1 when changed (cron/CI)
scrapekit diff --webhook https://hooks.slack.com/services/...

# Export the latest run (or --run N); --pages exports page records instead of items
scrapekit export --format jsonl -o items.jsonl
scrapekit export --format csv   -o items.csv
scrapekit export --format sqlite -o items.db
```

Crawl options: `--max-depth` (2), `--max-pages` (50), `--workers` (4), `--delay`
seconds between requests per domain (1.0, raised to the site's `Crawl-delay` if that is
higher), `--retries` (3), `--user-agent`, `--all-domains`, `--db`.

## Recipes

```json
{
  "name": "books",
  "match": "/catalogue/[^/]+_\\d+/index\\.html$",
  "item": null,
  "fields": {
    "title": "div.product_main h1",
    "price": {"selector": "div.product_main p.price_color", "regex": "[\\d.]+", "type": "float"},
    "rating": {"selector": "p.star-rating::attr(class)", "regex": "star-rating (\\w+)"},
    "tags":   {"selector": "ul.tags li", "many": true},
    "sku":    {"selector": ".sku", "default": "n/a"}
  },
  "follow": ["article.product_pod h3 a", "li.next a"]
}
```

| key | meaning |
|---|---|
| `match` | regex on the URL. Only matching pages are extracted (all pages if omitted) |
| `item` | optional container selector. Each match becomes one record (otherwise one record per page) |
| `fields` | name to selector string, or `{selector, regex, type (str/int/float), many, default}` |
| `follow` | optional selectors whose `href`s are followed instead of every link on the page |

A selector ends in `::text` (the default) or `::attr(name)`. `href`/`src` attributes
come back as absolute URLs. A `regex` keeps group 1 if the pattern has one, otherwise
the whole match.

Supported CSS: `tag`, `*`, `#id`, `.class`, `[attr]`, `[attr=v]`, `[attr^=v]`,
`[attr$=v]`, `[attr*=v]`, `[attr~=v]`, `:first-child`, `:last-child`,
`:nth-child(n)`, descendant (` `), child (`>`), groups (`,`). Unsupported syntax
raises an error rather than silently matching nothing. Sibling combinators, `:not()`
and commas inside attribute values are not supported.

## Change detection

Every fetched page gets a SHA-256 of what you care about: the recipe's extracted items
when there are any, otherwise the page's main text. So ads and timestamps in the
page chrome don't trigger alerts. `scrapekit diff` compares two runs, reports
added, removed and changed URLs, and shows a unified diff of the content. Run
`crawl` then `diff --exit-code --webhook ...` on a schedule to monitor a site.

## Real output

Crawl of the bundled fixture site (`python -m http.server --directory tests/site`):

```
$ scrapekit -v crawl http://127.0.0.1:8799/ --recipe recipes/fixture.json --db fx.db --delay 0.2 --max-depth 3
01:04:18 200 http://127.0.0.1:8799/ items=0
01:04:18 skipped http://127.0.0.1:8799/private/secret.html (robots.txt)
01:04:18 200 http://127.0.0.1:8799/about.html items=0
01:04:18 200 http://127.0.0.1:8799/products/2.html items=1
01:04:19 200 http://127.0.0.1:8799/products/1.html items=1
01:04:19 200 http://127.0.0.1:8799/products/3.html items=1
01:04:19 200 http://127.0.0.1:8799/index.html?a=1&b=2 items=0
01:04:19 200 http://127.0.0.1:8799/index.html items=0
01:04:19 200 http://127.0.0.1:8799/products/deep/level1.html items=0
01:04:20 200 http://127.0.0.1:8799/products/deep/level2.html items=0
run 1: done=9 failed=0 skipped=1 unvisited=0 items=3 db=fx.db

$ scrapekit export --db fx.db --format csv
name,price,stock,tags,_url
Blue Gadget,7.25,0,"[""tag-a"", ""tag-2""]",http://127.0.0.1:8799/products/2.html
Red Widget,10.5,5,"[""tag-a"", ""tag-1""]",http://127.0.0.1:8799/products/1.html
Green Gizmo,3.0,12,"[""tag-a"", ""tag-3""]",http://127.0.0.1:8799/products/3.html

$ scrapekit diff --db fx.db      # after a second identical crawl
diff run 1 -> run 2: 0 added, 0 removed, 0 changed
```

Crawl of [books.toscrape.com](https://books.toscrape.com), a sandbox built for
scraping practice, capped at 20 pages with 1 request/second:

```
$ scrapekit -v crawl https://books.toscrape.com/ --recipe recipes/books_toscrape.json \
      --db books.db --max-pages 20 --max-depth 3 --delay 1 --workers 4
01:04:29 200 https://books.toscrape.com/ items=0
01:04:30 200 https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html items=1
01:04:31 200 https://books.toscrape.com/catalogue/tipping-the-velvet_999/index.html items=1
...
01:04:48 200 https://books.toscrape.com/catalogue/libertarianism-for-beginners_982/index.html items=1
run 1: done=20 failed=0 skipped=0 unvisited=2 items=19 db=books.db

$ scrapekit export --db books.db | head -2
{"title": "A Light in the Attic", "price": 51.77, "availability": 22, "rating": "Three", "upc": "a897fe39b1053632", "category": "Poetry", "image": "https://books.toscrape.com/media/cache/fe/72/fe72f0532301ec28892ae79a629a293c.jpg", "_url": "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"}
{"title": "Tipping the Velvet", "price": 53.74, "availability": 20, "rating": "One", "upc": "90fa61229261140a", "category": "Historical Fiction", "image": "https://books.toscrape.com/media/cache/08/e9/08e94f3731d7d6b760dfbfbc02ca5c62.jpg", "_url": "https://books.toscrape.com/catalogue/tipping-the-velvet_999/index.html"}
```

With 4 workers, the per-domain limit still held the crawl to one request per second
(20 pages in 20 s).

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

The suite is fully offline. `tests/server.py` serves the bundled fixture shop in
`tests/site/` (with a `robots.txt`, nested pages, JSON-LD, duplicate and external
links) on a random local port, plus a `/flaky` endpoint that returns 503 twice
(for the retry tests) and a `/mutable` endpoint (for the change-detection tests). CI
runs it on Python 3.10 to 3.13.

## Ethical scraping and Terms of Service

scrapekit is polite by default, but you are still responsible for how you use it.

- **Read the site's Terms of Service.** Many sites forbid automated access or reuse of
  their content. `robots.txt` isn't a license. Honoring it is the minimum.
- **Keep the load low.** Keep `--delay` at 1 s or more for sites you don't own, cap
  `--max-pages`, and crawl off-peak. There's intentionally no flag to ignore `robots.txt`
  from the CLI.
- **Identify yourself.** Set `--user-agent` to something that names you or your project
  and gives a way to contact you.
- **Respect privacy and copyright.** Don't collect personal data without a lawful basis
  (GDPR/CCPA), don't republish copyrighted content, and don't get around logins,
  paywalls or CAPTCHAs.
- **Prefer an API** when the site offers one.
- Practice on sites built for it, such as books.toscrape.com and quotes.toscrape.com.

## Design notes

- Worker threads only fetch and parse. The main thread owns the single SQLite
  connection, which avoids cross-thread locking and keeps every commit atomic per page.
- If `robots.txt` can't be fetched (network error or 5xx), all URLs on that origin are
  disallowed. A 404 allows everything, and 401/403 disallows everything, matching
  `urllib.robotparser`.
- The main-text heuristic picks the single best block and doesn't merge siblings the
  way full Readability does. That's good enough for hashing and previews.

## License

MIT
