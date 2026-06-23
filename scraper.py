import requests
from bs4 import BeautifulSoup
import re
import csv
import time
from datetime import datetime

print("Fetching smartphones from Jumia and Justfones...")
print("=" * 50)

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

all_results = []

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

# ---------- JUMIA ----------
print("\n--- JUMIA ---")
pages_to_fetch = 5

for page_num in range(1, pages_to_fetch + 1):
    if page_num == 1:
        jumia_url = "https://www.jumia.com.ng/smartphones/"
    else:
        jumia_url = f"https://www.jumia.com.ng/smartphones/?page={page_num}"

    print(f"Fetching Jumia page {page_num}...")

    try:
        response = requests.get(jumia_url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            products = soup.find_all("article", class_="prd")

            for product in products:
                name_tag = product.find("h3", class_="name")
                price_tag = product.find("div", class_="prc")
                name = name_tag.text.strip() if name_tag else None
                price_text = price_tag.text.strip() if price_tag else None

                if name and price_text:
                    price_value = extract_first_price(price_text)
                    if price_value:
                        all_results.append({"name": name, "price": price_value, "store": "Jumia"})
        else:
            print(f"  Jumia page {page_num} returned status {response.status_code}")
    except Exception as e:
        print(f"  Error fetching Jumia page {page_num}: {e}")

    time.sleep(1.5)

print(f"Jumia: collected {len([r for r in all_results if r['store'] == 'Jumia'])} listings")

# ---------- JUSTFONES ----------
print("\n--- JUSTFONES ---")
pages_to_fetch_jf = 5

for page_num in range(1, pages_to_fetch_jf + 1):
    if page_num == 1:
        jf_url = "https://www.justfones.ng/smartphones.html"
    else:
        jf_url = f"https://www.justfones.ng/smartphones.html?p={page_num}"

    print(f"Fetching Justfones page {page_num}...")

    try:
        response = requests.get(jf_url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            # Justfones (Magento) product cards use this structure
            products = soup.find_all("li", class_="item product product-item")

            for product in products:
                name_tag = product.find("a", class_="product-item-link")
                price_tag = product.find("span", class_="price")

                name = name_tag.text.strip() if name_tag else None
                price_text = price_tag.text.strip() if price_tag else None

                if name and price_text:
                    price_value = extract_first_price(price_text)
                    if price_value:
                        all_results.append({"name": name, "price": price_value, "store": "Justfones"})
        else:
            print(f"  Justfones page {page_num} returned status {response.status_code}")
    except Exception as e:
        print(f"  Error fetching Justfones page {page_num}: {e}")

    time.sleep(1.5)

print(f"Justfones: collected {len([r for r in all_results if r['store'] == 'Justfones'])} listings")

# ---------- SAVE TO CSV ----------
print(f"\nTotal products collected: {len(all_results)}")

if all_results:
    seen = set()
    unique_results = []
    for item in all_results:
        key = (item["store"], item["name"])
        if key not in seen:
            seen.add(key)
            unique_results.append(item)

    unique_results.sort(key=lambda x: x["price"])

    filename = "price_comparison.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Store", "Product", "Price (NGN)", "Date Checked"])
        today = datetime.now().strftime("%Y-%m-%d %H:%M")
        for item in unique_results:
            writer.writerow([item["store"], item["name"], item["price"], today])

    print(f"Saved {len(unique_results)} unique results to {filename}")
else:
    print("No results to save.")

print("Done.")