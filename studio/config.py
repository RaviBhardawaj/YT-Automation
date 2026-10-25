"""Static paths and constants. Everything user-tunable lives in the settings table instead."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "YT Automation Studio"
APP_VERSION = "1.0.0"

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("STUDIO_DATA_DIR", ROOT / "data")).resolve()
DB_PATH = DATA_DIR / "studio.db"
LOG_DIR = DATA_DIR / "logs"
VOICES_DIR = DATA_DIR / "voices"
PRODUCTIONS_DIR = DATA_DIR / "productions"
CACHE_DIR = DATA_DIR / "cache"
ASSET_CACHE_DIR = CACHE_DIR / "assets"
MUSIC_DIR = DATA_DIR / "music"
BRAND_DIR = DATA_DIR / "brand"
SAMPLES_DIR = DATA_DIR / "samples"
FONTS_DIR = BRAND_DIR / "fonts"
MARKET_DIR = DATA_DIR / "market"                # imported datasets (immutable copies)
AUDIT_DIR = DATA_DIR / "audit"                  # per-episode audit artifacts

# espeak-ng (used by Piper and Kokoro) keeps its data path in a fixed-size buffer and
# silently falls back to a compiled-in path when the real one is too long. We keep a
# copy of each engine's data under this short path.
if sys.platform == "darwin":
    SHORT_CACHE_DIR = Path.home() / "Library" / "Caches" / "YTAutomationStudio"
elif sys.platform == "win32":
    SHORT_CACHE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "YTStudio"
else:
    SHORT_CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ytstudio"

HOST = os.environ.get("STUDIO_HOST", "127.0.0.1")       # 0.0.0.0 only behind a reverse proxy / tunnel
PORT = int(os.environ.get("STUDIO_PORT", "8765"))
# The address people use in their browser when the studio is online (e.g. https://studio.example.com).
# Empty = this computer only. YouTube sign-in redirects there, and cookies become HTTPS-only.
PUBLIC_URL = os.environ.get("STUDIO_PUBLIC_URL", "").rstrip("/")
BASE_URL = PUBLIC_URL or f"http://127.0.0.1:{PORT}"
SECURE_COOKIES = BASE_URL.startswith("https://")

KEYRING_SERVICE = "yt-automation-studio"
USER_AGENT = f"YTAutomationStudio/{APP_VERSION} (personal local research tool; honours robots.txt)"

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"

SHORT_W, SHORT_H = 1080, 1920
LONG_W, LONG_H = 1920, 1080
FPS = 30
THUMB_W, THUMB_H = 1280, 720


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOG_DIR, VOICES_DIR, PRODUCTIONS_DIR, CACHE_DIR, ASSET_CACHE_DIR,
              MUSIC_DIR, BRAND_DIR, SAMPLES_DIR, SHORT_CACHE_DIR, FONTS_DIR, MARKET_DIR, AUDIT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def frame_size(fmt: str) -> tuple[int, int]:
    return (SHORT_W, SHORT_H) if fmt == "short" else (LONG_W, LONG_H)
