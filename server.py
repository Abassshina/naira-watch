from flask import Flask, abort, render_template, request
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
from urllib.parse import quote, unquote

from database import (
    initialize_database,
    get_or_create_product,
    save_listing,
    save_price_history,
    clear_all_listings,
    get_all_listings_with_products,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))

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

ACCESSORY_SIGNALS = ["case", "cover", "pouch", "screen protector", "tempered glass", "charger",
                      "cable", "power bank", "protective film", "shockproof", "adapter",
                      "memory card", "sd card", "flash drive", "stylus", "stand", "holder",
                      "mount", "strap", "band"]

WATCH_SIGNALS = ["smart watch", "smartwatch", "watch storm", "watch series", "watch ultra"]

AUDIO_SIGNALS = ["earbud", "earbuds", "earphone", "headphone", "headset",
                  "bluetooth speaker", "wireless speaker", "soundbar"]

KNOWN_BRANDS = ["apple", "iphone", "samsung", "galaxy", "tecno", "infinix", "xiaomi", "redmi",
                "poco", "itel", "nokia", "oukitel", "honor", "huawei", "google", "pixel",
                "nubia", "oppo", "vivo", "realme", "freeyond", "philips"]


def is_accessory(name):
    # If an accessory word appears AFTER a '+' sign, it's a bundled freebie
    # (e.g. "Tablet + Free Case"), not the main product - only check the
    # part of the name before the first '+'.
    main_part = name.split("+")[0].lower()
    return any(signal in main_part for signal in ACCESSORY_SIGNALS)


def is_watch(name):
    main_part = name.split("+")[0].lower()
    return any(signal in main_part for signal in WATCH_SIGNALS)


def is_audio(name):
    main_part = name.split("+")[0].lower()
    return any(signal in main_part for signal in AUDIO_SIGNALS)


def extract_brand(name):
    lower = name.lower()
    for brand in KNOWN_BRANDS:
        if re.search(r"\b" + brand + r"\b", lower):
            normalize_map = {"iphone": "apple", "galaxy": "samsung", "pixel": "google"}
            return normalize_map.get(brand, brand).title()
    return None


def read_price_rows():
    rows = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

    for row in rows:
        row["Brand"] = extract_brand(row["Product"]) or "Other"
        row["ProductSlug"] = slugify_product_name(row["Product"])
        row["PriceValue"] = parse_price(row.get("Price (NGN)", ""))
    return rows


def parse_price(value):
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


def format_naira(value):
    return f"₦{int(value):,}"


def slugify_product_name(name):
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return quote(slug or "product")


def extract_model_key(name):
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
    for brand_word in brand_words:
        lower = re.sub(r"\b" + brand_word + r"\b", " ", lower)

    lower = re.sub(r"[^a-z0-9\s]", " ", lower)
    lower = re.sub(r"\s+", " ", lower).strip()

    words = lower.split()[:2]
    model_part = " ".join(words)

    if not model_part or len(model_part) < 2:
        return None
    return f"{brand}|{model_part}"


app.jinja_env.filters["naira"] = format_naira


def determine_category(name, page_category):
    """
    Returns the real category for a product, overriding the page's nominal
    category (e.g. 'Phones') when the product is actually a watch, earbud,
    case, charger, etc. that was just cross-listed on that page.
    Checked in order of specificity: watch/audio first (distinct categories),
    then generic accessories last.
    """
    if is_watch(name):
        return "Watches"
    if is_audio(name):
        return "Audio"
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

        print(f"Scraper finished: saved {len(unique_results)} results to CSV")

        # ---------- DATABASE SAVE ----------
        # Clear old listings first so prices that no longer exist don't linger.
        # Price history is untouched - it keeps accumulating over time.
        try:
            clear_all_listings()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            db_saved_count = 0
            for item in unique_results:
                brand = extract_brand(item["name"]) or "Other"
                product_id = get_or_create_product(brand, item["name"], item["category"])
                listing_id = save_listing(
                    product_id=product_id,
                    store=item["store"],
                    product_name=item["name"],
                    product_url="",
                    price=item["price"],
                    availability="In Stock",
                    checked_at=now_str,
                )
                save_price_history(listing_id, item["price"], now_str)
                db_saved_count += 1
            print(f"Scraper finished: saved {db_saved_count} results to database")
        except Exception as e:
            print(f"DATABASE SAVE ERROR: {e}")
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
    rows = read_price_rows()

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
                prices = [int(item["Price (NGN)"]) for item in items_sorted]
                lowest_price = prices[0]
                highest_price = prices[-1]
                average_price = sum(prices) // len(prices)
                savings = highest_price - lowest_price
                comparison_groups.append({
                    "key": key,
                    "display_name": items_sorted[0]["Product"],
                    "product_slug": items_sorted[0]["ProductSlug"],
                    "items": items_sorted,
                    "best_price": lowest_price,
                    "highest_price": highest_price,
                    "average_price": average_price,
                    "savings": savings,
                    "store_count": len(stores_in_group),
                })
        comparison_groups.sort(key=lambda g: g["best_price"])

    category_counts = {
        category: len([r for r in rows if r["Category"] == category and r["Store"] in selected_stores])
        for category in all_categories
    }

    return render_template(
        "home.html",
        rows=rows,
        all_stores=all_stores,
        all_categories=all_categories,
        all_brands=all_brands,
        selected_stores=selected_stores,
        selected_category=selected_category,
        selected_brand=selected_brand,
        search_query=search_query,
        max_price=max_price,
        view_mode=view_mode,
        page_rows=page_rows,
        comparison_groups=comparison_groups,
        current_page=current_page,
        total_pages=total_pages,
        total_items=total_items,
        category_counts=category_counts,
        page_link=page_link,
    )



@app.route("/products/<product_slug>")
def product_details(product_slug):
    rows = read_price_rows()
    decoded_slug = unquote(product_slug).strip("/")
    selected_row = next((row for row in rows if unquote(row["ProductSlug"]) == decoded_slug), None)

    if not selected_row:
        abort(404)

    model_key = extract_model_key(selected_row["Product"])
    if model_key:
        product_rows = [
            row for row in rows
            if extract_model_key(row["Product"]) == model_key
        ]
    else:
        product_rows = [
            row for row in rows
            if row["Product"].strip().lower() == selected_row["Product"].strip().lower()
        ]

    product_rows = sorted(product_rows, key=lambda row: row["PriceValue"])
    prices = [row["PriceValue"] for row in product_rows if row["PriceValue"] > 0]

    if not prices:
        abort(404)

    lowest_price = min(prices)
    highest_price = max(prices)
    average_price = round(sum(prices) / len(prices))
    last_updated = max((row["Date Checked"] for row in product_rows if row.get("Date Checked")), default="Not available")

    return render_template(
        "product_details.html",
        product=selected_row,
        product_rows=product_rows,
        lowest_price=lowest_price,
        highest_price=highest_price,
        average_price=average_price,
        last_updated=last_updated,
    )


initialize_database()

scraper_thread = threading.Thread(target=background_refresh_loop, daemon=True)
scraper_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("Starting server...")
    app.run(host="0.0.0.0", port=port)
