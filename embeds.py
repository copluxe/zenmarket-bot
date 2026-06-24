"""
Discord embed builder and button interaction handler for Mercari JP listings.
"""

import logging
from typing import Optional

import discord
from discord import ui

from scraper import Listing

logger = logging.getLogger(__name__)

# Embed color per source
SOURCE_COLORS = {
    'mercari': 0xFF4F00,  # Mercari orange
}

SOURCE_FULL = {
    'mercari': 'Mercari Japan',
}


def _format_price_line(listing: Listing) -> str:
    """Format the price line, with strikethrough if the price was reduced."""
    current = f'¥{listing.price_jpy:,} (€{listing.price_eur})'
    if listing.original_price_jpy:
        orig = f'~~¥{listing.original_price_jpy:,}~~'
        return f'{orig} {current}'
    return current


def build_listing_embed(
    listing: Listing,
    brand_name_fr: str,
    brand_name_jp: str,
) -> discord.Embed:
    source_label = SOURCE_FULL[listing.source]
    color = SOURCE_COLORS.get(listing.source, 0x7289DA)

    embed = discord.Embed(
        title=listing.title,
        url=listing.url,
        color=color,
        description=f'🆕 **Nouvelle annonce** • {source_label}',
    )

    embed.add_field(
        name='🏷️ Marque',
        value=f'{brand_name_fr} | {brand_name_jp}',
        inline=True,
    )
    embed.add_field(
        name='💴 Prix',
        value=_format_price_line(listing),
        inline=True,
    )
    embed.add_field(
        name='📦 Condition',
        value=listing.condition_fr or 'Non spécifié',
        inline=True,
    )
    embed.add_field(
        name='⏱️ Mise en ligne',
        value=f'Il y a {listing.posted_ago}' if listing.posted_ago != 'Inconnu' else 'Inconnu',
        inline=True,
    )
    embed.add_field(
        name='État',
        value='✅ Disponible' if listing.status == 'available' else '❌ Vendu',
        inline=True,
    )
    link_value = f'[🇯🇵 Mercari Japan]({listing.url})'
    if listing.zenmarket_url:
        link_value += f'\n[🛒 Chercher sur ZenMarket]({listing.zenmarket_url})'
    embed.add_field(
        name='🔗 Liens',
        value=link_value,
        inline=True,
    )

    if listing.image_url:
        embed.set_image(url=listing.image_url)

    embed.set_footer(text=f'ID: {listing.id}')
    return embed


def build_saved_embed(
    original_embed: discord.Embed,
    saved_by: discord.Member,
) -> discord.Embed:
    """Clone the listing embed with a ❤️ saved-by header."""
    saved = original_embed.copy()
    saved.description = (
        f'❤️ **Sauvegardé par {saved_by.mention}**\n\n'
        + (original_embed.description or '')
    )
    saved.color = 0xFF0000
    return saved


class SaveButton(ui.Button):
    def __init__(self):
        # Constant custom_id so the persistent view survives bot restarts.
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label='🤍 Sauvegarder',
            custom_id='save_listing',
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            await interaction.followup.send('Erreur: guild introuvable.', ephemeral=True)
            return

        favoris_ch = discord.utils.get(guild.text_channels, name='favoris')
        if not favoris_ch:
            await interaction.followup.send('Le canal #favoris est introuvable.', ephemeral=True)
            return

        # Retrieve the original embed from the message
        message = interaction.message
        if not message or not message.embeds:
            await interaction.followup.send('Embed introuvable.', ephemeral=True)
            return

        original_embed = message.embeds[0]
        saved_embed = build_saved_embed(original_embed, interaction.user)

        try:
            await favoris_ch.send(embed=saved_embed)
            await interaction.followup.send(
                f'✅ Sauvegardé dans {favoris_ch.mention} !', ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Je n'ai pas la permission d'écrire dans #favoris.", ephemeral=True
            )


class ListingView(ui.View):
    """Persistent view with the save button. timeout=None + fixed custom_id = survives restarts."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(SaveButton())
