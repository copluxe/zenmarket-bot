"""
Creates and maintains the Discord server channel structure defined in the README.
Idempotent: safe to call on every bot startup.
"""

import logging
from typing import Optional

import discord

logger = logging.getLogger(__name__)

# Each entry: (category_name, [channel_names])
SERVER_STRUCTURE: list[tuple[str, list[str]]] = [
    ('✨ POUR COMMENCER', [
        'comment-utiliser-le-bot',
        'meilleures-affaires',
        'nouveautes-toutes-marques',
    ]),
    ('📊 STATISTIQUES', [
        'top-modeles-du-jour',
        'records-marques-actives',
        'records-prix-bas',
        'records-cop-rapide',
    ]),
    ('❤️ FAVORIS', [
        'favoris',
    ]),
    ('LOUIS VUITTON', [
        'lv-toutes-annonces',
        'lv-neverfull', 'lv-keepall', 'lv-speedy', 'lv-alma',
        'lv-pochette-metis', 'lv-ellipse', 'lv-saint-cloud', 'lv-noe',
        'lv-pochettes', 'lv-cabas', 'lv-sacoches', 'lv-etuis',
        'lv-portefeuilles', 'lv-ceintures',
    ]),
    ('GUCCI', [
        'gucci-toutes-annonces',
        'gucci-soho', 'gucci-jackie', 'gucci-gg-supreme', 'gucci-marmont',
        'gucci-pochettes', 'gucci-cabas', 'gucci-sacoches', 'gucci-etuis',
        'gucci-portefeuilles', 'gucci-ceintures',
    ]),
    ('DIOR', [
        'dior-toutes-annonces',
        'dior-trotter', 'dior-bowling', 'dior-boston', 'dior-lady-dior',
        'dior-pochettes', 'dior-cabas', 'dior-sacoches', 'dior-etuis',
        'dior-portefeuilles', 'dior-ceintures',
    ]),
    ('CHANEL', [
        'chanel-toutes-annonces',
        'chanel-sacs', 'chanel-sacs-a-main',
        'chanel-pochettes', 'chanel-cabas', 'chanel-sacoches', 'chanel-etuis',
        'chanel-portefeuilles', 'chanel-ceintures',
    ]),
    ('HERMÈS', [
        'hermes-toutes-annonces',
        'hermes-sacs', 'hermes-sacs-a-main', 'hermes-foulards',
        'hermes-pochettes', 'hermes-cabas', 'hermes-sacoches', 'hermes-etuis',
        'hermes-portefeuilles', 'hermes-ceintures',
    ]),
    ('CÉLINE', [
        'celine-toutes-annonces',
        'celine-boston', 'celine-sacs', 'celine-sacs-a-main',
        'celine-pochettes', 'celine-cabas', 'celine-sacoches', 'celine-etuis',
        'celine-portefeuilles', 'celine-ceintures',
    ]),
    ('COACH', [
        'coach-toutes-annonces',
        'coach-tabby', 'coach-lana', 'coach-rowan',
        'coach-sacs', 'coach-sacs-a-main',
        'coach-pochettes', 'coach-cabas', 'coach-sacoches', 'coach-etuis',
        'coach-portefeuilles', 'coach-ceintures',
    ]),
    ('MIU MIU', [
        'miumiu-toutes-annonces',
        'miumiu-sacs', 'miumiu-sacs-a-main',
        'miumiu-pochettes', 'miumiu-cabas', 'miumiu-sacoches', 'miumiu-etuis',
        'miumiu-portefeuilles', 'miumiu-ceintures',
    ]),
    ('BALENCIAGA', [
        'balenciaga-toutes-annonces',
        'balenciaga-sacs', 'balenciaga-sacs-a-main',
        'balenciaga-pochettes', 'balenciaga-cabas', 'balenciaga-sacoches', 'balenciaga-etuis',
        'balenciaga-portefeuilles', 'balenciaga-ceintures',
    ]),
    ('LOEWE', [
        'loewe-toutes-annonces',
        'loewe-sacs', 'loewe-sacs-a-main',
        'loewe-pochettes', 'loewe-cabas', 'loewe-sacoches', 'loewe-etuis',
        'loewe-portefeuilles', 'loewe-ceintures',
    ]),
    ('VIVIENNE WESTWOOD', [
        'vw-toutes-annonces',
        'vw-sacs', 'vw-sacs-a-main',
        'vw-pochettes', 'vw-cabas', 'vw-sacoches', 'vw-etuis',
        'vw-portefeuilles', 'vw-ceintures',
    ]),
    ('CARTIER', [
        'cartier-toutes-annonces',
        'cartier-sacs', 'cartier-sacs-a-main',
        'cartier-pochettes', 'cartier-cabas', 'cartier-sacoches', 'cartier-etuis',
        'cartier-portefeuilles', 'cartier-ceintures',
    ]),
    ('FENDI', [
        'fendi-toutes-annonces',
        'fendi-sacs', 'fendi-sacs-a-main',
        'fendi-pochettes', 'fendi-cabas', 'fendi-sacoches', 'fendi-etuis',
        'fendi-portefeuilles', 'fendi-ceintures',
    ]),
    ('PRADA', [
        'prada-toutes-annonces',
        'prada-sacs', 'prada-sacs-a-main',
        'prada-pochettes', 'prada-cabas', 'prada-sacoches', 'prada-etuis',
        'prada-portefeuilles', 'prada-ceintures',
    ]),
]

HELP_TEXT = """
# 🤖 Mercari Japan Bot — Mode d'emploi

Ce bot surveille **Mercari Japan** directement et publie les nouvelles annonces de maroquinerie de luxe en temps réel.

## 📋 Channels disponibles
- **#nouveautes-toutes-marques** — Toutes les nouvelles annonces, toutes marques confondues
- **#meilleures-affaires** — Articles à prix réduit uniquement
- **#[marque]-toutes-annonces** — Toutes les annonces d'une marque
- **#[marque]-[modele]** — Annonces filtrées par modèle (ex: #lv-neverfull)
- **#[marque]-[categorie]** — Annonces filtrées par catégorie (ex: #lv-pochettes)
- **#top-modeles-du-jour** — Classement des modèles les plus listés chaque soir à 23h JST
- **#favoris** — Vos articles sauvegardés

## 💡 Utilisation
Chaque annonce affiche un bouton **🤍 Sauvegarder**. Cliquez dessus pour épingler l'annonce dans #favoris avec votre tag.

## ⚙️ Configuration
- Intervalle de vérification : toutes les 90 secondes par marque
- Filtre prix max : configurable via `MAX_PRICE_YEN` dans .env
- Source : Mercari Japan API directe
"""


async def setup_guild(guild: discord.Guild) -> dict[str, discord.TextChannel]:
    """Create all categories and channels if they don't exist. Returns channel map."""
    existing_categories = {cat.name: cat for cat in guild.categories}
    existing_channels = {ch.name: ch for ch in guild.text_channels}
    channel_map: dict[str, discord.TextChannel] = dict(existing_channels)

    for category_name, channel_names in SERVER_STRUCTURE:
        # Get or create category
        if category_name not in existing_categories:
            logger.info('Creating category: %s', category_name)
            try:
                category = await guild.create_category(category_name)
                existing_categories[category_name] = category
            except discord.Forbidden:
                logger.error('No permission to create category: %s', category_name)
                continue
        else:
            category = existing_categories[category_name]

        # Get or create channels within the category
        for ch_name in channel_names:
            if ch_name not in existing_channels:
                logger.info('Creating channel: #%s', ch_name)
                try:
                    ch = await guild.create_text_channel(ch_name, category=category)
                    channel_map[ch_name] = ch
                    existing_channels[ch_name] = ch
                except discord.Forbidden:
                    logger.error('No permission to create channel: #%s', ch_name)
                except discord.HTTPException as exc:
                    logger.error('Failed to create channel #%s: %s', ch_name, exc)
            else:
                channel_map[ch_name] = existing_channels[ch_name]

    # Post help message in #comment-utiliser-le-bot if it's empty
    help_ch: Optional[discord.TextChannel] = channel_map.get('comment-utiliser-le-bot')
    if help_ch:
        try:
            history = [m async for m in help_ch.history(limit=1)]
            if not history:
                await help_ch.send(HELP_TEXT)
        except discord.Forbidden:
            pass

    return channel_map
