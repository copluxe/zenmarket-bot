"""
Mercari Japan scraper — uses ZenMarket + curl-cffi (Cloudflare bypass) when
ZENMARKET_* cookies are configured in .env; falls back to jp.mercari.com via httpx.
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)

MERCARI_SEARCH_URL = 'https://jp.mercari.com/search?keyword={keyword}&status=on_sale'
MERCARI_ITEM_URL = 'https://jp.mercari.com/item/{item_id}'
ZENMARKET_SEARCH_URL = 'https://zenmarket.jp/fr/mercari.aspx?q={query}'

_BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'fr-FR,fr;q=0.9,ja;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'Referer': 'https://zenmarket.jp/',
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

_eur_rate_cache: dict = {'rate': None, 'fetched_at': 0.0}
_EUR_CACHE_TTL = 3600


def _get_eur_per_jpy() -> float:
    now = time.time()
    if _eur_rate_cache['rate'] and now - _eur_rate_cache['fetched_at'] < _EUR_CACHE_TTL:
        return _eur_rate_cache['rate']
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(
                'https://open.er-api.com/v6/latest/JPY',
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
    status: str           # 'available' | 'sold'
    image_url: str
    url: str              # Mercari JP direct link
    zenmarket_url: str    # ZenMarket search link for ordering
    source: str           # 'mercari'
    posted_ago: str
    direct_url: Optional[str] = None
    price_eur: int = field(init=False)

    def __post_init__(self):
        self.price_eur = jpy_to_eur(self.price_jpy)


def _map_condition(raw: str) -> str:
    for jp, fr in CONDITION_MAP.items():
        if jp in raw:
            return fr
    return raw or 'Non spécifié'


def _parse_posted_ago(timestamp: Optional[int]) -> str:
    if not timestamp:
        return 'Inconnu'
    elapsed = int(time.time()) - timestamp
    if elapsed < 60:
        return "À l'instant"
    if elapsed < 3600:
        m = elapsed // 60
        return f'{m} minute{"s" if m > 1 else ""}'
    if elapsed < 86400:
        h = elapsed // 3600
        return f'{h} heure{"s" if h > 1 else ""}'
    d = elapsed // 86400
    return f'{d} jour{"s" if d > 1 else ""}'


def _get_zenmarket_cookies() -> Optional[dict]:
    """Build cookie dict from ZENMARKET_* env vars; returns None if CF clearance is absent."""
    cf_clearance = os.getenv('ZENMARKET_CF_CLEARANCE')
    if not cf_clearance:
        return None

    cookies: dict[str, str] = {'cf_clearance': cf_clearance}

    session_id = os.getenv('ZENMARKET_SESSION_ID')
    if session_id:
        cookies['ASP.NET_SessionId'] = session_id

    auth = os.getenv('ZENMARKET_AUTH')
    if auth:
        cookies['.ASPXAUTH'] = auth

    arr = os.getenv('ZENMARKET_ARR')
    if arr:
        cookies['ARRAffinity'] = arr

    return cookies


def _items_from_zenmarket_html(html: str) -> list[dict]:
    """Extract listings from ZenMarket Mercari search page HTML."""
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    seen_ids: set[str] = set()

    for a_tag in soup.find_all('a', href=re.compile(r'itemCode=m\d+', re.I)):
        href = a_tag.get('href', '')
        m = re.search(r'itemCode=(m\d+)', href, re.I)
        if not m:
            continue
        item_id = m.group(1)
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        img = a_tag.find('img')
        image_url = ''
        if img:
            image_url = img.get('src') or img.get('data-src') or img.get('data-lazy') or ''

        title = (
            a_tag.get('title', '')
            or (img.get('alt', '') if img else '')
            or a_tag.get_text(strip=True)[:120]
        ).strip()

        # Walk up 3 levels to get a wide enough card container
        container = a_tag
        for _ in range(3):
            if container.parent:
                container = container.parent
            else:
                break

        # Priority: element with "price" in its class name
        price_digits = ''
        price_el = container.find(class_=re.compile(r'price', re.I))
        if price_el:
            digits = re.sub(r'[^\d]', '', price_el.get_text())
            if len(digits) >= 3:
                price_digits = digits

        # Fallback: largest number ≥ 100 in the container (avoids picking sub-prices/fees)
        if not price_digits:
            candidates: list[int] = []
            for text_node in container.find_all(string=re.compile(r'[\d,]{3,}')):
                digits = re.sub(r'[^\d]', '', str(text_node))
                if len(digits) >= 3:
                    val = int(digits)
                    if val >= 100:
                        candidates.append(val)
            if candidates:
                price_digits = str(max(candidates))

        if not price_digits:
            continue

        items.append({
            'id': item_id,
            'name': title,
            'price': int(price_digits),
            'thumbnails': [image_url] if image_url else [],
            'item_condition': {},
            'status': 'on_sale',
            'created': None,
        })

    return items


def _items_from_next_data(html: str) -> list[dict]:
    """Extract items from Next.js __NEXT_DATA__ JSON embedded in the page."""
    soup = BeautifulSoup(html, 'html.parser')
    tag = soup.find('script', id='__NEXT_DATA__')
    if not tag or not tag.string:
        return []
    try:
        data = json.loads(tag.string)
    except json.JSONDecodeError:
        return []

    page_props = data.get('props', {}).get('pageProps', {})

    for path in [
        ['items'],
        ['searchResult', 'items'],
        ['initialSearchList', 'items'],
        ['searchListResponse', 'items'],
        ['data', 'items'],
    ]:
        obj = page_props
        for key in path:
            obj = obj.get(key, {}) if isinstance(obj, dict) else {}
        if isinstance(obj, list) and obj:
            return obj

    return []


def _items_from_html(html: str) -> list[dict]:
    """Fallback: extract listing data from raw HTML anchor tags."""
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    seen_ids: set[str] = set()

    for a_tag in soup.find_all('a', href=re.compile(r'/item/m\d+')):
        href = a_tag.get('href', '')
        m = re.search(r'/item/(m\d+)', href)
        if not m:
            continue
        item_id = m.group(1)
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        img = a_tag.find('img')
        image_url = img.get('src', '') if img else ''
        title = (
            a_tag.get('aria-label', '')
            or (img.get('alt', '') if img else '')
        ).strip()

        price_text = ''
        for candidate in a_tag.find_all(string=re.compile(r'[¥￥]?\s*[\d,]+')):
            if re.search(r'[\d,]{2,}', str(candidate)):
                price_text = str(candidate)
                break

        digits = re.sub(r'[^\d]', '', price_text)
        if not digits:
            continue

        items.append({
            'id': item_id,
            'name': title,
            'price': int(digits),
            'thumbnails': [image_url] if image_url else [],
            'item_condition': {},
            'status': 'on_sale',
            'created': None,
        })

    return items


def _parse_listings(items: list[dict], query: str) -> list[Listing]:
    if not items:
        return []

    listings: list[Listing] = []
    zenmarket_url = ZENMARKET_SEARCH_URL.format(query=quote(query))

    for item in items:
        try:
            item_id = str(item.get('id', '')).strip()
            if not item_id:
                continue

            title = str(item.get('name', '') or item.get('title', '')).strip()

            price_jpy = item.get('price', 0)
            if not isinstance(price_jpy, int):
                try:
                    price_jpy = int(price_jpy)
                except (ValueError, TypeError):
                    continue
            if not price_jpy:
                continue

            image_url = str(
                item.get('thumbnails', [None])[0]
                if item.get('thumbnails')
                else item.get('image_url') or item.get('photo') or ''
            ).strip()

            condition_raw = str(
                item.get('item_condition', {}).get('name', '')
                if isinstance(item.get('item_condition'), dict)
                else item.get('item_condition') or item.get('condition') or ''
            ).strip()
            condition_fr = _map_condition(condition_raw)

            item_status_raw = str(item.get('status', '') or item.get('item_status', '')).lower()
            status = 'sold' if ('sold' in item_status_raw or '売り切れ' in item_status_raw) else 'available'

            created = item.get('created') or item.get('created_time') or item.get('updated')
            posted_ago = _parse_posted_ago(created)

            mercari_url = MERCARI_ITEM_URL.format(item_id=item_id)

            listings.append(Listing(
                id=f'mercari_{item_id}',
                title=title or f'Article {item_id}',
                price_jpy=price_jpy,
                original_price_jpy=None,
                condition=condition_raw,
                condition_fr=condition_fr,
                status=status,
                image_url=image_url,
                url=mercari_url,
                zenmarket_url=zenmarket_url,
                source='mercari',
                posted_ago=posted_ago,
                direct_url=mercari_url,
            ))
        except Exception as exc:
            logger.debug('Item parse error: %s', exc)

    return listings


async def fetch_listings(keyword: str, source: str = 'mercari', max_retries: int = 3) -> list[Listing]:
    """Fetch listings via ZenMarket (curl-cffi + CF cookies) or Mercari JP directly (httpx)."""
    encoded = quote(keyword)
    cookies = _get_zenmarket_cookies()

    if cookies:
        url = ZENMARKET_SEARCH_URL.format(query=encoded)
        logger.debug('ZenMarket mode (CF cookies present) for "%s"', keyword)
    else:
        url = MERCARI_SEARCH_URL.format(keyword=encoded)
        logger.debug('Mercari direct mode (no CF cookies) for "%s"', keyword)

    delay = 2
    for attempt in range(max_retries):
        try:
            if cookies:
                async with AsyncSession(impersonate='chrome124') as session:
                    response = await session.get(url, headers=_BROWSER_HEADERS, cookies=cookies)
                    response.raise_for_status()
                    html = response.text

                items = _items_from_zenmarket_html(html)
                if not items:
                    logger.debug('ZenMarket parser found nothing for "%s", trying __NEXT_DATA__', keyword)
                    items = _items_from_next_data(html) or _items_from_html(html)
            else:
                async with httpx.AsyncClient(
                    timeout=30,
                    headers=_BROWSER_HEADERS,
                    follow_redirects=True,
                ) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    html = response.text

                items = _items_from_next_data(html)
                if not items:
                    logger.debug('No __NEXT_DATA__ items for "%s", falling back to HTML parsing', keyword)
                    items = _items_from_html(html)

            if not items:
                logger.warning('No items found in page for "%s"', keyword)

            listings = _parse_listings(items, keyword)
            logger.info(
                'Fetched %d listings for "%s" via %s',
                len(listings), keyword, 'ZenMarket' if cookies else 'Mercari',
            )
            return listings

        except Exception as exc:
            logger.warning(
                'fetch_listings attempt %d/%d for "%s": %s',
                attempt + 1, max_retries, keyword, exc,
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
                delay *= 2

    return []
