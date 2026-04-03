import os
import re
import html
import json
import logging
import asyncio
import time
import base64
import shutil
import unicodedata
import uuid
import atexit
from io import BytesIO
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple

import requests
import redis
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageStat, UnidentifiedImageError

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputTextMessageContent,
    ForceReply,
    Message,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================

BOT_USERNAME = "@tigraoFMbot"
BOT_DISPLAY_NAME = "tigraoFM"

TOKEN = os.getenv("TELEGRAM_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BACKUP_PATH = os.getenv("BACKUP_PATH", "/tmp")

STORY_CACHE_DIR = Path(os.getenv("STORY_CACHE_DIR", os.path.join(BACKUP_PATH, "story_cache")))
STORY_BACKUP_DIR = Path(os.getenv("STORY_BACKUP_DIR", os.path.join(BACKUP_PATH, "story_backup")))
BASE_DIR = Path(__file__).resolve().parent

LOCK_FILE_PATH = Path(os.getenv("BOT_LOCK_FILE", os.path.join(BACKUP_PATH, "tigraoFMbot.lock")))
INSTANCE_LOCK_KEY = "tigraoFMbot:instance_lock"
INSTANCE_LOCK_TOKEN = str(uuid.uuid4())
INSTANCE_LOCK_TTL = 90
INSTANCE_LOCK_RENEW_INTERVAL = 30
INSTANCE_LOCK_ACQUIRED = False
LOCK_FILE_HANDLE = None
BACKGROUND_TASKS: List[asyncio.Task] = []

session = requests.Session()

# =========================
# ESTADO DO GRUPO
# =========================

PENDING_REPLIES: Dict[Tuple[int, int], float] = {}
PENDING_ACTIONS: Dict[Tuple[int, int], str] = {}
REPLY_TIMEOUT = 900  # 15 minutos

# =========================
# ESTADO ISOLADO /STORY
# =========================

STORY_PENDING_BY_PROMPT: Dict[Tuple[int, int], Dict[str, Any]] = {}
STORY_PENDING_BY_USER: Dict[Tuple[int, int], int] = {}
STORY_TIMEOUT = 900  # 15 minutos
STORY_CACHE_TTL_SECONDS = 60 * 60 * 24 * 30
STORY_RENDER_LOCKS: Dict[str, asyncio.Lock] = {}
STORY_MIN_MATCH_SCORE = 4.5

# =========================
# LOGGING
# =========================

LOG_BUFFER: List[str] = []
LOG_LIMIT = 300

class BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            LOG_BUFFER.append(msg)
            if len(LOG_BUFFER) > LOG_LIMIT:
                del LOG_BUFFER[0:len(LOG_BUFFER) - LOG_LIMIT]
        except Exception:
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("bot")
buffer_handler = BufferHandler()
buffer_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
logger.addHandler(buffer_handler)

# =========================
# REDIS INIT
# =========================

redis_client: Optional[redis.Redis] = None

def connect_redis() -> None:
    global redis_client
    if not REDIS_URL:
        logger.warning("REDIS_URL não definida")
        redis_client = None
        return

    try:
        redis_client = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True, 
        )
        redis_client.ping()
        logger.info("Redis conectado ✅")
    except Exception as e:
        logger.warning("Redis OFF: %s", e)
        redis_client = None

connect_redis()

# =========================
# SINGLE INSTANCE LOCK
# =========================

def acquire_instance_lock() -> bool:
    global INSTANCE_LOCK_ACQUIRED, LOCK_FILE_HANDLE

    if redis_client:
        try:
            if redis_client.set(INSTANCE_LOCK_KEY, INSTANCE_LOCK_TOKEN, nx=True, ex=INSTANCE_LOCK_TTL):
                INSTANCE_LOCK_ACQUIRED = True
                logger.info("Instance lock acquired via Redis ✅")
                return True

            owner = redis_client.get(INSTANCE_LOCK_KEY)
            logger.error("Outra instância do bot já está ativa (Redis lock). token_atual=%s", owner[:12] if owner else "unknown")
            return False
        except Exception as e:
            logger.warning("Não foi possível usar lock no Redis: %s", e)

    try:
        LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCK_FILE_HANDLE = open(LOCK_FILE_PATH, "a+")
        try:
            import fcntl
            fcntl.flock(LOCK_FILE_HANDLE, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError as e:
            logger.error("fcntl indisponível; não foi possível garantir lock exclusivo: %s", e)
            return False

        LOCK_FILE_HANDLE.seek(0)
        LOCK_FILE_HANDLE.truncate()
        LOCK_FILE_HANDLE.write(f"{os.getpid()}\n")
        LOCK_FILE_HANDLE.flush()
        INSTANCE_LOCK_ACQUIRED = True
        logger.info("Instance lock acquired via file ✅")
        return True
    except Exception as e:
        logger.error("Outra instância do bot já está ativa (file lock). %s", e)
        return False

def release_instance_lock() -> None:
    global INSTANCE_LOCK_ACQUIRED, LOCK_FILE_HANDLE

    if redis_client and INSTANCE_LOCK_ACQUIRED:
        try:
            current = redis_client.get(INSTANCE_LOCK_KEY)
            if current == INSTANCE_LOCK_TOKEN:
                redis_client.delete(INSTANCE_LOCK_KEY)
        except Exception as e:
            logger.warning("Falha ao liberar lock no Redis: %s", e)

    if LOCK_FILE_HANDLE is not None:
        try:
            try:
                import fcntl
                fcntl.flock(LOCK_FILE_HANDLE, fcntl.LOCK_UN)
            except Exception:
                pass
            LOCK_FILE_HANDLE.close()
        except Exception:
            pass
        finally:
            LOCK_FILE_HANDLE = None

    INSTANCE_LOCK_ACQUIRED = False

async def instance_lock_renew_task() -> None:
    while True:
        await asyncio.sleep(INSTANCE_LOCK_RENEW_INTERVAL)
        if not redis_client or not INSTANCE_LOCK_ACQUIRED:
            continue
        try:
            current = redis_client.get(INSTANCE_LOCK_KEY)
            if current == INSTANCE_LOCK_TOKEN:
                redis_client.expire(INSTANCE_LOCK_KEY, INSTANCE_LOCK_TTL)
            else:
                logger.error("Lock Redis perdido; encerrando renovação.")
                return
        except Exception as e:
            logger.warning("Falha ao renovar lock no Redis: %s", e)

# =========================
# SANITIZE / TRADUÇÃO / RANKING
# =========================

FORBIDDEN = re.compile(
    r'[\u0600-\u06FF\u0400-\u04FF\u4E00-\u9FFF\u0900-\u097F\u0980-\u09FF]'
)

def normalize_query(value: Any, max_len: int = 200) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if ch.isprintable())
    text = re.sub(r"\s+", " ", text).strip()
    if max_len and len(text) > max_len:
        text = text[:max_len].rstrip()
    return text

def translate_sync(text: str) -> str:
    try:
        r = session.get(
            "https://translate.googleapis.com/translate_a/single",
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": "en",
                "dt": "t",
                "q": text,
            },
            timeout=4,
        )
        r.raise_for_status()
        data = r.json()
        translated = "".join(part[0] for part in data[0] if part and part[0])
        return f"[en: {translated}]"
