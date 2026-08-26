# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## 0.3.0 - 2026-09-25

### Added
- `scrapekit lint RECIPE...` validates recipes offline (unknown keys, selectors,
  regexes, process steps, `follow`, `paginate.max_pages`) and exits 1 on errors.
  `--url` also reports how many items each field matched on a live page.
- `--proxy URL` for `crawl` and `extract`. `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`
  still apply without it. The proxy is never saved in the run config.
- `crawl --domain-delay HOST=SECONDS` (repeatable) overrides `--delay` per host.
- `main_text` merges sibling blocks and loose paragraphs, Readability-style.
- A Dockerfile. CI builds and smoke-tests the image, lints the bundled recipes and
  runs on Python 3.14 too.

### Fixed
- Pagination hop counts are stored in the frontier, so `--resume` no longer resets
  each chain's `max_pages` cap.
- Redirect targets are checked against robots.txt before they're followed.
- Same-domain crawls skip pages that redirect to another host.
- Pages served as `text/html` without a charset are decoded using their
  `<meta charset>` or `http-equiv` declaration instead of always UTF-8.
- `Retry-After` given as an HTTP date is honored (only seconds were before).
- Selectors with a leading or trailing combinator (`div >`, `> p`) raise instead of
  matching as if `*` followed.
- `export --run N` and `diff --old/--new N` exit with "no run N" for unknown runs
  instead of reporting nothing.
- `diff --webhook` prints "webhook failed: ..." and exits 2 when the POST fails or
  returns an error status, instead of a traceback.
- `crawl URL --resume` is rejected instead of silently ignoring the URL.

### Changed
- Main text on pages with split content is longer than before, so those pages hash
  differently. The first `diff` across the upgrade may report them as changed.
- The `frontier` table gains a `hops` column. Existing databases are upgraded in place.

## 0.2.0 - 2026-09-25

### Added
- Selectors: adjacent (`+`) and general (`~`) sibling combinators, `:not(selector list)`,
  and `:nth-child(an+b | odd | even)`.
- `crawl --sitemap` queues URLs from `robots.txt` `Sitemap:` lines and `/sitemap.xml`,
  following sitemap indexes and reading gzipped sitemaps.
- Recipe `paginate` rules (`selector` or `{selector, max_pages}`). Next pages are queued
  at the same depth.
- Recipe field `process` steps: `strip`, `regex`, `number`, `price` and `date`.
- Conditional re-crawls. Stored `ETag`/`Last-Modified` values are sent back, and a `304`
  reuses the previous run's content, items and links. `--no-conditional` turns this off.
- Per-fetch status, bytes and response time. `crawl` prints a one-line summary, and the
  new `scrapekit stats` command (`--run`, `--json`) shows the full breakdown.
- Recipes `books_listing.json` and `fixture_list.json`. `books_toscrape.json` now uses
  `paginate` and `process`.
- Tests for `robots.txt` 5xx (disallow all), YAML recipes, sitemaps, pagination,
  conditional requests and stats. CI has a PyYAML job.

### Fixed
- Selector groups are tokenized rather than split on every comma, so
  `a[title="a, b"]` works. Empty groups, dangling combinators and unknown
  pseudo-classes now raise `ValueError`.

### Changed
- The SQLite schema has new columns on `pages` and `frontier`. Databases from 0.1.0
  are upgraded in place when opened.

## 0.1.0 - 2026-09-25

### Added
- Polite fetcher (robots.txt, Crawl-delay, per-domain rate limit, retries with backoff).
- Concurrent, resumable SQLite-backed crawler with depth, page and domain limits.
- Generic extractors and declarative JSON/YAML recipes on a stdlib CSS selector engine.
- Run-to-run change detection with stdout and webhook alerts.
- JSONL, CSV and SQLite exporters and the `crawl`, `extract`, `diff` and `export` commands.
