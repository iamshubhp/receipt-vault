"""SQLite storage. One file on disk, so backups are a single copy."""
import json
import os
import sqlite3
from pathlib import Path

from .brands import SEED_BRANDS


def data_dir() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    (d / "images").mkdir(exist_ok=True)
    return d


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(data_dir() / "receipts.db", timeout=15, check_same_thread=False)  # one connection per request
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS brands (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    aliases  TEXT NOT NULL DEFAULT '[]',   -- words that identify the brand on a receipt
    requires TEXT NOT NULL DEFAULT '[]',   -- if set, one of these words must also appear (specific line only)
    rate     REAL,                         -- commission % override, NULL = use the default rate
    active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS receipts (
    id                  INTEGER PRIMARY KEY,
    receipt_no          TEXT NOT NULL,     -- "Transaction Seq. No."
    terminal            TEXT NOT NULL DEFAULT '',
    sale_date           TEXT NOT NULL,     -- YYYY-MM-DD
    sale_time           TEXT NOT NULL DEFAULT '',
    month               TEXT NOT NULL,     -- YYYY-MM, derived from sale_date
    store_ref           TEXT NOT NULL DEFAULT '',
    dep_date            TEXT NOT NULL DEFAULT '',
    destination         TEXT NOT NULL DEFAULT '',
    final_dest          TEXT NOT NULL DEFAULT '',
    flight_no           TEXT NOT NULL DEFAULT '',
    passenger_name      TEXT NOT NULL DEFAULT '',
    served_by           TEXT NOT NULL DEFAULT '',
    payment_method      TEXT NOT NULL DEFAULT '',
    auth_no             TEXT NOT NULL DEFAULT '',
    club_avolta         INTEGER NOT NULL DEFAULT 0,
    printed_total_cents INTEGER,           -- total as printed on the paper receipt
    notes               TEXT NOT NULL DEFAULT '',
    image_id            TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    -- A register's counter can repeat on another terminal or after a reset,
    -- so the receipt number alone is not unique.
    UNIQUE (terminal, receipt_no, sale_date)
);
CREATE INDEX IF NOT EXISTS receipts_month ON receipts (month);

CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY,
    receipt_id   INTEGER NOT NULL REFERENCES receipts (id) ON DELETE CASCADE,
    position     INTEGER NOT NULL DEFAULT 0,
    barcode      TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL,
    brand_id     INTEGER REFERENCES brands (id),  -- set only when the item earns commission
    brand_text   TEXT NOT NULL DEFAULT '',        -- brand as read, for any brand
    qty          INTEGER NOT NULL DEFAULT 1,
    list_cents   INTEGER NOT NULL,                -- price before any discount
    paid_cents   INTEGER NOT NULL,                -- price the customer paid
    needs_review INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_receipt ON items (receipt_id);

-- Remembers how a barcode was tagged the last time it was saved.
CREATE TABLE IF NOT EXISTS products (
    barcode     TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    brand_id    INTEGER REFERENCES brands (id),
    brand_text  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "commission_rate": "0",          # percent
    "commission_basis": "paid",      # "paid" = after discount, "list" = before discount
}


def init() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM brands").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO brands (name, aliases, requires) VALUES (?, ?, ?)",
                [(b["name"], json.dumps(b["aliases"]), json.dumps(b.get("requires", []))) for b in SEED_BRANDS],
            )
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        conn.commit()
    finally:
        conn.close()


def get_settings(conn) -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")})
    return {"commission_rate": float(s["commission_rate"]), "commission_basis": s["commission_basis"]}


def brand_rows(conn, only_active=False) -> list[dict]:
    q = "SELECT * FROM brands" + (" WHERE active = 1" if only_active else "") + " ORDER BY name COLLATE NOCASE"
    out = []
    for r in conn.execute(q):
        d = dict(r)
        d["aliases"] = json.loads(d["aliases"])
        d["requires"] = json.loads(d["requires"])
        d["active"] = bool(d["active"])
        out.append(d)
    return out
