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
        listing_id   TEXT PRIMARY KEY,
        brand        TEXT NOT NULL,
        source       TEXT NOT NULL,
        price_jpy    INTEGER,
        is_bag       INTEGER DEFAULT 0,
        posted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS daily_stats (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        date   TEXT NOT NULL,
        brand  TEXT NOT NULL,
        model  TEXT NOT NULL,
        source TEXT NOT NULL,
        count  INTEGER DEFAULT 1,
        UNIQUE(date, brand, model, source)
    )''')

    # Migrate existing DB: add new columns if they don't exist yet
    for col, defn in [
        ('price_jpy',    'INTEGER'),
        ('is_bag',       'INTEGER DEFAULT 0'),
        ('last_seen_at', 'TIMESTAMP'),
    ]:
        try:
            c.execute(f'ALTER TABLE seen_listings ADD COLUMN {col} {defn}')
        except sqlite3.OperationalError:
            pass  # Column already exists

    conn.commit()
    conn.close()


def is_seen(listing_id: str) -> bool:
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT 1 FROM seen_listings WHERE listing_id = ?', (listing_id,))
    result = c.fetchone()
    conn.close()
    return result is not None


def mark_seen(listing_id: str, brand: str, source: str,
              price_jpy: int = None, is_bag: bool = False):
    now = datetime.now().isoformat()
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        '''INSERT OR IGNORE INTO seen_listings
           (listing_id, brand, source, price_jpy, is_bag, posted_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (listing_id, brand, source, price_jpy, 1 if is_bag else 0, now, now),
    )
    conn.commit()
    conn.close()


def update_last_seen(listing_id: str):
    """Refresh last_seen_at for items still visible in search (used for sold-fast tracking)."""
    conn = get_connection()
    conn.execute(
        'UPDATE seen_listings SET last_seen_at = CURRENT_TIMESTAMP WHERE listing_id = ?',
        (listing_id,),
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


# ---------------------------------------------------------------------------
# Records — LV + Gucci bags only
# ---------------------------------------------------------------------------

def get_brand_activity(date: str, brands: list) -> list:
    """Count bag listings seen today per brand."""
    conn = get_connection()
    c = conn.cursor()
    placeholders = ','.join('?' for _ in brands)
    c.execute(
        f'''SELECT brand, COUNT(*) as count FROM seen_listings
            WHERE brand IN ({placeholders}) AND is_bag = 1
              AND date(posted_at) = ?
            GROUP BY brand
            ORDER BY count DESC''',
        brands + [date],
    )
    results = [{'brand': r['brand'], 'count': r['count']} for r in c.fetchall()]
    conn.close()
    return results


def get_price_records(date: str, brands: list) -> list:
    """Lowest bag price today vs 30-day rolling average, per brand."""
    conn = get_connection()
    c = conn.cursor()
    results = []
    for brand in brands:
        c.execute(
            '''SELECT listing_id, MIN(price_jpy) as min_price FROM seen_listings
               WHERE brand = ? AND is_bag = 1 AND date(posted_at) = ? AND price_jpy > 0''',
            (brand, date),
        )
        today_row = c.fetchone()

        c.execute(
            '''SELECT AVG(price_jpy) as avg_price FROM seen_listings
               WHERE brand = ? AND is_bag = 1
                 AND date(posted_at) >= date('now', '-30 days') AND price_jpy > 0''',
            (brand,),
        )
        avg_row = c.fetchone()

        if today_row and today_row['min_price']:
            avg = round(avg_row['avg_price']) if avg_row and avg_row['avg_price'] else None
            results.append({
                'brand': brand,
                'min_price': today_row['min_price'],
                'avg_price': avg,
                'listing_id': today_row['listing_id'],
            })
    conn.close()
    return results


def get_fastest_sold(date: str, brands: list) -> list:
    """Bag items posted today that disappeared fastest (= likely sold quickly).

    An item is considered 'possibly sold' when last_seen_at is more than 2 hours
    old — meaning it no longer appears in the search results.
    """
    conn = get_connection()
    c = conn.cursor()
    placeholders = ','.join('?' for _ in brands)
    c.execute(
        f'''SELECT listing_id, brand, price_jpy, posted_at, last_seen_at,
                   ROUND((julianday(last_seen_at) - julianday(posted_at)) * 24 * 60) AS lifetime_minutes
            FROM seen_listings
            WHERE brand IN ({placeholders}) AND is_bag = 1
              AND date(posted_at) = ?
              AND datetime(last_seen_at) < datetime('now', '-2 hours')
            ORDER BY lifetime_minutes ASC
            LIMIT 3''',
        brands + [date],
    )
    results = [dict(r) for r in c.fetchall()]
    conn.close()
    return results
