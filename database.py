import sqlite3
from datetime import datetime

DB_PATH = 'zenmarket.db'


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS seen_listings (
        listing_id    TEXT PRIMARY KEY,
        brand         TEXT NOT NULL,
        source        TEXT NOT NULL,
        title         TEXT,
        price_jpy     INTEGER,
        zenmarket_url TEXT,
        posted_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Migrate existing tables that lack the new columns
    for col, col_type in [('title', 'TEXT'), ('price_jpy', 'INTEGER'), ('zenmarket_url', 'TEXT')]:
        try:
            c.execute(f'ALTER TABLE seen_listings ADD COLUMN {col} {col_type}')
        except Exception:
            pass

    c.execute('''CREATE TABLE IF NOT EXISTS daily_stats (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        date   TEXT NOT NULL,
        brand  TEXT NOT NULL,
        model  TEXT NOT NULL,
        source TEXT NOT NULL,
        count  INTEGER DEFAULT 1,
        UNIQUE(date, brand, model, source)
    )''')

    conn.commit()
    conn.close()


def is_seen(listing_id: str) -> bool:
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT 1 FROM seen_listings WHERE listing_id = ?', (listing_id,))
    result = c.fetchone()
    conn.close()
    return result is not None


def mark_seen(
    listing_id: str,
    brand: str,
    source: str,
    title: str = None,
    price_jpy: int = None,
    zenmarket_url: str = None,
):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        '''INSERT OR IGNORE INTO seen_listings
           (listing_id, brand, source, title, price_jpy, zenmarket_url)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (listing_id, brand, source, title, price_jpy, zenmarket_url),
    )
    conn.commit()
    conn.close()


def record_stat(brand: str, model: str, source: str):
    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        '''INSERT INTO daily_stats (date, brand, model, source, count) VALUES (?, ?, ?, ?, 1)
           ON CONFLICT(date, brand, model, source) DO UPDATE SET count = count + 1''',
        (today, brand, model, source),
    )
    conn.commit()
    conn.close()


def get_brand_counts_today() -> list:
    today = datetime.now().strftime('%Y-%m-%d')
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        '''SELECT brand, COUNT(*) AS total
           FROM seen_listings
           WHERE DATE(posted_at) = ?
           GROUP BY brand
           ORDER BY total DESC
           LIMIT 15''',
        (today,),
    )
    results = c.fetchall()
    conn.close()
    return results


def get_cheapest_per_brand() -> list:
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        '''SELECT s.brand, s.title, s.price_jpy, s.zenmarket_url
           FROM seen_listings s
           INNER JOIN (
               SELECT brand, MIN(price_jpy) AS min_price
               FROM seen_listings
               WHERE price_jpy IS NOT NULL AND zenmarket_url IS NOT NULL
               GROUP BY brand
           ) m ON s.brand = m.brand AND s.price_jpy = m.min_price
           WHERE s.zenmarket_url IS NOT NULL
           GROUP BY s.brand
           ORDER BY s.price_jpy ASC''',
    )
    results = c.fetchall()
    conn.close()
    return results


def get_recent_listings(limit: int = 15) -> list:
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        '''SELECT brand, title, price_jpy, zenmarket_url
           FROM seen_listings
           WHERE zenmarket_url IS NOT NULL AND title IS NOT NULL AND price_jpy IS NOT NULL
           ORDER BY posted_at DESC
           LIMIT ?''',
        (limit,),
    )
    results = c.fetchall()
    conn.close()
    return results


def get_daily_top_models(brand: str, date: str = None, limit: int = 7):
    if date is None:
        date = datetime.now().strftime('%Y-%m-%d')
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        '''SELECT model,
                  SUM(count) AS total,
                  SUM(CASE WHEN source = 'mercari' THEN count ELSE 0 END) AS mercari_count,
                  SUM(CASE WHEN source = 'rakuma'  THEN count ELSE 0 END) AS rakuma_count
           FROM daily_stats
           WHERE date = ? AND brand = ?
           GROUP BY model
           ORDER BY total DESC
           LIMIT ?''',
        (date, brand, limit),
    )
    results = c.fetchall()
    conn.close()
    return results
