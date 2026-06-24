from flask import Flask, request
import csv
import os
import math
import re
import time
import threading
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
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
        if 1000 <= value <= 10_000_000:
            return value
    except ValueError:
        pass
    return None


def fetch_with_hard_timeout(url, hard_seconds=20):
    def do_request():
        return requests.get(url, headers=headers, timeout=(5, 15))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(do_request)
        try:
            return future.result(timeout=hard_seconds)
        except FutureTimeoutError:
            print(f"HARD TIMEOUT: {url} took longer than {hard_seconds}s, abandoning")
            return None
        except Exception as e:
            print(f"FETCH ERROR: {url} -> {e}")
            return None


def scrape_justfones_phones(all_results):
    print("JUSTFONES PHONES: starting")
    for page_num in range(1, 6):
        url = "https://www.justfones.ng/smartphones.html" if page_num == 1 else f"https://www.justfones.ng/smartphones.html?p={page_num}"
        print(f"JUSTFONES PHONES: fetching page {page_num}...")
        response = fetch_with_hard_timeout(url, hard_seconds=20)
        if response is not None:
            print(f"JUSTFONES PHONES: page {page_num} status = {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                products = soup.find_all("li", class_="item product product-item")
                print(f"JUSTFONES PHONES: page {page_num} found {len(products)} cards")
                for product in products:
                    name_tag = product.find("a", class_="product-item-link")
                    price_tag = product.find("span", class_="price")
                    name = name_tag.text.strip() if name_tag else None
                    price_text = price_tag.text.strip() if price_tag else None
                    if name and price_text:
                        price_value = extract_first_price(price_text)
                        if price_value:
                            all_results.append({"name": name, "price": price_value, "store": "Justfones", "category": "Phones"})
        else:
            print(f"JUSTFONES PHONES: page {page_num} skipped")
        time.sleep(1.5)
    print(f"JUSTFONES PHONES: finished, total = {len([r for r in all_results if r['store']=='Justfones' and r['category']=='Phones'])}")


def scrape_pointek_phones(all_results):
    print("POINTEK PHONES: starting")
    for page_num in range(1, 6):
        url = "https://www.pointekonline.com/product-category/mobile-phones/" if page_num == 1 else f"https://www.pointekonline.com/product-category/mobile-phones/page/{page_num}/"
        print(f"POINTEK PHONES: fetching page {page_num}...")
        response = fetch_with_hard_timeout(url, hard_seconds=20)
        if response is not None:
            print(f"POINTEK PHONES: page {page_num} status = {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                # Pointek's shop grid: each product is a <li> inside the products <ul>,
                # containing one link with the product title text and a separate price line.
                products = soup.select("ul.products > li")
                print(f"POINTEK PHONES: page {page_num} found {len(products)} cards")
                for product in products:
                    # The product name sits in a link whose text is "Name ₦price" combined,
                    # but there's also a dedicated price element we can use instead.
                    price_tag = product.find(class_=re.compile(r"^(price|woocommerce-Price-amount)"))
                    title_tag = product.find("h2") or product.find("h3")
                    name = None
                    if title_tag:
                        name = title_tag.get_text(strip=True)
                    if not name:
                        # Check all product links - the image link has no text,
                        # but the title link does. Use the first one with real text.
                        link_tags = product.find_all("a", href=re.compile(r"/product/"))
                        for link_tag in link_tags:
                            full_text = link_tag.get_text(strip=True)
                            if full_text:
                                name = re.split(r"₦", full_text)[0].strip()
                                break
                    price_text = price_tag.get_text(strip=True) if price_tag else None
                    if name and price_text and len(name) > 3:
                        price_value = extract_first_price(price_text)
                        if price_value:
                            all_results.append({"name": name, "price": price_value, "store": "Pointek", "category": "Phones"})
        else:
            print(f"POINTEK PHONES: page {page_num} skipped")
        time.sleep(1.5)
    print(f"POINTEK PHONES: finished, total = {len([r for r in all_results if r['store']=='Pointek' and r['category']=='Phones'])}")


def scrape_phonemart_phones(all_results):
    print("PHONEMART PHONES: starting")
    for page_num in range(1, 6):
        url = "https://www.phonemart.ng/product-category/phones/" if page_num == 1 else f"https://www.phonemart.ng/product-category/phones/page/{page_num}/"
        print(f"PHONEMART PHONES: fetching page {page_num}...")
        response = fetch_with_hard_timeout(url, hard_seconds=20)
        if response is not None:
            print(f"PHONEMART PHONES: page {page_num} status = {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                products = soup.find_all("li", class_=re.compile(r"\bproduct\b"))
                print(f"PHONEMART PHONES: page {page_num} found {len(products)} cards")
                if len(products) == 0 and page_num == 1:
                    print("PHONEMART DEBUG RAW HTML SNIPPET:")
                    total_price_matches = response.text.count("woocommerce-Price-amount")
                    print(f"PHONEMART DEBUG: total price-amount occurrences on page = {total_price_matches}")
                    # Try to find the actual shop loop container, which WooCommerce
                    # themes almost always wrap in a element with 'shop' or 'products' in its id/class
                    idx = response.text.find('class="products')
                    if idx == -1:
                        idx = response.text.find("id=\"main\"")
                    if idx == -1:
                        idx = len(response.text) // 2
                    print(response.text[idx:idx + 2500])
                for product in products:
                    name_tag = product.find("h3") or product.find("h2")
                    price_tag = product.find(class_=re.compile(r"^(price|woocommerce-Price-amount)"))
                    name = name_tag.get_text(strip=True) if name_tag else None
                    price_text = price_tag.get_text(strip=True) if price_tag else None
                    if name and price_text:
                        price_value = extract_first_price(price_text)
                        if price_value:
                            all_results.append({"name": name, "price": price_value, "store": "PhoneMart", "category": "Phones"})
        else:
            print(f"PHONEMART PHONES: page {page_num} skipped")
        time.sleep(1.5)
    print(f"PHONEMART PHONES: finished, total = {len([r for r in all_results if r['store']=='PhoneMart' and r['category']=='Phones'])}")


def run_scraper():
    print("Scraper starting...")
    all_results = []

    scrape_justfones_phones(all_results)
    scrape_pointek_phones(all_results)
    scrape_phonemart_phones(all_results)

    # ---------- SAVE ----------
    if all_results:
        seen = set()
        unique_results = []
        for item in all_results:
            key = (item["store"], item["category"], item["name"])
            if key not in seen:
                seen.add(key)
                unique_results.append(item)

        unique_results.sort(key=lambda x: x["price"])

        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Store", "Category", "Product", "Price (NGN)", "Date Checked"])
            today = datetime.now().strftime("%Y-%m-%d %H:%M")
            for item in unique_results:
                writer.writerow([item["store"], item["category"], item["name"], item["price"], today])

        print(f"Scraper finished: saved {len(unique_results)} results")
    else:
        print("Scraper finished: no results collected")


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
    all_categories = sorted(set(r["Category"] for r in rows)) if rows else []

    search_query = request.args.get("search", "").strip().lower()
    max_price = request.args.get("max_price", "").strip()
    selected_stores = request.args.getlist("store")
    selected_category = request.args.get("category", "").strip()

    if not selected_stores:
        selected_stores = all_stores

    filtered_rows = [r for r in rows if r["Store"] in selected_stores]

    if selected_category:
        filtered_rows = [r for r in filtered_rows if r["Category"] == selected_category]

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

    def page_link(p, category=None):
        params = [f"page={p}"]
        for s in selected_stores:
            params.append(f"store={s}")
        cat = category if category is not None else selected_category
        if cat:
            params.append(f"category={cat}")
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

    category_links = '<a class="cat-link {}" href="{}">All</a>'.format(
        "active" if not selected_category else "", page_link(1, category="")
    )
    for c in all_categories:
        active = "active" if c == selected_category else ""
        count = len([r for r in rows if r["Category"] == c and r["Store"] in selected_stores])
        category_links += f'<a class="cat-link {active}" href="{page_link(1, category=c)}">{c} <span class="cat-count">{count}</span></a>'

    html = f"""
    <html>
    <head>
        <title>NairaWatch — Price Intelligence</title>
        <style>
            * {{ box-sizing: border-box; }}
            body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f4f6f8; margin: 0; color: #1a2332; }}
            .topbar {{ background: #0f1b2d; padding: 18px 32px; display: flex; justify-content: space-between; align-items: center; }}
            .topbar .brand {{ color: #fff; font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }}
            .topbar .brand span {{ color: #18b894; }}
            .topbar .tagline {{ color: #8a99ab; font-size: 12px; }}
            .layout {{ display: flex; max-width: 1400px; margin: 0 auto; }}
            .sidebar {{ width: 220px; background: #fff; min-height: calc(100vh - 60px); padding: 24px 0; border-right: 1px solid #e3e8ee; }}
            .sidebar h4 {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: #8a99ab; padding: 0 20px; margin-bottom: 10px; }}
            .cat-link {{ display: flex; justify-content: space-between; align-items: center; padding: 10px 20px; color: #3c4a5c; text-decoration: none; font-size: 14px; border-left: 3px solid transparent; }}
            .cat-link:hover {{ background: #f4f6f8; }}
            .cat-link.active {{ background: #eef9f6; color: #0f1b2d; font-weight: 600; border-left-color: #18b894; }}
            .cat-count {{ font-size: 11px; color: #8a99ab; background: #f0f2f5; padding: 1px 7px; border-radius: 10px; }}
            .cat-link.active .cat-count {{ background: #d7f0ea; color: #0d7c66; }}
            .main {{ flex: 1; padding: 28px 32px; }}
            .filters {{ background: #fff; border: 1px solid #e3e8ee; border-radius: 8px; padding: 16px 18px; margin-bottom: 20px; }}
            .filter-row {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }}
            .filters input[type="text"], .filters input[type="number"] {{ padding: 9px 12px; border: 1px solid #d4dce4; border-radius: 5px; font-size: 14px; }}
            .filters input[name="search"] {{ flex: 1; min-width: 200px; }}
            .filters input[name="max_price"] {{ width: 150px; }}
            .filters button {{ padding: 9px 18px; background: #0d7c66; color: white; border: none; border-radius: 5px; cursor: pointer; font-size: 14px; font-weight: 600; }}
            .filters a.clear {{ color: #8a99ab; text-decoration: none; font-size: 13px; }}
            .store-row {{ display: flex; gap: 16px; flex-wrap: wrap; align-items: center; padding-top: 10px; border-top: 1px solid #eef1f4; }}
            .store-check {{ font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 6px; color: #3c4a5c; cursor: pointer; }}
            table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #e3e8ee; border-radius: 8px; overflow: hidden; }}
            th {{ background: #0f1b2d; color: #fff; padding: 12px 16px; text-align: left; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }}
            td {{ padding: 13px 16px; border-bottom: 1px solid #eef1f4; font-size: 14px; }}
            tr:hover td {{ background: #fafbfc; }}
            .price {{ font-weight: 700; color: #0d7c66; }}
            .best {{ background: #eef9f6; }}
            .empty {{ padding: 50px; text-align: center; color: #8a99ab; background: #fff; border-radius: 8px; }}
            .store-tag {{ display: inline-block; padding: 3px 9px; border-radius: 4px; font-size: 11px; font-weight: 700; color: white; }}
            .tag-Justfones {{ background: #2563eb; }}
            .tag-Pointek {{ background: #16a34a; }}
            .tag-PhoneMart {{ background: #d97706; }}
            .pagination {{ margin-top: 20px; text-align: center; }}
            .pagination a {{ display: inline-block; margin: 0 3px; padding: 7px 13px; background: white; color: #3c4a5c; text-decoration: none; border: 1px solid #d4dce4; border-radius: 5px; font-size: 13px; }}
            .pagination a.active {{ background: #0d7c66; color: white; border-color: #0d7c66; }}
            .count {{ text-align: center; color: #8a99ab; font-size: 13px; margin-top: 12px; }}
        </style>
    </head>
    <body>
        <div class="topbar">
            <div class="brand">Naira<span>Watch</span></div>
            <div class="tagline">{len(rows)} products tracked across {len(all_stores)} stores — refreshes every 4 hours</div>
        </div>
        <div class="layout">
            <div class="sidebar">
                <h4>Categories</h4>
                {category_links}
            </div>
            <div class="main">
                <form class="filters" method="get" action="/">
                    <input type="hidden" name="category" value="{selected_category}">
                    <div class="filter-row">
                        <input type="text" name="search" placeholder="Search products..." value="{search_query}">
                        <input type="number" name="max_price" placeholder="Max price (₦)" value="{max_price}">
                        <button type="submit">Apply</button>
                        <a class="clear" href="/">Clear</a>
                    </div>
                    <div class="store-row">
                        <span style="font-size:13px; color:#8a99ab;">Stores:</span>
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

        html += f'<div class="count">Page {current_page} of {total_pages} — {total_items} matching products</div>'
    elif not rows:
        html += '<div class="empty">First scrape is running — this can take a couple of minutes. Refresh shortly.</div>'
    else:
        html += '<div class="empty">No products match your filters.</div>'

    html += "</div></div></body></html>"
    return html


scraper_thread = threading.Thread(target=background_refresh_loop, daemon=True)
scraper_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("Starting server...")
    app.run(host="0.0.0.0", port=port)
