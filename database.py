"""
SQLite time-series storage for Blinkit scrape runs.

Schema:
  runs         — one row per scrape run (timestamp, category_url, pincode)
  products     — one row per product × run (all scraped fields)

Every scrape appends new rows — never overwrites — so you build a full
history that powers the analytics layer.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

from models import BlinkitProduct

DB_PATH = Path(__file__).parent / "output" / "blinkit.db"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db(db_path: Path = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    with db(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category_url TEXT NOT NULL,
                pincode     TEXT,
                scraped_at  TEXT NOT NULL,
                product_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS products (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id              INTEGER NOT NULL REFERENCES runs(id),
                scraped_at          TEXT NOT NULL,

                -- Identity
                product_id          TEXT,
                name                TEXT,
                brand               TEXT,
                category            TEXT,
                sub_category        TEXT,
                product_type        TEXT,

                -- Listing signals
                rank                INTEGER,

                -- Pricing
                price               REAL,
                mrp                 REAL,
                discount_pct        REAL,
                size                TEXT,

                -- Stock & demand
                in_stock            INTEGER,
                inventory           INTEGER,
                max_cart_qty        INTEGER,
                is_rationed         INTEGER,

                -- Details
                ingredients         TEXT,
                nutritional_info    TEXT,
                allergen_info       TEXT,
                storage_instructions TEXT,
                country_of_origin   TEXT,
                manufacturer        TEXT,
                fssai_license       TEXT,
                diet_preference     TEXT,
                flavour             TEXT,
                shelf_life          TEXT,
                key_features        TEXT,

                -- Structured nutrition (JSON)
                nutrition_json      TEXT,

                -- Rating
                rating_score        REAL,
                rating_count        INTEGER,

                -- Other
                product_url         TEXT,
                image_urls          TEXT,   -- JSON array
                pincode             TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_products_product_id
                ON products(product_id);
            CREATE INDEX IF NOT EXISTS idx_products_run_id
                ON products(run_id);
            CREATE INDEX IF NOT EXISTS idx_products_scraped_at
                ON products(scraped_at);
        """)


def save_run(
    category_url: str,
    products: list[BlinkitProduct],
    pincode: str = "",
    db_path: Path = DB_PATH,
) -> int:
    """Insert a full scrape run and return the run_id."""
    init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    with db(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO runs (category_url, pincode, scraped_at, product_count) VALUES (?,?,?,?)",
            (category_url, pincode, now, len(products)),
        )
        run_id = cur.lastrowid

        rows = []
        for p in products:
            nutrition_json = None
            if p.nutrition:
                nutrition_json = json.dumps(p.nutrition.model_dump(exclude_none=True))

            rows.append((
                run_id,
                p.scraped_at or now,
                p.product_id,
                p.name,
                p.brand,
                p.category,
                p.sub_category,
                p.product_type,
                p.rank,
                p.price,
                p.mrp,
                p.discount_pct,
                p.size,
                int(p.in_stock),
                p.inventory,
                p.max_cart_qty,
                int(p.is_rationed),
                p.ingredients,
                p.nutritional_info,
                p.allergen_info,
                p.storage_instructions,
                p.country_of_origin,
                p.manufacturer,
                p.fssai_license,
                p.diet_preference,
                p.flavour,
                p.shelf_life,
                p.key_features,
                nutrition_json,
                p.rating.score if p.rating else None,
                p.rating.count if p.rating else None,
                p.product_url,
                json.dumps(p.image_urls),
                p.location_pincode,
            ))

        conn.executemany("""
            INSERT INTO products (
                run_id, scraped_at, product_id, name, brand, category, sub_category,
                product_type, rank, price, mrp, discount_pct, size,
                in_stock, inventory, max_cart_qty, is_rationed,
                ingredients, nutritional_info, allergen_info, storage_instructions,
                country_of_origin, manufacturer, fssai_license, diet_preference,
                flavour, shelf_life, key_features, nutrition_json,
                rating_score, rating_count, product_url, image_urls, pincode
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
        """, rows)

    return run_id


def get_runs(db_path: Path = DB_PATH, limit: int = 50) -> list[dict]:
    init_db(db_path)
    with db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY scraped_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_product_history(
    product_id: str,
    db_path: Path = DB_PATH,
    limit: int = 200,
) -> list[dict]:
    """Full time-series for one product across all runs."""
    init_db(db_path)
    with db(db_path) as conn:
        rows = conn.execute("""
            SELECT p.*, r.category_url
            FROM products p
            JOIN runs r ON p.run_id = r.id
            WHERE p.product_id = ?
            ORDER BY p.scraped_at ASC
            LIMIT ?
        """, (product_id, limit)).fetchall()
    return [dict(r) for r in rows]


def get_latest_run_products(
    category_url: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> list[dict]:
    """Return all products from the most recent run (optionally filtered by category_url)."""
    init_db(db_path)
    with db(db_path) as conn:
        if category_url:
            run = conn.execute(
                "SELECT id FROM runs WHERE category_url=? ORDER BY scraped_at DESC LIMIT 1",
                (category_url,)
            ).fetchone()
        else:
            run = conn.execute(
                "SELECT id FROM runs ORDER BY scraped_at DESC LIMIT 1"
            ).fetchone()

        if not run:
            return []

        rows = conn.execute(
            "SELECT * FROM products WHERE run_id=? ORDER BY rank ASC",
            (run["id"],)
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_products_between(
    start: str,
    end: str,
    category_url: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> list[dict]:
    """Return all product rows between two ISO timestamps."""
    init_db(db_path)
    with db(db_path) as conn:
        if category_url:
            rows = conn.execute("""
                SELECT p.*, r.category_url FROM products p
                JOIN runs r ON p.run_id = r.id
                WHERE p.scraped_at BETWEEN ? AND ? AND r.category_url = ?
                ORDER BY p.product_id, p.scraped_at
            """, (start, end, category_url)).fetchall()
        else:
            rows = conn.execute("""
                SELECT p.*, r.category_url FROM products p
                JOIN runs r ON p.run_id = r.id
                WHERE p.scraped_at BETWEEN ? AND ?
                ORDER BY p.product_id, p.scraped_at
            """, (start, end)).fetchall()
    return [dict(r) for r in rows]
