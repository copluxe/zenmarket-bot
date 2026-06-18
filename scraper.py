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
ZENMARKET_SEARCH_URL = 'https://zenmarket.jp/mercari.aspx?q={query}'
ZENMARKET_ITEM_URL = 'https://zenmarket.jp/fr/mercariitem.aspx?itemCode={item_id}'

# Matches ¥ prices with comma or space thousand separators: "¥ 45,000" / "45 000 ¥" / "￥15000"
_PRICE_RE = re.compile(r'[¥￥]\s*([\d][\d\s,]*\d)|([\d][\d\s,]*\d)\s*[¥￥]')

# Matches € prices in French format: "€226,04"  "€2 769,01"  "€2\xa0769,01"
# Group 1 = digits after €, group 2 = digits before €
_EUR_PRICE_RE = re.compile(
    r'€\s*([\d][\d  ]*(?:[,]\d{1,2})?)'
    r'|([\d][\d  ]*(?:[,]\d{1,2})?)\s*€'
)

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


def eur_to_jpy(eur: float) -> int:
    rate = _get_eur_per_jpy()
    return round(eur / rate) if rate else round(eur * 160)


def _parse_eur_amount(raw: str) -> Optional[float]:
    """Parse a French-format EUR amount like '226,04' or '2\xa0769,01'."""
    clean = re.sub(r'[\s\xa0]', '', raw.strip())
    clean = clean.replace(',', '.')
    try:
        val = float(clean)
        return val if 0.5 <= val <= 150_000.0 else None
    except ValueError:
        return None


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

    cookies: dict[str, str] = {
        'cf_clearance': cf_clearance,
        'zlang': 'fr',            # UI in French
        'prefCCcurrency': 'JPY',  # Force JPY prices so price parsing always sees ¥
    }

    session_id = os.getenv('ZENMARKET_SESSION_ID')
    if session_id:
        cookies['ASP.NET_SessionId'] = session_id

    # Support both legacy .ASPXAUTH and newer .zenuauth token
    auth = os.getenv('ZENMARKET_AUTH')
    if auth:
        cookies['.ASPXAUTH'] = auth

    zenuauth = os.getenv('ZENMARKET_ZENUAUTH')
    if zenuauth:
        cookies['.zenuauth'] = zenuauth

    arr = os.getenv('ZENMARKET_ARR')
    if arr:
        cookies['ARRAffinity'] = arr
        cookies['ARRAffinitySameSite'] = arr

    return cookies


def _debug_card_structure(a_tag):
    """Log the ancestor chain for one item anchor to reveal where prices live."""
    node = a_tag
    for level in range(10):
        node = node.parent
        if node is None:
            break
        txt = node.get_text(separator=' ').strip().replace('\n', ' ')
        yen_hits = len(list(_PRICE_RE.finditer(node.get_text(separator=''))))
        num_hits = re.findall(r'\b\d[\d,\s]{2,6}\d\b', node.get_text(separator=' '))
        logger.warning(
            'DOM L%d <%s class=%r> ¥=%d nums=%s | %.100s',
            level + 1, node.name, ' '.join(node.get('class', [])),
            yen_hits, num_hits[:5], txt[:100],
        )


def _price_from_card(a_tag) -> Optional[int]:
    """Walk up from an item anchor to find its price.

    ZenMarket shows prices in EUR (French format) when the account has EUR
    preference, ignoring the prefCCcurrency cookie. We try ¥ first, then € and
    convert to JPY. We stop climbing as soon as a container has > 2 prices
    (meaning we've left the card scope and entered the grid).
    """
    node = a_tag.parent
    for _ in range(8):
        if node is None:
            return None
        combined = node.get_text(separator='')

        # --- Try JPY (¥) prices ---
        jpy_valid = [
            int(re.sub(r'[^\d]', '', m.group(1) or m.group(2) or ''))
            for m in _PRICE_RE.finditer(combined)
            if re.sub(r'[^\d]', '', m.group(1) or m.group(2) or '')
            and 100 <= int(re.sub(r'[^\d]', '', m.group(1) or m.group(2) or '')) <= 10_000_000
        ]
        if jpy_valid:
            return jpy_valid[0] if len(jpy_valid) <= 2 else None

        # --- Try EUR (€) prices — French format "€226,04" / "€2\xa0769,01" ---
        eur_valid = [
            v for v in (
                _parse_eur_amount(m.group(1) or m.group(2) or '')
                for m in _EUR_PRICE_RE.finditer(combined)
            )
            if v is not None
        ]
        if eur_valid:
            return eur_to_jpy(eur_valid[0]) if len(eur_valid) <= 2 else None

        node = node.parent
    return None


def _items_from_zenmarket_html(html: str) -> list[dict]:
    """Extract listings from ZenMarket Mercari search page HTML."""
    soup = BeautifulSoup(html, 'html.parser')
    seen_ids: set[str] = set()
    items = []

    for a_tag in soup.find_all('a', class_='product-item'):
        href = a_tag.get('href', '')
        m = re.search(r'itemCode=(m\d+)', href, re.I)
        if not m:
            continue
        item_id = m.group(1)
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        if href.startswith('http'):
            zm_url = href
        else:
            zm_url = 'https://zenmarket.jp' + (href if href.startswith('/') else '/' + href)

        img = a_tag.find('img')
        image_url = (img.get('src') or img.get('data-src') or img.get('data-lazy') or '') if img else ''

        # Title is in <h3 class="item-title"> — it shows the Mercari category path
        # translated to French (e.g. "Sacs, Sacs à main Louis Vuitton M47270").
        title_elem = a_tag.find('h3', class_='item-title')
        if title_elem:
            title = (title_elem.get('title', '') or title_elem.get_text(strip=True)).strip()
        else:
            title = (a_tag.get('title', '') or a_tag.get('aria-label', '') or '').strip()

        # Price: span.amount carries data-jpy="¥1,800" — use it directly so we
        # never have to convert from EUR.
        price = None
        amount_elem = a_tag.find('span', class_='amount')
        if amount_elem:
            jpy_raw = amount_elem.get('data-jpy', '')
            digits = re.sub(r'[^\d]', '', jpy_raw)
            if digits:
                val = int(digits)
                if 100 <= val <= 100_000_000:
                    price = val

        if price is None:
            price = _price_from_card(a_tag)  # EUR fallback

        if price is None:
            logger.warning('No price found for item %s', item_id)
            continue

        logger.info('Item %s → ¥%d  %s', item_id, price, zm_url)
        items.append({
            'id': item_id,
            'name': title,
            'price': price,
            'zm_url': zm_url,
            'thumbnails': [image_url] if image_url else [],
            'item_condition': {},
            'status': 'on_sale',
            'created': None,
        })

    logger.info('ZenMarket: %d items parsed', len(items))
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

    for item in items:
        try:
            item_id = str(item.get('id', '')).strip()
            if not item_id:
                continue

            zm_url = item.get('zm_url')
            zenmarket_url = zm_url if zm_url else ZENMARKET_ITEM_URL.format(item_id=item_id)

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
