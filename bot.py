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

import config
import database
from channel_manager import setup_guild
from embeds import ListingView, build_listing_embed
from router import BRANDS, get_channels
from scraper import Listing, fetch_item_title, fetch_listings, _get_zenmarket_cookies

# Channel suffix keys that identify bag listings (used for records filtering)
_BAG_CHANNEL_KEYS = ('sacs', 'cabas', 'sacoches', 'pochettes')

# Brands tracked for daily records
_RECORD_BRANDS = ['louis_vuitton', 'gucci']
_RECORD_BRAND_NAMES = {'louis_vuitton': 'Louis Vuitton', 'gucci': 'Gucci'}

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

        # Refresh help message — delete bot's own old messages then repost
        help_ch = self.channel_map.get('comment-utiliser-le-bot')
        if help_ch:
            try:
                from channel_manager import HELP_TEXT
                async for msg in help_ch.history(limit=20):
                    if msg.author == self.user:
                        await msg.delete()
                await help_ch.send(HELP_TEXT)
            except discord.Forbidden:
                logger.warning('No permission to refresh #comment-utiliser-le-bot')
            except discord.HTTPException as exc:
                logger.error('Failed to refresh help message: %s', exc)

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
            await asyncio.sleep(2)

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
        keyword = brand_info['keyword']
        listings = await fetch_listings(keyword, source)
        zm_cookies = _get_zenmarket_cookies()

        for listing in listings:
            # Price filter
            if config.MAX_PRICE_YEN and listing.price_jpy > config.MAX_PRICE_YEN:
                continue

            if database.is_seen(listing.id):
                # Keep last_seen_at fresh so sold-fast records work correctly
                database.update_last_seen(listing.id)
                continue

            # Fetch the real product title from the ZenMarket item page.
            if zm_cookies and listing.zenmarket_url and listing.zenmarket_url.startswith('https://zenmarket'):
                real_title = await fetch_item_title(listing.zenmarket_url, zm_cookies)
                if real_title:
                    listing.title = real_title
                await asyncio.sleep(0.8)

            # Determine channels now so we can persist is_bag in the DB
            channels_names, model_label = get_channels(brand_key, listing.title)
            is_bag = any(key in ch for ch in channels_names for key in _BAG_CHANNEL_KEYS)

            database.mark_seen(listing.id, brand_key, source,
                               price_jpy=listing.price_jpy, is_bag=is_bag)
            await self._post_listing(listing, brand_key, brand_info,
                                     channels_names=channels_names, model_label=model_label)

    async def _post_listing(
        self, listing: Listing, brand_key: str, brand_info: dict,
        channels_names: list = None, model_label: str = None,
    ):
        if channels_names is None or model_label is None:
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

        for ch_name in all_targets:
            ch = self.channel_map.get(ch_name)
            if not ch:
                logger.debug('Channel not found in map: #%s', ch_name)
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
            await self._post_daily_records()

    @daily_stats_loop.before_loop
    async def before_stats(self):
        await self.wait_until_ready()

    @daily_stats_loop.error
    async def stats_error(self, error: Exception):
        logger.error('daily_stats_loop crashed: %s', error, exc_info=error)
        if not self.daily_stats_loop.is_running():
            logger.warning('Restarting daily_stats_loop after crash.')
            self.daily_stats_loop.restart()

    async def _post_daily_records(self):
        today = datetime.now(JST).strftime('%Y-%m-%d')

        # 1. Most active brand (LV vs Gucci bags)
        ch = self.channel_map.get('records-marque-active')
        if ch:
            activity = database.get_brand_activity(today, _RECORD_BRANDS)
            if activity:
                lines = [f'🏆 **MARQUE LA PLUS ACTIVE — {today}**',
                         '*(LV & Gucci — sacs uniquement)*', '']
                for i, row in enumerate(activity, 1):
                    name = _RECORD_BRAND_NAMES.get(row['brand'], row['brand'])
                    medal = ['🥇', '🥈'][i - 1] if i <= 2 else f'{i}.'
                    lines.append(f'{medal} **{name}** — {row["count"]} nouvelles annonces')
                try:
                    await ch.send('\n'.join(lines))
                except discord.HTTPException as exc:
                    logger.error('records-marque-active post failed: %s', exc)

        # 2. Best price deal (lowest vs 30-day average)
        ch = self.channel_map.get('records-prix-bas')
        if ch:
            deals = database.get_price_records(today, _RECORD_BRANDS)
            if deals:
                lines = [f'💰 **PRIX LE PLUS BAS DU JOUR — {today}**',
                         '*(LV & Gucci — sacs uniquement)*', '']
                for deal in deals:
                    name = _RECORD_BRAND_NAMES.get(deal['brand'], deal['brand'])
                    lines.append(f'**{name}**')
                    lines.append(f'  Prix le plus bas : ¥{deal["min_price"]:,}')
                    if deal.get('avg_price'):
                        diff = deal['avg_price'] - deal['min_price']
                        pct = round(diff / deal['avg_price'] * 100)
                        lines.append(
                            f'  Moyenne 30 jours : ¥{deal["avg_price"]:,} '
                            f'({pct}% en dessous de la moyenne)'
                        )
                    lines.append('')
                try:
                    await ch.send('\n'.join(lines))
                except discord.HTTPException as exc:
                    logger.error('records-prix-bas post failed: %s', exc)

        # 3. Fastest cop (all brands, all items)
        ch = self.channel_map.get('records-cop-rapide')
        if ch:
            fast = database.get_fastest_sold(today)
            if fast:
                lines = [f'⚡ **TOP 3 COPS LES PLUS RAPIDES — {today}**',
                         '*(toutes marques, tous articles)*', '']
                for i, item in enumerate(fast, 1):
                    brand_info_rec = BRANDS.get(item['brand'], {})
                    name = brand_info_rec.get('name_fr', item['brand'])
                    mins = int(item['lifetime_minutes'] or 0)
                    if mins < 60:
                        duration = f'{mins} minute{"s" if mins != 1 else ""}'
                    else:
                        h, m = divmod(mins, 60)
                        duration = f'{h}h{m:02d}'
                    medal = ['🥇', '🥈', '🥉'][i - 1]
                    lines.append(f'{medal} **{name}** — copé en **{duration}**')
                    if item.get('price_jpy'):
                        lines.append(f'  Prix : ¥{item["price_jpy"]:,}')
                    lines.append('')
                try:
                    await ch.send('\n'.join(lines))
                except discord.HTTPException as exc:
                    logger.error('records-cop-rapide post failed: %s', exc)

        await asyncio.sleep(1)

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
