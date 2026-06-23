from flask import Flask, request
import csv
import os
import math
import re
import time
import threading
import requests
from bs4 import BeautifulSoup
from datetime import datetime

app = Flask(__name__)

# ==========================================
# CONFIG
# ==========================================
CSV_FILE = "price_comparison.csv"
ITEMS_PER_PAGE = 20
REFRESH_INTERVAL_SECONDS = 4 * 60 * 60

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ==========================================
# PRICE EXTRACTION
# ==========================================
def extract_first_price(price_text):
    matches = re.findall(r"[\d]{1,3}(?:,\d{3})*", price_text)

    if not matches:
        return None

    try:
        value = int(matches[0].replace(",", ""))

        if 1000 <= value <= 5000000:
            return value

    except ValueError:
        pass

    return None


# ==========================================
# SCRAPER
# ==========================================
def run_scraper():

    print("Starting scrape...")

    all_results = []

    # ==========================================
    # JUSTFONES
    # ==========================================
    for page_num in range(1, 6):

        if page_num == 1:
            url = "https://www.justfones.ng/smartphones.html"
        else:
            url = f"https://www.justfones.ng/smartphones.html?p={page_num}"

        try:

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=20
            )

            print(f"Page {page_num}: {response.status_code}")

            if response.status_code != 200:
                continue

            soup = BeautifulSoup(response.text, "html.parser")

            products = soup.find_all(
                "li",
                class_="item product product-item"
            )

            print(f"Found {len(products)} products")

            for product in products:

                name_tag = product.find(
                    "a",
                    class_="product-item-link"
                )

                price_tag = product.find(
                    "span",
                    class_="price"
                )

                if not name_tag or not price_tag:
                    continue

                name = name_tag.get_text(strip=True)
                price_text = price_tag.get_text(strip=True)

                price = extract_first_price(price_text)

                if price:

                    all_results.append({
                        "store": "Justfones",
                        "name": name,
                        "price": price
                    })

        except Exception as e:

            print(f"Justfones error page {page_num}: {e}")

        time.sleep(1)

    # ==========================================
    # REMOVE DUPLICATES
    # ==========================================
    unique = []
    seen = set()

    for item in all_results:

        key = (item["store"], item["name"])

        if key not in seen:
            seen.add(key)
            unique.append(item)

    unique.sort(key=lambda x: x["price"])

    # ==========================================
    # SAVE CSV
    # ==========================================
    if unique:

        with open(
            CSV_FILE,
            "w",
            newline="",
            encoding="utf-8"
        ) as f:

            writer = csv.writer(f)

            writer.writerow([
                "Store",
                "Product",
                "Price (NGN)",
                "Date Checked"
            ])

            checked_time = datetime.now().strftime(
                "%Y-%m-%d %H:%M"
            )

            for item in unique:

                writer.writerow([
                    item["store"],
                    item["name"],
                    item["price"],
                    checked_time
                ])

        print(f"Saved {len(unique)} phones")

    else:

        print("No phones collected")


# ==========================================
# BACKGROUND REFRESH
# ==========================================
def background_refresh_loop():

    while True:

        try:
            run_scraper()

        except Exception as e:
            print(f"Background scraper error: {e}")

        time.sleep(REFRESH_INTERVAL_SECONDS)


# ==========================================
# HOME PAGE
# ==========================================
@app.route("/")
def home():

    rows = []

    if os.path.exists(CSV_FILE):

        with open(
            CSV_FILE,
            "r",
            encoding="utf-8",
            errors="replace"
        ) as f:

            reader = csv.DictReader(f)
            rows = list(reader)

    stores = sorted(
        set(r["Store"] for r in rows)
    )

    search = request.args.get(
        "search",
        ""
    ).strip().lower()

    max_price = request.args.get(
        "max_price",
        ""
    ).strip()

    selected_stores = request.args.getlist(
        "store"
    )

    if not selected_stores:
        selected_stores = stores

    filtered = [
        r for r in rows
        if r["Store"] in selected_stores
    ]

    if search:

        filtered = [
            r for r in filtered
            if search in r["Product"].lower()
        ]

    if max_price:

        try:

            max_price_value = int(max_price)

            filtered = [
                r for r in filtered
                if int(r["Price (NGN)"]) <= max_price_value
            ]

        except ValueError:
            pass

    filtered.sort(
        key=lambda x: int(x["Price (NGN)"])
    )

    total_items = len(filtered)

    total_pages = max(
        1,
        math.ceil(total_items / ITEMS_PER_PAGE)
    )

    try:
        current_page = int(
            request.args.get("page", 1)
        )
    except:
        current_page = 1

    start = (current_page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE

    page_rows = filtered[start:end]

    html = f"""
    <html>
    <head>
        <title>NairaWatch</title>
        <style>
            body {{
                font-family: Arial;
                background:#f6f1e4;
                padding:30px;
            }}

            h1 {{
                color:#c4541f;
            }}

            table {{
                width:100%;
                border-collapse:collapse;
                background:white;
            }}

            th {{
                background:black;
                color:white;
                padding:10px;
            }}

            td {{
                padding:10px;
                border-bottom:1px solid #ddd;
            }}

            .price {{
                color:green;
                font-weight:bold;
            }}
        </style>
    </head>
    <body>

        <h1>NairaWatch</h1>

        <p>
            {len(rows)} phones across
            {len(stores)} stores
        </p>
    """

    if page_rows:

        html += """
        <table>
        <tr>
            <th>Store</th>
            <th>Product</th>
            <th>Price</th>
            <th>Checked</th>
        </tr>
        """

        for row in page_rows:

            html += f"""
            <tr>
                <td>{row['Store']}</td>
                <td>{row['Product']}</td>
                <td class='price'>
                    ₦{int(row['Price (NGN)']):,}
                </td>
                <td>{row['Date Checked']}</td>
            </tr>
            """

        html += "</table>"

    else:

        html += """
        <h3>
        First scrape is running...
        Refresh in 1-2 minutes.
        </h3>
        """

    html += """
    </body>
    </html>
    """

    return html


# ==========================================
# STARTUP
# ==========================================
if not os.path.exists(CSV_FILE):

    try:
        run_scraper()

    except Exception as e:
        print(e)

scraper_thread = threading.Thread(
    target=background_refresh_loop,
    daemon=True
)

scraper_thread.start()


# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", 5000)
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
