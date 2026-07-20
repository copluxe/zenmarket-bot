"""
Routing logic: given a listing title and brand key, return the list of
Discord channel names the listing should be posted to.
"""

from typing import List

# Shared category detection keywords and their channel suffix.
# Channel name = {brand_prefix}-{suffix}
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    'sacs':         ['バッグ', '鞄', 'かばん'],
    'sacs-a-main':  ['ハンドバッグ'],
    'pochettes':    ['pochette', 'ポシェット', 'ポーチ'],
    'cabas':        ['cabas', 'カバ', 'トート'],
    'sacoches':     ['sacoche', 'サコッシュ', 'ショルダー'],
    'etuis':        ['étui', 'etui', 'ケース', '小物'],
    'portefeuilles': ['portefeuille', '財布', 'ポルトフォイユ', 'ウォレット'],
    'ceintures':    ['ceinture', 'ベルト'],
}

# Per-brand model detection. Keys are brand identifiers matching BRANDS below.
MODEL_KEYWORDS: dict[str, dict[str, dict]] = {
    'louis_vuitton': {
        'neverfull':     {'channel': 'lv-neverfull',      'kw': ['neverfull', 'ネヴァーフル', 'ネバーフル', 'ネバフル']},
        'keepall':       {'channel': 'lv-keepall',        'kw': ['keepall', 'キーポル', 'キーポール']},
        'speedy':        {'channel': 'lv-speedy',         'kw': ['speedy', 'スピーディ', 'スピーディー']},
        'alma':          {'channel': 'lv-alma',           'kw': ['alma', 'アルマ']},
        'pochette_metis':{'channel': 'lv-pochette-metis', 'kw': ['pochette métis', 'pochette metis', 'ポシェットメティス', 'ポシェット・メティス', 'ポシェットメチス']},
        'ellipse':       {'channel': 'lv-ellipse',        'kw': ['ellipse', 'エリプス', 'エリプサ']},
        'saint_cloud':   {'channel': 'lv-saint-cloud',    'kw': ['saint-cloud', 'saint cloud', 'サンクルー', 'サン・クルー']},
        'noe':           {'channel': 'lv-noe',            'kw': ['noé', 'noe', 'ノエ', 'ノエ・バケット', 'ノエバケット']},
        'papillon':      {'channel': 'lv-papillon',       'kw': ['papillon', 'パピヨン']},
        'boston':        {'channel': 'lv-boston',         'kw': ['boston', 'ボストン', 'ボストンバッグ']},
        'saumur':        {'channel': 'lv-saumur',         'kw': ['saumur', 'ソミュール', 'ソーミュール']},
        'trouville':     {'channel': 'lv-trouville',      'kw': ['trouville', 'トルーヴィル', 'トルービル', 'トゥルーヴィル']},
    },
    'gucci': {
        'soho':       {'channel': 'gucci-soho',       'kw': ['soho', 'ソーホー', 'ソホ']},
        'jackie':     {'channel': 'gucci-jackie',     'kw': ['jackie', 'ジャッキー', 'ジャッキ']},
        'gg_supreme': {'channel': 'gucci-gg-supreme', 'kw': ['gg supreme', 'GGスプリーム', 'gg スプリーム', 'ggスプリーム', 'ジージースプリーム']},
        'marmont':    {'channel': 'gucci-marmont',    'kw': ['marmont', 'マーモント', 'マルモント']},
    },
    'dior': {
        'trotter':    {'channel': 'dior-trotter',    'kw': ['trotter', 'トロッター', 'トロッタ']},
        'bowling':    {'channel': 'dior-bowling',    'kw': ['bowling', 'ボーリング', 'ボウリング']},
        'boston':     {'channel': 'dior-boston',     'kw': ['boston', 'ボストン']},
        'lady_dior':  {'channel': 'dior-lady-dior',  'kw': ['lady dior', 'レディディオール', 'レディ ディオール', 'レディ・ディオール', 'lady christian dior']},
    },
    'coach': {
        'tabby': {'channel': 'coach-tabby', 'kw': ['tabby', 'タビー']},
        'lana':  {'channel': 'coach-lana',  'kw': ['lana', 'ラナ']},
        'rowan': {'channel': 'coach-rowan', 'kw': ['rowan', 'ローワン']},
    },
    'celine': {
        'boston': {'channel': 'celine-boston', 'kw': ['boston', 'ボストン']},
    },
    'hermes': {
        'foulards': {'channel': 'hermes-foulards', 'kw': ['カレ', 'カレ90', 'スカーフ', 'バンダナ', 'foulard', 'carré', 'carre', 'bandana', 'ツイリー', 'twilly']},
    },
}

# Brand metadata used by the scraper and router.
BRANDS: dict[str, dict] = {
    'louis_vuitton': {
        'name_fr': 'Louis Vuitton',
        'name_jp': 'ルイヴィトン',
        'keywords': ['ルイヴィトン', 'ルイ・ヴィトン', 'ヴィトン', 'LOUIS VUITTON'],
        'prefix':  'lv',
        'all_channel': 'lv-toutes-annonces',
    },
    'gucci': {
        'name_fr': 'Gucci',
        'name_jp': 'グッチ',
        'keywords': ['グッチ', 'GUCCI', 'グッチー'],
        'prefix':  'gucci',
        'all_channel': 'gucci-toutes-annonces',
    },
    'dior': {
        'name_fr': 'Dior',
        'name_jp': 'ディオール',
        'keywords': ['ディオール', 'DIOR'],
        'prefix':  'dior',
        'all_channel': 'dior-toutes-annonces',
    },
    'chanel': {
        'name_fr': 'Chanel',
        'name_jp': 'シャネル',
        'keywords': ['シャネル', 'CHANEL'],
        'prefix':  'chanel',
        'all_channel': 'chanel-toutes-annonces',
    },
    'hermes': {
        'name_fr': 'Hermès',
        'name_jp': 'エルメス',
        'keywords': ['エルメス', 'HERMES'],
        'prefix':  'hermes',
        'all_channel': 'hermes-toutes-annonces',
    },
    'celine': {
        'name_fr': 'Céline',
        'name_jp': 'セリーヌ',
        'keywords': ['セリーヌ', 'CELINE', 'セリーン'],
        'prefix':  'celine',
        'all_channel': 'celine-toutes-annonces',
    },
    'coach': {
        'name_fr': 'Coach',
        'name_jp': 'コーチ',
        'keywords': ['コーチ', 'COACH'],
        'prefix':  'coach',
        'all_channel': 'coach-toutes-annonces',
    },
    'miumiu': {
        'name_fr': 'Miu Miu',
        'name_jp': 'ミュウミュウ',
        'keywords': ['ミュウミュウ', 'MIU MIU'],
        'prefix':  'miumiu',
        'all_channel': 'miumiu-toutes-annonces',
    },
    'burberry': {
        'name_fr': 'Burberry',
        'name_jp': 'バーバリー',
        'keywords': ['バーバリー', 'BURBERRY', 'バーバリ'],
        'prefix':  'burberry',
        'all_channel': 'burberry-toutes-annonces',
    },
    'loewe': {
        'name_fr': 'Loewe',
        'name_jp': 'ロエベ',
        'keywords': ['ロエベ', 'LOEWE'],
        'prefix':  'loewe',
        'all_channel': 'loewe-toutes-annonces',
    },
    'vivienne_westwood': {
        'name_fr': 'Vivienne Westwood',
        'name_jp': 'ヴィヴィアンウエストウッド',
        'keywords': ['ヴィヴィアンウエストウッド', 'ヴィヴィアン・ウエストウッド'],
        'prefix':  'vw',
        'all_channel': 'vw-toutes-annonces',
    },
    'cartier': {
        'name_fr': 'Cartier',
        'name_jp': 'カルティエ',
        'keywords': ['カルティエ', 'CARTIER'],
        'prefix':  'cartier',
        'all_channel': 'cartier-toutes-annonces',
    },
    'fendi': {
        'name_fr': 'Fendi',
        'name_jp': 'フェンディ',
        'keywords': ['フェンディ', 'FENDI'],
        'prefix':  'fendi',
        'all_channel': 'fendi-toutes-annonces',
    },
    'prada': {
        'name_fr': 'Prada',
        'name_jp': 'プラダ',
        'keywords': ['プラダ', 'PRADA'],
        'prefix':  'prada',
        'all_channel': 'prada-toutes-annonces',
    },
}


def _lower(s: str) -> str:
    return s.lower()


def get_channels(brand_key: str, title: str) -> tuple[List[str], str]:
    """Return (list_of_channel_names, detected_model_label).

    The first element always contains the brand's catch-all channel.
    Additional specific channels are appended when keywords are matched.
    detected_model_label is the model name used for daily stats (or 'Autres').
    """
    brand = BRANDS[brand_key]
    channels: List[str] = [brand['all_channel']]
    model_label = 'Autres'
    title_lower = _lower(title)

    # Model detection
    for model_name, info in MODEL_KEYWORDS.get(brand_key, {}).items():
        for kw in info['kw']:
            if _lower(kw) in title_lower:
                ch = info['channel']
                if ch not in channels:
                    channels.append(ch)
                model_label = model_name.replace('_', ' ').title()
                break

    # Category detection (uses brand prefix)
    prefix = brand['prefix']
    for cat_suffix, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if _lower(kw) in title_lower:
                ch = f'{prefix}-{cat_suffix}'
                if ch not in channels:
                    channels.append(ch)
                break

    return channels, model_label
