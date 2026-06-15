```
Create a complete Discord bot in Python that monitors ZenMarket (Mercari Japan + Rakuma) for luxury goods and posts listings automatically into organized Discord channels.

=== DISCORD SERVER STRUCTURE ===

Create these categories and channels automatically on first run:

📁 ✨ POUR COMMENCER
- #comment-utiliser-le-bot
- #meilleures-affaires
- #nouveautes-toutes-marques

📁 📊 STATISTIQUES
- #top-modeles-du-jour
- #records

📁 ❤️ FAVORIS
- #favoris

📁 LOUIS VUITTON
- #lv-toutes-annonces
- #lv-neverfull, #lv-keepall, #lv-speedy, #lv-alma, #lv-pochette-metis, #lv-ellipse, #lv-saint-cloud, #lv-noe
- #lv-pochettes, #lv-cabas, #lv-sacoches, #lv-etuis, #lv-portefeuilles, #lv-ceintures

📁 GUCCI
- #gucci-toutes-annonces
- #gucci-soho, #gucci-jackie, #gucci-gg-supreme, #gucci-marmont
- #gucci-pochettes, #gucci-cabas, #gucci-sacoches, #gucci-etuis, #gucci-portefeuilles, #gucci-ceintures

📁 DIOR
- #dior-toutes-annonces
- #dior-trotter, #dior-bowling, #dior-boston, #dior-lady-dior
- #dior-pochettes, #dior-cabas, #dior-sacoches, #dior-etuis, #dior-portefeuilles, #dior-ceintures

📁 CHANEL
- #chanel-toutes-annonces
- #chanel-sacs, #chanel-sacs-a-main
- #chanel-pochettes, #chanel-cabas, #chanel-sacoches, #chanel-etuis, #chanel-portefeuilles, #chanel-ceintures

📁 HERMÈS
- #hermes-toutes-annonces
- #hermes-sacs, #hermes-sacs-a-main, #hermes-foulards
- #hermes-pochettes, #hermes-cabas, #hermes-sacoches, #hermes-etuis, #hermes-portefeuilles, #hermes-ceintures

📁 CÉLINE
- #celine-toutes-annonces
- #celine-boston, #celine-sacs, #celine-sacs-a-main
- #celine-pochettes, #celine-cabas, #celine-sacoches, #celine-etuis, #celine-portefeuilles, #celine-ceintures

📁 COACH
- #coach-toutes-annonces
- #coach-tabby, #coach-lana, #coach-rowan, #coach-sacs, #coach-sacs-a-main
- #coach-pochettes, #coach-cabas, #coach-sacoches, #coach-etuis, #coach-portefeuilles, #coach-ceintures

📁 MIU MIU
- #miumiu-toutes-annonces
- #miumiu-sacs, #miumiu-sacs-a-main
- #miumiu-pochettes, #miumiu-cabas, #miumiu-sacoches, #miumiu-etuis, #miumiu-portefeuilles, #miumiu-ceintures

📁 BALENCIAGA
- #balenciaga-toutes-annonces
- #balenciaga-sacs, #balenciaga-sacs-a-main
- #balenciaga-pochettes, #balenciaga-cabas, #balenciaga-sacoches, #balenciaga-etuis, #balenciaga-portefeuilles, #balenciaga-ceintures

📁 LOEWE
- #loewe-toutes-annonces
- #loewe-sacs, #loewe-sacs-a-main
- #loewe-pochettes, #loewe-cabas, #loewe-sacoches, #loewe-etuis, #loewe-portefeuilles, #loewe-ceintures

📁 VIVIENNE WESTWOOD
- #vw-toutes-annonces
- #vw-sacs, #vw-sacs-a-main
- #vw-pochettes, #vw-cabas, #vw-sacoches, #vw-etuis, #vw-portefeuilles, #vw-ceintures

📁 CARTIER
- #cartier-toutes-annonces
- #cartier-sacs, #cartier-sacs-a-main
- #cartier-pochettes, #cartier-cabas, #cartier-sacoches, #cartier-etuis, #cartier-portefeuilles, #cartier-ceintures

📁 FENDI
- #fendi-toutes-annonces
- #fendi-sacs, #fendi-sacs-a-main
- #fendi-pochettes, #fendi-cabas, #fendi-sacoches, #fendi-etuis, #fendi-portefeuilles, #fendi-ceintures

📁 PRADA
- #prada-toutes-annonces
- #prada-sacs, #prada-sacs-a-main
- #prada-pochettes, #prada-cabas, #prada-sacoches, #prada-etuis, #prada-portefeuilles, #prada-ceintures

=== SCRAPING ===

Scrape ZenMarket search results (Mercari Japan + Rakuma) every 90 seconds per brand keyword.
Search keywords in Japanese:
- Louis Vuitton: ルイヴィトン
- Gucci: グッチ
- Dior: ディオール
- Chanel: シャネル
- Hermès: エルメス
- Céline: セリーヌ
- Coach: コーチ
- Miu Miu: ミュウミュウ
- Balenciaga: バレンシアガ
- Loewe: ロエベ
- Vivienne Westwood: ヴィヴィアンウエストウッド
- Cartier: カルティエ
- Fendi: フェンディ
- Prada: プラダ

Store all seen listing IDs in SQLite to never post duplicates.

=== ROUTING LOGIC ===

Each listing is posted in TWO channels:
1. Always in #[brand]-toutes-annonces
2. In the specific model/category channel if detected in title

Detection keywords (FR + JP):
- LV: Neverfull/ネヴァーフル, Keepall/キーポル, Speedy/スピーディ, Alma/アルマ, Pochette Métis/ポシェットメティス, Ellipse/エリプス, Saint-Cloud/サンクルー, Noé/ノエ
- Gucci: Soho/ソーホー, Jackie/ジャッキー, GG Supreme/GGスプリーム, Marmont/マーモント
- Dior: Trotter/トロッター, Bowling/ボーリング, Boston/ボストン, Lady Dior/レディディオール
- Coach: Tabby/タビー, Lana/ラナ, Rowan/ローワン
- Céline: Boston/ボストン
- Hermès foulards: カレ, スカーフ, バンダナ, foulard, carré, bandana
- Categories (all brands): pochette/ポシェット, cabas/カバ, sacoche/サコッシュ, étui/ケース, portefeuille/財布, ceinture/ベルト

=== DISCORD EMBED FORMAT ===

🆕 Nouvelle annonce • Mercari Japan OR Rakuma

🏷️ [French name] | [Japanese name]
💴 ~~¥XX,XXX~~ ¥XX,XXX (€XXX) → show crossed out original price ONLY if reduced
📦 Condition: Neuf / Très bon état / Bon état / État correct
⏱️ Mis en ligne il y a X minutes/heures
✅ Disponible OR ❌ Vendu
🔗 Voir sur ZenMarket (Mercari) OR ZenMarket (Rakuma)
[listing thumbnail photo]
[ 🤍 Sauvegarder ]

When 🤍 is clicked → instantly reposts the embed in #favoris with "❤️ Sauvegardé par @[username]"

=== DAILY STATISTICS ===

Every day at 23:00 JST post in #top-modeles-du-jour:

🏆 TOP 7 DU JOUR - [BRAND]
1. Neverfull → 47 annonces (Mercari: 38 | Rakuma: 9)
2. Speedy → 31 annonces
...

=== TECH STACK ===
- Python 3.11
- discord.py with buttons (interactions)
- BeautifulSoup4 + requests for scraping
- sqlite3 for seen listings + daily stats
- python-dotenv for config
- exchangerate-api for live EUR/JPY conversion

=== CONFIG (.env) ===
DISCORD_TOKEN=
GUILD_ID=
MAX_PRICE_YEN=
CHECK_INTERVAL_SECONDS=90

=== IMPORTANT ===
- Never post the same listing twice (SQLite check by listing ID)
- Handle rate limiting with exponential backoff
- Log all errors to errors.log
- Auto-reconnect if Discord connection drops
- Source always shown as ZenMarket (Mercari) or ZenMarket (Rakuma)
```

---

Colle ça dans Claude Code et envoie ! Tiens-moi au courant de ce qu'il génère 🚀
