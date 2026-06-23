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

ITEMS_PER_PAGE = 20
CSV_FILE = "price_comparison.csv"
REFRESH_INTERVAL_SECONDS = 4 * 60 * 60  # 4 hours

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def extract_first_price(price_text):
    matches = re.findall(r"[\d]{1,3}(?:,\d{3})*", price_text)
    if not matches:
        return None
    first_price_str = matches[0].replace(",", "")
    try:
        value = int(first_price_str)
        if 1000 <= value <= 5_000_000:
            return value
    except ValueError:
        pass
    return None

    
    def run_scraper():

        all_results = []

 # ---------- JUSTFONES ----------
    for page_num in range(1, 6):

        jf_url = (
            "https://www.justfones.ng/smartphones.html"
            if page_num == 1
            else f"https://www.justfones.ng/smartphones.html?p={page_num}"
        )

        try:

            response = requests.get(
                jf_url,
                headers=headers,
                timeout=10
            )

            if response.status_code == 200:

                soup = BeautifulSoup(
                    response.text,
                    "html.parser"
                )

                products = soup.find_all(
                    "li",
                    class_="item product product-item"
                )

                for product in products:

                    name_tag = product.find(
                        "a",
                        class_="product-item-link"
                    )

                    price_tag = product.find(
                        "span",
                        class_="price"
                    )

                    name = (
                        name_tag.text.strip()
                        if name_tag else None
                    )

                    price_text = (
                        price_tag.text.strip()
                        if price_tag else None
                    )

                    if name and price_text:

                        price_value = extract_first_price(
                            price_text
                        )

                        if price_value:

                            all_results.append({
                                "name": name,
                                "price": price_value,
                                "store": "Justfones"
                            })

        except Exception as e:

            print(
                f"Justfones page {page_num} error: {e}"
            )

        time.sleep(1.5)

    # ---------- SAVE ----------
    if all_results:

        seen = set()
        unique_results = []

        for item in all_results:

            key = (
                item["store"],
                item["name"]
            )

            if key not in seen:
                seen.add(key)
                unique_results.append(item)

        unique_results.sort(
            key=lambda x: x["price"]
        )

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

            today = datetime.now().strftime(
                "%Y-%m-%d %H:%M"
            )

            for item in unique_results:

                writer.writerow([
                    item["store"],
                    item["name"],
                    item["price"],
                    today
                ])

        print(
            f"Saved {len(unique_results)} products"
        )

    else:

        print("No products collected")
def background_refresh_loop():
    while True:
        try:
            run_scraper()
        except Exception as e:
            print(f"Background scraper error: {e}")
        time.sleep(REFRESH_INTERVAL_SECONDS)


@app.route("/")
def home():
    rows = []

    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

    all_stores = sorted(set(r["Store"] for r in rows))

    search_query = request.args.get("search", "").strip().lower()
    max_price = request.args.get("max_price", "").strip()
    selected_stores = request.args.getlist("store")

    if not selected_stores:
        selected_stores = all_stores

    filtered_rows = [r for r in rows if r["Store"] in selected_stores]

    if search_query:
        filtered_rows = [r for r in filtered_rows if search_query in r["Product"].lower()]

    if max_price:
        try:
            max_price_value = int(max_price)
            filtered_rows = [r for r in filtered_rows if int(r["Price (NGN)"]) <= max_price_value]
        except ValueError:
            pass

    if filtered_rows:
        filtered_rows.sort(key=lambda r: int(r["Price (NGN)"]))

    total_items = len(filtered_rows)
    total_pages = max(1, math.ceil(total_items / ITEMS_PER_PAGE))

    try:
        current_page = int(request.args.get("page", 1))
    except ValueError:
        current_page = 1
    current_page = max(1, min(current_page, total_pages))

    start = (current_page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_rows = filtered_rows[start:end]

    def page_link(p):
        params = [f"page={p}"]
        for s in selected_stores:
            params.append(f"store={s}")
        if search_query:
            params.append(f"search={search_query}")
        if max_price:
            params.append(f"max_price={max_price}")
        return "/?" + "&".join(params)

    store_checkboxes = ""
    for s in all_stores:
        checked = "checked" if s in selected_stores else ""
        store_checkboxes += f"""
        <label class="store-check">
            <input type="checkbox" name="store" value="{s}" {checked}> {s}
        </label>
        """

    html = f"""
    <html>
    <head>
        <title>NairaWatch — Live Price Tracker</title>
        <style>
            body {{ font-family: Arial, sans-serif; background: #f6f1e4; margin: 0; padding: 30px; }}
            h1 {{ color: #c4541f; }}
            .subtitle {{ color: #666; margin-bottom: 20px; font-size: 14px; }}
            .filters {{ background: white; padding: 16px; border-radius: 6px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
            .filter-row {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }}
            .filters input[type="text"], .filters input[type="number"] {{ padding: 9px 12px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }}
            .filters input[name="search"] {{ flex: 1; min-width: 200px; }}
            .filters input[name="max_price"] {{ width: 160px; }}
            .store-row {{ display: flex; gap: 16px; flex-wrap: wrap; align-items: center; padding-top: 8px; border-top: 1px solid #eee; }}
            .store-check {{ font-size: 14px; font-weight: 600; display: flex; align-items: center; gap: 6px; cursor: pointer; }}
            .store-check input {{ width: 16px; height: 16px; cursor: pointer; }}
            .filters button {{ padding: 9px 18px; background: #c4541f; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }}
            .filters a.clear {{ color: #888; text-decoration: none; font-size: 13px; padding: 9px 6px; }}
            table {{ width: 100%; border-collapse: collapse; background: white; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
            th {{ background: #15140f; color: white; padding: 12px; text-align: left; }}
            td {{ padding: 12px; border-bottom: 1px solid #eee; }}
            tr:hover {{ background: #f9f5ea; }}
            .price {{ font-weight: bold; color: #3c5a40; }}
            .best {{ background: #eef2e8; }}
            .empty {{ padding: 40px; text-align: center; color: #999; }}
            .store-tag {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; color: white; }}
            .tag-Jumia {{ background: #f68b1e; }}
            .tag-Justfones {{ background: #2563eb; }}
            .pagination {{ margin-top: 20px; text-align: center; }}
            .pagination a {{ display: inline-block; margin: 0 4px; padding: 8px 14px; background: white; color: #15140f; text-decoration: none; border: 1px solid #ddd; border-radius: 4px; }}
            .pagination a.active {{ background: #c4541f; color: white; border-color: #c4541f; }}
            .pagination a:hover {{ background: #eee; }}
            .count {{ text-align: center; color: #888; font-size: 13px; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <h1>NairaWatch</h1>
        <div class="subtitle">Live prices pulled by your scraper — {len(rows)} phones total across {len(all_stores)} stores — auto-refreshes every 4 hours</div>

        <form class="filters" method="get" action="/">
            <div class="filter-row">
                <input type="text" name="search" placeholder="Search e.g. Samsung, Tecno, iPhone..." value="{search_query}">
                <input type="number" name="max_price" placeholder="Max price (₦)" value="{max_price}">
                <button type="submit">Apply</button>
                <a class="clear" href="/">Clear all</a>
            </div>
            <div class="store-row">
                <span style="font-size:13px; color:#888;">Compare stores:</span>
                {store_checkboxes}
            </div>
        </form>
    """

    if page_rows:
        html += "<table><tr><th>Store</th><th>Product</th><th>Price</th><th>Checked</th></tr>"
        for i, row in enumerate(page_rows):
            row_class = "best" if (start + i == 0) else ""
            price_formatted = f"₦{int(row['Price (NGN)']):,}"
            store_name = row['Store']
            html += f"""
            <tr class="{row_class}">
                <td><span class="store-tag tag-{store_name}">{store_name}</span></td>
                <td>{row['Product']}</td>
                <td class="price">{price_formatted}</td>
                <td>{row['Date Checked']}</td>
            </tr>
            """
        html += "</table>"

        html += '<div class="pagination">'
        if current_page > 1:
            html += f'<a href="{page_link(current_page - 1)}">&laquo; Prev</a>'
        for p in range(1, total_pages + 1):
            active_class = "active" if p == current_page else ""
            html += f'<a class="{active_class}" href="{page_link(p)}">{p}</a>'
        if current_page < total_pages:
            html += f'<a href="{page_link(current_page + 1)}">Next &raquo;</a>'
        html += '</div>'

        html += f'<div class="count">Page {current_page} of {total_pages} — showing {len(page_rows)} of {total_items} matching phones</div>'
    elif not rows:
        html += '<div class="empty">First scrape is running now — this can take 1-2 minutes. Refresh this page shortly.</div>'
    else:
        html += '<div class="empty">No phones match your filters. Try selecting a store or clearing filters.</div>'

    html += "</body></html>"
    return html


# Start the background scraper loop when the app starts
if not os.path.exists(CSV_FILE):
    run_scraper()
    
scraper_thread = threading.Thread(target=background_refresh_loop, daemon=True)
scraper_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("Starting server...")
    app.run(host="0.0.0.0", port=port)
