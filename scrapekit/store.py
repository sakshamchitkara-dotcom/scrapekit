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
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

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
    def enqueue(self, run_id: int, url: str, depth: int) -> bool:
        """Add url if unseen in this run. Returns True if it was new."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO frontier(run_id, url, depth, seq) "
            "VALUES (?,?,?,(SELECT COALESCE(MAX(seq),0)+1 FROM frontier WHERE run_id=?))",
            (run_id, url, depth, run_id))
        return cur.rowcount == 1

    def claim(self, run_id: int, n: int) -> list[tuple[str, int]]:
        rows = self.db.execute(
            "SELECT url, depth FROM frontier WHERE run_id=? AND state='queued' ORDER BY seq LIMIT ?",
            (run_id, n)).fetchall()
        self.db.executemany("UPDATE frontier SET state='inflight' WHERE run_id=? AND url=?",
                            [(run_id, r["url"]) for r in rows])
        self.db.commit()
        return [(r["url"], r["depth"]) for r in rows]

    def mark(self, run_id: int, url: str, state: str, note: str | None = None):
        self.db.execute("UPDATE frontier SET state=?, note=? WHERE run_id=? AND url=?",
                        (state, note, run_id, url))

    def reset_inflight(self, run_id: int):
        self.db.execute("UPDATE frontier SET state='queued' WHERE run_id=? AND state='inflight'",
                        (run_id,))
        self.db.commit()

    def count(self, run_id: int, *states: str) -> int:
        q = f"SELECT COUNT(*) FROM frontier WHERE run_id=? AND state IN ({','.join('?' * len(states))})"
        return self.db.execute(q, (run_id, *states)).fetchone()[0]

    # --------------------------------------------------------------- pages
    def save_page(self, run_id: int, url: str, status: int, title: str | None,
                  hash_: str | None, content: str | None, items: list[dict], recipe: str | None):
        self.db.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?)",
                        (run_id, url, status, time.time(), title, hash_, content))
        self.db.execute("DELETE FROM items WHERE run_id=? AND url=?", (run_id, url))
        self.db.executemany("INSERT INTO items VALUES (?,?,?,?)",
                            [(run_id, url, recipe, json.dumps(i, ensure_ascii=False)) for i in items])

    def commit(self):
        self.db.commit()

    def pages(self, run_id: int) -> dict[str, sqlite3.Row]:
        return {r["url"]: r for r in self.db.execute(
            "SELECT * FROM pages WHERE run_id=? AND hash IS NOT NULL", (run_id,))}

    def items(self, run_id: int) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT data FROM items WHERE run_id=? ORDER BY rowid", (run_id,))]
