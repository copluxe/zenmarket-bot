"""
ZenMarket scraper for Mercari Japan and Rakuma search results.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
}

SEARCH_URLS = {
    'mercari': 'https://zenmarket.jp/mercari.aspx?q={query}',
    'rakuma':  'https://zenmarket.jp/rakuma.aspx?q={query}',
}

CONDITION_MAP = {
    '新品、未使用':       'Neuf',
    '新品未使用':         'Neuf',
    '未使用に近い':       'Très bon état',
    '目立つ傷や汚れなし': 'Très bon état',
    'やや傷や汚れあり':   'Bon état',
    '傷や汚れあり':       'État correct',
    '全体的に状態が悪い': 'Mauvais état',
}

# Cached exchange rate (JPY per EUR, refreshed hourly)
_eur_rate_cache: dict = {'rate': None, 'fetched_at': 0.0}
_EUR_CACHE_TTL = 3600


def _get_eur_per_jpy() -> float:
    """Return the EUR/JPY exchange rate (EUR per 1 JPY)."""
    now = time.time()
    if _eur_rate_cache['rate'] and now - _eur_rate_cache['fetched_at'] < _EUR_CACHE_TTL:
        return _eur_rate_cache['rate']
    try:
        r = requests.get(
            'https://open.er-api.com/v6/latest/JPY',
            timeout=10,
            headers={'User-Agent': 'ZenMarketBot/1.0'},
        )
        data = r.json()
        rate = data['rates']['EUR']
        _eur_rate_cache['rate'] = rate
        _eur_rate_cache['fetched_at'] = now
        return rate
    except Exception as exc:
        logger.warning('Exchange rate fetch failed: %s', exc)
        return 1 / 160.0  # fallback ~160 JPY/EUR


def jpy_to_eur(jpy: int) -> int:
    return round(jpy * _get_eur_per_jpy())


@dataclass
class Listing:
    id: str
    title: str
    price_jpy: int
    original_price_jpy: Optional[int]
    condition: str
    condition_fr: str
    status: str          # 'available' | 'sold'
    image_url: str
    url: str
    source: str          # 'mercari' | 'rakuma'
    posted_ago: str      # e.g. "2 heures"
    price_eur: int = field(init=False)

    def __post_init__(self):
        self.price_eur = jpy_to_eur(self.price_jpy)


def _parse_price(text: str) -> Optional[int]:
    if not text:
        return None
    digits = re.sub(r'[^\d]', '', text)
    return int(digits) if digits else None


def _map_condition(raw: str) -> str:
    for jp, fr in CONDITION_MAP.items():
        if jp in raw:
            return fr
    return raw or 'Non spécifié'


def _extract_id(href: str, source: str) -> Optional[str]:
    """Extract a unique item ID from ZenMarket product URLs."""
    m = re.search(r'itemId=([^&]+)', href or '')
    if m:
        return m.group(1)
    m = re.search(r'/product/([^/?]+)', href or '')
    if m:
        return m.group(1)
    return None


def _parse_posted_ago(text: str) -> str:
    if not text:
        return 'Inconnu'
    text = text.strip()
    # Convert common Japanese time expressions
    text = re.sub(r'(\d+)分前', r'\1 minutes', text)
    text = re.sub(r'(\d+)時間前', r'\1 heures', text)
    text = re.sub(r'(\d+)日前', r'\1 jours', text)
    return text


def _parse_listings_html(html: str, source: str) -> list[Listing]:
    soup = BeautifulSoup(html, 'html.parser')
    listings: list[Listing] = []

    # ZenMarket uses Bootstrap grid — product cards share common patterns.
    # Try multiple container selectors in order of specificity.
    containers = (
        soup.select('.col-6.col-sm-6.col-md-4.col-lg-3')
        or soup.select('.product-container')
        or soup.select('.item-box')
        or soup.select('[class*="product"]')
        or soup.select('[class*="item"]')
    )

    if not containers:
        # Fallback: any anchor wrapping an image and a price
        containers = [
            a.parent for a in soup.find_all('a', href=re.compile(r'itemId='))
            if a.find('img')
        ]

    for card in containers:
        try:
            # --- URL & ID ---
            anchor = card.find('a', href=re.compile(r'itemId=|/product/'))
            if not anchor:
                anchor = card if card.name == 'a' else None
            if not anchor:
                continue

            href = anchor.get('href', '')
            if href.startswith('/'):
                href = 'https://zenmarket.jp' + href

            item_id = _extract_id(anchor.get('href', ''), source)
            if not item_id:
                continue

            # --- Image ---
            img = card.find('img')
            image_url = ''
            if img:
                image_url = img.get('data-src') or img.get('src') or ''

            # --- Title ---
            title_el = (
                card.find(class_=re.compile(r'(name|title|caption|label)', re.I))
                or card.find('p')
                or card.find(['h3', 'h4', 'h5'])
            )
            title = title_el.get_text(strip=True) if title_el else ''

            # --- Prices ---
            price_els = card.find_all(
                class_=re.compile(r'price', re.I)
            ) or card.find_all(string=re.compile(r'¥'))
            current_price: Optional[int] = None
            original_price: Optional[int] = None

            # Attempt structured extraction first
            price_current_el = card.find(class_=re.compile(r'price.?(current|now|sale)', re.I))
            price_orig_el = card.find(class_=re.compile(r'price.?(original|old|before|was)', re.I))
            if price_current_el:
                current_price = _parse_price(price_current_el.get_text())
            if price_orig_el:
                original_price = _parse_price(price_orig_el.get_text())

            if current_price is None and price_els:
                prices_found = []
                for el in price_els:
                    txt = el if isinstance(el, str) else el.get_text()
                    p = _parse_price(txt)
                    if p and p > 0:
                        prices_found.append(p)
                if len(prices_found) >= 2:
                    original_price = max(prices_found)
                    current_price = min(prices_found)
                elif prices_found:
                    current_price = prices_found[0]

            if not current_price:
                continue

            # --- Condition ---
            cond_el = card.find(class_=re.compile(r'condition|status|state', re.I))
            condition_raw = cond_el.get_text(strip=True) if cond_el else ''
            condition_fr = _map_condition(condition_raw)

            # --- Status (sold / available) ---
            sold_marker = card.find(
                class_=re.compile(r'sold|soldout|unavailable', re.I)
            ) or card.find(string=re.compile(r'SOLD|売り切れ|Sold', re.I))
            status = 'sold' if sold_marker else 'available'

            # --- Posted ago ---
            time_el = card.find(class_=re.compile(r'time|date|ago|when', re.I))
            posted_ago = _parse_posted_ago(time_el.get_text() if time_el else '')

            listings.append(
                Listing(
                    id=f'{source}_{item_id}',
                    title=title or f'Article {item_id}',
                    price_jpy=current_price,
                    original_price_jpy=original_price if original_price and original_price > current_price else None,
                    condition=condition_raw,
                    condition_fr=condition_fr,
                    status=status,
                    image_url=image_url,
                    url=href,
                    source=source,
                    posted_ago=posted_ago,
                )
            )

        except Exception as exc:
            logger.debug('Card parse error: %s', exc)

    return listings


def fetch_listings(keyword: str, source: str, max_retries: int = 3) -> list[Listing]:
    """Fetch listings for a keyword from a ZenMarket source (mercari or rakuma)."""
    url = SEARCH_URLS[source].format(query=quote(keyword))
    delay = 2
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            listings = _parse_listings_html(resp.text, source)
            logger.info(
                'Fetched %d listings for "%s" on %s', len(listings), keyword, source
            )
            return listings
        except requests.RequestException as exc:
            logger.warning(
                'Fetch attempt %d/%d failed for %s %s: %s',
                attempt + 1, max_retries, source, keyword, exc,
            )
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
    return []
