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


# ---------- BRAND & MODEL GROUPING (tested against real product names) ----------

ACCESSORY_SIGNALS = ["case", "cover", "screen protector", "tempered glass", "charger",
                      "cable", "earphone", "headphone", "power bank", "protective film", "shockproof"]

KNOWN_BRANDS = ["apple", "iphone", "samsung", "galaxy", "tecno", "infinix", "xiaomi", "redmi",
                "poco", "itel", "nokia", "oukitel", "honor", "huawei", "google", "pixel",
                "nubia", "oppo", "vivo", "realme", "freeyond", "philips"]


def is_accessory(name):
    lower = name.lower()
    return any(signal in lower for signal in ACCESSORY_SIGNALS)


def extract_brand(name):
    lower = name.lower()
    for brand in KNOWN_BRANDS:
        if re.search(r"\b" + brand + r"\b", lower):
            normalize_map = {"iphone": "apple", "galaxy": "samsung", "pixel": "google"}
            return normalize_map.get(brand, brand).title()
    return None


def determine_category(name, page_category):
    """
    Returns the real category for a product, overriding the page's nominal
    category (e.g. 'Phones') to 'Phone Accessories' when the product is
    actually a case, pouch, charger, etc. that was just listed on that page.
    """
    if is_accessory(name):
        return f"{page_category} Accessories" if page_category in ("Phones", "Laptops") else "Accessories"
    return page_category
    if is_accessory(name):
        return None
    brand = extract_brand(name)
    if not brand:
        return None

    lower = " " + name.lower() + " "
    lower = re.sub(r'\d+\.\d+["\']?\s*(inch(es)?)?', " ", lower)

    stop_words = [
        r"\d+\s*gb", r"\d+\s*mb", r"\d+\s*tb", r"ram", r"rom",
        r"dual\s*sim", r"single\s*sim", r"\bsim\b", r"\be[\-\s]?sim\b",
        r"\b5g\b", r"\b4g\b", r"\blte\b", r"android\s*\d+",
        # Multi-word colors MUST come before their single-word components
        r"light\s*blue", r"dark\s*blue", r"light\s*green", r"dark\s*green",
        r"\bblack\b", r"\bblue\b", r"\bgold\b", r"\bwhite\b", r"\bsilver\b",
        r"\bgreen\b", r"\bgrey\b", r"\bgray\b", r"\bpurple\b", r"\borange\b",
        r"\bred\b", r"\bpink\b", r"\bdusk\b", r"\bvelvet\b",
        r"\bdisplay\b", r"\bamoled\b", r"\bips\b", r"\bhd\+?\b", r"\bfhd\+?\b",
        r"\d+hz\b", r"\(.*?\)",
    ]
    for pattern in stop_words:
        lower = re.sub(pattern, " ", lower)

    brand_words = ["apple", "iphone", "samsung", "galaxy", "tecno", "infinix", "xiaomi",
                   "redmi", "poco", "itel", "nokia", "oukitel", "honor", "huawei",
                   "google", "pixel", "nubia", "oppo", "vivo", "realme"]
    for bw in brand_words:
        lower = re.sub(r"\b" + bw + r"\b", " ", lower)

    lower = re.sub(r"[^a-z0-9\s]", " ", lower)
    lower = re.sub(r"\s+", " ", lower).strip()

    words = lower.split()[:2]
    model_part = " ".join(words)

    if not model_part or len(model_part) < 2:
        return None
    return f"{brand}|{model_part}"


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


def scrape_justfones_category(all_results, url_slug, category_label):
    """Generic scraper for any Justfones (Magento) category page."""
    tag = f"JUSTFONES {category_label.upper()}"
    print(f"{tag}: starting")
    page_num = 1
    max_safety_pages = 40
    while page_num <= max_safety_pages:
        url = f"https://www.justfones.ng/{url_slug}.html" if page_num == 1 else f"https://www.justfones.ng/{url_slug}.html?p={page_num}"
        print(f"{tag}: fetching page {page_num}...")
        response = fetch_with_hard_timeout(url, hard_seconds=20)
        if response is not None:
            print(f"{tag}: page {page_num} status = {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                products = soup.find_all("li", class_="item product product-item")
                print(f"{tag}: page {page_num} found {len(products)} cards")
                if len(products) == 0:
                    print(f"{tag}: page {page_num} empty, reached the end")
                    break
                for product in products:
                    name_tag = product.find("a", class_="product-item-link")
                    price_tag = product.find("span", class_="price")
                    name = name_tag.text.strip() if name_tag else None
                    price_text = price_tag.text.strip() if price_tag else None
                    if name and price_text:
                        price_value = extract_first_price(price_text)
                        if price_value:
                            all_results.append({"name": name, "price": price_value, "store": "Justfones", "category": determine_category(name, category_label)})
            else:
                print(f"{tag}: page {page_num} not OK, stopping")
                break
        else:
            print(f"{tag}: page {page_num} skipped (timeout), stopping")
            break
        page_num += 1
        time.sleep(1.5)
    print(f"{tag}: finished, total = {len([r for r in all_results if r['store']=='Justfones' and r['category']==category_label])}")


def scrape_pointek_category(all_results, url_slug, category_label):
    """Generic scraper for any Pointek (WooCommerce) category page."""
    tag = f"POINTEK {category_label.upper()}"
    print(f"{tag}: starting")
    page_num = 1
    max_safety_pages = 40
    while page_num <= max_safety_pages:
        url = f"https://www.pointekonline.com/{url_slug}/" if page_num == 1 else f"https://www.pointekonline.com/{url_slug}/page/{page_num}/"
        print(f"{tag}: fetching page {page_num}...")
        response = fetch_with_hard_timeout(url, hard_seconds=20)
        if response is not None:
            print(f"{tag}: page {page_num} status = {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                # Pointek's shop grid: each product is a <li> inside the products <ul>,
                # containing one link with the product title text and a separate price line.
                products = soup.select("ul.products > li")
                print(f"{tag}: page {page_num} found {len(products)} cards")
                if len(products) == 0:
                    print(f"{tag}: page {page_num} empty, reached the end")
                    break
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
                            all_results.append({"name": name, "price": price_value, "store": "Pointek", "category": determine_category(name, category_label)})
            else:
                print(f"{tag}: page {page_num} not OK, stopping")
                break
        else:
            print(f"{tag}: page {page_num} skipped (timeout), stopping")
            break
        page_num += 1
        time.sleep(1.5)
    print(f"{tag}: finished, total = {len([r for r in all_results if r['store']=='Pointek' and r['category']==category_label])}")


def scrape_phonemart_category(all_results, url_slug, category_label):
    """Generic scraper for any PhoneMart (WoodMart theme) category page."""
    tag = f"PHONEMART {category_label.upper()}"
    print(f"{tag}: starting")
    page_num = 1
    max_safety_pages = 40
    while page_num <= max_safety_pages:
        url = f"https://www.phonemart.ng/product-category/{url_slug}/" if page_num == 1 else f"https://www.phonemart.ng/product-category/{url_slug}/page/{page_num}/"
        print(f"{tag}: fetching page {page_num}...")
        response = fetch_with_hard_timeout(url, hard_seconds=20)
        if response is not None:
            print(f"{tag}: page {page_num} status = {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                # PhoneMart uses the WoodMart theme, which wraps each product in a
                # <div class="product-grid-item ... product ...">, not an <li> like
                # standard WooCommerce themes. Match div tags with "product" as a
                # standalone class word.
                products = soup.find_all("div", class_=lambda c: c and "product" in c.split())
                print(f"{tag}: page {page_num} found {len(products)} cards")
                if len(products) == 0:
                    print(f"{tag}: page {page_num} empty, reached the end")
                    break
                for product in products:
                    name_tag = product.find("h3") or product.find("h2")
                    price_tag = product.find(class_=re.compile(r"^(price|woocommerce-Price-amount)"))
                    name = None
                    if name_tag:
                        name = name_tag.get_text(strip=True)
                    if not name:
                        # Fall back to the product page link's "title" attribute or text
                        link_tag = product.find("a", class_="woocommerce-LoopProduct-link") or product.find("a", title=True)
                        if link_tag and link_tag.get("title"):
                            name = link_tag.get("title").strip()
                    price_text = price_tag.get_text(strip=True) if price_tag else None
                    if name and price_text and len(name) > 3:
                        price_value = extract_first_price(price_text)
                        if price_value:
                            all_results.append({"name": name, "price": price_value, "store": "PhoneMart", "category": determine_category(name, category_label)})
            else:
                print(f"{tag}: page {page_num} not OK, stopping")
                break
        else:
            print(f"{tag}: page {page_num} skipped (timeout), stopping")
            break
        page_num += 1
        time.sleep(1.5)
    print(f"{tag}: finished, total = {len([r for r in all_results if r['store']=='PhoneMart' and r['category']==category_label])}")


def run_scraper():
    print("Scraper starting...")
    all_results = []

    # Justfones (Magento)
    scrape_justfones_category(all_results, "smartphones", "Phones")
    scrape_justfones_category(all_results, "laptops", "Laptops")
    scrape_justfones_category(all_results, "tablets", "Tablets")
    scrape_justfones_category(all_results, "smartwatches", "Watches")

    # Pointek (WooCommerce)
    scrape_pointek_category(all_results, "product-category/mobile-phones", "Phones")
    scrape_pointek_category(all_results, "product-category/computers", "Laptops")
    scrape_pointek_category(all_results, "product-category/tablets", "Tablets")
    scrape_pointek_category(all_results, "product-category/accessories/smart-watch", "Watches")
    scrape_pointek_category(all_results, "product-category/accessories", "Accessories")

    # PhoneMart (WoodMart/WooCommerce)
    scrape_phonemart_category(all_results, "phones", "Phones")
    scrape_phonemart_category(all_results, "laptops", "Laptops")
    scrape_phonemart_category(all_results, "accessories", "Accessories")

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

    # Tag each row with its detected brand (used for the brand filter and grouping)
    for r in rows:
        r["Brand"] = extract_brand(r["Product"]) or "Other"

    all_brands = sorted(set(r["Brand"] for r in rows if r["Brand"] != "Other"))

    search_query = request.args.get("search", "").strip().lower()
    max_price = request.args.get("max_price", "").strip()
    selected_stores = request.args.getlist("store")
    selected_category = request.args.get("category", "").strip()
    selected_brand = request.args.get("brand", "").strip()
    view_mode = request.args.get("view", "list").strip()  # "list" or "compare"

    if not selected_stores:
        selected_stores = all_stores

    filtered_rows = [r for r in rows if r["Store"] in selected_stores]

    if selected_category:
        filtered_rows = [r for r in filtered_rows if r["Category"] == selected_category]

    if selected_brand:
        filtered_rows = [r for r in filtered_rows if r["Brand"] == selected_brand]

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

    def page_link(p, category=None, brand=None, view=None):
        params = [f"page={p}"]
        for s in selected_stores:
            params.append(f"store={s}")
        cat = category if category is not None else selected_category
        if cat:
            params.append(f"category={cat}")
        br = brand if brand is not None else selected_brand
        if br:
            params.append(f"brand={br}")
        v = view if view is not None else view_mode
        if v and v != "list":
            params.append(f"view={v}")
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

    brand_options = '<option value="">All brands</option>'
    for b in all_brands:
        selected_attr = "selected" if b == selected_brand else ""
        brand_options += f'<option value="{b}" {selected_attr}>{b}</option>'

    # ---------- BUILD COMPARISON GROUPS (for "Compare" view) ----------
    comparison_groups = []
    if view_mode == "compare":
        groups = {}
        group_order = []
        for r in filtered_rows:
            model_key = extract_model_key(r["Product"])
            if not model_key:
                continue
            if model_key not in groups:
                groups[model_key] = []
                group_order.append(model_key)
            groups[model_key].append(r)

        # Only keep groups with 2+ stores represented - that's the whole point of "compare"
        for key in group_order:
            items = groups[key]
            stores_in_group = set(item["Store"] for item in items)
            if len(stores_in_group) >= 2:
                items_sorted = sorted(items, key=lambda x: int(x["Price (NGN)"]))
                comparison_groups.append({
                    "key": key,
                    "display_name": items_sorted[0]["Product"],
                    "items": items_sorted,
                    "best_price": int(items_sorted[0]["Price (NGN)"]),
                    "store_count": len(stores_in_group),
                })
        comparison_groups.sort(key=lambda g: g["best_price"])

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
            .filters select {{ padding: 9px 12px; border: 1px solid #d4dce4; border-radius: 5px; font-size: 14px; background: white; }}
            .view-toggle {{ display: flex; gap: 0; border: 1px solid #d4dce4; border-radius: 5px; overflow: hidden; }}
            .view-toggle a {{ padding: 8px 16px; font-size: 13px; color: #3c4a5c; text-decoration: none; background: white; }}
            .view-toggle a.active {{ background: #0d7c66; color: white; font-weight: 600; }}
            .compare-card {{ background: #fff; border: 1px solid #e3e8ee; border-radius: 8px; margin-bottom: 14px; overflow: hidden; }}
            .compare-card-header {{ padding: 14px 18px; background: #fafbfc; border-bottom: 1px solid #eef1f4; display: flex; justify-content: space-between; align-items: center; }}
            .compare-card-title {{ font-weight: 700; font-size: 15px; color: #0f1b2d; }}
            .compare-card-meta {{ font-size: 12px; color: #8a99ab; }}
            .compare-row {{ display: flex; justify-content: space-between; align-items: center; padding: 11px 18px; border-bottom: 1px solid #f4f6f8; }}
            .compare-row:last-child {{ border-bottom: none; }}
            .compare-row.cheapest {{ background: #eef9f6; }}
            .compare-row .left {{ display: flex; align-items: center; gap: 10px; }}
            .compare-row .product-name {{ font-size: 13px; color: #3c4a5c; max-width: 420px; }}
            .compare-badge {{ font-size: 10px; font-weight: 700; color: #0d7c66; background: #d7f0ea; padding: 2px 7px; border-radius: 10px; }}
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
                    <input type="hidden" name="view" value="{view_mode}">
                    <div class="filter-row">
                        <input type="text" name="search" placeholder="Search products..." value="{search_query}">
                        <select name="brand">
                            {brand_options}
                        </select>
                        <input type="number" name="max_price" placeholder="Max price (₦)" value="{max_price}">
                        <button type="submit">Apply</button>
                        <a class="clear" href="/">Clear</a>
                        <div class="view-toggle">
                            <a class="{'active' if view_mode == 'list' else ''}" href="{page_link(1, view='list')}">List</a>
                            <a class="{'active' if view_mode == 'compare' else ''}" href="{page_link(1, view='compare')}">Compare</a>
                        </div>
                    </div>
                    <div class="store-row">
                        <span style="font-size:13px; color:#8a99ab;">Stores:</span>
                        {store_checkboxes}
                    </div>
                </form>
    """

    if view_mode == "compare":
        if comparison_groups:
            html += f'<div class="count" style="margin-bottom:14px;">{len(comparison_groups)} products with prices from 2+ stores</div>'
            for group in comparison_groups:
                html += f"""
                <div class="compare-card">
                    <div class="compare-card-header">
                        <span class="compare-card-title">{group['display_name']}</span>
                        <span class="compare-card-meta">{group['store_count']} stores</span>
                    </div>
                """
                for i, item in enumerate(group["items"]):
                    row_class = "cheapest" if i == 0 else ""
                    badge = '<span class="compare-badge">BEST PRICE</span>' if i == 0 else ""
                    price_formatted = f"₦{int(item['Price (NGN)']):,}"
                    store_name = item["Store"]
                    html += f"""
                    <div class="compare-row {row_class}">
                        <div class="left">
                            <span class="store-tag tag-{store_name}">{store_name}</span>
                            <span class="product-name">{item['Product']}</span>
                            {badge}
                        </div>
                        <span class="price">{price_formatted}</span>
                    </div>
                    """
                html += "</div>"
        elif not rows:
            html += '<div class="empty">First scrape is running — this can take a couple of minutes. Refresh shortly.</div>'
        else:
            html += '<div class="empty">No matching products found across 2+ stores. Try clearing filters, or switch to List view.</div>'
    else:
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
