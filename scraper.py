"""
Scraper pentru comparatie preturi PC gaming: enter.online, xstore.md, darwin.md
Scrie rezultatele direct in Supabase (products_raw + products_fingerprint).

Rulare: python scraper.py
Necesita variabile de mediu: SUPABASE_URL, SUPABASE_KEY
"""

import os
import re
import time
import requests
from playwright.sync_api import sync_playwright


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# Normalizare / fingerprint - comun pentru toate site-urile
# ---------------------------------------------------------------------------

CPU_PATTERNS = [
    r"(i[3579][- ]\d{4,5}[A-Z]{0,3})",
    r"(Ryzen [3579]\s*(?:PRO\s*)?\d{3,4}[A-Z0-9]{0,4})",
    r"(Ultra [3579][- ]\d{2,3}[A-Z]{0,4})",
    r"(Pentium\s*\w*)",
]

GPU_PATTERNS = [
    r"(RTX\s?\d{4}\s?Ti)",
    r"(RTX\s?\d{4})",
    r"(RX\s?\d{4}\s?XT)",
    r"(RX\s?\d{4})",
    r"(GTX\s?\d{3,4}\s?Ti)",
    r"(GTX\s?\d{3,4})",
    r"(GT\s?\d{3,4})",
]


def normalize_cpu(text):
    for pattern in CPU_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1).upper()
            # unificam separatorul: cratima -> spatiu, apoi colapsam spatiile
            value = value.replace("-", " ")
            value = re.sub(r"\s+", " ", value).strip()
            return value
    return None


def normalize_gpu(text):
    for pattern in GPU_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1).upper()
            # eliminam TOATE spatiile: unele site-uri scriu "RTX 5060 TI",
            # altele "RTX5060TI" - trebuie sa ajunga identice
            value = re.sub(r"\s+", "", value)
            return value
    return None


def normalize_ram_gb(text):
    matches = re.findall(r"(\d+)\s*GB", text, re.IGNORECASE)
    if not matches:
        return None
    # RAM e de regula prima valoare mica (8-96), SSD e valoarea mare (250-4000)
    candidates = [int(m) for m in matches if int(m) <= 128]
    return candidates[0] if candidates else None


def normalize_ssd_gb(text):
    tb_match = re.search(r"(\d+)\s*TB", text, re.IGNORECASE)
    if tb_match:
        return int(tb_match.group(1)) * 1000
    gb_matches = re.findall(r"(\d+)\s*GB", text, re.IGNORECASE)
    candidates = [int(m) for m in gb_matches if int(m) > 128]
    return candidates[0] if candidates else None


def clean_price(text):
    if not text:
        return None
    numbers = re.findall(r"\d+", text.replace("\xa0", " "))
    return int("".join(numbers)) if numbers else None


def build_fingerprint(cpu, gpu, ram_gb, ssd_gb):
    parts = [
        cpu or "unknown",
        gpu or "unknown",
        str(ram_gb) if ram_gb else "unknown",
        str(ssd_gb) if ssd_gb else "unknown",
    ]
    return "|".join(parts).lower()


# ---------------------------------------------------------------------------
# Scraper: enter.online
# ---------------------------------------------------------------------------

def scrape_enter(page, max_pages=30):
    results = []
    category_url = "https://enter.online/for-gamers/unitate-pc-gaming?page={}"

    for page_number in range(1, max_pages + 1):
        page.goto(category_url.format(page_number), wait_until="domcontentloaded")
        page.wait_for_timeout(500)

        links = page.locator("a").evaluate_all("els => els.map(e => e.href)")
        product_links = list(dict.fromkeys(
            l for l in links if "/for-gamers/unitate-pc-gaming/" in l
        ))

        if not product_links:
            if page_number == 1:
                try:
                    page_title = page.title()
                except Exception:
                    page_title = "?"
                body_snippet = page.locator("body").inner_text()[:300]
                print(f"  [diagnostic] 0 linkuri gasite. Titlu pagina: {page_title!r}")
                print(f"  [diagnostic] Primele 300 caractere din body: {body_snippet!r}")
            break

        stop = False

        for link in product_links:
            page.goto(link, wait_until="domcontentloaded")
            page.wait_for_timeout(400)
            # sectiunea de pret se incarca lazy, dupa scroll
            page.mouse.wheel(0, 800)
            try:
                page.wait_for_selector("text=CUMPĂRĂ", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(400)

            text = page.locator("body").inner_text()
            if "În stoc" not in text:
                if link == product_links[0]:
                    try:
                        page_title = page.title()
                    except Exception:
                        page_title = "?"
                    print(f"  [diagnostic] Primul produs a esuat verificarea stocului.")
                    print(f"  [diagnostic] Link: {link}")
                    print(f"  [diagnostic] Titlu pagina: {page_title!r}")
                    print(f"  [diagnostic] Primele 500 caractere din body: {text[:500]!r}")
                stop = True
                break

            try:
                title = page.locator("h1").inner_text()
            except Exception:
                title = ""

            lines = title.split("\n")
            produs = lines[0].strip() if lines else ""
            config_text = lines[1].strip() if len(lines) > 1 else ""

            # Structura reala confirmata pe pagina:
            # fara reducere:  "17 499 lei CUMPĂRĂ Cashback 525 lei"
            # cu reducere:    "14 999 lei 12 999 lei CUMPĂRĂ -13% -2 000 lei Cashback 390 lei"
            pret = None
            pret_vechi = None
            buy_idx = text.find("CUMPĂRĂ")

            if buy_idx != -1:
                pre_window = text[max(0, buy_idx - 100):buy_idx]
                lei_matches = re.findall(r"([\d][\d\s]*)\s*lei", pre_window)
                lei_values = [clean_price(m) for m in lei_matches]
                lei_values = [v for v in lei_values if v]

                if len(lei_values) == 1:
                    pret = lei_values[0]
                elif len(lei_values) > 1:
                    pret_vechi = lei_values[0]
                    pret = lei_values[-1]
                    if pret_vechi <= pret:
                        pret_vechi = None

            reducere_lei = None
            reducere_proc = None
            if pret and pret_vechi:
                reducere_lei = pret_vechi - pret
                reducere_proc = round((reducere_lei / pret_vechi) * 100, 1)

            cashback = None
            if buy_idx != -1:
                cash_idx = text.find("Cashback", buy_idx)
                if cash_idx != -1:
                    cash_window = text[cash_idx:cash_idx + 60]
                    m = re.search(r"([\d][\d\s]*)\s*lei", cash_window)
                    if m:
                        cashback = clean_price(m.group(1))

            results.append({
                "site": "enter.online",
                "titlu": produs,
                "cpu_raw": config_text,
                "pret": pret,
                "pret_vechi": pret_vechi,
                "reducere_lei": reducere_lei,
                "reducere_proc": reducere_proc,
                "cashback": cashback,
                "stoc": "În stoc",
                "link": link,
            })

        if stop:
            break

    return results


# ---------------------------------------------------------------------------
# Scraper: xstore.md
# ---------------------------------------------------------------------------

def scrape_xstore(page, category_path="calculatoare-pc/gaming", max_pages=30):
    results = []
    base_url = f"https://xstore.md/{category_path}?page={{}}"

    for page_number in range(1, max_pages + 1):
        page.goto(base_url.format(page_number), wait_until="domcontentloaded")
        page.wait_for_timeout(800)

        items = page.evaluate("""
        () => {
            const anchors = Array.from(
                document.querySelectorAll("a[href*='/calculatoare-pc/gaming/']")
            );
            const seenHref = new Set();
            const out = [];

            for (const a of anchors) {
                const href = a.href;
                if (href.replace(/\\/$/, '').endsWith('/gaming')) continue;

                const titlu = (a.innerText || '').trim();
                if (!titlu) continue;
                if (seenHref.has(href)) continue;
                seenHref.add(href);

                // config-ul (CPU/GPU/RAM/SSD) e text-sibling langa titlu,
                // urcam pana gasim un ancestor al carui text (minus titlul)
                // contine "/"
                let configLine = '';
                let node1 = a;
                for (let i = 0; i < 3; i++) {
                    if (!node1.parentElement) break;
                    node1 = node1.parentElement;
                    const t = (node1.innerText || '').trim();
                    const rest = t.startsWith(titlu) ? t.slice(titlu.length).trim() : t;
                    if (rest.includes('/')) { configLine = rest; break; }
                }

                // pretul: urcam pana gasim linii cu "lei" care NU sunt
                // rata lunara (contin "lunar")
                let node2 = a;
                let priceLines = [];
                for (let i = 0; i < 8; i++) {
                    if (!node2.parentElement) break;
                    node2 = node2.parentElement;
                    const text = node2.innerText || '';
                    const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
                    const candidates = lines.filter(
                        l => l.includes('lei') && !l.toLowerCase().includes('lunar')
                    );
                    if (candidates.length > 0) {
                        priceLines = candidates;
                        break;
                    }
                }

                out.push({href, titlu, configLine, priceLines});
            }
            return out;
        }
        """)

        if not items:
            break

        page_results = 0

        for item in items:
            href = item["href"]
            titlu = item["titlu"]
            config_line = item["configLine"]
            price_lines = item["priceLines"]

            if not price_lines:
                continue

            def first_price(line):
                m = re.search(r"([\d\s]{4,}?)\s*lei", line)
                return clean_price(m.group(1)) if m else None

            if len(price_lines) == 1:
                pret = first_price(price_lines[0])
                pret_vechi = None
            else:
                pret_vechi = first_price(price_lines[0])
                pret = first_price(price_lines[-1])
                if pret_vechi and pret and pret_vechi <= pret:
                    pret_vechi = None

            if not pret:
                continue

            reducere_lei = None
            reducere_proc = None
            if pret and pret_vechi:
                reducere_lei = pret_vechi - pret
                reducere_proc = round((reducere_lei / pret_vechi) * 100, 1)

            results.append({
                "site": "xstore.md",
                "titlu": titlu,
                "cpu_raw": config_line or titlu,
                "pret": pret,
                "pret_vechi": pret_vechi,
                "reducere_lei": reducere_lei,
                "reducere_proc": reducere_proc,
                "cashback": None,
                "stoc": "În stoc",
                "link": href,
            })
            page_results += 1

        if page_results == 0:
            break

    return results



# ---------------------------------------------------------------------------
# Scraper: darwin.md
# ---------------------------------------------------------------------------

def scrape_darwin(page, category_path="gaming/sisteme-pc", max_pages=30):
    results = []
    seen_hrefs = set()

    page.goto(f"https://darwin.md/{category_path}", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    for page_number in range(1, max_pages + 1):
        page.wait_for_timeout(800)

        items = page.evaluate("""
        () => {
            const anchors = Array.from(document.querySelectorAll("a[href$='.html']"));
            const out = [];
            const seen = new Set();
            for (const a of anchors) {
                const href = a.href;
                if (seen.has(href)) continue;
                const cardText = (a.innerText || '').trim();
                if (!cardText) continue;
                seen.add(href);

                let node = a;
                let prices = [];
                for (let i = 0; i < 8; i++) {
                    node = node.parentElement;
                    if (!node) break;
                    const text = node.innerText || '';
                    const regex = /([\\d\\s]{4,})\\s*lei/g;
                    let m;
                    const found = [];
                    while ((m = regex.exec(text)) !== null) {
                        const before = text.slice(Math.max(0, m.index - 12), m.index);
                        if (!before.includes('Cashback')) found.push(m[1]);
                    }
                    if (found.length > 0) {
                        prices = found;
                        break;
                    }
                }
                out.push({href, text: cardText, prices});
            }
            return out;
        }
        """)

        new_items = [item for item in items if item["href"] not in seen_hrefs]

        if not new_items:
            print(f"  Darwin: nicio pagina noua la pagina {page_number}, opresc paginarea.")
            break

        for item in new_items:
            href = item["href"]
            text = item["text"]
            prices = [clean_price(p) for p in item["prices"]]
            prices = [p for p in prices if p]

            cashback_match = re.search(r"Cashback ([\d\s]+) lei", text)
            cashback = clean_price(cashback_match.group(1)) if cashback_match else None
            titlu = re.sub(r"Cashback.*$", "", text).strip()

            pret = min(prices) if prices else None
            pret_vechi = max(prices) if len(prices) > 1 and max(prices) != pret else None

            reducere_lei = None
            reducere_proc = None
            if pret and pret_vechi:
                reducere_lei = pret_vechi - pret
                reducere_proc = round((reducere_lei / pret_vechi) * 100, 1)

            results.append({
                "site": "darwin.md",
                "titlu": titlu,
                "cpu_raw": titlu,
                "pret": pret,
                "pret_vechi": pret_vechi,
                "reducere_lei": reducere_lei,
                "reducere_proc": reducere_proc,
                "cashback": cashback,
                "stoc": "În stoc",
                "link": href,
            })
            seen_hrefs.add(href)

        advanced = False
        next_page_label = str(page_number + 1)

        for selector in [
            f"a:text-is('{next_page_label}')",
            "a:has-text('Mai departe')",
            "a:has-text('Următoarea')",
            "[class*='pagination'] a:has-text('>')",
            "a[rel='next']",
        ]:
            try:
                target = page.locator(selector).first
                if target.count() > 0:
                    target.click(timeout=2000)
                    page.wait_for_timeout(1200)
                    advanced = True
                    break
            except Exception:
                continue

        if not advanced:
            print(f"  Darwin: nu am gasit buton/link pentru pagina {page_number + 1}, opresc.")
            break

    return results


# ---------------------------------------------------------------------------
# Insert in Supabase
# ---------------------------------------------------------------------------

def insert_products(rows):
    inserted = []
    batch_size = 50

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        payload = []

        for row in batch:
            config_source = row.pop("cpu_raw", "")
            cpu = normalize_cpu(config_source)
            gpu = normalize_gpu(config_source)
            ram_gb = normalize_ram_gb(config_source)
            ssd_gb = normalize_ssd_gb(config_source)

            payload.append({
                **row,
                "cpu": cpu,
                "gpu": gpu,
                "ram": f"{ram_gb}GB" if ram_gb else None,
                "ssd": f"{ssd_gb}GB" if ssd_gb else None,
            })

        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/products_raw",
            headers={**HEADERS, "Prefer": "return=representation"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        inserted.extend(resp.json())

    return inserted


def insert_fingerprints(inserted_rows):
    payload = []

    for row in inserted_rows:
        cpu = row.get("cpu")
        gpu = row.get("gpu")
        ram_gb = int(row["ram"].replace("GB", "")) if row.get("ram") else None
        ssd_gb = int(row["ssd"].replace("GB", "")) if row.get("ssd") else None

        payload.append({
            "raw_id": row["id"],
            "cpu_model": cpu,
            "gpu_model": gpu,
            "ram_gb": ram_gb,
            "ssd_gb": ssd_gb,
            "fingerprint_key": build_fingerprint(cpu, gpu, ram_gb, ssd_gb),
        })

    for i in range(0, len(payload), 50):
        batch = payload[i:i + 50]
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/products_fingerprint",
            headers=HEADERS,
            json=batch,
            timeout=30,
        )
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    all_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ))

        print("Scraping enter.online...")
        enter_results = scrape_enter(page)
        print(f"  {len(enter_results)} produse")
        all_results.extend(enter_results)
        time.sleep(2)

        print("Scraping xstore.md...")
        xstore_results = scrape_xstore(page)
        print(f"  {len(xstore_results)} produse")
        all_results.extend(xstore_results)

        # darwin.md - dezactivat temporar (necesita verificare stoc per-produs,
        # revenim la el ulterior). Cod pastrat mai jos in fisier, functia
        # scrape_darwin() ramane disponibila.
        # print("Scraping darwin.md...")
        # darwin_results = scrape_darwin(page)
        # print(f"  {len(darwin_results)} produse")
        # all_results.extend(darwin_results)

        browser.close()

    print(f"\nTotal produse: {len(all_results)}")
    print("Inserare in Supabase...")

    inserted = insert_products(all_results)
    insert_fingerprints(inserted)

    print(f"Gata. {len(inserted)} produse salvate.")


if __name__ == "__main__":
    main()
