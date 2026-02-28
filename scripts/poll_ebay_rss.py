# scripts/poll_ebay_rss.py
# eBay RSS monitor with:
# - per-search RSS polling (no HTML scraping)
# - jitter + shuffled order (stealth)
# - dedupe via data/seen.json
# - Discord embeds routed to 3 channels: priority / camera / general
# - best-effort price + listing-type extraction from RSS description

import json
import os
import re
import time
import random
import hashlib
from html import unescape
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests

SITE_BASE = {
    "US": "https://www.ebay.com",
    "UK": "https://www.ebay.co.uk",
    "DE": "https://www.ebay.de",
    "FR": "https://www.ebay.fr",
}

WEBHOOKS = {
    "priority": os.getenv("DISCORD_WEBHOOK_PRIORITY", ""),
    "camera": os.getenv("DISCORD_WEBHOOK_CAMERA", ""),
    "general": os.getenv("DISCORD_WEBHOOK_GENERAL", ""),
}

SEEN_PATH = "data/seen.json"
CFG_PATH = "config/searches.json"

UA = "Mozilla/5.0 (compatible; rss-monitor/1.0; +https://github.com/)"

def rss_url(site: str, query: str) -> str:
    base = SITE_BASE[site]
    # _sop=10 -> newly listed (best for monitoring)
    return f"{base}/sch/i.html?_nkw={quote_plus(query)}&_sop=10&rt=nc&_rss=1"

def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path: str, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def stable_key(site: str, query: str, guid: str, link: str) -> str:
    raw = f"{site}|{query}|{guid}|{link}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def strip_html(s: str) -> str:
    s = unescape(s or "")
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def guess_price(text: str) -> str:
    """
    Best-effort extraction. eBay RSS description formats vary a lot.
    We'll try:
      - 'EUR 12.34', 'USD 12.34', 'GBP 12.34'
      - '$12.34', '€12.34', '£12.34'
    """
    if not text:
        return ""
    patterns = [
        r"(?i)\b(USD|EUR|GBP)\s*([0-9][0-9.,]*)",
        r"([$€£])\s*([0-9][0-9.,]*)",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            a, b = m.group(1), m.group(2)
            return f"{a} {b}".replace("  ", " ")
    return ""

def guess_format(text: str, title: str) -> str:
    """
    Best-effort listing type:
    - Auction indicators: auction/bid/bids (English) + common locale hints
    - BIN indicators: 'Buy It Now' and simple localized cues
    """
    blob = ((text or "") + " " + (title or "")).lower()
    if any(k in blob for k in ["auction", "bid", "bids", "gebot", "enchère", "enchere", "asta"]):
        return "Auction"
    if any(k in blob for k in ["buy it now", "sofort-kaufen", "sofort kaufen", "achat immédiat", "achat immediat"]):
        return "Buy It Now"
    return "Listing"

def discord_post(bucket: str, embed: dict):
    url = WEBHOOKS.get(bucket, "") or WEBHOOKS.get("general", "")
    if not url:
        return
    payload = {"embeds": [embed]}
    requests.post(url, json=payload, timeout=20)

def main():
    group = os.getenv("GROUP", "A").strip().upper()

    cfg = load_json(CFG_PATH, {})
    jobs = cfg.get("groups", {}).get(group, [])

    if not jobs:
        print(f"Group {group} has 0 searches.")
        return

    # Stealth: shuffle & jitter
    random.shuffle(jobs)

    seen_list = load_json(SEEN_PATH, [])
    if not isinstance(seen_list, list):
        seen_list = []
    seen = set(seen_list)
    new_seen = set(seen)

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"
    })

    new_count = 0

    for job in jobs:
        site = job["site"]
        query = job["query"]
        bucket = (job.get("bucket", "general") or "general").lower().strip()

        if site not in SITE_BASE:
            print(f"[WARN] Unknown site code: {site} (skipping)")
            continue
        if bucket not in WEBHOOKS:
            bucket = "general"

        url = rss_url(site, query)

        # jitter between requests to avoid looking bot-like
        time.sleep(random.uniform(1.0, 2.5))

        try:
            r = sess.get(url, timeout=25)
            r.raise_for_status()
        except Exception as e:
            print(f"[WARN] fetch failed: {site} {query} ({e})")
            continue

        try:
            root = ET.fromstring(r.text)
        except Exception as e:
            print(f"[WARN] parse failed: {site} {query} ({e})")
            continue

        for item in root.findall(".//item")[:25]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or link or title).strip()
            desc_html = (item.findtext("description") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()

            k = stable_key(site, query, guid, link)
            if k in seen:
                continue

            new_seen.add(k)
            new_count += 1

            desc_text = strip_html(desc_html)
            price = guess_price(desc_text)
            fmt = guess_format(desc_text, title)

            # Emoji per bucket
            bucket_emoji = {"priority": "🔥", "camera": "📷", "general": "📦"}.get(bucket, "📦")

            embed = {
                "title": (f"{bucket_emoji} {title}")[:256] if title else f"{bucket_emoji} New eBay listing",
                "url": link,
                "description": f"**Site:** {site}  •  **Query:** {query}",
                "fields": [],
            }

            if price:
                embed["fields"].append({"name": "Price (best effort)", "value": price, "inline": True})
            embed["fields"].append({"name": "Type (best effort)", "value": fmt, "inline": True})

            if pub:
                embed["footer"] = {"text": pub}

            try:
                discord_post(bucket, embed)
            except Exception as e:
                print(f"[WARN] discord failed: {e}")

    save_json(SEEN_PATH, sorted(list(new_seen)))
    print(f"Group {group}: {new_count} new items")

if __name__ == "__main__":
    main()
