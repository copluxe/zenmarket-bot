#!/usr/bin/env python3
"""
discover_ajax.py — find the ZenMarket AJAX endpoint for listing data.

Run locally (not on the server) with your ZENMARKET_SESSION_COOKIE set:

    ZENMARKET_SESSION_COOKIE="..." python discover_ajax.py
    # or, if .env is populated:
    python discover_ajax.py

The script:
  1. Fetches the raw HTML of the Mercari search page.
  2. Scans inline <script> blocks for fetch/ajax/URL patterns.
  3. Downloads each zenmarket.jp JS bundle and searches it.
  4. Probes ~15 common AJAX endpoint candidates.
  5. Tries an ASP.NET UpdatePanel POST if the page uses WebForms.
"""

import asyncio
import os
import re
import sys

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SEARCH_URL = 'https://zenmarket.jp/mercari.aspx?q=louis+vuitton'
BASE_URL   = 'https://zenmarket.jp'

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Referer': 'https://zenmarket.jp/',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'same-origin',
}

_AJAX_HEADERS = {
    **_HEADERS,
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'X-Requested-With': 'XMLHttpRequest',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
}

# Patterns that look like internal URL strings in JS source
_URL_RE = re.compile(
    r"""['"](/(?:api|search|ajax|en|mercari|rakuma|product|item|handler|'
    r'ashx|json|data|fetch|query|list)[^'"<>]{0,200}?)['"]""",
    re.IGNORECASE,
)

# Lines around AJAX-style calls
_AJAX_RE = re.compile(
    r'(?:fetch\s*\(|axios\s*\.\s*(?:get|post)|'
    r'\$\s*\.\s*(?:ajax|get|post|getJSON)|'
    r'XMLHttpRequest|new\s+Request\s*\()',
    re.IGNORECASE,
)

# Endpoint candidates to probe (GET, JSON accept)
_CANDIDATES = [
    '/en/mercari/search?q=louis+vuitton',
    '/api/mercari/search?q=louis+vuitton',
    '/api/search?q=louis+vuitton&type=mercari',
    '/api/v1/search?q=louis+vuitton&type=mercari',
    '/api/v2/search?q=louis+vuitton&type=mercari',
    '/mercari/search.json?q=louis+vuitton',
    '/SearchHandler.ashx?q=louis+vuitton&source=mercari',
    '/MercariHandler.ashx?q=louis+vuitton',
    '/mercari.aspx/GetItems',
    '/mercari.aspx/Search',
    '/handlers/search.ashx?q=louis+vuitton',
    '/api/items?q=louis+vuitton&platform=mercari',
    '/search/mercari?q=louis+vuitton&format=json',
    '/en/search?q=louis+vuitton&source=mercari&format=json',
    '/mercariitems.ashx?q=louis+vuitton',
]


def _banner(title: str) -> None:
    print(f'\n{"=" * 70}')
    print(f'  {title}')
    print('=' * 70)


async def discover() -> None:
    cookie = os.getenv('ZENMARKET_SESSION_COOKIE', '').strip()
    if not cookie:
        print('[ERROR] ZENMARKET_SESSION_COOKIE is not set.')
        print('        Export it or add it to your .env file, then re-run.')
        sys.exit(1)

    auth_headers = {**_HEADERS, 'Cookie': cookie}

    # ------------------------------------------------------------------
    # Step 1: Fetch the main search page
    # ------------------------------------------------------------------
    _banner('Step 1 — Fetch main search page')
    print(f'  GET {SEARCH_URL}')
    async with AsyncSession() as s:
        resp = await s.get(SEARCH_URL, headers=auth_headers, impersonate='chrome120', timeout=30)
    status = resp.status_code
    html   = resp.text
    print(f'  HTTP {status}   Length: {len(html):,} chars')

    if status != 200:
        print(f'  [WARN] Unexpected status — cookie may be expired.')

    soup = BeautifulSoup(html, 'html.parser')

    # Quick sanity check: does the page even have listing containers?
    containers = (
        soup.select('.col-6.col-sm-6.col-md-4.col-lg-3')
        or soup.select('.product-container')
        or soup.select('.item-box')
    )
    print(f'  Static listing containers found in HTML: {len(containers)}')
    if not containers:
        print('  → Confirmed: listings are NOT in the initial HTML (AJAX-loaded).')

    # ------------------------------------------------------------------
    # Step 2: Scan inline <script> blocks
    # ------------------------------------------------------------------
    _banner('Step 2 — Scan inline <script> blocks')
    inline_urls: set[str] = set()
    ajax_snippets: list[str] = []

    for script in soup.find_all('script'):
        src = script.get('src')
        if src:
            continue  # external file, handled later
        text = script.string or ''
        if not text.strip():
            continue

        for m in _URL_RE.finditer(text):
            inline_urls.add(m.group(1))

        # Grab lines with AJAX calls + a few lines of context
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if _AJAX_RE.search(line):
                ctx = '\n'.join(lines[max(0, i-1): i+4])
                ajax_snippets.append(ctx)

    if inline_urls:
        print('  URL-like strings in inline JS:')
        for u in sorted(inline_urls):
            print(f'    {u}')
    else:
        print('  No URL strings found in inline JS.')

    if ajax_snippets:
        print('\n  AJAX call sites in inline JS:')
        for snip in ajax_snippets:
            print('  ---')
            for line in snip.splitlines():
                print(f'    {line}')
    else:
        print('  No AJAX call patterns found in inline JS.')

    # ------------------------------------------------------------------
    # Step 3: Extract and search external JS bundles
    # ------------------------------------------------------------------
    _banner('Step 3 — External JS bundles')
    js_urls: list[str] = []
    for tag in soup.find_all('script', src=True):
        raw = tag['src']
        if raw.startswith('//'):
            raw = 'https:' + raw
        elif raw.startswith('/'):
            raw = BASE_URL + raw
        if BASE_URL in raw:
            js_urls.append(raw)
            print(f'  {raw}')

    if not js_urls:
        print('  No zenmarket.jp JS files found.')

    bundle_urls: set[str] = set()
    bundle_ajax:  list[str] = []

    async with AsyncSession() as s:
        for js_url in js_urls:
            try:
                r = await s.get(js_url, headers=_HEADERS, impersonate='chrome120', timeout=15)
                text = r.text
                print(f'\n  Scanning ({len(text):,} chars): {js_url}')

                for m in _URL_RE.finditer(text):
                    u = m.group(1)
                    if any(k in u.lower() for k in ['search', 'api', 'ajax', 'item', 'mercari', 'rakuma', 'handler', 'json']):
                        bundle_urls.add(u)
                        print(f'    URL: {u}')

                lines = text.splitlines()
                for i, line in enumerate(lines):
                    if _AJAX_RE.search(line):
                        ctx = '\n'.join(lines[max(0, i-1): i+5])
                        bundle_ajax.append(ctx)
                        print(f'    AJAX site → {line.strip()[:120]}')
            except Exception as exc:
                print(f'  Error fetching {js_url}: {exc}')

    # ------------------------------------------------------------------
    # Step 4: Probe common AJAX endpoint candidates
    # ------------------------------------------------------------------
    _banner('Step 4 — Probe candidate endpoints')
    ajax_auth = {**_AJAX_HEADERS, 'Cookie': cookie}

    async with AsyncSession() as s:
        for path in _CANDIDATES:
            url = BASE_URL + path
            try:
                r = await s.get(url, headers=ajax_auth, impersonate='chrome120', timeout=10)
                ct = r.headers.get('content-type', '')
                flag = ' *** JSON! ***' if (r.status_code == 200 and 'json' in ct.lower()) else ''
                print(f'  HTTP {r.status_code}  {ct[:55]:<55}  {path}{flag}')
                if flag:
                    print(f'  Preview: {r.text[:600]}')
            except Exception as exc:
                print(f'  ERROR  {path}: {exc}')

    # ------------------------------------------------------------------
    # Step 5: ASP.NET UpdatePanel POST attempt
    # ------------------------------------------------------------------
    _banner('Step 5 — ASP.NET UpdatePanel POST')
    hidden = {
        inp['name']: inp.get('value', '')
        for inp in soup.find_all('input', type='hidden')
        if inp.get('name')
    }

    if '__VIEWSTATE' not in hidden:
        print('  No __VIEWSTATE found — page likely uses a REST/JSON API, not UpdatePanel.')
        print('  If Step 4 found nothing, inspect the Network tab in browser DevTools.')
    else:
        print(f'  __VIEWSTATE present. Hidden fields: {list(hidden.keys())}')
        form = soup.find('form')
        action = (form.get('action') if form else None) or '/mercari.aspx'
        if not action.startswith('http'):
            action = BASE_URL + action

        post_data = {
            **hidden,
            'q': 'louis vuitton',
            '__ASYNCPOST': 'true',
        }
        # Find ScriptManager field to trigger partial postback
        sm = soup.find(id=re.compile(r'ScriptManager|ToolkitScriptManager', re.I))
        if sm:
            sm_id = sm['id']
            post_data['ctl00$ScriptManager1'] = f'{sm_id}|{sm_id}'

        post_hdrs = {
            **auth_headers,
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-MicrosoftAjax': 'Delta=true',
            'X-Requested-With': 'XMLHttpRequest',
        }
        print(f'\n  POST {action}')
        async with AsyncSession() as s:
            r = await s.post(action, data=post_data, headers=post_hdrs, impersonate='chrome120', timeout=30)
        ct = r.headers.get('content-type', '')
        print(f'  HTTP {r.status_code}  {ct}')
        print(f'  Response (first 1 500 chars):\n{r.text[:1500]}')

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    _banner('Summary')
    all_urls = inline_urls | bundle_urls
    if all_urls:
        print('  URL strings found in JS (review manually):')
        for u in sorted(all_urls):
            print(f'    {u}')
    print()
    print('  Next steps:')
    print('  1. Look for a "HTTP 200 + JSON" line in Step 4 above.')
    print('  2. If nothing found, open Chrome DevTools → Network → XHR/Fetch')
    print('     while loading zenmarket.jp/mercari.aspx?q=louis+vuitton')
    print('     and note the URL, method, and request headers of the listing call.')
    print('  3. Share the endpoint with Claude Code so it can update scraper.py.')


if __name__ == '__main__':
    asyncio.run(discover())
