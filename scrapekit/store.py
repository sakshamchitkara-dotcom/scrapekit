"""SQLite persistence: runs, crawl frontier, pages and extracted items."""
from __future__ import annotations

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed TEXT NOT NULL,
    config TEXT NOT NULL,
    started REAL NOT NULL,
    finished REAL
);
CREATE TABLE IF NOT EXISTS frontier (
    run_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    depth INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',   -- queued | inflight | done | failed | skipped
    note TEXT,
    seq INTEGER NOT NULL,
    PRIMARY KEY (run_id, url)
);
CREATE INDEX IF NOT EXISTS frontier_state ON frontier(run_id, state, seq);
CREATE TABLE IF NOT EXISTS pages (
    run_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    status INTEGER,
    fetched_at REAL,
    title TEXT,
    hash TEXT,
    content TEXT,
    PRIMARY KEY (run_id, url)
);
CREATE TABLE IF NOT EXISTS items (
    run_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    recipe TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_run ON items(run_id);
CREATE INDEX IF NOT EXISTS pages_url ON pages(url, run_id);
"""
# Columns added after 0.1.0; created on open so older databases keep working.
ADDED_COLUMNS = {
    "pages": {"etag": "TEXT", "last_modified": "TEXT", "links": "TEXT", "recipe_fp": "TEXT"},
    "frontier": {"status": "INTEGER", "bytes": "INTEGER", "elapsed_ms": "REAL",
                 "hops": "INTEGER NOT NULL DEFAULT 0"},  # pagination hops from the chain start
}


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        for table, cols in ADDED_COLUMNS.items():
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.commit()

    def close(self):
        self.db.close()

    # ---------------------------------------------------------------- runs
    def new_run(self, seed: str, config: dict) -> int:
        cur = self.db.execute("INSERT INTO runs(seed, config, started) VALUES (?,?,?)",
                              (seed, json.dumps(config), time.time()))
        self.db.commit()
        return cur.lastrowid

    def unfinished_run(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM runs WHERE finished IS NULL ORDER BY id DESC LIMIT 1").fetchone()

    def finish_run(self, run_id: int):
        self.db.execute("UPDATE runs SET finished=? WHERE id=?", (time.time(), run_id))
        self.db.commit()

    def runs(self, finished_only=True) -> list[sqlite3.Row]:
        q = "SELECT * FROM runs" + (" WHERE finished IS NOT NULL" if finished_only else "")
        return self.db.execute(q + " ORDER BY id").fetchall()

    # ------------------------------------------------------------ frontier
    def enqueue(self, run_id: int, url: str, depth: int, hops: int = 0) -> bool:
        """Add url if unseen in this run. Returns True if it was new."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO frontier(run_id, url, depth, hops, seq) "
            "VALUES (?,?,?,?,(SELECT COALESCE(MAX(seq),0)+1 FROM frontier WHERE run_id=?))",
            (run_id, url, depth, hops, run_id))
        return cur.rowcount == 1

    def claim(self, run_id: int, n: int) -> list[tuple[str, int, int]]:
        """Mark up to n queued URLs inflight. Returns (url, depth, hops) tuples."""
        rows = self.db.execute(
            "SELECT url, depth, hops FROM frontier WHERE run_id=? AND state='queued' ORDER BY seq LIMIT ?",
            (run_id, n)).fetchall()
        self.db.executemany("UPDATE frontier SET state='inflight' WHERE run_id=? AND url=?",
                            [(run_id, r["url"]) for r in rows])
        self.db.commit()
        return [(r["url"], r["depth"], r["hops"]) for r in rows]

    def mark(self, run_id: int, url: str, state: str, note: str | None = None,
             status: int | None = None, bytes_: int | None = None, elapsed_ms: float | None = None):
        self.db.execute("UPDATE frontier SET state=?, note=?, status=?, bytes=?, elapsed_ms=?"
                        " WHERE run_id=? AND url=?",
                        (state, note, status, bytes_, elapsed_ms, run_id, url))

    def reset_inflight(self, run_id: int):
        self.db.execute("UPDATE frontier SET state='queued' WHERE run_id=? AND state='inflight'",
                        (run_id,))
        self.db.commit()

    def count(self, run_id: int, *states: str) -> int:
        q = f"SELECT COUNT(*) FROM frontier WHERE run_id=? AND state IN ({','.join('?' * len(states))})"
        return self.db.execute(q, (run_id, *states)).fetchone()[0]

    def stats(self, run_id: int) -> dict:
        """Summary of one run: states, HTTP status codes, bytes and response timings."""
        run = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise SystemExit(f"no run {run_id}")

        def q(sql):
            return self.db.execute(sql, (run_id,)).fetchall()

        ms = sorted(r[0] for r in q("SELECT elapsed_ms FROM frontier WHERE run_id=? AND elapsed_ms IS NOT NULL"))

        def pct(f):  # nearest-rank percentile
            return round(ms[int(f * (len(ms) - 1))], 1) if ms else None
        return {
            "run": run_id, "seed": run["seed"],
            "duration_s": round(run["finished"] - run["started"], 2) if run["finished"] else None,
            "states": dict(q("SELECT state, COUNT(*) FROM frontier WHERE run_id=? GROUP BY state")),
            "status_codes": {str(k): v for k, v in q(
                "SELECT status, COUNT(*) FROM frontier WHERE run_id=? AND status IS NOT NULL"
                " GROUP BY status ORDER BY status")},
            "bytes": q("SELECT COALESCE(SUM(bytes), 0) FROM frontier WHERE run_id=?")[0][0],
            "items": q("SELECT COUNT(*) FROM items WHERE run_id=?")[0][0],
            "timing_ms": {"count": len(ms), "mean": round(sum(ms) / len(ms), 1) if ms else None,
                          "p50": pct(0.5), "p95": pct(0.95), "max": pct(1.0)},
            "slowest": [dict(r) for r in self.db.execute(
                "SELECT url, status, round(elapsed_ms, 1) AS ms FROM frontier WHERE run_id=?"
                " AND elapsed_ms IS NOT NULL ORDER BY elapsed_ms DESC LIMIT 5", (run_id,))],
        }

    # --------------------------------------------------------------- pages
    def save_page(self, run_id: int, url: str, status: int, title: str | None,
                  hash_: str | None, content: str | None, items: list[dict], recipe: str | None,
                  etag: str | None = None, last_modified: str | None = None,
                  links: dict | None = None, recipe_fp: str | None = None):
        self.db.execute(
            "INSERT OR REPLACE INTO pages(run_id, url, status, fetched_at, title, hash, content,"
            " etag, last_modified, links, recipe_fp) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, url, status, time.time(), title, hash_, content, etag, last_modified,
             json.dumps(links) if links is not None else None, recipe_fp))
        self.db.execute("DELETE FROM items WHERE run_id=? AND url=?", (run_id, url))
        self.db.executemany("INSERT INTO items VALUES (?,?,?,?)",
                            [(run_id, url, recipe, json.dumps(i, ensure_ascii=False)) for i in items])

    def commit(self):
        self.db.commit()

    def pages(self, run_id: int) -> dict[str, sqlite3.Row]:
        return {r["url"]: r for r in self.db.execute(
            "SELECT * FROM pages WHERE run_id=? AND hash IS NOT NULL", (run_id,))}

    def previous_page(self, url: str, before_run: int, recipe_fp: str) -> sqlite3.Row | None:
        """Latest earlier copy of url made with the same recipe, if it has cache validators."""
        return self.db.execute(
            "SELECT * FROM pages WHERE url=? AND run_id<? AND recipe_fp=? AND links IS NOT NULL"
            " AND hash IS NOT NULL AND (etag IS NOT NULL OR last_modified IS NOT NULL)"
            " ORDER BY run_id DESC LIMIT 1", (url, before_run, recipe_fp)).fetchone()

    def page_items(self, run_id: int, url: str) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT data FROM items WHERE run_id=? AND url=? ORDER BY rowid", (run_id, url))]

    def items(self, run_id: int) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT data FROM items WHERE run_id=? ORDER BY rowid", (run_id,))]
