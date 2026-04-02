import os
import re
import html
import json
import logging
import asyncio
import time
import base64
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
    InlineQueryResultPhoto,
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

session = requests.Session()

# =========================
# ESTADO DO GRUPO
# =========================

PENDING_REPLIES: Dict[Tuple[int, int], float] = {}
REPLY_TIMEOUT = 900  # 15 minutos

# =========================
# ESTADO ISOLADO /STORY
# =========================

STORY_PENDING_BY_PROMPT: Dict[Tuple[int, int], Dict[str, Any]] = {}
STORY_PENDING_BY_USER: Dict[Tuple[int, int], int] = {}
STORY_TIMEOUT = 900  # 15 minutos
STORY_IMAGE_SIZE = (1080, 1920)
STORY_FOREGROUND_SIZE = 780
STORY_BG_BLUR = 28
STORY_BG_DARKEN = 0.42
STORY_CACHE_TTL_SECONDS = 60 * 60 * 24 * 30

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
            decode_responses=False,
        )
        redis_client.ping()
        logger.info("Redis conectado ✅")
    except Exception as e:
        logger.warning("Redis OFF: %s", e)
        redis_client = None

connect_redis()

# =========================
# SANITIZE / TRADUÇÃO
# =========================

FORBIDDEN = re.compile(
    r'[\u0600-\u06FF\u0400-\u04FF\u4E00-\u9FFF\u0900-\u097F\u0980-\u09FF]'
)

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
    except Exception:
        return "Unknown"


def sanitize(text: Any) -> str:
    if text is None:
        return "Unknown"

    text = str(text).strip()
    if not text:
        return "Unknown"

    if FORBIDDEN.search(text):
        return translate_sync(text)

    return text


def esc(text: Any) -> str:
    return html.escape(sanitize(text))

# =========================
# HELPERS DE LAYOUT
# =========================

def build_caption(title: Any, artist: Any, plays: int, user_first_name: Optional[str] = None) -> str:
    header = ""
    if user_first_name:
        header = f"🎹 {esc(user_first_name)} está ouvindo...\n"

    return (
        f"{header}"
        f"🎧 <b>{esc(title)}</b>\n"
        f"🎤 <i>{esc(artist)}</i>\n"
        f"<i>🔁 {plays} Plays</i>"
    )


def build_track_meta(track: Dict[str, Any]) -> Dict[str, str]:
    return {
        "title": str(track.get("title") or "Unknown"),
        "artist": str((track.get("artist") or {}).get("name") or "Unknown"),
        "cover_big": str((track.get("album") or {}).get("cover_big") or ""),
        "cover_small": str((track.get("album") or {}).get("cover_small") or ""),
    }


def remember_track(track: Dict[str, Any]) -> None:
    if not redis_client or not track:
        return

    track_id = str(track.get("id") or "")
    if not track_id:
        return

    meta = build_track_meta(track)
    try:
        redis_client.hset(f"trackmeta:{track_id}", mapping={k: v.encode("utf-8") for k, v in meta.items()})
    except Exception as e:
        logger.warning("Falha ao salvar trackmeta %s: %s", track_id, e)


def get_play_count(user_id: int, track_id: Any) -> int:
    if not redis_client:
        return 0

    try:
        value = redis_client.get(f"plays:{user_id}:{track_id}")
        return int(value) if value else 0
    except Exception:
        return 0


def register_play(user_id: int, track: Dict[str, Any]) -> int:
    if not redis_client or not track:
        return 0

    track_id = str(track.get("id") or "")
    if not track_id:
        return 0

    remember_track(track)

    try:
        pipe = redis_client.pipeline()
        pipe.incr(f"plays:{user_id}:{track_id}")
        pipe.zincrby(f"top:user:{user_id}", 1, track_id)
        pipe.zincrby("top:tracks", 1, track_id)
        result = pipe.execute()
        return int(result[0] or 0)
    except Exception as e:
        logger.warning("Falha ao registrar play: %s", e)
        return 0


async def fetch_track_meta(track_id: str) -> Dict[str, str]:
    if redis_client:
        try:
            meta = redis_client.hgetall(f"trackmeta:{track_id}")
            if meta and meta.get(b"title"):
                return {k.decode("utf-8"): v.decode("utf-8") for k, v in meta.items()}
        except Exception:
            pass

    track = await deezer_track(track_id)
    if track and track.get("id"):
        remember_track(track)
        if redis_client:
            try:
                meta = redis_client.hgetall(f"trackmeta:{track_id}")
                if meta and meta.get(b"title"):
                    return {k.decode("utf-8"): v.decode("utf-8") for k, v in meta.items()}
            except Exception:
                pass

    return {
        "title": f"Track {track_id}",
        "artist": "Unknown",
        "cover_big": "",
        "cover_small": "",
    }


async def resolve_track(track_id: str) -> Dict[str, Any]:
    track = await deezer_track(track_id)
    if track and track.get("id"):
        remember_track(track)
        return track

    meta = await fetch_track_meta(track_id)
    return {
        "id": track_id,
        "title": meta.get("title", "Unknown"),
        "artist": {"name": meta.get("artist", "Unknown")},
        "album": {
            "cover_big": meta.get("cover_big", ""),
            "cover_small": meta.get("cover_small", ""),
        }
    }

# =========================
# DEEZER
# =========================

async def deezer_search(query: str):
    query = (query or "").strip()
    if not query:
        return []

    try:
        r = await asyncio.to_thread(
            session.get,
            "https://api.deezer.com/search",
            params={"q": query},
            timeout=6,
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        logger.warning("Erro Deezer search: %s", e)
        return []


async def deezer_track(track_id: str):
    try:
        r = await asyncio.to_thread(
            session.get,
            f"https://api.deezer.com/track/{track_id}",
            timeout=6,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("Erro Deezer track %s: %s", track_id, e)
        return None

# =========================
# STORY CACHE
# =========================


def _safe_track_id(track_id: Any) -> str:
    value = str(track_id or "").strip()
    value = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    return value or "unknown"


def _story_cache_path(track_id: Any) -> Path:
    STORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return STORY_CACHE_DIR / f"{_safe_track_id(track_id)}.jpg"


def story_cache_get(track_id: Any) -> Optional[Path]:
    path = _story_cache_path(track_id)
    if path.exists() and path.is_file() and path.stat().st_size > 0:
        return path

    if redis_client:
        try:
            raw = redis_client.get(f"story:cache:{_safe_track_id(track_id)}")
            if raw:
                cached_path = Path(raw.decode("utf-8"))
                if cached_path.exists() and cached_path.is_file() and cached_path.stat().st_size > 0:
                    return cached_path
        except Exception as e:
            logger.warning("story_cache_get redis falhou: %s", e)

    return None


def story_cache_set(track_id: Any, image_bytes: bytes) -> Path:
    path = _story_cache_path(track_id)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_bytes(image_bytes)
    tmp_path.replace(path)

    if redis_client:
        try:
            redis_client.set(
                f"story:cache:{_safe_track_id(track_id)}",
                str(path).encode("utf-8"),
                ex=STORY_CACHE_TTL_SECONDS,
            )
        except Exception as e:
            logger.warning("story_cache_set redis falhou: %s", e)

    return path

# =========================
# STORY IMAGE RENDERING
# =========================

try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE_LANCZOS = Image.LANCZOS

try:
    TRANSPOSE = Image.Transpose
except AttributeError:
    TRANSPOSE = Image

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    try:
        return ImageFont.truetype(path, size=size)
    except Exception:
        return ImageFont.load_default()


def _hex_from_int(value: int) -> str:
    return f"{value & 0xFF:02x}"


def _hash_color(seed: str) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    h = abs(hash(seed))
    c1 = (
        40 + (h % 140),
        30 + ((h // 7) % 140),
        50 + ((h // 13) % 140),
    )
    c2 = (
        10 + ((h // 17) % 90),
        10 + ((h // 23) % 90),
        10 + ((h // 29) % 90),
    )
    return c1, c2


def _make_placeholder_cover(track: Dict[str, Any]) -> Image.Image:
    title = sanitize(track.get("title") or "Unknown")
    artist = sanitize((track.get("artist") or {}).get("name") or "Unknown")
    seed = f"{track.get('id', '')}:{title}:{artist}"
    c1, c2 = _hash_color(seed)

    size = (1200, 1200)
    img = Image.new("RGB", size, c1)
    draw = ImageDraw.Draw(img)

    # Vertical gradient
    for y in range(size[1]):
        ratio = y / max(size[1] - 1, 1)
        r = int(c1[0] * (1 - ratio) + c2[0] * ratio)
        g = int(c1[1] * (1 - ratio) + c2[1] * ratio)
        b = int(c1[2] * (1 - ratio) + c2[2] * ratio)
        draw.line((0, y, size[0], y), fill=(r, g, b))

    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.ellipse((-150, -80, 600, 670), fill=(255, 255, 255, 28))
    od.ellipse((650, 500, 1350, 1250), fill=(255, 255, 255, 20))
    img = Image.alpha_composite(img.convert("RGBA"), overlay)

    draw = ImageDraw.Draw(img)
    title_font = _load_font(60, bold=True)
    artist_font = _load_font(36, bold=False)

    def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
        words = text.split()
        lines: List[str] = []
        current = ""
        for word in words:
            trial = word if not current else f"{current} {word}"
            bbox = draw.textbbox((0, 0), trial, font=font)
            width = bbox[2] - bbox[0]
            if width <= max_width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines[:4]

    title_lines = wrap_text(title, title_font, 980)
    artist_lines = wrap_text(artist, artist_font, 980)

    total_h = len(title_lines) * 72 + len(artist_lines) * 48 + 70
    y = (size[1] - total_h) // 2

    for line in title_lines:
        bbox = draw.textbbox((0, 0), line, font=title_font)
        w = bbox[2] - bbox[0]
        draw.text(((size[0] - w) / 2, y), line, font=title_font, fill=(255, 255, 255, 235))
        y += 72

    y += 12
    for line in artist_lines:
        bbox = draw.textbbox((0, 0), line, font=artist_font)
        w = bbox[2] - bbox[0]
        draw.text(((size[0] - w) / 2, y), line, font=artist_font, fill=(255, 255, 255, 210))
        y += 48

    return img.convert("RGB")


def _round_corners(img: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, img.size[0] - 1, img.size[1] - 1), radius=radius, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img.convert("RGBA"), (0, 0), mask)
    return out


def _add_shadow(base: Image.Image, box: Tuple[int, int, int, int], radius: int = 42) -> None:
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow)
    x1, y1, x2, y2 = box
    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(24))
    base.alpha_composite(shadow)


def story_render_image(track: Dict[str, Any], cover_image: Image.Image) -> bytes:
    cover = ImageOps.exif_transpose(cover_image).convert("RGB")

    bg = ImageOps.fit(cover, STORY_IMAGE_SIZE, method=RESAMPLE_LANCZOS, centering=(0.5, 0.5))
    bg = bg.filter(ImageFilter.GaussianBlur(STORY_BG_BLUR))
    bg = ImageEnhance.Brightness(bg).enhance(STORY_BG_DARKEN)
    bg = bg.convert("RGBA")

    overlay = Image.new("RGBA", STORY_IMAGE_SIZE, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle((0, 0, STORY_IMAGE_SIZE[0], STORY_IMAGE_SIZE[1]), fill=(0, 0, 0, 35))
    od.rectangle((0, 0, STORY_IMAGE_SIZE[0], 260), fill=(0, 0, 0, 55))
    od.rectangle((0, STORY_IMAGE_SIZE[1] - 300, STORY_IMAGE_SIZE[0], STORY_IMAGE_SIZE[1]), fill=(0, 0, 0, 70))
    bg.alpha_composite(overlay)

    fg = ImageOps.fit(cover, (STORY_FOREGROUND_SIZE, STORY_FOREGROUND_SIZE), method=RESAMPLE_LANCZOS, centering=(0.5, 0.5))
    fg = _round_corners(fg, 56)

    x = (STORY_IMAGE_SIZE[0] - STORY_FOREGROUND_SIZE) // 2
    y = (STORY_IMAGE_SIZE[1] - STORY_FOREGROUND_SIZE) // 2

    _add_shadow(bg, (x - 10, y - 10, x + STORY_FOREGROUND_SIZE + 10, y + STORY_FOREGROUND_SIZE + 10), radius=60)

    border = Image.new("RGBA", (STORY_FOREGROUND_SIZE + 12, STORY_FOREGROUND_SIZE + 12), (255, 255, 255, 0))
    bd = ImageDraw.Draw(border)
    bd.rounded_rectangle((0, 0, border.size[0] - 1, border.size[1] - 1), radius=60, outline=(255, 255, 255, 100), width=4)
    bg.alpha_composite(border, (x - 6, y - 6))
    bg.alpha_composite(fg, (x, y))

    # Salva em JPEG para ser leve e compatível com Telegram
    out = BytesIO()
    bg.convert("RGB").save(out, format="JPEG", quality=92, optimize=True, progressive=True)
    return out.getvalue()


def _story_clear_pending(now: Optional[float] = None) -> None:
    if now is None:
        now = time.time()

    expired_prompts = []
    for key, value in STORY_PENDING_BY_PROMPT.items():
        if now - float(value.get("ts", 0)) > STORY_TIMEOUT:
            expired_prompts.append(key)

    for key in expired_prompts:
        data = STORY_PENDING_BY_PROMPT.pop(key, None)
        if not data:
            continue
        user_key = (key[0], int(data.get("user_id", 0)))
        if STORY_PENDING_BY_USER.get(user_key) == key[1]:
            STORY_PENDING_BY_USER.pop(user_key, None)


def _story_register_prompt(chat_id: int, user_id: int, message_id: int) -> None:
    now = time.time()
    _story_clear_pending(now)

    old_prompt_id = STORY_PENDING_BY_USER.get((chat_id, user_id))
    if old_prompt_id:
        STORY_PENDING_BY_PROMPT.pop((chat_id, old_prompt_id), None)

    STORY_PENDING_BY_PROMPT[(chat_id, message_id)] = {
        "user_id": user_id,
        "ts": now,
    }
    STORY_PENDING_BY_USER[(chat_id, user_id)] = message_id


def _story_consume_prompt(chat_id: int, user_id: int, message_id: int) -> None:
    STORY_PENDING_BY_PROMPT.pop((chat_id, message_id), None)
    if STORY_PENDING_BY_USER.get((chat_id, user_id)) == message_id:
        STORY_PENDING_BY_USER.pop((chat_id, user_id), None)


class StoryReplyFilter(filters.MessageFilter):
    def filter(self, message: Message) -> bool:
        if not message or not message.reply_to_message:
            return False
        if not message.from_user or not message.reply_to_message.from_user:
            return False

        key = (message.chat.id, message.reply_to_message.message_id)
        pending = STORY_PENDING_BY_PROMPT.get(key)
        if not pending:
            return False
        if int(pending.get("user_id", 0)) != message.from_user.id:
            return False
        if time.time() - float(pending.get("ts", 0)) > STORY_TIMEOUT:
            return False
        return True


async def story_fetch_track(query: str) -> Optional[Dict[str, Any]]:
    cleaned = re.sub(r"\s+", " ", (query or "").strip())
    if not cleaned:
        return None

    tracks = await deezer_search(cleaned)
    if not tracks:
        return None

    track = tracks[0]
    if not track or not track.get("id"):
        return None

    remember_track(track)
    return track


async def story_fetch_cover(track: Dict[str, Any]) -> Image.Image:
    album = track.get("album") or {}
    urls = [
        album.get("cover_xl"),
        album.get("cover_big"),
        album.get("cover_medium"),
        album.get("cover"),
        album.get("cover_small"),
    ]
    urls = [u for u in urls if isinstance(u, str) and u.strip()]

    for url in urls:
        try:
            response = await asyncio.to_thread(session.get, url, timeout=8)
            response.raise_for_status()
            data = response.content
            if not data:
                continue
            img = Image.open(BytesIO(data))
            img.load()
            return ImageOps.exif_transpose(img)
        except (requests.RequestException, UnidentifiedImageError, OSError, ValueError) as e:
            logger.warning("story_fetch_cover falhou para %s: %s", url, e)
        except Exception as e:
            logger.warning("story_fetch_cover erro inesperado: %s", e)

    return _make_placeholder_cover(track)


async def story_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not update.effective_chat or not update.effective_user:
        return

    prompt = await msg.reply_text(
        "🎵 Responda esta mensagem com o nome da música.",
        parse_mode=ParseMode.HTML,
    )
    _story_register_prompt(update.effective_chat.id, update.effective_user.id, prompt.message_id)


async def story_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.reply_to_message or not update.effective_chat or not update.effective_user:
        return

    reply_text = (msg.text or msg.caption or "").strip()
    if not reply_text:
        await msg.reply_text("🎵 Envie um nome de música válido.")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    prompt_id = msg.reply_to_message.message_id

    pending = STORY_PENDING_BY_PROMPT.get((chat_id, prompt_id))
    if not pending:
        return

    if int(pending.get("user_id", 0)) != user_id:
        return

    if time.time() - float(pending.get("ts", 0)) > STORY_TIMEOUT:
        _story_consume_prompt(chat_id, user_id, prompt_id)
        await msg.reply_text("⏱️ Tempo expirado. Use /story novamente.")
        return

    _story_consume_prompt(chat_id, user_id, prompt_id)

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)

    track = await story_fetch_track(reply_text)
    if not track:
        await msg.reply_text("🔎 Música não encontrada.")
        return

    track_id = str(track.get("id") or "")
    cached = story_cache_get(track_id)
    if cached:
        try:
            await msg.reply_photo(photo=str(cached))
            return
        except Exception as e:
            logger.warning("Falha ao enviar cache story %s: %s", track_id, e)

    try:
        cover = await story_fetch_cover(track)
        image_bytes = await asyncio.to_thread(story_render_image, track, cover)
        cached_path = await asyncio.to_thread(story_cache_set, track_id, image_bytes)
        await msg.reply_photo(photo=str(cached_path))
    except Exception as e:
        logger.warning("Falha ao gerar /story para %s: %s", track_id, e)
        await msg.reply_text("⚠️ Não foi possível gerar a imagem.")

# =========================
# BACKUP / STATS EXPORT
# =========================

def _serialize_redis_key(key: bytes) -> Dict[str, Any]:
    assert redis_client is not None

    try:
        key_type = redis_client.type(key)
        ttl = redis_client.ttl(key)

        if key_type == b"string":
            value = redis_client.get(key)
        elif key_type == b"hash":
            value = redis_client.hgetall(key)
        elif key_type == b"zset":
            value = redis_client.zrange(key, 0, -1, withscores=True)
        elif key_type == b"set":
            value = sorted(list(redis_client.smembers(key)))
        elif key_type == b"list":
            value = redis_client.lrange(key, 0, -1)
        else:
            value = None

        return {
            "type": key_type.decode("utf-8", errors="ignore"),
            "ttl": ttl,
            "value": value,
        }
    except Exception as e:
        return {
            "type": "error",
            "ttl": None,
            "value": f"{type(e).__name__}: {e}",
        }


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


async def backup_redis_to_disk() -> Optional[str]:
    if not redis_client:
        return None

    try:
        keys = list(redis_client.scan_iter("*"))
        dump: Dict[str, Any] = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "redis_url_present": bool(REDIS_URL),
            "keys_count": len(keys),
            "keys": {},
        }

        for key in keys:
            dump["keys"][key.decode("utf-8", errors="ignore")] = _serialize_redis_key(key)

        filename = f"redis_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        path = os.path.join(BACKUP_PATH, filename)
        _write_json(path, dump)
        logger.info("Backup Redis salvo em %s", path)
        return path
    except Exception as e:
        logger.error("Erro no backup do Redis: %s", e)
        return None


async def export_stats_to_disk() -> Optional[str]:
    if not redis_client:
        return None

    try:
        top_global = redis_client.zrevrange(b"top:tracks", 0, 99, withscores=True)
        exported_users: Dict[str, Any] = {}

        for key in redis_client.scan_iter(b"top:user:*"):
            try:
                user_id = key.decode("utf-8", errors="ignore").split("top:user:", 1)[1]
                exported_users[user_id] = redis_client.zrevrange(key, 0, 99, withscores=True)
            except Exception:
                continue

        payload: Dict[str, Any] = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "bot": BOT_DISPLAY_NAME,
            "top_global": top_global,
            "top_users": exported_users,
            "summary": {
                "global_entries": len(top_global),
                "users_exported": len(exported_users),
            },
        }

        filename = f"stats_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        path = os.path.join(BACKUP_PATH, filename)
        _write_json(path, payload)
        logger.info("Stats exportado em %s", path)
        return path
    except Exception as e:
        logger.error("Erro ao exportar stats: %s", e)
        return None

# =========================
# START / HELP
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"🎶 <b>{BOT_DISPLAY_NAME}</b>\n"
        f"🎧 Digite o nome de uma música ou use <code>{BOT_USERNAME} nome</code>\n\n"
        f"📌 Comandos:\n"
        f"/charts — suas músicas mais ouvidas\n"
        f"/top — ranking global\n"
        f"/play – enviar uma música pelo grupo\n"
        f"/story — gerar story 9:16 da música"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# =========================
# BUSCA NORMAL
# =========================

async def search_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.message.text or "").strip()
    if not query:
        await update.message.reply_text("🎤 Digite o nome de uma música.")
        return

    tracks = await deezer_search(query)

    if not tracks:
        await update.message.reply_text("🔎 Nada encontrado.")
        return

    keyboard = []

    for t in tracks[:5]:
        try:
            track_id = str(t["id"])
            remember_track(t)

            title = sanitize(t.get("title"))
            artist = sanitize((t.get("artist") or {}).get("name"))

            keyboard.append([
                InlineKeyboardButton(
                    f"🎵 {title} — {artist}",
                    callback_data=f"play:{track_id}"
                )
            ])
        except Exception as e:
            logger.warning("Erro montando botão: %s", e)

    await update.message.reply_text(
        "🎧 Escolha:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# =========================
# NOVO: GRUPO EXCLUSIVO
# =========================

def cleanup_pending(now: float) -> None:
    expired = [k for k, ts in PENDING_REPLIES.items() if now - ts > REPLY_TIMEOUT]
    for k in expired:
        PENDING_REPLIES.pop(k, None)


async def group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ["group", "supergroup"]:
        return

    key = (chat.id, user.id)
    now = time.time()
    cleanup_pending(now)

    text = (msg.text or "").strip()

    is_command = text.startswith("/play")
    is_mention = BOT_USERNAME.lower() in text.lower()

    is_reply_to_bot = (
        msg.reply_to_message
        and msg.reply_to_message.from_user
        and msg.reply_to_message.from_user.id == context.bot.id
    )

    if is_command or is_mention:
        PENDING_REPLIES[key] = now
        await msg.reply_text(
            "🎧Responda aqui o nome de uma música ou use "
            f"{BOT_USERNAME} para pesquisar <i>inline</i>",
            parse_mode=ParseMode.HTML
        )
        return

    if is_reply_to_bot and key in PENDING_REPLIES:
        if now - PENDING_REPLIES[key] > REPLY_TIMEOUT:
            PENDING_REPLIES.pop(key, None)
            await msg.reply_text("⏱️ Tempo expirado. Use /play novamente.")
            return

        PENDING_REPLIES.pop(key, None)
        await search_music(update, context)
        return

    return


async def play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    now = time.time()

    if chat.type in ["group", "supergroup"]:
        key = (chat.id, user.id)
        cleanup_pending(now)
        PENDING_REPLIES[key] = now

        await update.message.reply_text(
            "🎧Responda aqui o nome de uma música ou use "
            f"{BOT_USERNAME} para pesquisar <i>inline</i>",
            parse_mode=ParseMode.HTML
        )
        return

    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("🎤 Digite o nome de uma música.")
        return

    tracks = await deezer_search(query)

    if not tracks:
        await update.message.reply_text("🔎 Nada encontrado.")
        return

    keyboard = []

    for t in tracks[:5]:
        try:
            track_id = str(t["id"])
            remember_track(t)

            title = sanitize(t.get("title"))
            artist = sanitize((t.get("artist") or {}).get("name"))

            keyboard.append([
                InlineKeyboardButton(
                    f"🎵 {title} — {artist}",
                    callback_data=f"play:{track_id}"
                )
            ])
        except Exception as e:
            logger.warning("Erro montando botão: %s", e)

    await update.message.reply_text(
        "🎧 Escolha:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# =========================
# CLICK DO CHAT
# =========================

async def click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()

    try:
        track_id = cb.data.split(":", 1)[1]
    except Exception:
        await cb.answer("⚠️ Ação inválida.", show_alert=True)
        return

    try:
        t = await resolve_track(track_id)
    except Exception as e:
        logger.warning("Falha ao resolver track %s: %s", track_id, e)
        await cb.answer("🔄 Não foi possível carregar a música.", show_alert=True)
        return

    if not t or not t.get("id"):
        await cb.answer("🔄 Refaça a busca.", show_alert=True)
        return

    count = register_play(cb.from_user.id, t)

    caption = build_caption(
        title=t.get("title"),
        artist=(t.get("artist") or {}).get("name"),
        plays=count,
        user_first_name=cb.from_user.first_name,
    )

    photo = (t.get("album") or {}).get("cover_big")

    try:
        if photo:
            await cb.message.reply_photo(
                photo=photo,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        else:
            await cb.message.reply_text(
                caption,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
    except Exception as e:
        logger.warning("Falha ao enviar música: %s", e)
        await cb.message.reply_text(
            caption,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )

# =========================
# INLINE MODE
# =========================

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.inline_query.query or "").strip()
    if not query:
        return

    user = update.inline_query.from_user
    tracks = await deezer_search(query)

    results = []

    for t in tracks[:10]:
        try:
            track_id = str(t["id"])
            title = sanitize(t.get("title"))
            artist = sanitize((t.get("artist") or {}).get("name"))
            cover_big = (t.get("album") or {}).get("cover_big")
            cover_small = (t.get("album") or {}).get("cover_small")

            if not cover_big:
                continue

            remember_track(t)
            current_count = get_play_count(user.id, track_id)

            results.append(
                InlineQueryResultPhoto(
                    id=f"track:{track_id}",
                    photo_url=cover_big,
                    thumbnail_url=cover_small or cover_big,
                    caption=build_caption(
                        title=title,
                        artist=artist,
                        plays=current_count,
                        user_first_name=user.first_name,
                    ),
                    parse_mode=ParseMode.HTML,
                    title=f"{title} — {artist}",
                    description=f"{artist} • {current_count} Plays"
                )
            )
        except Exception as e:
            logger.warning("Erro inline item: %s", e)

    await update.inline_query.answer(
        results,
        cache_time=2,
        is_personal=True
    )

# =========================
# CHOSEN INLINE RESULT
# =========================

async def chosen_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not redis_client:
        return

    try:
        result_id = update.chosen_inline_result.result_id or ""
        if not result_id.startswith("track:"):
            return

        track_id = result_id.split(":", 1)[1]
        user_id = update.chosen_inline_result.from_user.id

        track = await resolve_track(track_id)
        if not track:
            meta = await fetch_track_meta(track_id)
            track = {
                "id": track_id,
                "title": meta.get("title", "Unknown"),
                "artist": {"name": meta.get("artist", "Unknown")},
                "album": {
                    "cover_big": meta.get("cover_big", ""),
                    "cover_small": meta.get("cover_small", ""),
                }
            }

        register_play(user_id, track)
    except Exception as e:
        logger.warning("Erro no chosen_inline: %s", e)

# =========================
# STATS (AGORA /CHARTS)
# =========================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not redis_client:
        await update.message.reply_text("⚠️ Redis indisponível.")
        return

    user_id = update.effective_user.id
    user_first_name = update.effective_user.first_name
    entries: List[Tuple[bytes, float]] = redis_client.zrevrange(
        f"top:user:{user_id}".encode("utf-8"),
        0,
        9,
        withscores=True
    )

    if not entries:
        await update.message.reply_text("🎧 Você ainda não ouviu músicas.")
        return

    metas = await asyncio.gather(*(fetch_track_meta(track_id.decode("utf-8")) for track_id, _ in entries))

    lines = [
        f"📊 <b>Músicas mais ouvidas de {esc(user_first_name or 'Usuário')} no {BOT_DISPLAY_NAME}</b>",
        ""
    ]

    for i, ((track_id, score), meta) in enumerate(zip(entries, metas), 1):
        track_id_str = track_id.decode("utf-8")
        title = sanitize(meta.get("title") or f"Track {track_id_str}")
        artist = sanitize(meta.get("artist") or "Unknown")

        lines.append(f"{i}. 🎧 <b>{esc(title)}</b>")
        lines.append(f"   🎤 <i>{esc(artist)}</i>")
        lines.append(f"   <i>🔁 {int(score)} Plays</i>")
        lines.append("")

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="\n".join(lines).strip(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

# =========================
# TOP
# =========================

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not redis_client:
        await update.message.reply_text("⚠️ Redis indisponível.")
        return

    entries: List[Tuple[bytes, float]] = redis_client.zrevrange(
        b"top:tracks",
        0,
        9,
        withscores=True
    )

    if not entries:
        await update.message.reply_text("🎧 Ainda não há plays registrados.")
        return

    metas = await asyncio.gather(*(fetch_track_meta(track_id.decode("utf-8")) for track_id, _ in entries))

    lines = [
        f"📈 <b>Top global do {BOT_DISPLAY_NAME}</b>",
        ""
    ]

    for i, ((track_id, score), meta) in enumerate(zip(entries, metas), 1):
        track_id_str = track_id.decode("utf-8")
        title = sanitize(meta.get("title") or f"Track {track_id_str}")
        artist = sanitize(meta.get("artist") or "Unknown")

        lines.append(f"{i}. 🎧 <b>{esc(title)}</b>")
        lines.append(f"   🎤 <i>{esc(artist)}</i>")
        lines.append(f"   <i>🔁 {int(score)} Plays</i>")
        lines.append("")

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="\n".join(lines).strip(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

# =========================
# /LOG
# =========================

def _chunk_text(text: str, limit: int = 3800) -> List[str]:
    if len(text) <= limit:
        return [text]

    parts = []
    start = 0
    while start < len(text):
        parts.append(text[start:start + limit])
        start += limit
    return parts


async def log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    lines = LOG_BUFFER[-40:]
    if not lines:
        await update.message.reply_text("Sem logs no buffer.")
        return

    payload = "<pre>" + html.escape("\n".join(lines)) + "</pre>"
    for chunk in _chunk_text(payload, 3800):
        await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)

# =========================
# TAREFAS AUTOMÁTICAS
# =========================

async def redis_backup_task():
    while True:
        await asyncio.sleep(86400)
        try:
            if redis_client:
                await backup_redis_to_disk()
        except Exception as e:
            logger.error("Falha no backup diário: %s", e)


async def stats_export_task():
    while True:
        await asyncio.sleep(86400)
        try:
            if redis_client:
                await export_stats_to_disk()
        except Exception as e:
            logger.error("Falha no export diário de stats: %s", e)


async def redis_monitor_task():
    while True:
        await asyncio.sleep(3600)
        try:
            if redis_client is None:
                logger.warning("Redis desconectado, tentando reconectar...")
                connect_redis()
            else:
                redis_client.ping()
                logger.info("Monitor Redis: OK")
        except Exception as e:
            logger.warning("Monitor Redis detectou falha: %s", e)
            connect_redis()


async def post_init(application: Application):
    application.create_task(redis_backup_task())
    application.create_task(stats_export_task())
    application.create_task(redis_monitor_task())
    logger.info("Tarefas automáticas iniciadas")

# =========================
# ERROS
# =========================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("ERRO:", exc_info=context.error)

# =========================
# MAIN
# =========================

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN não definido")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("play", play))
    app.add_handler(CommandHandler("story", story_command))
    app.add_handler(CommandHandler("charts", stats))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("log", log_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.REPLY & StoryReplyFilter(), story_reply_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, group_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, search_music))

    app.add_handler(CallbackQueryHandler(click))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(ChosenInlineResultHandler(chosen_inline))
    app.add_error_handler(error_handler)

    logger.info("BOT ONLINE 🚀")
    app.run_polling()


if __name__ == "__main__":
    main()
