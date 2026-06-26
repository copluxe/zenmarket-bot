"""
Mercari Japan scraper — uses Playwright + Google Chrome to render pages
and intercept the internal API response. ZenMarket URLs are constructed
from Mercari item IDs. No cookies required.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

MERCARI_ITEM_URL = 'https://jp.mercari.com/item/{item_id}'

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
    status: str
    image_url: str
    url: str
    zenmarket_url: str
    source: str
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


def _parse_listings(items: list[dict], query: str) -> list[Listing]:
    if not items:
        return []

    listings: list[Listing] = []

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
            zenmarket_url = f'https://zenmarket.jp/mercariproduct.aspx/?itemCode={item_id}'

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


# ---------------------------------------------------------------------------
# Playwright browser singleton
# ---------------------------------------------------------------------------

_pw = None
_browser = None


async def _ensure_browser():
    global _pw, _browser
    from playwright.async_api import async_playwright

    if _browser is not None:
        try:
            if _browser.is_connected():
                return _browser
        except Exception:
            pass

    if _pw is None:
        _pw = await async_playwright().start()

    _browser = await _pw.chromium.launch(
        headless=True,
        channel='chrome',
        args=[
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-setuid-sandbox',
            '--disable-gpu',
        ],
    )
    logger.info('Chrome browser launched')
    return _browser


async def fetch_listings(keyword: str, source: str = 'mercari', max_retries: int = 3) -> list[Listing]:
    """Fetch listings from Mercari Japan using a real Chrome browser."""
    global _browser
    encoded = quote(keyword)
    url = (
        f'https://jp.mercari.com/search?keyword={encoded}'
        '&status=on_sale&sort=created_time&order=desc'
    )

    for attempt in range(max_retries):
        context = None
        try:
            browser = await _ensure_browser()
            context = await browser.new_context(
                locale='ja-JP',
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                ),
            )
            page = await context.new_page()

            captured_items: list[dict] = []

            async def on_response(response):
                if 'entities:search' in response.url:
                    try:
                        data = await response.json()
                        if isinstance(data.get('items'), list):
                            captured_items.extend(data['items'])
                    except Exception:
                        pass

            page.on('response', on_response)

            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(5000)

            await context.close()
            context = None

            if not captured_items:
                logger.warning('No items captured for "%s"', keyword)

            listings = _parse_listings(captured_items, keyword)
            logger.info('Fetched %d listings for "%s" via Mercari', len(listings), keyword)
            return listings

        except Exception as exc:
            logger.warning(
                'fetch_listings attempt %d/%d for "%s": %s',
                attempt + 1, max_retries, keyword, exc,
            )
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            _browser = None
            if attempt < max_retries - 1:
                await asyncio.sleep(5)

    return []
