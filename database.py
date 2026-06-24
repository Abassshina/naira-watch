import sqlite3

DATABASE = "nairawatch.db"


def get_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database():
    conn = get_connection()
    cursor = conn.cursor()

    # Products table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        brand TEXT,
        model TEXT,
        category TEXT,
        image_url TEXT,
        specifications TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(brand, model)
    )
    """)

    # Listings table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER,
        store TEXT,
        product_name TEXT,
        product_url TEXT,
        price INTEGER,
        availability TEXT,
        checked_at TIMESTAMP,
        FOREIGN KEY(product_id) REFERENCES products(id)
    )
    """)

    # Price history
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id INTEGER,
        price INTEGER,
        checked_at TIMESTAMP,
        FOREIGN KEY(listing_id) REFERENCES listings(id)
    )
    """)

    conn.commit()
    conn.close()
