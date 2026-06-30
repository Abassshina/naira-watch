from flask import Flask, Response, abort, render_template, request
import csv
import os
import math
import re
import time
import threading
import requests
from html import escape
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime
from urllib.parse import quote, unquote, urljoin

from database import (
    initialize_database,
    get_or_create_product,
    get_product,
    save_listing,
    save_price_history,
    clear_all_listings,
    get_all_listings_with_products,
    get_price_history_for_product,
    get_price_stats_for_product,
    get_first_recorded_price_for_product,
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


VALID_AVAILABILITY_VALUES = ["In Stock", "Out of Stock", "Pre-order", "Coming Soon", "Unknown"]


def normalize_availability(raw_text):
    """
    Normalizes any retailer's raw stock-status text/signal into exactly
    one of: 'In Stock', 'Out of Stock', 'Pre-order', 'Coming Soon', 'Unknown'.
    Checked in order of specificity - 'out of stock'/'sold out' must be
    checked before generic 'available' substring matches, since some
    sites phrase things like "Currently unavailable" vs "Available".
    """
    if not raw_text:
        return "Unknown"

    text = raw_text.strip().lower()

    if any(phrase in text for phrase in ["coming soon", "notify me", "launching soon"]):
        return "Coming Soon"
    if any(phrase in text for phrase in ["pre-order", "preorder", "pre order"]):
        return "Pre-order"
    if any(phrase in text for phrase in ["out of stock", "sold out", "unavailable", "not available", "read more"]):
        return "Out of Stock"
    if any(phrase in text for phrase in ["in stock", "available", "add to cart", "select options", "buy now"]):
        return "In Stock"

    return "Unknown"


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
        row["ImageUrl"] = resolve_product_image_url(row)
        row["ProductUrl"] = str(row.get("Product URL") or "").strip()
        row["Availability"] = normalize_availability(row.get("Availability"))
    return rows


def resolve_product_image_url(row):
    """Returns a real product image when available, otherwise a stable placeholder."""
    for key in ("Image URL", "ImageUrl", "image_url", "image"):
        image_url = str(row.get(key) or "").strip()
        if image_url:
            return image_url
    return placeholder_image_url(row.get("Category", "Products"))


def placeholder_image_url(category):
    category_slug = slugify_product_name(category or "Products")
    return f"/images/placeholders/{category_slug}.svg"


def parse_price(value):
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


def format_naira(value):
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return "Price unavailable"
    if amount <= 0:
        return "Price unavailable"
    return f"₦{amount:,}"


def compute_price_change(current_price, first_price):
    """
    Returns a dict describing how price has moved since the first
    recorded entry, used to render the green/red/grey change indicator.
    Returns None if there's nothing to compare against.
    """
    if current_price is None or first_price is None:
        return None

    difference = current_price - first_price

    if difference < 0:
        return {"direction": "down", "amount": abs(difference), "css_class": "price-down"}
    elif difference > 0:
        return {"direction": "up", "amount": difference, "css_class": "price-up"}
    else:
        return {"direction": "none", "amount": 0, "css_class": "price-flat"}


def slugify_product_name(name):
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return quote(slug or "product")


def extract_ram(name):
    """
    Returns RAM as a normalized string like '4GB', or None if not found.
    Looks for explicit 'RAM' labeling first, then falls back to the
    common 'XGB + YGB' shorthand. Different stores write this in
    different orders (some RAM-first like '4GB+64GB', others
    storage-first like '64GB+4GB'), so rather than assume position, we
    treat whichever of the two numbers is smaller as RAM - a safe
    assumption since RAM is essentially always smaller than storage on
    real phones.
    """
    lower = name.lower()
    match = re.search(r"(\d+)\s*gb\s*ram", lower)
    if match:
        return f"{match.group(1)}GB"
    match = re.search(r"ram[:\s]+(\d+)\s*gb", lower)
    if match:
        return f"{match.group(1)}GB"
    match = re.search(r"(\d+)\s*gb\s*[+/]\s*(\d+)\s*(gb|tb)", lower)
    if match:
        first_amount = int(match.group(1))
        second_amount = int(match.group(2))
        if first_amount <= 24 or second_amount <= 24:
            smaller = min(first_amount, second_amount)
            return f"{smaller}GB"
    return None


def extract_storage(name):
    """
    Returns storage as a normalized string like '128GB' or '1TB', or None.
    Prefers explicit 'ROM' labeling, then the 'XGB+YGB' shorthand
    (storage = second number), then falls back to any remaining size
    mentioned that wasn't already claimed as RAM.
    """
    lower = name.lower()

    match = re.search(r"(\d+)\s*(gb|tb)\s*rom", lower)
    if match:
        return f"{match.group(1)}{match.group(2).upper()}"
    match = re.search(r"rom[:\s]+(\d+)\s*(gb|tb)", lower)
    if match:
        return f"{match.group(1)}{match.group(2).upper()}"

    match = re.search(r"(\d+)\s*gb\s*[+/]\s*(\d+)\s*(gb|tb)", lower)
    if match:
        first_amount = int(match.group(1))
        second_amount = int(match.group(2))
        if first_amount <= 24 or second_amount <= 24:
            larger = max(first_amount, second_amount)
            return f"{larger}{match.group(3).upper()}"

    sizes = re.findall(r"(\d+)\s*(gb|tb)\b", lower)
    ram = extract_ram(name)
    if len(sizes) >= 1:
        for amount, unit in sizes:
            candidate = f"{amount}{unit.upper()}"
            if candidate != ram:
                return candidate
    return None


# Noise phrases/words that should never affect product matching, since
# they describe the listing/sale terms rather than the product itself.
MATCH_NOISE_PHRASES = [
    "official warranty", "brand new", "factory unlocked", "global version",
    "nigerian version", "new arrival", "latest model", "android phone",
    "mobile phone", "smartphone", "dual sim", "single sim", "unlocked",
]
MATCH_NOISE_WORDS = ["lte", "4g", "5g", "nfc", "esim", "sim", "android", "mobile", "smart", "phone", "with"]
MATCH_COLOR_PHRASES = ["light blue", "dark blue", "light green", "dark green", "rose gold"]
MATCH_COLORS = ["black", "blue", "gold", "white", "silver", "green", "grey", "gray",
                "purple", "orange", "red", "pink", "dusk", "velvet", "charcoal",
                "midnight", "starlight", "graphite", "titanium", "lavender"]
MATCH_BRAND_WORDS = ["apple", "iphone", "samsung", "galaxy", "tecno", "infinix", "xiaomi",
                      "redmi", "poco", "itel", "nokia", "oukitel", "honor", "huawei",
                      "google", "pixel", "nubia", "oppo", "vivo", "realme",
                      "hp", "dell", "lenovo", "asus", "acer", "msi", "microsoft", "lg"]


def clean_model_text(name):
    """
    Strips noise phrases, RAM/storage numbers, colors, and brand words,
    leaving just the model identifier text (e.g. 'a06', 'note 40 pro').

    Only the part of the name BEFORE a '+' bundle separator is used,
    since text after '+' is typically a bundled freebie (e.g. "+ Free
    Case") rather than part of the actual product/model name.
    """
    main_part = name.split("+")[0]
    lower = " " + main_part.lower() + " "

    # Strip manufacturer-internal SKU/model codes that are hyphenated onto
    # the model name (e.g. "A06-A065F" -> "A06", "Spark 50-KN4" -> "Spark 50").
    # These codes are store-specific noise, not part of how shoppers
    # actually search for or compare the model.
    lower = re.sub(r"-[a-z]{1,3}\d{1,4}[a-z]?\b", " ", lower)

    for phrase in MATCH_NOISE_PHRASES:
        lower = lower.replace(phrase, " ")

    lower = re.sub(r'\d+\.\d+["\']?\s*(inch(es)?)?', " ", lower)
    lower = re.sub(r"\d+\s*(gb|mb|tb)\s*ram", " ", lower)
    lower = re.sub(r"\d+\s*(gb|tb)\s*rom", " ", lower)
    lower = re.sub(r"ram[:\s]+\d+\s*(gb|mb)", " ", lower)
    lower = re.sub(r"rom[:\s]+\d+\s*(gb|tb)", " ", lower)
    lower = re.sub(r"\d+\s*(gb|tb)\b", " ", lower)
    lower = re.sub(r"\bram\b", " ", lower)
    lower = re.sub(r"\brom\b", " ", lower)

    for word in MATCH_NOISE_WORDS:
        lower = re.sub(r"\b" + re.escape(word) + r"\b", " ", lower)

    for phrase in MATCH_COLOR_PHRASES:
        lower = lower.replace(phrase, " ")
    for color in MATCH_COLORS:
        lower = re.sub(r"\b" + color + r"\b", " ", lower)

    for bw in MATCH_BRAND_WORDS:
        lower = re.sub(r"\b" + bw + r"\b", " ", lower)

    lower = re.sub(r"[^a-z0-9\s]", " ", lower)
    lower = re.sub(r"\s+", " ", lower).strip()
    return lower


def extract_model(name):
    """Returns up to 3 significant words of the cleaned model text."""
    cleaned = clean_model_text(name)
    words = cleaned.split()[:3]
    return " ".join(words) if words else None


def detect_device_type(name, category_hint=None):
    """Returns the device type bucket used for matching."""
    if is_watch(name):
        return "Watch"
    if is_audio(name):
        return "Audio"
    if is_accessory(name):
        return "Accessory"
    if category_hint:
        hint = category_hint.lower()
        if "laptop" in hint:
            return "Laptop"
        if "tablet" in hint:
            return "Tablet"
        if "watch" in hint:
            return "Watch"
        if "audio" in hint:
            return "Audio"
        if "accessor" in hint:
            return "Accessory"
        if "phone" in hint:
            return "Phone"
    return "Other"


def extract_product_attributes(name, category_hint=None):
    """
    Parses a product name into its matching attributes (brand, model,
    RAM, storage, device type) in a single pass. Returns None for
    accessories or names with no recognizable brand.

    To add a new matching attribute later (color, processor, screen
    size, etc.): write an extract_<attribute>(name) function following
    the pattern of extract_ram/extract_storage above, then include its
    result in this dict and in build_group_key() below.
    """
    if is_accessory(name):
        return None

    brand = extract_brand(name)
    if not brand:
        return None

    model = extract_model(name)
    if not model:
        return None

    return {
        "brand": brand,
        "model": model,
        "ram": extract_ram(name),
        "storage": extract_storage(name),
        "device_type": detect_device_type(name, category_hint),
    }


def build_group_key(attributes):
    """
    Primary grouping key (brand+model+storage+device_type), deliberately
    excluding RAM so that listings with unspecified RAM can still join a
    group - RAM is reconciled separately in group_products_by_match().
    """
    return f"{attributes['brand']}|{attributes['model']}|{attributes['storage'] or '—'}|{attributes['device_type']}"


def build_canonical_key(name, category_hint=None):
    """Full display key including RAM, e.g. 'Samsung|Galaxy A06|4GB|64GB|Phone'."""
    attributes = extract_product_attributes(name, category_hint)
    if not attributes:
        return None
    ram = attributes["ram"] or "—"
    return f"{attributes['brand']}|{attributes['model']}|{ram}|{attributes['storage'] or '—'}|{attributes['device_type']}"


def group_products_by_match(items, name_field="Product", category_field="Category"):
    """
    Groups product rows into matching configurations (same brand, model,
    storage, and device type). RAM is treated as a soft signal: items
    with unspecified RAM act as a wildcard and join whichever single
    RAM variant exists for that model+storage. If multiple distinct RAM
    variants exist for the same model+storage, RAM-unspecified items are
    NOT guessed into one of them - they form their own group instead,
    to avoid incorrectly merging different configurations.

    Returns a list of groups, each a list of the original row dicts.
    Rows that can't be parsed (no brand, or accessories) are dropped.
    """
    primary_buckets = {}
    for item in items:
        name = item[name_field]
        category_hint = item.get(category_field) if category_field else None
        attributes = extract_product_attributes(name, category_hint)
        if not attributes:
            continue
        primary_key = build_group_key(attributes)
        primary_buckets.setdefault(primary_key, []).append((item, attributes))

    final_groups = []
    for primary_key, bucketed_items in primary_buckets.items():
        ram_known_groups = {}
        ram_unknown_items = []

        for item, attributes in bucketed_items:
            if attributes["ram"]:
                ram_known_groups.setdefault(attributes["ram"], []).append(item)
            else:
                ram_unknown_items.append(item)

        if not ram_known_groups:
            if ram_unknown_items:
                final_groups.append(ram_unknown_items)
        elif len(ram_known_groups) == 1:
            only_group = next(iter(ram_known_groups.values()))
            only_group.extend(ram_unknown_items)
            final_groups.append(only_group)
        else:
            for group in ram_known_groups.values():
                final_groups.append(group)
            if ram_unknown_items:
                final_groups.append(ram_unknown_items)

    return final_groups


app.jinja_env.filters["naira"] = format_naira


def availability_css_class(value):
    return "status-" + (value or "Unknown").lower().replace(" ", "-")


AVAILABILITY_EMOJI = {
    "In Stock": "🟢",
    "Out of Stock": "🔴",
    "Pre-order": "🟠",
    "Coming Soon": "🟡",
    "Unknown": "⚪",
}


def availability_emoji(value):
    return AVAILABILITY_EMOJI.get(value, "⚪")


app.jinja_env.filters["availability_css"] = availability_css_class
app.jinja_env.filters["availability_emoji"] = availability_emoji


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


def normalize_image_url(image_url, page_url):
    """
    Ensures an image URL is fully absolute, using Python's standard
    urljoin to correctly resolve every relative URL form a site might
    use: absolute 'https://...', protocol-relative '//cdn...',
    site-root-relative '/wp-content/...', and parent-relative
    '../../wp-content/...' paths (this last form needs the FULL page
    URL it was found on, not just the domain, to resolve correctly -
    that's the actual bug this replaces: gluing the domain directly onto
    a '../' path produces a URL that looks valid but points nowhere).
    """
    if not image_url:
        return None
    if image_url.startswith("data:image"):
        return None
    return urljoin(page_url, image_url)


def resolve_link(href, page_url):
    """
    Resolves a product link's href into a full absolute URL, using the
    same urljoin logic as normalize_image_url (it works identically for
    any relative URL, not just images) - kept as a separate name so the
    intent is clear at each call site.
    """
    if not href:
        return None
    return urljoin(page_url, href)


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
                    product_url = resolve_link(name_tag.get("href"), url) if name_tag else None

                    image_url = None
                    img_tag = product.find("img")
                    if img_tag:
                        # Magento sometimes lazy-loads images, with the real URL
                        # sitting in data-src/data-lazy-src instead of src.
                        raw_image_url = (
                            img_tag.get("src")
                            or img_tag.get("data-src")
                            or img_tag.get("data-lazy-src")
                        )
                        if raw_image_url and raw_image_url.startswith("data:image"):
                            raw_image_url = img_tag.get("data-src") or img_tag.get("data-lazy-src")
                        image_url = normalize_image_url(raw_image_url, url)

                    # Out-of-stock Justfones items show "Out of stock" text and
                    # have no price/Add to Cart button at all. In-stock items
                    # show a price and an "Add to Cart" button.
                    card_text = product.get_text(" ", strip=True)
                    if "out of stock" in card_text.lower():
                        availability = "Out of Stock"
                    elif price_text:
                        availability = "In Stock"
                    else:
                        availability = "Unknown"

                    if name and (price_text or availability == "Out of Stock"):
                        price_value = extract_first_price(price_text) if price_text else 0
                        if price_value or availability == "Out of Stock":
                            all_results.append({
                                "name": name,
                                "price": price_value or 0,
                                "store": "Justfones",
                                "category": determine_category(name, category_label),
                                "image_url": image_url,
                                "product_url": product_url,
                                "availability": availability,
                            })
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

                    product_url = None
                    any_product_link = product.find("a", href=re.compile(r"/product/"))
                    if any_product_link:
                        product_url = resolve_link(any_product_link.get("href"), url)

                    # WooCommerce's add-to-cart button text is a reliable stock
                    # signal: "Add to cart"/"Select options" means in stock,
                    # "Read more" is WooCommerce's default out-of-stock button text.
                    cart_button = product.find(class_=re.compile(r"add_to_cart_button|product_type_"))
                    availability_text = cart_button.get_text(strip=True) if cart_button else None
                    if not availability_text:
                        card_text = product.get_text(" ", strip=True).lower()
                        if "out of stock" in card_text:
                            availability_text = "Out of Stock"
                        elif "pre-order" in card_text or "preorder" in card_text:
                            availability_text = "Pre-order"
                    availability = normalize_availability(availability_text)

                    image_url = None
                    img_tag = product.find("img")
                    if img_tag:
                        raw_image_url = (
                            img_tag.get("src")
                            or img_tag.get("data-src")
                            or img_tag.get("data-lazy-src")
                        )
                        if raw_image_url and raw_image_url.startswith("data:image"):
                            raw_image_url = img_tag.get("data-src") or img_tag.get("data-lazy-src")
                        image_url = normalize_image_url(raw_image_url, url)


                    if name and price_text and len(name) > 3:
                        price_value = extract_first_price(price_text)
                        if price_value:
                            all_results.append({
                                "name": name,
                                "price": price_value,
                                "store": "Pointek",
                                "category": determine_category(name, category_label),
                                "image_url": image_url,
                                "product_url": product_url,
                                "availability": availability,
                            })
            else:
                print(f"{tag}: page {page_num} not OK, stopping")
                break
        else:
            print(f"{tag}: page {page_num} skipped (timeout), stopping")
            break
        page_num += 1
        time.sleep(1.5)
    print(f"{tag}: finished, total = {len([r for r in all_results if r['store']=='Pointek' and r['category']==category_label])}")


def scrape_tokkahub_category(all_results, url_slug, category_label):
    """
    Generic scraper for any Tokka Hub (WoodMart theme, same as PhoneMart)
    category page. Reuses the same div-based product selector already
    proven to work for PhoneMart, since both sites use the same theme.
    """
    tag = f"TOKKAHUB {category_label.upper()}"
    print(f"{tag}: starting")
    page_num = 1
    max_safety_pages = 40
    previous_page_fingerprint = None
    consecutive_repeats = 0
    while page_num <= max_safety_pages:
        if page_num == 1:
            url = f"https://tokkahub.com/product-category/{url_slug}/"
        else:
            url = f"https://tokkahub.com/product-category/{url_slug}/page/{page_num}/"
        print(f"{tag}: fetching page {page_num}...")
        response = fetch_with_hard_timeout(url, hard_seconds=20)
        used_fallback = False
        if response is None and page_num > 1:
            # The /page/N/ path style redirect-loops on some WooCommerce
            # permalink configurations - fall back to the older
            # ?paged=N query-string style WordPress also supports.
            fallback_url = f"https://tokkahub.com/product-category/{url_slug}/?paged={page_num}"
            print(f"{tag}: retrying page {page_num} with ?paged= fallback...")
            response = fetch_with_hard_timeout(fallback_url, hard_seconds=20)
            used_fallback = True
        if response is not None:
            print(f"{tag}: page {page_num} status = {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                products = soup.find_all("div", class_=lambda c: c and "product" in c.split())
                print(f"{tag}: page {page_num} found {len(products)} cards")
                if len(products) == 0:
                    print(f"{tag}: page {page_num} empty, reached the end")
                    break

                # The ?paged= fallback URL isn't always genuinely supported by
                # every WooCommerce permalink setup - some sites silently
                # re-serve page 1's content for every ?paged= value instead
                # of erroring. Detect this by fingerprinting the page (first
                # and last product names) and stopping once the SAME
                # fingerprint repeats, rather than looping until the safety
                # cap regardless of whether real new content is arriving.
                page_fingerprint = (
                    products[0].get_text(" ", strip=True)[:80],
                    products[-1].get_text(" ", strip=True)[:80],
                )
                if used_fallback and page_fingerprint == previous_page_fingerprint:
                    consecutive_repeats += 1
                    print(f"{tag}: page {page_num} content identical to previous page (fallback not supported), stopping")
                    break
                else:
                    consecutive_repeats = 0
                previous_page_fingerprint = page_fingerprint

                for product in products:
                    name_tag = product.find("h3") or product.find("h2")
                    price_tag = product.find(class_=re.compile(r"^(price|woocommerce-Price-amount)"))
                    name = None
                    if name_tag:
                        name = name_tag.get_text(strip=True)
                    link_tag = product.find("a", class_="woocommerce-LoopProduct-link") or product.find("a", title=True)
                    if not name and link_tag and link_tag.get("title"):
                        name = link_tag.get("title").strip()
                    price_text = price_tag.get_text(strip=True) if price_tag else None

                    product_url = None
                    any_link = link_tag or product.find("a", href=True)
                    if any_link:
                        product_url = resolve_link(any_link.get("href"), url)

                    cart_button = product.find(class_=re.compile(r"add_to_cart_button|product_type_"))
                    availability_text = cart_button.get_text(strip=True) if cart_button else None
                    if not availability_text:
                        card_text = product.get_text(" ", strip=True).lower()
                        if "sold out" in card_text or "out of stock" in card_text:
                            availability_text = "Sold out"
                        elif "pre-order" in card_text or "preorder" in card_text:
                            availability_text = "Pre-order"
                    availability = normalize_availability(availability_text)

                    image_url = None
                    img_tag = product.find("img")
                    if img_tag:
                        raw_image_url = img_tag.get("src") or img_tag.get("data-src")
                        if raw_image_url and raw_image_url.startswith("data:image"):
                            raw_image_url = img_tag.get("data-src")
                        image_url = normalize_image_url(raw_image_url, url)

                    if name and price_text and len(name) > 3:
                        price_value = extract_first_price(price_text)
                        if price_value:
                            all_results.append({
                                "name": name,
                                "price": price_value,
                                "store": "TokkaHub",
                                "category": determine_category(name, category_label),
                                "image_url": image_url,
                                "product_url": product_url,
                                "availability": availability,
                            })
            else:
                print(f"{tag}: page {page_num} not OK, stopping")
                break
        else:
            print(f"{tag}: page {page_num} skipped (timeout), stopping")
            break
        page_num += 1
        time.sleep(1.5)
    print(f"{tag}: finished, total = {len([r for r in all_results if r['store']=='TokkaHub' and r['category']==category_label])}")


def scrape_3chub_category(all_results, url_slug, category_label):
    """
    Generic scraper for any 3CHUB (Shopify) collection page.
    Confirmed via direct inspection of real fetched HTML: each product
    sits in <li class="grid-item">, with price in
    <span class="price-item price-item-regular">, and the product name
    is the visible text of the <a href="/products/..."> link.
    """
    tag = f"3CHUB {category_label.upper()}"
    print(f"{tag}: starting")
    page_num = 1
    max_safety_pages = 40
    while page_num <= max_safety_pages:
        url = f"https://www.3chub.com/collections/{url_slug}" if page_num == 1 else f"https://www.3chub.com/collections/{url_slug}?page={page_num}"
        print(f"{tag}: fetching page {page_num}...")
        response = fetch_with_hard_timeout(url, hard_seconds=20)
        if response is not None:
            print(f"{tag}: page {page_num} status = {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")

                products = soup.find_all("li", class_=lambda c: c and "grid-item" in c.split())

                print(f"{tag}: page {page_num} found {len(products)} cards")
                if len(products) == 0:
                    print(f"{tag}: page {page_num} empty, reached the end")
                    break

                for product in products:
                    price_tag = product.find(class_=lambda c: c and "price-item-regular" in c.split())
                    if not price_tag:
                        price_tag = product.find(class_=lambda c: c and "price-item" in c.split())
                    price_text = price_tag.get_text(strip=True) if price_tag else None

                    name = None
                    product_link = product.find("a", href=re.compile(r"/products/"))
                    if product_link:
                        # Use only the link's own direct text, not text from
                        # nested elements like the star-rating caption,
                        # which would otherwise get concatenated in.
                        direct_texts = [t.strip() for t in product_link.find_all(string=True, recursive=False) if t.strip()]
                        if direct_texts:
                            name = " ".join(direct_texts)
                        elif product_link.get("href"):
                            # Fall back to deriving a readable name from the URL slug
                            # if the link itself has no usable direct text
                            slug = product_link["href"].rstrip("/").rsplit("/", 1)[-1]
                            name = slug.replace("-", " ").title()

                    image_url = None
                    img_tag = product.find("img")
                    if img_tag:
                        raw_image_url = img_tag.get("src") or img_tag.get("data-src")
                        image_url = normalize_image_url(raw_image_url, url)

                    product_url = resolve_link(product_link.get("href"), url) if product_link else None

                    # Shopify's standard signal: the add-to-cart/select-options
                    # button text changes to "Sold out" when a product has no
                    # available inventory.
                    action_button = product.find(class_=re.compile(r"button|product-form__submit"))
                    availability_text = action_button.get_text(strip=True) if action_button else None
                    if not availability_text:
                        card_text = product.get_text(" ", strip=True).lower()
                        if "sold out" in card_text:
                            availability_text = "Sold out"
                        elif "pre-order" in card_text or "preorder" in card_text or "coming soon" in card_text:
                            availability_text = "Pre-order" if "pre" in card_text else "Coming Soon"
                    availability = normalize_availability(availability_text)

                    if name and price_text:
                        price_value = extract_first_price(price_text)
                        if price_value:
                            all_results.append({
                                "name": name,
                                "price": price_value,
                                "store": "3CHUB",
                                "category": determine_category(name, category_label),
                                "image_url": image_url,
                                "product_url": product_url,
                                "availability": availability,
                            })
            else:
                print(f"{tag}: page {page_num} not OK, stopping")
                break
        else:
            print(f"{tag}: page {page_num} skipped (timeout), stopping")
            break
        page_num += 1
        time.sleep(1.5)
    print(f"{tag}: finished, total = {len([r for r in all_results if r['store']=='3CHUB' and r['category']==category_label])}")


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
                    # Fall back to the product page link's "title" attribute or text
                    link_tag = product.find("a", class_="woocommerce-LoopProduct-link") or product.find("a", title=True)
                    if not name and link_tag and link_tag.get("title"):
                        name = link_tag.get("title").strip()
                    price_text = price_tag.get_text(strip=True) if price_tag else None

                    product_url = None
                    any_link = link_tag or product.find("a", href=True)
                    if any_link:
                        product_url = resolve_link(any_link.get("href"), url)

                    cart_button = product.find(class_=re.compile(r"add_to_cart_button|product_type_"))
                    availability_text = cart_button.get_text(strip=True) if cart_button else None
                    if not availability_text:
                        card_text = product.get_text(" ", strip=True).lower()
                        if "sold out" in card_text or "out of stock" in card_text:
                            availability_text = "Sold out"
                        elif "pre-order" in card_text or "preorder" in card_text:
                            availability_text = "Pre-order"
                    availability = normalize_availability(availability_text)

                    image_url = None
                    img_tag = product.find("img")
                    if img_tag:
                        raw_image_url = (
                            img_tag.get("src")
                            or img_tag.get("data-src")
                            or img_tag.get("data-lazy-src")
                        )
                        if raw_image_url and raw_image_url.startswith("data:image"):
                            raw_image_url = img_tag.get("data-src") or img_tag.get("data-lazy-src")
                        image_url = normalize_image_url(raw_image_url, url)


                    if name and price_text and len(name) > 3:
                        price_value = extract_first_price(price_text)
                        if price_value:
                            all_results.append({
                                "name": name,
                                "price": price_value,
                                "store": "PhoneMart",
                                "category": determine_category(name, category_label),
                                "image_url": image_url,
                                "product_url": product_url,
                                "availability": availability,
                            })
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

    # 3CHUB (Shopify)
    scrape_3chub_category(all_results, "mobile-phones", "Phones")
    scrape_3chub_category(all_results, "acc", "Accessories")

    # Tokka Hub (WoodMart theme, same selectors as PhoneMart)
    scrape_tokkahub_category(all_results, "shop-with-tokka/mobile-phones-and-tablets-in-nigeria", "Phones")
    scrape_tokkahub_category(all_results, "shop-with-tokka/buy-laptops-computers-in-nigeria", "Laptops")
    scrape_tokkahub_category(all_results, "shop-with-tokka/accessories", "Accessories")
    scrape_tokkahub_category(all_results, "shop-with-tokka/digital-gadgets/wearables/smartwatches", "Watches")
    scrape_tokkahub_category(all_results, "shop-with-tokka/digital-gadgets/wearables/airpods-earbuds", "Audio")

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
            writer.writerow(["Store", "Category", "Product", "Price (NGN)", "Date Checked", "Image URL", "Product URL", "Availability"])
            today = datetime.now().strftime("%Y-%m-%d %H:%M")
            for item in unique_results:
                image_url = item.get("image_url") or item.get("image") or item.get("ImageUrl") or ""
                product_url = item.get("product_url") or ""
                availability = item.get("availability") or "Unknown"
                writer.writerow([item["store"], item["category"], item["name"], item["price"], today, image_url, product_url, availability])

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
                image_url = item.get("image_url") or item.get("image") or item.get("ImageUrl")
                product_id = get_or_create_product(brand, item["name"], item["category"], image_url=image_url)
                listing_id = save_listing(
                    product_id=product_id,
                    store=item["store"],
                    product_name=item["name"],
                    product_url=item.get("product_url") or "",
                    price=item["price"],
                    availability=item.get("availability") or "Unknown",
                    checked_at=now_str,
                )
                save_price_history(listing_id, item["price"], now_str, product_id=product_id, store=item["store"])
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


@app.route("/images/placeholders/<category_slug>.svg")
def product_image_placeholder(category_slug):
    label = unquote(category_slug).replace("-", " ").strip().title() or "Product"
    initials = "".join(word[0] for word in label.split()[:2]).upper() or "NW"
    safe_label = escape(label)
    safe_initials = escape(initials)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="700" height="700" viewBox="0 0 700 700" role="img" aria-label="{safe_label} placeholder">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#eef9f6"/>
      <stop offset="1" stop-color="#dfe7ef"/>
    </linearGradient>
  </defs>
  <rect width="700" height="700" rx="54" fill="url(#bg)"/>
  <rect x="150" y="120" width="400" height="460" rx="42" fill="#ffffff" stroke="#c9d5df" stroke-width="12"/>
  <rect x="190" y="170" width="320" height="310" rx="24" fill="#f4f6f8"/>
  <circle cx="350" cy="525" r="24" fill="#0d7c66"/>
  <text x="350" y="345" text-anchor="middle" font-family="Segoe UI, Arial, sans-serif" font-size="104" font-weight="800" fill="#0d7c66">{safe_initials}</text>
  <text x="350" y="640" text-anchor="middle" font-family="Segoe UI, Arial, sans-serif" font-size="34" font-weight="700" fill="#3c4a5c">NairaWatch</text>
</svg>"""
    return Response(svg, mimetype="image/svg+xml")


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
    selected_availability = request.args.getlist("availability")
    view_mode = request.args.get("view", "list").strip()  # "list" or "compare"

    if not selected_stores:
        selected_stores = all_stores

    if not selected_availability:
        selected_availability = VALID_AVAILABILITY_VALUES

    filtered_rows = [r for r in rows if r["Store"] in selected_stores]

    filtered_rows = [r for r in filtered_rows if r["Availability"] in selected_availability]

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
        # Only include availability params when the user has actively
        # narrowed the filter - if all values are selected (the default),
        # omit it entirely to keep URLs clean.
        if set(selected_availability) != set(VALID_AVAILABILITY_VALUES):
            for av in selected_availability:
                params.append(f"availability={av}")
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
        matched_groups = group_products_by_match(filtered_rows, name_field="Product", category_field="Category")

        # Only keep groups with 2+ stores represented - that's the whole point of "compare"
        for items in matched_groups:
            stores_in_group = set(item["Store"] for item in items)
            if len(stores_in_group) >= 2:
                items_sorted = sorted(items, key=lambda x: int(x["Price (NGN)"]))
                prices = [int(item["Price (NGN)"]) for item in items_sorted]
                highest_price = max(prices) if prices else 0
                average_price = sum(prices) // len(prices) if prices else 0

                # "Best Price" only applies to In Stock items - an Out of
                # Stock listing should never win the badge just because it
                # happens to have the lowest (possibly stale/placeholder) price.
                in_stock_items = [item for item in items_sorted if item["Availability"] == "In Stock"]
                if in_stock_items:
                    best_price = min(int(item["Price (NGN)"]) for item in in_stock_items)
                    any_in_stock = True
                else:
                    best_price = min(prices) if prices else 0
                    any_in_stock = False

                savings = highest_price - best_price if any_in_stock else 0

                comparison_groups.append({
                    "display_name": items_sorted[0]["Product"],
                    "product_slug": items_sorted[0]["ProductSlug"],
                    "items": items_sorted,
                    "best_price": best_price,
                    "highest_price": highest_price,
                    "average_price": average_price,
                    "savings": savings,
                    "store_count": len(stores_in_group),
                    "any_in_stock": any_in_stock,
                })
        # Groups with at least one in-stock item sort by their real best
        # price; fully-unavailable groups sort to the end, since their
        # "lowest price" isn't a genuine deal worth surfacing first.
        comparison_groups.sort(key=lambda g: (not g["any_in_stock"], g["best_price"]))

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
        all_availability_values=VALID_AVAILABILITY_VALUES,
        selected_stores=selected_stores,
        selected_category=selected_category,
        selected_brand=selected_brand,
        selected_availability=selected_availability,
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

    matched_groups = group_products_by_match(rows, name_field="Product", category_field="Category")
    product_rows = next(
        (group for group in matched_groups if selected_row in group),
        None,
    )
    if not product_rows:
        # Selected product had no recognizable brand/model (e.g. an
        # accessory) - fall back to exact name matching so the page
        # still works instead of showing nothing.
        product_rows = [
            row for row in rows
            if row["Product"].strip().lower() == selected_row["Product"].strip().lower()
        ]

    product_rows = sorted(product_rows, key=lambda row: row["PriceValue"])

    # "Best Price" stats should only reflect In Stock listings - an Out of
    # Stock item's price (real or placeholder) shouldn't be presented as
    # the current lowest price a shopper can actually buy at.
    in_stock_rows = [row for row in product_rows if row["Availability"] == "In Stock"]
    stores_in_stock = len(set(row["Store"] for row in in_stock_rows))
    stores_out_of_stock = len(set(row["Store"] for row in product_rows if row["Availability"] != "In Stock"))

    if in_stock_rows:
        prices = [row["PriceValue"] for row in in_stock_rows if row["PriceValue"] > 0]
    else:
        # Nothing is in stock anywhere - fall back to whatever real prices
        # exist (if any) just so the page has something to display, but
        # the template shows "Currently unavailable" instead of a deal badge.
        prices = [row["PriceValue"] for row in product_rows if row["PriceValue"] > 0]

    if not prices and not product_rows:
        abort(404)

    lowest_price = min(prices) if prices else 0
    highest_price = max(prices) if prices else 0
    average_price = round(sum(prices) / len(prices)) if prices else 0
    currently_unavailable = not in_stock_rows
    last_updated = max((row["Date Checked"] for row in product_rows if row.get("Date Checked")), default="Not available")

    # ---------- PHASE 3: PRICE INTELLIGENCE ----------
    # Look up this exact product's database id so we can query its
    # historical price_history records. We use the cheapest store's
    # listing as the canonical (brand, model, category) key, since that's
    # exactly how run_scraper() saved it via get_or_create_product().
    canonical_row = product_rows[0]
    canonical_brand = extract_brand(canonical_row["Product"]) or "Other"
    product_id = get_product(canonical_brand, canonical_row["Product"], canonical_row["Category"])

    price_stats = None
    price_history = []
    price_change = None
    first_seen = "Not available"

    if product_id:
        price_stats = get_price_stats_for_product(product_id)
        price_history = get_price_history_for_product(product_id)
        first_record = get_first_recorded_price_for_product(product_id)
        if first_record:
            first_seen = price_stats["first_seen"] if price_stats else first_record["checked_at"]
            price_change = compute_price_change(lowest_price, first_record["price"])

    return render_template(
        "product_details.html",
        product=selected_row,
        product_rows=product_rows,
        lowest_price=lowest_price,
        highest_price=highest_price,
        average_price=average_price,
        stores_in_stock=stores_in_stock,
        stores_out_of_stock=stores_out_of_stock,
        currently_unavailable=currently_unavailable,
        last_updated=last_updated,
        price_stats=price_stats,
        price_history=price_history,
        price_change=price_change,
        first_seen=first_seen,
    )


initialize_database()

scraper_thread = threading.Thread(target=background_refresh_loop, daemon=True)
scraper_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("Starting server...")
    app.run(host="0.0.0.0", port=port)