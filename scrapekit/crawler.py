"""Resumable, concurrent, same-domain crawler."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field

from .dom import parse
from .extract import Recipe, links, main_text, title
from .fetch import Fetcher, RobotsDisallowed
from .store import Store
from .urls import normalize, same_domain

log = logging.getLogger("scrapekit")


@dataclass
class CrawlConfig:
    max_depth: int = 2
    max_pages: int = 50
    workers: int = 4
    same_domain: bool = True
    user_agent: str | None = None
    delay: float = 1.0
    retries: int = 3
    respect_robots: bool = True
    recipe_path: str | None = None
    sitemap: bool = False
    conditional: bool = True
    domain_delays: dict[str, float] = field(default_factory=dict)  # host -> seconds


def content_hash(items: list[dict], text: str) -> tuple[str, str]:
    """Hash what we care about: recipe items if any, else the page's main text."""
    content = json.dumps(items, sort_keys=True, ensure_ascii=False, indent=1) if items else text
    return hashlib.sha256(content.encode()).hexdigest(), content


def discover_sitemap(fetcher: Fetcher, seed: str, limit: int, max_sitemaps: int = 20) -> list[str]:
    """Up to `limit` page URLs from the seed origin's sitemaps.

    Follows sitemap indexes (at most `max_sitemaps` files), accepts gzipped
    sitemaps, and skips any sitemap that is missing, disallowed or malformed.
    """
    todo, seen, urls = fetcher.sitemap_locations(seed), set(), {}
    while todo and len(seen) < max_sitemaps and len(urls) < limit:
        sm = todo.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            resp = fetcher.fetch(sm)
            body = resp.body
            if body[:2] == b"\x1f\x8b":
                body = gzip.GzipFile(fileobj=io.BytesIO(body)).read(50_000_000)
            root = ET.fromstring(body)
        except (RobotsDisallowed, urllib.error.URLError, OSError, EOFError, ET.ParseError):
            continue
        if resp.status != 200:
            continue
        locs = [normalize(e.text.strip(), sm) for e in root.iter()
                if e.tag.rsplit("}", 1)[-1] == "loc" and e.text]
        if root.tag.rsplit("}", 1)[-1] == "sitemapindex":
            todo += [u for u in locs if u]
        else:
            urls.update(dict.fromkeys(u for u in locs if u))
        log.info("sitemap %s: %d locs", sm, len(locs))
    return list(urls)[:limit]


class Crawler:
    def __init__(self, store: Store, cfg: CrawlConfig, recipe: Recipe | None = None,
                 fetcher: Fetcher | None = None, proxy: str | None = None):
        self.store = store
        self.cfg = cfg
        self.recipe = recipe
        # proxy is not part of CrawlConfig, so credentials in it never reach the database
        kw = {"delay": cfg.delay, "retries": cfg.retries, "respect_robots": cfg.respect_robots,
              "proxy": proxy, "domain_delays": cfg.domain_delays}
        if cfg.user_agent:
            kw["user_agent"] = cfg.user_agent
        self.fetcher = fetcher or Fetcher(**kw)
        self.recipe_fp = recipe.fingerprint if recipe else ""

    # ------------------------------------------------------------ worker
    def _process(self, url: str, validators: tuple = ()) -> dict:
        """Runs in a worker thread. No DB access here."""
        try:
            resp = self.fetcher.fetch(url, *validators)
        except RobotsDisallowed as e:
            return {"state": "skipped", "note": e.reason}
        except (urllib.error.URLError, OSError) as e:
            return {"state": "failed", "note": str(e)}
        cache = {"etag": resp.headers.get("etag"), "last_modified": resp.headers.get("last-modified")}
        metrics = {"status": resp.status, "bytes": len(resp.body), "elapsed_ms": resp.elapsed * 1000}
        if self.cfg.same_domain and not same_domain(resp.final_url, url):
            return {"state": "skipped", "note": f"redirected off-site to {resp.final_url}", **metrics}
        if resp.status == 304 and validators:
            return {"state": "done", "not_modified": True, **cache, **metrics}
        if resp.status >= 400:
            return {"state": "failed", "note": f"HTTP {resp.status}", **metrics}
        if not resp.is_html:
            return {"state": "skipped", "note": resp.content_type, **metrics}
        final = normalize(resp.final_url) or url
        doc = parse(resp.text)
        items, pages = [], []
        next_urls = links(doc, final)
        if self.recipe:
            pages = self.recipe.next_pages(doc, final)
            if self.recipe.applies(final):
                items = self.recipe.extract(doc, final)
            follow = self.recipe.follow_links(doc, final)
            if follow is not None:
                next_urls = follow
        h, content = content_hash(items, main_text(doc))
        return {"state": "done", **metrics, "final": final, "title": title(doc),
                "hash": h, "content": content, "items": items, "links": next_urls,
                "pages": pages, **cache}

    # ------------------------------------------------------------ driver
    def run(self, seed: str | None = None, resume: bool = False) -> int:
        """Crawl from seed (new run) or resume the latest unfinished run. Returns run id."""
        st = self.store
        if resume:
            row = st.unfinished_run()
            if row is None:
                raise SystemExit("nothing to resume")
            run_id, seed = row["id"], row["seed"]
            st.reset_inflight(run_id)
            log.info("resuming run %s (%s queued)", run_id, st.count(run_id, "queued"))
        else:
            seed = normalize(seed)
            if not seed:
                raise SystemExit("seed must be an http(s) URL")
            run_id = st.new_run(seed, asdict(self.cfg))
            st.enqueue(run_id, seed, 0)
            if self.cfg.sitemap:
                # ponytail: capped at max_pages URLs; sitemap lastmod/priority are ignored.
                for u in discover_sitemap(self.fetcher, seed, self.cfg.max_pages):
                    if not self.cfg.same_domain or same_domain(u, seed):
                        st.enqueue(run_id, u, 0)
            st.commit()

        cfg = self.cfg
        inflight: dict = {}
        pool = ThreadPoolExecutor(cfg.workers)
        try:
            while True:
                fetched = st.count(run_id, "done", "failed", "inflight")
                room = min(cfg.workers - len(inflight), cfg.max_pages - fetched)
                if room > 0:
                    for url, depth, hops in st.claim(run_id, room):
                        prev = st.previous_page(url, run_id, self.recipe_fp) if cfg.conditional else None
                        validators = (prev["etag"], prev["last_modified"]) if prev else ()
                        inflight[pool.submit(self._process, url, validators)] = (url, depth, hops, prev)
                if not inflight:
                    break
                done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    url, depth, hops, prev = inflight.pop(fut)
                    r = fut.result()
                    if r.get("not_modified"):
                        r = self._reuse(prev, r)
                    self._record(run_id, url, depth, r, hops)
        except BaseException:
            # On Ctrl-C, don't sit out in-flight fetches (and their retries or rate-limit
            # waits); they stay 'inflight' in the frontier and are queued again by --resume.
            self.fetcher.stop()
            raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        st.finish_run(run_id)
        return run_id

    def _reuse(self, prev, r: dict) -> dict:
        """Fill a 304 result from the previous run's copy of the page."""
        links = json.loads(prev["links"])
        return {**r, "title": prev["title"], "hash": prev["hash"], "content": prev["content"],
                "items": self.store.page_items(prev["run_id"], prev["url"]),
                "links": links["links"], "pages": links["pages"],
                "etag": r["etag"] or prev["etag"],
                "last_modified": r["last_modified"] or prev["last_modified"]}

    def _redirect_dedup(self, run_id: int, url: str, final: str, r: dict) -> dict:
        """url redirected to final. Keep one copy of the page: if final was already
        crawled, url becomes a skipped duplicate; otherwise final is marked skipped so
        it isn't fetched again. Returns the (possibly replaced) result for url."""
        st = self.store
        row = st.db.execute("SELECT state FROM frontier WHERE run_id=? AND url=?",
                            (run_id, final)).fetchone()
        if row and row["state"] == "done":
            return {"state": "skipped", "note": f"redirects to {final} (already crawled)",
                    **{k: r[k] for k in ("status", "bytes", "elapsed_ms")}}
        # ponytail: an 'inflight' target is left alone, so both copies may be stored.
        if row is None or row["state"] == "queued":
            st.enqueue(run_id, final, 0)
            st.mark(run_id, final, "skipped", f"duplicate of {url} (redirect)")
        return r

    def _record(self, run_id: int, url: str, depth: int, r: dict, hops: int = 0):
        st = self.store
        final = r.get("final")
        if r["state"] == "done" and final and final != url:
            r = self._redirect_dedup(run_id, url, final, r)
        st.mark(run_id, url, r["state"], r.get("note"), r.get("status"), r.get("bytes"),
                r.get("elapsed_ms"))
        if r["state"] == "done":
            st.save_page(run_id, url, r["status"], r["title"], r["hash"], r["content"],
                         r["items"], self.recipe.name if self.recipe else None,
                         etag=r["etag"], last_modified=r["last_modified"],
                         links={"links": r["links"], "pages": r["pages"]}, recipe_fp=self.recipe_fp)
            seed = st.db.execute("SELECT seed FROM runs WHERE id=?", (run_id,)).fetchone()[0]

            def in_scope(u):
                return not self.cfg.same_domain or same_domain(u, seed)
            limit = self.recipe.max_pagination if self.recipe else None
            hops += 1  # stored in the frontier, so the cap survives --resume
            for page in r["pages"]:  # same depth: paging through a listing isn't going deeper
                if in_scope(page) and (limit is None or hops <= limit):
                    st.enqueue(run_id, page, depth, hops)
            if depth < self.cfg.max_depth:
                for link in r["links"]:
                    if in_scope(link):
                        st.enqueue(run_id, link, depth + 1)
            log.info("%s %s items=%d", r["status"], url, len(r["items"]),
                     extra={"run": run_id, "url": url, "state": "done", "status": r["status"],
                            "items": len(r["items"]), "ms": round(r["elapsed_ms"], 1)})
        else:
            log.info("%s %s (%s)", r["state"], url, r.get("note"),
                     extra={"run": run_id, "url": url, "state": r["state"],
                            "status": r.get("status"), "note": r.get("note")})
        st.commit()  # per page, so a killed crawl can resume
