"""Export extracted items (or page records) to JSONL, CSV or SQLite."""
from __future__ import annotations

import csv
import json
import sqlite3
from typing import IO


def _cell(v):
    return v if v is None or isinstance(v, (str, int, float)) else json.dumps(v, ensure_ascii=False)


def columns(rows: list[dict]) -> list[str]:
    cols: dict[str, None] = {}
    for r in rows:
        cols.update(dict.fromkeys(r))
    return list(cols)


def to_jsonl(rows: list[dict], fh: IO[str]):
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def to_csv(rows: list[dict], fh: IO[str]):
    w = csv.DictWriter(fh, fieldnames=columns(rows))
    w.writeheader()
    for r in rows:
        w.writerow({k: _cell(v) for k, v in r.items()})


def to_sqlite(rows: list[dict], path: str, table: str = "items"):
    if not table.isidentifier():
        raise ValueError(f"bad table name {table!r}")
    cols = columns(rows)
    db = sqlite3.connect(path)
    with db:
        db.execute(f'DROP TABLE IF EXISTS "{table}"')
        db.execute(f'CREATE TABLE "{table}" ({", ".join(f"[{c}]" for c in cols)})')
        db.executemany(
            f'INSERT INTO "{table}" VALUES ({",".join("?" * len(cols))})',
            [[_cell(r.get(c)) for c in cols] for r in rows])
    db.close()
