"""scrapekit command-line interface."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import fields

from . import __version__
from . import __doc__ as _pkg_doc
from .crawler import CrawlConfig, Crawler
from .diff import diff_runs, format_report, has_changes, pick_runs, post_webhook
from .export import to_csv, to_jsonl, to_sqlite
from .extract import Recipe, generic
from .fetch import DEFAULT_UA, Fetcher, RobotsDisallowed
from .store import Store


def cmd_crawl(a) -> int:
    store = Store(a.db)
    if a.resume:
        row = store.unfinished_run()
        if row is None:
            sys.exit("nothing to resume")
        known = {f.name for f in fields(CrawlConfig)}
        cfg = CrawlConfig(**{k: v for k, v in json.loads(row["config"]).items() if k in known})
    else:
        if not a.url:
            sys.exit("crawl needs a URL (or --resume)")
        cfg = CrawlConfig(max_depth=a.max_depth, max_pages=a.max_pages, workers=a.workers,
                          same_domain=not a.all_domains, user_agent=a.user_agent,
                          delay=a.delay, retries=a.retries, recipe_path=a.recipe,
                          sitemap=a.sitemap, conditional=not a.no_conditional)
    recipe = Recipe.load(cfg.recipe_path) if cfg.recipe_path else None
    run = Crawler(store, cfg, recipe).run(a.url, resume=a.resume)
    st = store.stats(run)
    counts = st["states"]
    print(f"run {run}: done={counts.get('done', 0)} failed={counts.get('failed', 0)} "
          f"skipped={counts.get('skipped', 0)} unvisited={counts.get('queued', 0)} "
          f"items={st['items']} db={a.db}")
    print(stats_line(st))
    store.close()
    return 0


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def stats_line(st: dict) -> str:
    codes = " ".join(f"{k}={v}" for k, v in st["status_codes"].items()) or "none"
    t = st["timing_ms"]
    timing = f"p50 {t['p50']} ms, p95 {t['p95']} ms, max {t['max']} ms" if t["count"] else "no timings"
    return f"  http {codes} | {_size(st['bytes'])} | {timing} | {st['duration_s']} s"


def cmd_extract(a) -> int:
    f = Fetcher(user_agent=a.user_agent or DEFAULT_UA, delay=0)
    try:
        resp = f.fetch(a.url)
    except RobotsDisallowed:
        sys.exit(f"disallowed by robots.txt: {a.url}")
    if resp.status >= 400:
        sys.exit(f"HTTP {resp.status}: {a.url}")
    if a.recipe:
        out = Recipe.load(a.recipe).extract(resp.text, resp.final_url)
    else:
        out = generic(resp.text, resp.final_url)
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0


def cmd_diff(a) -> int:
    store = Store(a.db)
    old, new = pick_runs(store, a.old, a.new)
    d = diff_runs(store, old, new)
    store.close()
    print(json.dumps(d, indent=2) if a.json else format_report(d))
    if a.webhook and has_changes(d):
        print(f"webhook -> HTTP {post_webhook(a.webhook, d)}", file=sys.stderr)
    return 1 if a.exit_code and has_changes(d) else 0


def cmd_export(a) -> int:
    store = Store(a.db)
    runs = store.runs()
    if not a.run and not runs:
        sys.exit("no finished runs to export")
    run = a.run or runs[-1]["id"]
    if a.pages:
        rows = [dict(r) for r in store.db.execute(
            "SELECT url, status, fetched_at, title, hash FROM pages WHERE run_id=? ORDER BY url", (run,))]
    else:
        rows = store.items(run)
    store.close()
    if a.format == "sqlite":
        if not a.output:
            sys.exit("--format sqlite needs -o PATH")
        to_sqlite(rows, a.output, "pages" if a.pages else "items")
    else:
        fh = open(a.output, "w", newline="", encoding="utf-8") if a.output else sys.stdout
        (to_csv if a.format == "csv" else to_jsonl)(rows, fh)
        if a.output:
            fh.close()
    print(f"exported {len(rows)} rows from run {run}", file=sys.stderr)
    return 0


def cmd_stats(a) -> int:
    store = Store(a.db)
    runs = store.runs(finished_only=False)
    if not runs:
        sys.exit("no runs")
    st = store.stats(a.run or runs[-1]["id"])
    store.close()
    if a.json:
        print(json.dumps(st, indent=2))
        return 0
    s = st["states"]
    print(f"run {st['run']}: {st['seed']}")
    print(f"  pages   done={s.get('done', 0)} failed={s.get('failed', 0)} "
          f"skipped={s.get('skipped', 0)} unvisited={s.get('queued', 0) + s.get('inflight', 0)} "
          f"items={st['items']}")
    print(stats_line(st))
    for r in st["slowest"]:
        print(f"  slow    {r['ms']:>8} ms  {r['status']}  {r['url']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scrapekit", description=_pkg_doc)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true", help="log each fetched URL")
    sub = p.add_subparsers(dest="cmd", required=True)

    def db(sp):
        sp.add_argument("--db", default="scrapekit.db", help="sqlite database (default: %(default)s)")

    c = sub.add_parser("crawl", help="crawl a site from a seed URL")
    c.add_argument("url", nargs="?")
    db(c)
    c.add_argument("--recipe", help="JSON/YAML extraction recipe")
    c.add_argument("--max-depth", type=int, default=2)
    c.add_argument("--max-pages", type=int, default=50)
    c.add_argument("--workers", type=int, default=4)
    c.add_argument("--delay", type=float, default=1.0, help="min seconds between requests per domain")
    c.add_argument("--retries", type=int, default=3)
    c.add_argument("--user-agent")
    c.add_argument("--all-domains", action="store_true", help="follow links off the seed's domain")
    c.add_argument("--sitemap", action="store_true",
                   help="also seed from robots.txt Sitemap: entries and /sitemap.xml")
    c.add_argument("--no-conditional", action="store_true",
                   help="always refetch in full instead of revalidating with ETag/Last-Modified")
    c.add_argument("--resume", action="store_true", help="resume the latest unfinished run")
    c.set_defaults(fn=cmd_crawl)

    e = sub.add_parser("extract", help="fetch one URL and print extracted JSON")
    e.add_argument("url")
    e.add_argument("--recipe")
    e.add_argument("--user-agent")
    e.set_defaults(fn=cmd_extract)

    d = sub.add_parser("diff", help="show changes between two runs")
    db(d)
    d.add_argument("--old", type=int)
    d.add_argument("--new", type=int)
    d.add_argument("--json", action="store_true")
    d.add_argument("--webhook", help="POST changes as JSON to this URL")
    d.add_argument("--exit-code", action="store_true", help="exit 1 when changes are found")
    d.set_defaults(fn=cmd_diff)

    t = sub.add_parser("stats", help="status codes, bytes and timings of a run")
    db(t)
    t.add_argument("--run", type=int, help="run id (default: latest)")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_stats)

    x = sub.add_parser("export", help="export items (or pages) of a run")
    db(x)
    x.add_argument("--run", type=int, help="run id (default: latest finished)")
    x.add_argument("--format", choices=["jsonl", "csv", "sqlite"], default="jsonl")
    x.add_argument("-o", "--output")
    x.add_argument("--pages", action="store_true", help="export page records instead of items")
    x.set_defaults(fn=cmd_export)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
