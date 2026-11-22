import os
from pathlib import Path

BASE = Path(os.environ.get("FABRIKA_HOME") or Path(__file__).resolve().parent.parent)
INBOX = BASE / "inbox"
LIBRARY = BASE / "kutuphane"
ARCHIVE = BASE / "arsiv"
SECRETS = BASE / "secrets"
CLIPS_DIR = BASE / "klipler"
DB_PATH = BASE / "fabrika.db"

# FABRIKA_DENEME=1: gerçek yükleme yok, kuyruk ve zamanlama sahte yükleyiciyle çalışır.
DRY_RUN = os.environ.get("FABRIKA_DENEME") == "1"

HOST = "127.0.0.1"
PORT = 8765

TARGETS = {
    "youtube": "YouTube",
    "youtube_shorts": "YouTube Shorts",
    "tiktok": "TikTok",
    "instagram_reels": "Instagram Reels",
}

# inbox altındaki klasör -> o klasöre atılan dosyanın gideceği hedefler
FOLDER_TARGETS = {
    "youtube": ["youtube"],
    "shorts": ["youtube_shorts"],
    "tiktok": ["tiktok"],
    "reels": ["instagram_reels"],
    "dikey": ["youtube_shorts", "tiktok", "instagram_reels"],
}

# Aynı hesabı ya da kotayı paylaşan hedefler birlikte duraklatılır.
SHARED_LIMITS = {
    "youtube": ("youtube", "youtube_shorts"),
    "youtube_shorts": ("youtube", "youtube_shorts"),
    "tiktok": ("tiktok",),
    "instagram_reels": ("instagram_reels",),
}

DEFAULT_DAILY_LIMITS = {
    "youtube": 10,
    "youtube_shorts": 10,
    "tiktok": 5,
    "instagram_reels": 25,
}

UNITS = {"sn": 1, "dk": 60, "saat": 3600, "gun": 86400}

VIDEO_EXTS = {".mp4", ".mov", ".webm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

STABLE_SECONDS = 5  # kopyalanması bitmemiş dosyaya dokunmamak için bekleme
IMAGE_SECONDS = 8  # görselden üretilen videonun süresi
MAX_ATTEMPTS = 5
TICK_SECONDS = 1


def ensure_dirs():
    for directory in (LIBRARY, ARCHIVE, SECRETS, CLIPS_DIR / "gelen", BASE / "muzik", *(INBOX / name for name in FOLDER_TARGETS)):
        directory.mkdir(parents=True, exist_ok=True)
