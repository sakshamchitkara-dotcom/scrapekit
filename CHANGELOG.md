# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

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
