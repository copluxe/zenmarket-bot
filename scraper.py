"""
ZenMarket scraper — uses curl-cffi with a session cookie to fetch ZenMarket
search pages for Mercari Japan and Rakuma listings.
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)

ZENMARKET_MERCARI_SEARCH = 'https://zenmarket.jp/mercari.aspx?q={query}'
ZENMARKET_MERCARI_LINK = 'https://zenmarket.jp/mercari.aspx?itemid={item_id}'
MERCARI_DIRECT_LINK = 'https://jp.mercari.com/item/{item_id}'
ZENMARKET_RAKUMA_SEARCH = 'https://zenmarket.jp/rakuma.aspx?q={query}'

# AJAX endpoints
ZENMARKET_MERCARI_HANDLER = 'https://zenmarket.jp/MercariHandler.ashx?q={query}'
ZENMARKET_RAKUMA_HANDLER = 'https://zenmarket.jp/SearchHandler.ashx?q={query}&source=rakuma'

CONDITION_MAP = {
    '新品、未使用':       'Neuf',
    '新品未使用':         'Neuf',
    '未使用に近い':       'Très bon état',
    '目立つ傷や汚れなし': 'Très bon état',
    'やや傷や汚れあり':   'Bon état',
    '傷や汚れあり':       'État correct',
    '全体的に状態が悪い': 'Mauvais état',
}

_HTML_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
}

_AJAX_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Accept-Language': 'ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7',
    'X-Requested-With': 'XMLHttpRequest',
    'Referer': 'https://zenmarket.jp/mercari.aspx',
}

# Cached exchange rate (JPY per EUR, refreshed hourly)
_eur_rate_cache: dict = {'rate': None, 'fetched_at': 0.0}
_EUR_CACHE_TTL = 3600


def _get_eur_per_jpy() -> float:
    now = time.time()
    if _eur_rate_cache['rate'] and now - _eur_rate_cache['fetched_at'] < _EUR_CACHE_TTL:
        return _eur_rate_cache['rate']
    try:
        r = cffi_requests.get(
            'https://open.er-api.com/v6/latest/JPY',
            impersonate='chrome120',
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
        return 1 / 160.0


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
    direct_url: Optional[str] = None
    price_eur: int = field(init=False)

    def __post_init__(self):
        self.price_eur = jpy_to_eur(self.price_jpy)


def _map_condition(raw: str) -> str:
    for jp, fr in CONDITION_MAP.items():
        if jp in raw:
            return fr
    return raw or 'Non spécifié'


def _parse_price(text: str) -> Optional[int]:
    if not text:
        return None
    digits = re.sub(r'[^\d]', '', text)
    return int(digits) if digits else None


def _parse_posted_ago(text: str) -> str:
    if not text:
        return 'Inconnu'
    text = text.strip()
    text = re.sub(r'(\d+)分前', r'\1 minutes', text)
    text = re.sub(r'(\d+)時間前', r'\1 heures', text)
    text = re.sub(r'(\d+)日前', r'\1 jours', text)
    return text


def _extract_id(href: str) -> Optional[str]:
    m = re.search(r'[Ii]tem[Ii]d=([^&]+)', href or '')
    if m:
        return m.group(1)
    m = re.search(r'/product/([^/?]+)', href or '')
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# ZenMarket session-cookie fetch (curl-cffi, impersonates Chrome)
# ---------------------------------------------------------------------------

async def _zenmarket_fetch(target_url: str, max_retries: int = 3) -> str:
    """Fetch a ZenMarket page using a session cookie for authenticated access."""
    session_cookie = os.getenv('ZENMARKET_SESSION_COOKIE', '')
    if not session_cookie:
        logger.error('ZENMARKET_SESSION_COOKIE is not set — cannot fetch ZenMarket pages')
        return ''
    headers = {
        **_HTML_HEADERS,
        'Cookie': session_cookie,
    }
    delay = 2
    for attempt in range(max_retries):
        try:
            async with AsyncSession() as session:
                response = await session.get(
                    target_url,
                    headers=headers,
                    impersonate='chrome120',
                    timeout=30,
                )
                response.raise_for_status()
                return response.text
        except Exception as exc:
            logger.warning(
                'ZenMarket fetch attempt %d/%d for "%s": %s',
                attempt + 1, max_retries, target_url, exc,
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
                delay *= 2
    return ''


# ---------------------------------------------------------------------------
# ZenMarket AJAX fetch & JSON parser
# ---------------------------------------------------------------------------

async def _zenmarket_ajax_fetch(target_url: str) -> Optional[Any]:
    """Fetch a ZenMarket .ashx AJAX endpoint and return parsed JSON, or None on failure."""
    session_cookie = os.getenv('ZENMARKET_SESSION_COOKIE', '')
    headers = {
        **_AJAX_HEADERS,
        'Cookie': session_cookie,
    }
    try:
        async with AsyncSession() as session:
            response = await session.get(
                target_url,
                headers=headers,
                impersonate='chrome120',
                timeout=30,
            )
            response.raise_for_status()
            text = response.text.strip()
            if not text:
                return None
            if text[0] in ('{', '['):
                return json.loads(text)
            content_type = response.headers.get('Content-Type', '')
            if 'json' in content_type or 'javascript' in content_type:
                return json.loads(text)
    except Exception as exc:
        logger.debug('AJAX fetch failed for "%s": %s', target_url, exc)
    return None


def _parse_ajax_listings(data: Any, source: str) -> list[Listing]:
    """Map JSON from an AJAX endpoint to Listing objects.

    Handles common shapes: top-level list, or dict with an 'items'/'data'/
    'results'/'listings'/'products'/'list' key that contains a list.
    """
    if isinstance(data, dict):
        for key in ('items', 'data', 'results', 'listings', 'products', 'list'):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            for v in data.values():
                if isinstance(v, list):
                    data = v
                    break

    if not isinstance(data, list):
        return []

    listings: list[Listing] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            item_id = str(
                item.get('id') or item.get('itemId') or item.get('item_id') or
                item.get('code') or item.get('productId') or ''
            ).strip()
            if not item_id:
                continue

            title = str(
                item.get('name') or item.get('title') or item.get('productName') or
                item.get('caption') or item.get('description') or ''
            ).strip()

            price_raw = (
                item.get('price') or item.get('currentPrice') or item.get('sellPrice') or
                item.get('priceJpy') or item.get('price_jpy') or 0
            )
            current_price = (
                price_raw if isinstance(price_raw, int)
                else _parse_price(str(price_raw))
            )
            if not current_price:
                continue

            orig_raw = (
                item.get('originalPrice') or item.get('original_price') or
                item.get('beforePrice') or item.get('regularPrice')
            )
            original_price: Optional[int] = None
            if orig_raw:
                original_price = (
                    orig_raw if isinstance(orig_raw, int) else _parse_price(str(orig_raw))
                )
                if original_price and original_price <= current_price:
                    original_price = None

            image_url = str(
                item.get('imageUrl') or item.get('image_url') or item.get('image') or
                item.get('thumbnailUrl') or item.get('thumbnail') or item.get('photo') or ''
            ).strip()

            condition_raw = str(
                item.get('condition') or item.get('itemCondition') or
                item.get('conditionName') or ''
            ).strip()
            condition_fr = _map_condition(condition_raw)

            is_sold = item.get('isSold') or item.get('is_sold') or item.get('sold') or False
            status_raw = str(item.get('status') or item.get('itemStatus') or '').lower()
            status = 'sold' if (is_sold or 'sold' in status_raw or '売り切れ' in status_raw) else 'available'

            time_raw = str(
                item.get('postedAt') or item.get('posted_at') or item.get('createdAt') or
                item.get('time') or item.get('date') or ''
            ).strip()
            posted_ago = _parse_posted_ago(time_raw)

            if source == 'mercari':
                zen_url = ZENMARKET_MERCARI_LINK.format(item_id=item_id)
                direct_url = MERCARI_DIRECT_LINK.format(item_id=item_id)
            else:
                zen_url = str(item.get('url') or item.get('link') or ZENMARKET_MERCARI_LINK.format(item_id=item_id))
                direct_url = None

            listings.append(Listing(
                id=f'{source}_{item_id}',
                title=title or f'Article {item_id}',
                price_jpy=current_price,
                original_price_jpy=original_price,
                condition=condition_raw,
                condition_fr=condition_fr,
                status=status,
                image_url=image_url,
                url=zen_url,
                source=source,
                posted_ago=posted_ago,
                direct_url=direct_url,
            ))
        except Exception as exc:
            logger.debug('AJAX item parse error: %s', exc)

    return listings


# ---------------------------------------------------------------------------
# ZenMarket HTML parser (Mercari & Rakuma share the same page structure)
# ---------------------------------------------------------------------------

def _parse_zenmarket_html(html: str, source: str) -> list[Listing]:
    soup = BeautifulSoup(html, 'html.parser')
    listings: list[Listing] = []

    containers = (
        soup.select('.col-6.col-sm-6.col-md-4.col-lg-3')
        or soup.select('.product-container')
        or soup.select('.item-box')
        or soup.select('[class*="product"]')
        or soup.select('[class*="item"]')
    )
    if not containers:
        containers = [
            a.parent for a in soup.find_all('a', href=re.compile(r'[Ii]tem[Ii]d='))
            if a.find('img')
        ]

    for card in containers:
        try:
            anchor = card.find('a', href=re.compile(r'[Ii]tem[Ii]d=|/product/'))
            if not anchor:
                anchor = card if card.name == 'a' else None
            if not anchor:
                continue

            href = anchor.get('href', '')
            if href.startswith('/'):
                href = 'https://zenmarket.jp' + href

            item_id = _extract_id(anchor.get('href', ''))
            if not item_id:
                continue

            img = card.find('img')
            image_url = ''
            if img:
                image_url = img.get('data-src') or img.get('src') or ''

            title_el = (
                card.find(class_=re.compile(r'(name|title|caption|label)', re.I))
                or card.find('p')
                or card.find(['h3', 'h4', 'h5'])
            )
            title = title_el.get_text(strip=True) if title_el else ''

            price_els = card.find_all(class_=re.compile(r'price', re.I)) or \
                        card.find_all(string=re.compile(r'¥'))
            current_price: Optional[int] = None
            original_price: Optional[int] = None

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

            cond_el = card.find(class_=re.compile(r'condition|status|state', re.I))
            condition_raw = cond_el.get_text(strip=True) if cond_el else ''
            condition_fr = _map_condition(condition_raw)

            sold_marker = card.find(class_=re.compile(r'sold|soldout|unavailable', re.I)) or \
                          card.find(string=re.compile(r'SOLD|売り切れ|Sold', re.I))
            status = 'sold' if sold_marker else 'available'

            time_el = card.find(class_=re.compile(r'time|date|ago|when', re.I))
            posted_ago = _parse_posted_ago(time_el.get_text() if time_el else '')

            if source == 'mercari':
                zen_url = ZENMARKET_MERCARI_LINK.format(item_id=item_id)
                direct_url = MERCARI_DIRECT_LINK.format(item_id=item_id)
            else:
                zen_url = href
                direct_url = None

            listings.append(Listing(
                id=f'{source}_{item_id}',
                title=title or f'Article {item_id}',
                price_jpy=current_price,
                original_price_jpy=original_price if original_price and original_price > current_price else None,
                condition=condition_raw,
                condition_fr=condition_fr,
                status=status,
                image_url=image_url,
                url=zen_url,
                source=source,
                posted_ago=posted_ago,
                direct_url=direct_url,
            ))
        except Exception as exc:
            logger.debug('%s card parse error: %s', source, exc)

    return listings


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def fetch_listings(keyword: str, source: str, max_retries: int = 3) -> list[Listing]:
    """Fetch listings for a keyword from the given source ('mercari' or 'rakuma')."""
    encoded = quote(keyword)

    if source == 'mercari':
        url = ZENMARKET_MERCARI_HANDLER.format(query=encoded)
        referer = 'https://zenmarket.jp/mercari.aspx'
    elif source == 'rakuma':
        url = ZENMARKET_RAKUMA_HANDLER.format(query=encoded)
        referer = 'https://zenmarket.jp/rakuma.aspx'
    else:
        logger.error('Unknown source: %s', source)
        return []

    session_cookie = os.getenv('ZENMARKET_SESSION_COOKIE', '')
    headers = {
        **_AJAX_HEADERS,
        'Cookie': session_cookie,
        'Referer': referer,
    }

    delay = 2
    raw = ''
    for attempt in range(max_retries):
        try:
            async with AsyncSession() as session:
                response = await session.get(
                    url,
                    headers=headers,
                    impersonate='chrome120',
                    timeout=30,
                )
                response.raise_for_status()
                raw = response.text
            break
        except Exception as exc:
            logger.warning(
                'fetch_listings attempt %d/%d for "%s" on %s: %s',
                attempt + 1, max_retries, keyword, source, exc,
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
                delay *= 2

    print(f'[fetch_listings] {source} raw response ({len(raw)} chars):\n{raw[:3000]}')

    if not raw.strip():
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning('JSON parse failed for %s response: %s', source, exc)
        return []

    listings = _parse_ajax_listings(data, source)
    logger.info('Fetched %d listings for "%s" on %s', len(listings), keyword, source)
    return listings
