"""
Mercari Japan Discord Bot
Monitors Mercari Japan and posts luxury goods listings.
"""

import asyncio
import logging
import logging.handlers
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord.ext import tasks

import time

import config
import database
from channel_manager import setup_guild
from embeds import ListingView, build_listing_embed
from router import BRANDS, get_channels
from scraper import Listing, fetch_brand_listings

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging():
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    file_handler = logging.handlers.RotatingFileHandler(
        'errors.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
    )
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


logger = logging.getLogger(__name__)

# Japan Standard Time (UTC+9)
JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# Price filters per channel (min_eur, max_eur). No entry = no filter.
# ---------------------------------------------------------------------------

CHANNEL_PRICE_FILTERS: dict[str, tuple[int, int]] = {
    'lv-neverfull':     (100, 500),
    'lv-keepall':       (100, 500),
    'lv-speedy':        (100, 500),
    'lv-alma':          (100, 500),
    'lv-saint-cloud':   (40,  300),
    'lv-noe':           (100, 500),
    'lv-papillon':      (100, 500),
    'lv-boston':        (100, 500),
    'lv-trouville':     (100, 500),
    'lv-portefeuilles': (0,    90),
    'lv-ceintures':     (40,  300),
    'lv-etuis':         (0,    70),
    'lv-sacoches':      (40,  300),
    'lv-pochettes':     (40,  300),
    'lv-sacs':          (100, 500),
    'lv-sacs-a-main':   (100, 500),
    'lv-cabas':         (100, 500),
}

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class ZenMarketBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True
        super().__init__(intents=intents)

        self.guild: Optional[discord.Guild] = None
        self.channel_map: dict[str, discord.TextChannel] = {}

        # Register persistent view types (re-attached on restart)
        # The custom_id prefix 'save_' matches SaveButton's custom_id pattern.
        # We add a generic fallback view that discord.py will route by custom_id.
        self.persistent_views_added = False
        self._last_scrape_time: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def setup_hook(self):
        database.init_db()

    async def on_ready(self):
        logger.info('Logged in as %s (ID: %s)', self.user, self.user.id)

        self.guild = self.get_guild(config.GUILD_ID)
        if not self.guild:
            logger.error('Guild %s not found. Check GUILD_ID in .env', config.GUILD_ID)
            return

        logger.info('Setting up channel structure in guild: %s', self.guild.name)
        self.channel_map = await setup_guild(self.guild)
        logger.info('Channel map built: %d channels', len(self.channel_map))

        # Register the persistent view once; the fixed custom_id 'save_listing'
        # means a single registration covers all listing messages.
        if not self.persistent_views_added:
            self.add_view(ListingView())
            self.persistent_views_added = True

        if not self.scrape_loop.is_running():
            self.scrape_loop.start()
        if not self.daily_stats_loop.is_running():
            self.daily_stats_loop.start()
        if not self.watchdog_loop.is_running():
            self.watchdog_loop.start()

    async def on_disconnect(self):
        logger.warning('Bot disconnected from Discord — will auto-reconnect.')

    # ------------------------------------------------------------------
    # Scraping task
    # ------------------------------------------------------------------

    @tasks.loop(seconds=config.CHECK_INTERVAL_SECONDS)
    async def scrape_loop(self):
        if not self.guild:
            return
        for brand_key, brand_info in BRANDS.items():
            await self._process_brand_source(brand_key, brand_info, 'mercari')
            await asyncio.sleep(3)

    @scrape_loop.before_loop
    async def before_scrape(self):
        await self.wait_until_ready()

    @scrape_loop.error
    async def scrape_error(self, error: Exception):
        logger.error('scrape_loop crashed: %s', error, exc_info=error)
        await asyncio.sleep(10)
        if not self.scrape_loop.is_running():
            logger.warning('Restarting scrape_loop after crash.')
            self.scrape_loop.restart()

    async def _process_brand_source(
        self, brand_key: str, brand_info: dict, source: str
    ):
        self._last_scrape_time = time.time()
        keywords = brand_info.get('keywords', [brand_info.get('keyword', '')])
        all_listings = await fetch_brand_listings(keywords, source)
        logger.info('[%s] %d listings total (%d mots-clés)', brand_key, len(all_listings), len(keywords))

        posted = 0
        for listing in all_listings:
            # Price filter
            if config.MAX_PRICE_YEN and listing.price_jpy > config.MAX_PRICE_YEN:
                logger.info('[%s] PRIX FILTRE ¥%d > ¥%d — %s', brand_key, listing.price_jpy, config.MAX_PRICE_YEN, listing.id)
                continue
            # Dedup
            if database.is_seen(listing.id):
                logger.debug('[%s] deja vu: %s', brand_key, listing.id)
                continue
            database.mark_seen(listing.id, brand_key, source)
            logger.info('[%s] NOUVEAU: %s ¥%d — "%s"', brand_key, listing.id, listing.price_jpy, listing.title[:50])
            await self._post_listing(listing, brand_key, brand_info)
            posted += 1

        if posted == 0 and all_listings:
            logger.info('[%s] Aucune nouvelle annonce (toutes deja vues)', brand_key)

    async def _post_listing(
        self, listing: Listing, brand_key: str, brand_info: dict
    ):
        channels_names, model_label = get_channels(brand_key, listing.title)

        # Record stats
        database.record_stat(brand_key, model_label, listing.source)

        embed = build_listing_embed(
            listing,
            brand_name_fr=brand_info['name_fr'],
            brand_name_jp=brand_info['name_jp'],
        )
        view = ListingView()

        # Also send to #nouveautes-toutes-marques
        all_targets = list(channels_names)
        if 'nouveautes-toutes-marques' not in all_targets:
            all_targets.append('nouveautes-toutes-marques')

        # If price is reduced, also send to #meilleures-affaires
        if listing.original_price_jpy:
            if 'meilleures-affaires' not in all_targets:
                all_targets.append('meilleures-affaires')

        # Cross-brand category channels
        _CATEGORY_GLOBAL = {
            'sacs': 'sacs-toutes-les-marques',
            'sacs-a-main': 'sacs-à-main',
            'boston': 'boston',
            'cabas': 'cabas',
            'sacoches': 'sacoches',
            'pochettes': 'pochettes',
            'portefeuilles': 'portefeuilles',
            'etuis': 'étuis',
        }
        prefix = brand_info['prefix']
        for ch in channels_names:
            if ch.startswith(prefix + '-'):
                suffix = ch[len(prefix) + 1:]
                global_ch = _CATEGORY_GLOBAL.get(suffix)
                if global_ch and global_ch not in all_targets:
                    all_targets.append(global_ch)

        logger.info('Envoi vers salons: %s', all_targets)

        for ch_name in all_targets:
            # Price filter per channel (in EUR)
            _price_filter = CHANNEL_PRICE_FILTERS.get(ch_name)
            if _price_filter:
                _min, _max = _price_filter
                if not (_min <= listing.price_eur <= _max):
                    logger.debug('Prix filtré pour #%s: €%d hors [€%d-€%d]', ch_name, listing.price_eur, _min, _max)
                    continue

            ch = self.channel_map.get(ch_name)
            if not ch:
                logger.warning('Channel not found in map: #%s', ch_name)
                continue
            try:
                await ch.send(embed=embed, view=view)
            except discord.Forbidden:
                logger.error('No permission to post in #%s', ch_name)
            except discord.HTTPException as exc:
                logger.error('Failed to post in #%s: %s', ch_name, exc)
            # Small sleep to respect Discord rate limits
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # Watchdog — restarts loops if they die silently
    # ------------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def watchdog_loop(self):
        if not self.scrape_loop.is_running():
            logger.warning('Watchdog: scrape_loop stopped, restarting.')
            self.scrape_loop.restart()
        if not self.daily_stats_loop.is_running():
            logger.warning('Watchdog: daily_stats_loop stopped, restarting.')
            self.daily_stats_loop.restart()
        # If no scraping in 15 min despite loop running → force restart via systemd
        if self._last_scrape_time and time.time() - self._last_scrape_time > 900:
            logger.error('Watchdog: aucun scraping depuis 15 min — redemarrage force.')
            sys.exit(1)

    @watchdog_loop.before_loop
    async def before_watchdog(self):
        await self.wait_until_ready()

    # ------------------------------------------------------------------
    # Daily stats task (23:00 JST)
    # ------------------------------------------------------------------

    @tasks.loop(minutes=1)
    async def daily_stats_loop(self):
        now = datetime.now(JST)
        if now.hour == 23 and now.minute == 0:
            await self._post_daily_stats()

    @daily_stats_loop.before_loop
    async def before_stats(self):
        await self.wait_until_ready()

    @daily_stats_loop.error
    async def stats_error(self, error: Exception):
        logger.error('daily_stats_loop crashed: %s', error, exc_info=error)
        if not self.daily_stats_loop.is_running():
            logger.warning('Restarting daily_stats_loop after crash.')
            self.daily_stats_loop.restart()

    async def _post_daily_stats(self):
        ch = self.channel_map.get('top-modeles-du-jour')
        if not ch:
            logger.warning('Channel #top-modeles-du-jour not found')
            return

        today = datetime.now(JST).strftime('%Y-%m-%d')

        for brand_key, brand_info in BRANDS.items():
            rows = database.get_daily_top_models(brand_key, today, limit=7)
            if not rows:
                continue

            lines = [
                f'🏆 **TOP 7 DU JOUR — {brand_info["name_fr"].upper()}**',
                f'📅 {today}',
                '',
            ]
            for i, row in enumerate(rows, start=1):
                model, total, mercari_count, rakuma_count = (
                    row['model'], row['total'], row['mercari_count'], row['rakuma_count']
                )
                lines.append(
                    f'{i}. **{model}** → {total} annonces '
                    f'(Mercari: {mercari_count} | Rakuma: {rakuma_count})'
                )

            try:
                await ch.send('\n'.join(lines))
            except discord.HTTPException as exc:
                logger.error('Failed to post daily stats: %s', exc)

            await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    _setup_logging()

    if not config.DISCORD_TOKEN:
        logger.error('DISCORD_TOKEN is not set in .env')
        sys.exit(1)

    if not config.GUILD_ID:
        logger.error('GUILD_ID is not set in .env')
        sys.exit(1)

    bot = ZenMarketBot()
    bot.run(config.DISCORD_TOKEN, reconnect=True, log_handler=None)


if __name__ == '__main__':
    main()
