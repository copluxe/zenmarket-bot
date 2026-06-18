"""
One-shot diagnostic: fetch ZenMarket LV search and dump the first item card HTML.
Run: python3 debug_zm.py
"""
import asyncio, os, re
from dotenv import load_dotenv
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

load_dotenv()

async def main():
    cookies = {
        'cf_clearance': os.getenv('ZENMARKET_CF_CLEARANCE', ''),
        'zlang': 'fr',
        'prefCCcurrency': 'JPY',
    }
    if os.getenv('ZENMARKET_SESSION_ID'):
        cookies['ASP.NET_SessionId'] = os.getenv('ZENMARKET_SESSION_ID')
    if os.getenv('ZENMARKET_ZENUAUTH'):
        cookies['.zenuauth'] = os.getenv('ZENMARKET_ZENUAUTH')
    if os.getenv('ZENMARKET_ARR'):
        cookies['ARRAffinity'] = os.getenv('ZENMARKET_ARR')
        cookies['ARRAffinitySameSite'] = os.getenv('ZENMARKET_ARR')

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'fr-FR,fr;q=0.9',
        'Referer': 'https://zenmarket.jp/',
    }

    url = 'https://zenmarket.jp/mercari.aspx?q=%E3%83%AB%E3%82%A4%E3%83%B4%E3%82%A3%E3%83%88%E3%83%B3'
    async with AsyncSession(impersonate='chrome124') as session:
        r = await session.get(url, headers=headers, cookies=cookies)
        html = r.text

    soup = BeautifulSoup(html, 'html.parser')
    a_tag = soup.find('a', href=re.compile(r'itemCode=m\d+', re.I))
    if not a_tag:
        print('No item anchor found — check cookies')
        return

    print('=== A_TAG ATTRS ===')
    print(a_tag.attrs)

    print('\n=== A_TAG HTML (first 1000 chars) ===')
    print(str(a_tag)[:1000])

    # Walk up to card level
    card = a_tag
    for _ in range(3):
        if card.parent:
            card = card.parent
    print('\n=== CARD HTML (first 2000 chars) ===')
    print(str(card)[:2000])

    print('\n=== ALL TEXT IN CARD ===')
    print(repr(card.get_text(separator=' | ', strip=True)))

asyncio.run(main())
