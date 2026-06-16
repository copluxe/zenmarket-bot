import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
GUILD_ID = int(os.getenv('GUILD_ID', '0'))
MAX_PRICE_YEN = int(os.getenv('MAX_PRICE_YEN')) if os.getenv('MAX_PRICE_YEN') else None
CHECK_INTERVAL_SECONDS = int(os.getenv('CHECK_INTERVAL_SECONDS', '90'))
ZENMARKET_SESSION_COOKIE = os.getenv('ZENMARKET_SESSION_COOKIE', '')
