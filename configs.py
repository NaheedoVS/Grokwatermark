import os

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", 0))
UPDATES_CHANNEL = int(os.environ.get("UPDATES_CHANNEL", 0))
PRESET = os.environ.get("PRESET", "ultrafast")
STREAMTAPE_API_USERNAME = os.environ.get("STREAMTAPE_API_USERNAME", "")
STREAMTAPE_API_PASS = os.environ.get("STREAMTAPE_API_PASS", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
