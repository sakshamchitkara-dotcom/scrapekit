# scrapekit

A polite, resumable web crawler with declarative extraction recipes and change
detection. **Pure Python standard library** (3.10+), no required dependencies.

- **Polite crawling**: honors `robots.txt` (including `Crawl-delay`), rate-limits
  per domain, sends a configurable User-Agent, and retries 429/5xx/network errors
  with exponential backoff and jitter (honoring `Retry-After`).
- **Scoped and bounded**: same-domain by default, max depth, max pages, URL
  normalization (case, default ports, dot segments, fragments, query order,
  percent-encoding), deduplication, `rel="nofollow"` respected.
- **Sitemap seeding**: `--sitemap` also queues URLs from `robots.txt` `Sitemap:` lines
  and `/sitemap.xml` (sitemap indexes and gzip included).
- **Concurrent and resumable**: thread pool fetches, and the frontier lives in SQLite
  and is committed after every page, so an interrupted crawl continues with `--resume`.
- **Cheap re-crawls**: pages are revalidated with `If-None-Match` / `If-Modified-Since`.
  A `304` reuses the previous run's content, items and links instead of downloading
  the page again.
- **Extraction**: JSON/YAML recipes built on a small CSS selector engine that runs on
  `html.parser`, with pagination rules and per-field post-processing (strip, regex,
  number, price, date), plus generic extractors for title, meta tags, links, JSON-LD
  and readability-style main text.
- **Crawl stats**: status codes, bytes and response-time percentiles per run
  (`scrapekit stats`).
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

# Every book on the first 20 catalogue pages: follow only "next", 20 records per page
scrapekit -v crawl https://books.toscrape.com/ --recipe recipes/books_listing.json \
    --max-pages 20 --max-depth 0 --delay 1

# Also seed from the site's sitemaps
scrapekit crawl https://example.com/ --sitemap --max-pages 100

# Resume the latest unfinished crawl (after Ctrl-C, a crash, etc.)
scrapekit crawl --resume

# Status codes, bytes and timings of the latest run (or --run N, --json)
scrapekit stats

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
higher), `--retries` (3), `--user-agent`, `--all-domains`, `--sitemap`,
`--no-conditional` (always download in full), `--db`.

## Recipes

```json
{
  "name": "books",
  "match": "/catalogue/[^/]+_\\d+/index\\.html$",
  "item": null,
  "fields": {
    "title": "div.product_main h1",
    "price": {"selector": "div.product_main p.price_color", "process": ["price"]},
    "stock": {"selector": "p.availability", "process": [{"regex": "\\((\\d+) available"}, "number"]},
    "rating": {"selector": "p.star-rating::attr(class)", "regex": "star-rating (\\w+)"},
    "tags":   {"selector": "ul.tags li", "many": true},
    "sku":    {"selector": ".sku", "default": "n/a"}
  },
  "follow": ["article.product_pod h3 a"],
  "paginate": {"selector": "li.next a", "max_pages": 49}
}
```

| key | meaning |
|---|---|
| `match` | regex on the URL. Only matching pages are extracted (all pages if omitted) |
| `item` | optional container selector. Each match becomes one record (otherwise one record per page) |
| `fields` | name to selector string, or `{selector, regex, process, type (str/int/float), many, default}` |
| `follow` | optional selectors whose `href`s are followed instead of every link on the page (`[]` follows nothing) |
| `paginate` | optional next-page selector, or `{selector, max_pages}`. Next pages are queued at the *same* depth, so paging through a listing never hits `--max-depth`. `max_pages` caps hops per chain |

A selector ends in `::text` (the default) or `::attr(name)`. `href`/`src` attributes
come back as absolute URLs. A `regex` keeps group 1 if the pattern has one, otherwise
the whole match.

`process` is a list of steps applied in order (after `regex`, before `type`). A step
that can't parse its input drops the value, the same as a regex that doesn't match.

| step | effect |
|---|---|
| `"strip"` / `{"strip": "£$ "}` | strip whitespace / the given characters |
| `{"regex": "..."}` | keep group 1 (or the whole match) |
| `"number"` | first number, English style: `"1,234.5 pts"` to `1234.5`, `"22 available"` to `22` |
| `"price"` | amount with either decimal mark: `"£51.77"` to `51.77`, `"1.234,56 €"` to `1234.56` |
| `"date"` / `{"date": "%d/%m/%Y"}` | ISO 8601 date. Without a format it tries ISO 8601, RFC 2822 and month-name formats, and rejects ambiguous `01/02/2026` |

Supported CSS: `tag`, `*`, `#id`, `.class`, `[attr]`, `[attr=v]`, `[attr^=v]`,
`[attr$=v]`, `[attr*=v]`, `[attr~=v]`, `:first-child`, `:last-child`,
`:nth-child(an+b | odd | even)`, `:not(selector list)`, descendant (` `), child
(`>`), adjacent sibling (`+`), general sibling (`~`), and groups (`,`, commas inside
quoted attribute values are fine). Unsupported syntax raises an error rather than
silently matching nothing.

## Change detection

Every fetched page gets a SHA-256 of what you care about: the recipe's extracted items
when there are any, otherwise the page's main text. So ads and timestamps in the
page chrome don't trigger alerts. `scrapekit diff` compares two runs, reports
added, removed and changed URLs, and shows a unified diff of the content. Run
`crawl` then `diff --exit-code --webhook ...` on a schedule to monitor a site.

Re-crawls are cheap. Each page's `ETag` and `Last-Modified` are stored, and the next
crawl of the same URL sends them back. When the server answers `304 Not Modified`,
scrapekit copies the previous run's content hash, items and outgoing links, so the
diff and link discovery come out the same as after a full download. A cached copy is
only reused if it was made with the same recipe (by content fingerprint), so editing
a recipe forces full downloads.

## Real output

Crawl of the bundled fixture site (`python -m http.server 8799 --directory tests/site`),
seeded from its sitemaps, then crawled again:

```
$ scrapekit -v crawl http://127.0.0.1:8799/ --recipe recipes/fixture.json --db fx.db \
      --delay 0.2 --max-depth 1 --sitemap
01:27:48 sitemap http://127.0.0.1:8799/sitemap_index.xml: 2 locs
01:27:48 sitemap http://127.0.0.1:8799/sitemap.xml: 3 locs
01:27:49 sitemap http://127.0.0.1:8799/sitemap-extra.xml: 1 locs
01:27:49 200 http://127.0.0.1:8799/ items=0
01:27:49 skipped http://127.0.0.1:8799/private/secret.html (robots.txt)
01:27:49 200 http://127.0.0.1:8799/about.html items=0
01:27:49 200 http://127.0.0.1:8799/products/deep/level3.html items=0
01:27:50 200 http://127.0.0.1:8799/orphan.html items=0
01:27:50 200 http://127.0.0.1:8799/products/1.html items=1
01:27:50 200 http://127.0.0.1:8799/products/2.html items=1
01:27:50 200 http://127.0.0.1:8799/products/3.html items=1
01:27:50 200 http://127.0.0.1:8799/index.html?a=1&b=2 items=0
run 1: done=8 failed=0 skipped=1 unvisited=0 items=3 db=fx.db
  http 200=8 | 4.6 KB | p50 0.7 ms, p95 0.7 ms, max 2.5 ms | 2.22 s

$ scrapekit -v crawl ...same command...
...
01:27:51 304 http://127.0.0.1:8799/ items=0
01:27:52 304 http://127.0.0.1:8799/products/1.html items=1
...
run 2: done=8 failed=0 skipped=1 unvisited=0 items=3 db=fx.db
  http 304=8 | 0 B | p50 0.8 ms, p95 1.1 ms, max 1.9 ms | 2.22 s

$ scrapekit diff --db fx.db
diff run 1 -> run 2: 0 added, 0 removed, 0 changed
```

`orphan.html` isn't linked from any page, and `level3.html` is past `--max-depth 1`.
Both came from the sitemaps.

Pagination and post-processing on the fixture's four-page listing, with `--max-depth 0`:

```
$ scrapekit -v crawl http://127.0.0.1:8799/list/page1.html --recipe recipes/fixture_list.json \
      --db list.db --delay 0.2 --max-depth 0
01:27:33 200 http://127.0.0.1:8799/list/page1.html items=2
01:27:33 200 http://127.0.0.1:8799/list/page2.html items=2
01:27:33 200 http://127.0.0.1:8799/list/page3.html items=2
01:27:34 200 http://127.0.0.1:8799/list/page4.html items=2
run 1: done=4 failed=0 skipped=0 unvisited=0 items=8 db=list.db
  http 200=4 | 1.7 KB | p50 0.7 ms, p95 1.3 ms, max 2.1 ms | 0.62 s

$ scrapekit export --db list.db --format csv | head -3
name,price,listed,_url
Item 1-a,1.5,2026-01-01,http://127.0.0.1:8799/list/page1.html
Item 1-b,1001.0,2026-01-11,http://127.0.0.1:8799/list/page1.html
```

(The source text is `€1,50`, `€1.001,00` and `11 Jan 2026`.)

[books.toscrape.com](https://books.toscrape.com) is a sandbox built for scraping
practice. This crawl pages through the catalogue with `books_listing.json`, capped at
20 pages at 1 request/second. It collects 400 books in 20 requests:

```
$ scrapekit -v crawl https://books.toscrape.com/ --recipe recipes/books_listing.json \
      --db books.db --max-pages 20 --max-depth 0 --delay 1 --workers 2
01:28:04 200 https://books.toscrape.com/ items=20
01:28:05 200 https://books.toscrape.com/catalogue/page-2.html items=20
01:28:06 200 https://books.toscrape.com/catalogue/page-3.html items=20
...
01:28:23 200 https://books.toscrape.com/catalogue/page-20.html items=20
run 1: done=20 failed=0 skipped=0 unvisited=0 items=400 db=books.db
  http 200=20 | 998.7 KB | p50 674.0 ms, p95 852.2 ms, max 899.9 ms | 20.25 s

$ scrapekit export --db books.db | head -1
{"title": "A Light in the Attic", "url": "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html", "price": 51.77, "rating": "Three", "availability": "In stock", "_url": "https://books.toscrape.com/"}

$ scrapekit stats --db books.db
run 1: https://books.toscrape.com/
  pages   done=20 failed=0 skipped=0 unvisited=0 items=400
  http 200=20 | 998.7 KB | p50 674.0 ms, p95 852.2 ms, max 899.9 ms | 20.25 s
  slow       899.9 ms  200  https://books.toscrape.com/
  slow       852.2 ms  200  https://books.toscrape.com/catalogue/page-3.html
  slow       805.6 ms  200  https://books.toscrape.com/catalogue/page-13.html
  slow       764.2 ms  200  https://books.toscrape.com/catalogue/page-12.html
  slow       756.7 ms  200  https://books.toscrape.com/catalogue/page-10.html

$ scrapekit -v crawl https://books.toscrape.com/ --recipe recipes/books_listing.json \
      --db books.db --max-pages 5 --max-depth 0 --delay 1 --workers 2      # re-crawl
01:28:31 304 https://books.toscrape.com/ items=20
01:28:32 304 https://books.toscrape.com/catalogue/page-2.html items=20
...
01:28:35 304 https://books.toscrape.com/catalogue/page-5.html items=20
run 2: done=5 failed=0 skipped=0 unvisited=1 items=100 db=books.db
  http 304=5 | 0 B | p50 385.2 ms, p95 427.3 ms, max 429.0 ms | 4.73 s
```

The detail-page recipe (`books_toscrape.json`) follows book links and paginates, with
price and stock parsed by `process` steps:

```
$ scrapekit -v crawl https://books.toscrape.com/ --recipe recipes/books_toscrape.json \
      --db detail.db --max-pages 10 --max-depth 1 --delay 1 --workers 2
01:28:36 200 https://books.toscrape.com/ items=0
01:28:37 200 https://books.toscrape.com/catalogue/page-2.html items=0
01:28:38 200 https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html items=1
...
run 1: done=10 failed=0 skipped=0 unvisited=33 items=8 db=detail.db
  http 200=10 | 221.0 KB | p50 526.4 ms, p95 589.3 ms, max 595.9 ms | 9.79 s

$ scrapekit export --db detail.db | head -1
{"title": "A Light in the Attic", "price": 51.77, "availability": 22, "rating": "Three", "upc": "a897fe39b1053632", "category": "Poetry", "image": "https://books.toscrape.com/media/cache/fe/72/fe72f0532301ec28892ae79a629a293c.jpg", "_url": "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"}
```

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

The suite is fully offline. `tests/server.py` serves the bundled fixture shop in
`tests/site/` (with a `robots.txt`, sitemaps, a paginated listing, nested pages,
JSON-LD, duplicate and external links) on a random local port. It also serves a
`/flaky` endpoint that returns 503 twice (for the retry tests), a `/mutable` endpoint
with an ETag (for change detection and conditional requests), and can make
`/robots.txt` return any error status. CI runs it on Python 3.10 to 3.13, plus a job
with PyYAML installed so the YAML recipe tests run instead of being skipped.

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
- Response times in `stats` cover the final attempt of each request only. Rate-limit
  waits and retry backoff aren't included. `robots.txt` and sitemap fetches are not
  counted as pages.
- Pagination hop counts are kept in memory, so `--resume` starts them again from zero.
- Databases from 0.1.0 are upgraded in place on open (new columns only). Their old
  runs have no validators or timings, so the first re-crawl downloads everything.

## License

MIT
