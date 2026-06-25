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
            price INTEGER,
            checked_at TEXT,
            FOREIGN KEY(listing_id) REFERENCES listings(id)
        )
    """)

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


def create_product(brand, model, category):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO products (brand, model, category) VALUES (?, ?, ?)",
        (brand, model, category)
    )
    conn.commit()
    product_id = cursor.lastrowid
    conn.close()
    return product_id


def get_or_create_product(brand, model, category):
    product_id = get_product(brand, model, category)
    if product_id:
        return product_id
    return create_product(brand, model, category)


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


def save_price_history(listing_id, price, checked_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO price_history (listing_id, price, checked_at) VALUES (?, ?, ?)",
        (listing_id, price, checked_at)
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
            products.category
        FROM listings
        JOIN products ON listings.product_id = products.id
        ORDER BY listings.price ASC
    """)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]