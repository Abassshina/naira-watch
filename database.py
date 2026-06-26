import sqlite3

DATABASE = "nairawatch.db"


def get_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brand TEXT,
            model TEXT,
            category TEXT,
            image_url TEXT,
            specifications TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(brand, model, category)
        )
    """)

    cursor.execute("PRAGMA table_info(products)")
    product_columns = [row["name"] for row in cursor.fetchall()]
    if "image_url" not in product_columns:
        cursor.execute("ALTER TABLE products ADD COLUMN image_url TEXT")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            store TEXT,
            product_name TEXT,
            product_url TEXT,
            price INTEGER,
            availability TEXT,
            checked_at TEXT,
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER,
            product_id INTEGER,
            store TEXT,
            price INTEGER,
            checked_at TEXT,
            FOREIGN KEY(listing_id) REFERENCES listings(id),
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
    """)

    cursor.execute("PRAGMA table_info(price_history)")
    price_history_columns = [row["name"] for row in cursor.fetchall()]
    if "product_id" not in price_history_columns:
        cursor.execute("ALTER TABLE price_history ADD COLUMN product_id INTEGER")
    if "store" not in price_history_columns:
        cursor.execute("ALTER TABLE price_history ADD COLUMN store TEXT")

    conn.commit()
    conn.close()


def get_product(brand, model, category):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM products WHERE brand = ? AND model = ? AND category = ?",
        (brand, model, category)
    )
    row = cursor.fetchone()
    conn.close()
    return row["id"] if row else None


def create_product(brand, model, category, image_url=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO products (brand, model, category, image_url) VALUES (?, ?, ?, ?)",
        (brand, model, category, image_url)
    )
    conn.commit()
    product_id = cursor.lastrowid
    conn.close()
    return product_id


def get_or_create_product(brand, model, category, image_url=None):
    product_id = get_product(brand, model, category)
    if product_id:
        return product_id
    return create_product(brand, model, category, image_url)


def save_listing(product_id, store, product_name, product_url, price, availability, checked_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO listings (product_id, store, product_name, product_url, price, availability, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (product_id, store, product_name, product_url, price, availability, checked_at))
    conn.commit()
    listing_id = cursor.lastrowid
    conn.close()
    return listing_id


def save_price_history(listing_id, price, checked_at, product_id=None, store=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO price_history (listing_id, product_id, store, price, checked_at) VALUES (?, ?, ?, ?, ?)",
        (listing_id, product_id, store, price, checked_at)
    )
    conn.commit()
    conn.close()


def clear_all_listings():
    """Removes all listings before a fresh scrape, so old/stale listings don't linger."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM listings")
    conn.commit()
    conn.close()


def get_all_listings_with_products():
    """Returns every current listing joined with its product info, as plain dicts."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            listings.id AS listing_id,
            listings.store,
            listings.product_name,
            listings.price,
            listings.checked_at,
            products.category,
            products.image_url
        FROM listings
        JOIN products ON listings.product_id = products.id
        ORDER BY listings.price ASC
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_price_history_for_product(product_id, limit=200):
    """
    Returns every recorded price-history row for a product, newest first.
    Falls back gracefully if a row predates the store/product_id columns
    (older rows may have NULL store - those are simply omitted, since we
    can't know which store they belonged to).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT store, price, checked_at
        FROM price_history
        WHERE product_id = ? AND store IS NOT NULL
        ORDER BY checked_at DESC
        LIMIT ?
    """, (product_id, limit))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_price_stats_for_product(product_id):
    """
    Returns aggregate price statistics for a product, computed entirely
    in SQL (not by looping over rows in Python) for performance.
    Returns None if there is no history at all for this product.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            MIN(price) AS lowest_ever,
            MAX(price) AS highest_ever,
            AVG(price) AS average_price,
            COUNT(*) AS record_count,
            COUNT(DISTINCT store) AS store_count,
            MIN(checked_at) AS first_seen,
            MAX(checked_at) AS last_updated
        FROM price_history
        WHERE product_id = ? AND store IS NOT NULL
    """, (product_id,))
    row = cursor.fetchone()
    conn.close()

    if not row or row["record_count"] == 0:
        return None

    return {
        "lowest_ever": row["lowest_ever"],
        "highest_ever": row["highest_ever"],
        "average_price": round(row["average_price"]) if row["average_price"] is not None else None,
        "record_count": row["record_count"],
        "store_count": row["store_count"],
        "first_seen": row["first_seen"],
        "last_updated": row["last_updated"],
    }


def get_first_recorded_price_for_product(product_id):
    """
    Returns the price and date of the very first recorded entry for this
    product, used to compute the price-change-since-first-seen indicator.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT price, checked_at
        FROM price_history
        WHERE product_id = ? AND store IS NOT NULL
        ORDER BY checked_at ASC
        LIMIT 1
    """, (product_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None