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
from scraper import Listing, fetch_listings, jpy_to_eur

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

# Channels that indicate a bag listing (sac / sac-à-main)
_BAG_CHANNEL_SUFFIXES = ('-sacs', '-sacs-a-main')
_BAG_MODEL_CHANNELS = {
    'lv-neverfull', 'lv-keepall', 'lv-speedy', 'lv-alma',
    'lv-ellipse', 'lv-saint-cloud', 'lv-noe',
    'gucci-soho', 'gucci-jackie', 'gucci-gg-supreme', 'gucci-marmont',
    'dior-trotter', 'dior-bowling', 'dior-boston', 'dior-lady-dior',
    'coach-tabby', 'coach-lana', 'coach-rowan',
    'celine-boston',
}


def _is_bag(channels: list[str]) -> bool:
    for ch in channels:
        if ch.endswith(_BAG_CHANNEL_SUFFIXES) or ch in _BAG_MODEL_CHANNELS:
            return True
    return False


def _elapsed_str(posted_at_str: str) -> str:
    try:
        posted = datetime.strptime(posted_at_str, '%Y-%m-%d %H:%M:%S')
        elapsed = int((datetime.utcnow() - posted).total_seconds())
        if elapsed < 60:
            return f'{elapsed}s'
        if elapsed < 3600:
            return f'{elapsed // 60}min'
        return f'{elapsed // 3600}h'
    except Exception:
        return '?'


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
        self._records_msg_ids: dict[str, int] = {}

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
            await asyncio.sleep(2)
        await self._update_records_channels()

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

        for listing in listings:
            # Price filter
            if config.MAX_PRICE_YEN and listing.price_jpy > config.MAX_PRICE_YEN:
                continue
            # Dedup
            if database.is_seen(listing.id):
                continue
            channels_names, _ = get_channels(brand_key, listing.title)
            database.mark_seen(
                listing.id, brand_key, source,
                title=listing.title,
                price_jpy=listing.price_jpy,
                zenmarket_url=listing.zenmarket_url,
                is_bag=_is_bag(channels_names),
            )
            await self._post_listing(listing, brand_key, brand_info)

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
    # Records channels
    # ------------------------------------------------------------------

    async def _send_or_edit_record(self, channel_name: str, content: str):
        ch = self.channel_map.get(channel_name)
        if not ch:
            return
        msg_id = self._records_msg_ids.get(channel_name)
        if msg_id:
            try:
                msg = await ch.fetch_message(msg_id)
                await msg.edit(content=content)
                return
            except (discord.NotFound, discord.HTTPException):
                pass
        try:
            msg = await ch.send(content)
            self._records_msg_ids[channel_name] = msg.id
        except discord.HTTPException as exc:
            logger.error('Failed to update #%s: %s', channel_name, exc)

    async def _update_records_channels(self):
        today = datetime.now(JST).strftime('%Y-%m-%d')

        # --- records-marques-actives: all 14 brands, sorted by activity, no links ---
        counts_today = {row['brand']: row['total'] for row in database.get_brand_counts_today()}
        ranked = sorted(BRANDS.items(), key=lambda x: counts_today.get(x[0], 0), reverse=True)
        lines = [f'🏆 **MARQUES LES PLUS ACTIVES — {today}**', '']
        medals = ['🥇', '🥈', '🥉']
        for i, (key, info) in enumerate(ranked, 1):
            count = counts_today.get(key, 0)
            prefix = medals[i - 1] if i <= 3 else f'{i}.'
            s = 's' if count > 1 else ''
            lines.append(f'{prefix} **{info["name_fr"]}** — {count} annonce{s}')
        await self._send_or_edit_record('records-marques-actives', '\n'.join(lines))

        # --- records-prix-bas: sacs & sacs-à-main uniquement ---
        cheapest = database.get_cheapest_per_brand()
        if cheapest:
            lines = ['💰 **RECORDS PRIX BAS — sacs & sacs-à-main, tous temps**', '']
            for row in cheapest:
                info = BRANDS.get(row['brand'], {})
                name = info.get('name_fr', row['brand'])
                eur = jpy_to_eur(row['price_jpy'])
                title = (row['title'] or 'Article')[:60]
                lines.append(f'🔻 **{name}** — [{title}]({row["zenmarket_url"]}) — ¥{row["price_jpy"]:,} (€{eur})')
            await self._send_or_edit_record('records-prix-bas', '\n'.join(lines))

        # --- records-cop-rapide: 15 dernières annonces avec temps écoulé ---
        recent = database.get_recent_listings(limit=15)
        if recent:
            lines = ['⚡ **DERNIÈRES ANNONCES DÉTECTÉES**', '']
            for row in recent:
                info = BRANDS.get(row['brand'], {})
                name = info.get('name_fr', row['brand'])
                eur = jpy_to_eur(row['price_jpy'])
                title = (row['title'] or 'Article')[:55]
                ago = _elapsed_str(row['posted_at'])
                lines.append(f'• **{name}** — [{title}]({row["zenmarket_url"]}) — ¥{row["price_jpy"]:,} (€{eur}) • il y a {ago}')
            await self._send_or_edit_record('records-cop-rapide', '\n'.join(lines))

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
