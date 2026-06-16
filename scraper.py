"""
ZenMarket scraper — Mercari Japan listings fetched directly from the Mercari
search API; ZenMarket item links are constructed from the returned item IDs.
Rakuma listings still use the ZenMarket HTML scrape path (legacy).
"""

import asyncio
import base64
import json
import logging
import re
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

from ecdsa import SigningKey, NIST256p
from ecdsa.util import sigencode_string
from curl_cffi import requests as cffi_requests
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

MERCARI_API_URL = 'https://api.mercari.jp/v2/entities:search'
ZENMARKET_MERCARI_LINK = 'https://zenmarket.jp/mercari.aspx?itemid={item_id}'
MERCARI_DIRECT_LINK = 'https://jp.mercari.com/item/{item_id}'

RAKUMA_SEARCH_URL = 'https://zenmarket.jp/rakuma.aspx?q={query}'

CONDITION_MAP = {
    '新品、未使用':       'Neuf',
    '新品未使用':         'Neuf',
    '未使用に近い':       'Très bon état',
    '目立つ傷や汚れなし': 'Très bon état',
    'やや傷や汚れあり':   'Bon état',
    '傷や汚れあり':       'État correct',
    '全体的に状態が悪い': 'Mauvais état',
}

# Mercari condition IDs (fallback when condition object is absent)
_CONDITION_ID_MAP = {
    1: '新品、未使用',
    2: '未使用に近い',
    3: '目立つ傷や汚れなし',
    4: 'やや傷や汚れあり',
    5: '傷や汚れあり',
    6: '全体的に状態が悪い',
}

_MERCARI_HEADERS = {
    'X-Platform': 'web',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Encoding': 'deflate, gzip',
    'Accept-Language': 'ja-JP,ja;q=0.9',
    'Content-Type': 'application/json; charset=utf-8',
    'Origin': 'https://jp.mercari.com',
    'Referer': 'https://jp.mercari.com/',
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/125.0.0.0 Safari/537.36'
    ),
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')


def _generate_dpop(method: str, url: str) -> str:
    """Generate a DPoP JWT signed with a fresh ECDSA P-256 key pair."""
    sk = SigningKey.generate(curve=NIST256p)
    vk = sk.get_verifying_key()
    pub_key = vk.to_string()
    x = _b64url(pub_key[:32])
    y = _b64url(pub_key[32:])
    jwk = {'crv': 'P-256', 'kty': 'EC', 'x': x, 'y': y}
    header_json = json.dumps({'typ': 'dpop+jwt', 'alg': 'ES256', 'jwk': jwk}, separators=(',', ':'))
    payload_json = json.dumps({
        'iat': int(time.time()),
        'jti': str(_uuid.uuid4()),
        'htu': url,
        'htm': method.upper(),
    }, separators=(',', ':'))
    signing_input = f'{_b64url(header_json.encode())}.{_b64url(payload_json.encode())}'
    sig_bytes = sk.sign(signing_input.encode('utf-8'), sigencode=sigencode_string)
    return f'{signing_input}.{_b64url(sig_bytes)}'


_HTML_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
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


def _posted_ago_from_ts(ts: int) -> str:
    delta = int(time.time()) - ts
    if delta < 3600:
        return f'{max(delta // 60, 1)} minutes'
    if delta < 86400:
        return f'{delta // 3600} heures'
    return f'{delta // 86400} jours'


# ---------------------------------------------------------------------------
# Mercari Japan API
# ---------------------------------------------------------------------------

def _parse_mercari_items(items: list) -> list[Listing]:
    listings: list[Listing] = []
    for item in items:
        try:
            item_id = item.get('id', '')
            if not item_id:
                continue

            title = item.get('name', '') or f'Article {item_id}'
            price_jpy = int(item.get('price', 0))
            if price_jpy <= 0:
                continue

            thumbnails = item.get('thumbnails') or []
            image_url = thumbnails[0] if thumbnails else ''

            cond = item.get('item_condition')
            if isinstance(cond, dict):
                condition_raw = cond.get('name', '')
            else:
                cond_id = item.get('item_condition_id', 0)
                condition_raw = _CONDITION_ID_MAP.get(int(cond_id), '')
            condition_fr = _map_condition(condition_raw)

            status_raw = item.get('status', 'on_sale')
            status = 'sold' if status_raw in ('sold_out', 'trading') else 'available'

            created = item.get('created', 0)
            posted_ago = _posted_ago_from_ts(int(created)) if created else 'Inconnu'

            listings.append(Listing(
                id=f'mercari_{item_id}',
                title=title,
                price_jpy=price_jpy,
                original_price_jpy=None,
                condition=condition_raw,
                condition_fr=condition_fr,
                status=status,
                image_url=image_url,
                url=ZENMARKET_MERCARI_LINK.format(item_id=item_id),
                source='mercari',
                posted_ago=posted_ago,
                direct_url=MERCARI_DIRECT_LINK.format(item_id=item_id),
            ))
        except Exception as exc:
            logger.debug('Mercari item parse error: %s', exc)
    return listings


async def _fetch_mercari_api(keyword: str, limit: int = 30, max_retries: int = 3) -> list[Listing]:
    body = {
        'userId': '',
        'pageSize': limit,
        'pageToken': '',
        'searchSessionId': str(_uuid.uuid4()),
        'indexRouting': 'INDEX_ROUTING_UNSPECIFIED',
        'thumbnailTypes': [],
        'searchCondition': {
            'keyword': keyword,
            'sort': 'SORT_CREATED_TIME',
            'order': 'ORDER_DESC',
            'status': ['STATUS_ON_SALE'],
            'sizeId': [], 'categoryId': [], 'brandId': [], 'sellerId': [],
            'priceMin': 0, 'priceMax': 0,
            'itemConditionId': [], 'shippingPayerId': [],
            'shippingFromArea': [], 'shippingMethod': [],
            'colorId': [], 'hasCoupon': False,
            'attributes': [], 'itemTypes': [], 'skuIds': [],
            'excludeKeyword': '',
        },
        'defaultDatasets': [],
        'serviceFrom': 'suruga',
    }
    delay = 2

    async with AsyncSession() as session:
        for attempt in range(max_retries):
            try:
                headers = dict(_MERCARI_HEADERS)
                headers['DPoP'] = _generate_dpop('POST', MERCARI_API_URL)

                response = await session.post(
                    MERCARI_API_URL,
                    impersonate='chrome120',
                    headers=headers,
                    json=body,
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()
                # v2 response shape: {"meta": {...}, "items": [...]}
                items = data.get('items') or data.get('data', {}).get('items', [])
                return _parse_mercari_items(items)
            except Exception as exc:
                logger.warning(
                    'Mercari API attempt %d/%d failed for "%s": %s',
                    attempt + 1, max_retries, keyword, exc,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
    return []


# ---------------------------------------------------------------------------
# Rakuma via ZenMarket HTML (legacy — may return 0 results if JS-rendered)
# ---------------------------------------------------------------------------

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
    m = re.search(r'itemId=([^&]+)', href or '')
    if m:
        return m.group(1)
    m = re.search(r'/product/([^/?]+)', href or '')
    if m:
        return m.group(1)
    return None


def _parse_rakuma_html(html: str) -> list[Listing]:
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
            a.parent for a in soup.find_all('a', href=re.compile(r'itemId='))
            if a.find('img')
        ]

    for card in containers:
        try:
            anchor = card.find('a', href=re.compile(r'itemId=|/product/'))
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

            listings.append(Listing(
                id=f'rakuma_{item_id}',
                title=title or f'Article {item_id}',
                price_jpy=current_price,
                original_price_jpy=original_price if original_price and original_price > current_price else None,
                condition=condition_raw,
                condition_fr=condition_fr,
                status=status,
                image_url=image_url,
                url=href,
                source='rakuma',
                posted_ago=posted_ago,
            ))
        except Exception as exc:
            logger.debug('Rakuma card parse error: %s', exc)

    return listings


async def _fetch_rakuma(keyword: str, max_retries: int = 3) -> list[Listing]:
    url = RAKUMA_SEARCH_URL.format(query=quote(keyword))
    delay = 2
    async with AsyncSession() as session:
        for attempt in range(max_retries):
            try:
                response = await session.get(
                    url,
                    impersonate='chrome120',
                    headers=_HTML_HEADERS,
                    timeout=30,
                )
                response.raise_for_status()
                return _parse_rakuma_html(response.text)
            except Exception as exc:
                logger.warning(
                    'Rakuma fetch attempt %d/%d failed for "%s": %s',
                    attempt + 1, max_retries, keyword, exc,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
    return []


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def fetch_listings(keyword: str, source: str, max_retries: int = 3) -> list[Listing]:
    """Fetch listings for a keyword from the given source ('mercari' or 'rakuma')."""
    if source == 'mercari':
        listings = await _fetch_mercari_api(keyword, max_retries=max_retries)
    elif source == 'rakuma':
        listings = await _fetch_rakuma(keyword, max_retries=max_retries)
    else:
        logger.error('Unknown source: %s', source)
        return []
    logger.info('Fetched %d listings for "%s" on %s', len(listings), keyword, source)
    return listings
