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

# locks para evitar duplicidade de render/lookup da mesma música
STORY_RENDER_LOCKS: Dict[str, asyncio.Lock] = {}

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


def _redis_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _redis_jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if isinstance(value, dict):
        return {_redis_jsonable(k): _redis_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redis_jsonable(v) for v in value]
    return value

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

    track_id = _redis_text(track.get("id") or "").strip()
    if not track_id:
        return

    meta = build_track_meta(track)
    try:
        redis_client.hset(f"trackmeta:{track_id}", mapping=meta)
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
    track_id = _redis_text(track_id).strip()
    if redis_client and track_id:
        try:
            meta = redis_client.hgetall(f"trackmeta:{track_id}")
            if meta:
                normalized = {_redis_text(k): _redis_text(v) for k, v in meta.items()}
                if normalized.get("title"):
                    return normalized
        except Exception:
            pass

    track = await deezer_track(track_id)
    if track and track.get("id"):
        remember_track(track)
        if redis_client:
            try:
                meta = redis_client.hgetall(f"trackmeta:{track_id}")
                if meta:
                    normalized = {_redis_text(k): _redis_text(v) for k, v in meta.items()}
                    if normalized.get("title"):
                        return normalized
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
        data = r.json()
        return data.get("data", [])
    except Exception as e:
        logger.warning("Erro Deezer search: %s", e)
        return None


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

def _score_itunes_result(query: str, item: Dict[str, Any]) -> float:
    q = re.sub(r"\s+", " ", query).strip().lower()
    title = str(item.get("trackName") or "").lower()
    artist = str(item.get("artistName") or "").lower()
    collection = str(item.get("collectionName") or "").lower()

    score = 0.0
    if q and q in title:
        score += 3.0
    if q and q in artist:
        score += 1.5
    if q and q in collection:
        score += 0.5

    q_tokens = [tok for tok in re.split(r"\s+", q) if tok]
    if q_tokens:
        for tok in q_tokens:
            if tok in title:
                score += 0.4
            if tok in artist:
                score += 0.2

    if item.get("trackViewUrl"):
        score += 0.1

    return score


def _upgrade_artwork_url(url: str, size: int) -> str:
    if not url:
        return ""
    if "100x100" in url:
        return url.replace("100x100bb", f"{size}x{size}bb").replace("100x100", f"{size}x{size}")
    if "60x60" in url:
        return url.replace("60x60bb", f"{size}x{size}bb").replace("60x60", f"{size}x{size}")
    return url


def _itunes_search_sync(query: str) -> Optional[Dict[str, Any]]:
    params = {
        "term": query,
        "entity": "song",
        "media": "music",
        "limit": 10,
        "country": "BR",
    }
    try:
        r = session.get("https://itunes.apple.com/search", params=params, timeout=8)
        r.raise_for_status()
        data = r.json()
        results = data.get("results") or []
        if not results:
            return None

        best = max(results, key=lambda item: _score_itunes_result(query, item))
        track_id = best.get("trackId") or best.get("collectionId")
        if not track_id:
            return None

        artwork = str(best.get("artworkUrl100") or best.get("artworkUrl60") or "")
        return {
            "id": str(track_id),
            "title": str(best.get("trackName") or query),
            "artist": {"name": str(best.get("artistName") or "Unknown")},
            "album": {
                "cover_big": _upgrade_artwork_url(artwork, 1000),
                "cover_small": _upgrade_artwork_url(artwork, 300),
            },
            "source": "itunes",
            "artwork_url": _upgrade_artwork_url(artwork, 1000),
        }
    except Exception as e:
        logger.warning("story iTunes falhou: %s", e)
        return None


def _musicbrainz_search_sync(query: str) -> Optional[Dict[str, Any]]:
    headers = {
        "User-Agent": f"{BOT_DISPLAY_NAME}/1.0 (Telegram bot)",
        "Accept": "application/json",
    }
    params = {
        "query": query,
        "fmt": "json",
        "limit": 5,
    }
    try:
        r = session.get(
            "https://musicbrainz.org/ws/2/recording/",
            params=params,
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        recordings = data.get("recordings") or []
        if not recordings:
            return None

        best = recordings[0]
        recording_id = best.get("id")
        if not recording_id:
            return None

        artist_credit = best.get("artist-credit") or []
        artist_name = " & ".join(
            str(part.get("name") or part.get("artist", {}).get("name") or "")
            for part in artist_credit
            if isinstance(part, dict)
        ).strip() or "Unknown"

        releases = best.get("releases") or []
        release_id = ""
        release_title = ""
        if releases and isinstance(releases[0], dict):
            release_id = str(releases[0].get("id") or "")
            release_title = str(releases[0].get("title") or "")

        cover_url = ""
        if release_id:
            cover_url = f"https://coverartarchive.org/release/{release_id}/front-500"

        return {
            "id": str(recording_id),
            "title": str(best.get("title") or query),
            "artist": {"name": artist_name},
            "album": {
                "cover_big": cover_url,
                "cover_small": cover_url,
                "release_title": release_title,
            },
            "source": "musicbrainz",
            "release_id": release_id,
            "artwork_url": cover_url,
        }
    except Exception as e:
        logger.warning("story MusicBrainz falhou: %s", e)
        return None


try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE_LANCZOS = Image.LANCZOS

try:
    TRANSPOSE = Image.Transpose
except AttributeError:
    TRANSPOSE = Image

FONT_REGULAR = str(BASE_DIR / "DejaVuSans.ttf")
FONT_BOLD = str(BASE_DIR / "DejaVuSans-Bold.ttf")


def _slugify(text: Any, max_len: int = 72) -> str:
    value = sanitize(text)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:max_len] if value else "unknown"


def _safe_track_id(track_id: Any) -> str:
    value = str(track_id or "").strip()
    value = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    return value or "unknown"


def _story_track_tag(track: Dict[str, Any]) -> str:
    track_id = _safe_track_id(track.get("id"))
    title = _slugify(track.get("title") or "unknown")
    artist = _slugify((track.get("artist") or {}).get("name") or "unknown")
    return f"{track_id}__{title}__{artist}"


def _story_bundle_dir(base: Path, track_id: Any) -> Path:
    return base / _safe_track_id(track_id)


def _story_bundle_paths(track_id: Any) -> Dict[str, Path]:
    cache_dir = _story_bundle_dir(STORY_CACHE_DIR, track_id)
    backup_dir = _story_bundle_dir(STORY_BACKUP_DIR, track_id)
    return {
        "cache_dir": cache_dir,
        "backup_dir": backup_dir,
        "cache_image": cache_dir / "story.jpg",
        "cache_meta": cache_dir / "meta.json",
        "backup_image": backup_dir / "story.jpg",
        "backup_meta": backup_dir / "meta.json",
    }


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        Path(FONT_BOLD if bold else FONT_REGULAR),
        BASE_DIR / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
        Path.cwd() / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        try:
            if candidate.exists():
                return ImageFont.truetype(str(candidate), size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _truncate(text: Any, max_len: int) -> str:
    text = sanitize(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _format_duration(seconds: Any) -> str:
    try:
        total = int(seconds or 0)
    except Exception:
        total = 0
    if total <= 0:
        return ""
    minutes = total // 60
    sec = total % 60
    return f"{minutes}:{sec:02d}"


def _download_image(url: str) -> Optional[Image.Image]:
    if not url:
        return None
    try:
        r = session.get(url, timeout=8)
        r.raise_for_status()
        data = r.content
        if not data:
            return None
        img = Image.open(BytesIO(data))
        img.load()
        return ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:
        logger.warning("Falha ao baixar imagem %s: %s", url, e)
        return None


def _load_avatar_image(avatar_bytes: Optional[bytes], size: int = 96) -> Image.Image:
    if avatar_bytes:
        try:
            img = Image.open(BytesIO(avatar_bytes))
            img.load()
            img = ImageOps.exif_transpose(img).convert("RGBA")
            img = ImageOps.fit(img, (size, size), method=RESAMPLE_LANCZOS, centering=(0.5, 0.5))
            return img
        except Exception as e:
            logger.warning("Falha ao carregar avatar do bot: %s", e)

    avatar = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(avatar)
    draw.ellipse((0, 0, size - 1, size - 1), fill=(28, 28, 28, 255))
    note_font = _load_font(int(size * 0.45), bold=True)
    label = "♫"
    bbox = draw.textbbox((0, 0), label, font=note_font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(((size - tw) / 2, (size - th) / 2 - 4), label, font=note_font, fill=(255, 255, 255, 235))
    return avatar


def _make_placeholder_cover(track: Dict[str, Any]) -> Image.Image:
    title = sanitize(track.get("title") or "Unknown")
    artist = sanitize((track.get("artist") or {}).get("name") or "Unknown")
    seed = f"{track.get('id', '')}:{title}:{artist}"
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

    size = (1200, 1200)
    img = Image.new("RGB", size, c1)
    draw = ImageDraw.Draw(img)

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


def story_cache_get(track_id: Any) -> Optional[Path]:
    track_id = _safe_track_id(track_id)
    paths = _story_bundle_paths(track_id)

    if paths["cache_image"].exists() and paths["cache_image"].is_file() and paths["cache_image"].stat().st_size > 0:
        return paths["cache_image"]

    if paths["backup_image"].exists() and paths["backup_image"].is_file() and paths["backup_image"].stat().st_size > 0:
        try:
            paths["cache_dir"].mkdir(parents=True, exist_ok=True)
            shutil.copy2(paths["backup_image"], paths["cache_image"])
            if paths["backup_meta"].exists() and paths["backup_meta"].is_file():
                shutil.copy2(paths["backup_meta"], paths["cache_meta"])
        except Exception as e:
            logger.warning("Falha ao restaurar story do backup: %s", e)
        return paths["backup_image"]

    if redis_client:
        try:
            raw_path = redis_client.get(f"story:cache:{track_id}")
            if raw_path:
                candidate = Path(_redis_text(raw_path))
                if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate
        except Exception as e:
            logger.warning("story_cache_get redis falhou: %s", e)

    return None


def story_cache_set(
    track: Dict[str, Any],
    image_bytes: bytes,
    *,
    cover_url: str = "",
    user_name: str = "",
    bot_avatar_present: bool = False,
) -> Path:
    track_id = _safe_track_id(track.get("id"))
    paths = _story_bundle_paths(track_id)
    paths["cache_dir"].mkdir(parents=True, exist_ok=True)
    paths["backup_dir"].mkdir(parents=True, exist_ok=True)

    meta: Dict[str, Any] = {
        "track_id": track_id,
        "tag": _story_track_tag(track),
        "title": sanitize(track.get("title")),
        "artist": sanitize((track.get("artist") or {}).get("name")),
        "album": sanitize((track.get("album") or {}).get("title")),
        "duration": int(track.get("duration") or 0),
        "duration_human": _format_duration(track.get("duration")),
        "cover_url": cover_url,
        "user_name": sanitize(user_name),
        "bot_name": BOT_DISPLAY_NAME,
        "bot_avatar_present": bool(bot_avatar_present),
        "created_at": datetime.utcnow().isoformat() + "Z",
        "image_size": len(image_bytes),
    }

    tmp_cache = paths["cache_image"].with_suffix(".tmp")
    tmp_backup = paths["backup_image"].with_suffix(".tmp")
    tmp_cache.write_bytes(image_bytes)
    tmp_backup.write_bytes(image_bytes)
    tmp_cache.replace(paths["cache_image"])
    tmp_backup.replace(paths["backup_image"])

    _write_json(paths["cache_meta"], meta)
    _write_json(paths["backup_meta"], meta)

    if redis_client:
        try:
            redis_client.set(
                f"story:cache:{track_id}",
                str(paths["cache_image"]),
                ex=STORY_CACHE_TTL_SECONDS,
            )
            redis_client.set(
                f"story:meta:{track_id}",
                json.dumps(meta, ensure_ascii=False),
                ex=STORY_CACHE_TTL_SECONDS,
            )
        except Exception as e:
            logger.warning("story_cache_set redis falhou: %s", e)

    return paths["cache_image"]


def _add_shadow(base: Image.Image, box: Tuple[int, int, int, int], radius: int = 42) -> None:
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow)
    x1, y1, x2, y2 = box
    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=(0, 0, 0, 130))
    shadow = shadow.filter(ImageFilter.GaussianBlur(24))
    base.alpha_composite(shadow)


def _rounded_image(img: Image.Image, radius: int) -> Image.Image:
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, img.size[0] - 1, img.size[1] - 1), radius=radius, fill=255)
    rounded = Image.new("RGBA", img.size, (0, 0, 0, 0))
    rounded.paste(img, (0, 0), mask)
    return rounded


def story_render_image(
    track: Dict[str, Any],
    cover_image: Image.Image,
    user_name: str,
    bot_avatar_bytes: Optional[bytes],
) -> bytes:
    cover = ImageOps.exif_transpose(cover_image).convert("RGB")

    W, H = 1080, 1920
    bg = ImageOps.fit(cover, (W, H), method=RESAMPLE_LANCZOS, centering=(0.5, 0.5))
    bg = bg.filter(ImageFilter.GaussianBlur(28))
    bg = ImageEnhance.Brightness(bg).enhance(0.42).convert("RGBA")

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle((0, 0, W, H), fill=(0, 0, 0, 38))
    od.rectangle((0, 0, W, 290), fill=(0, 0, 0, 60))
    od.rectangle((0, H - 360, W, H), fill=(0, 0, 0, 88))
    bg.alpha_composite(overlay)

    draw = ImageDraw.Draw(bg)

    avatar_size = 92
    avatar = _load_avatar_image(bot_avatar_bytes, avatar_size)
    avatar_mask = Image.new("L", (avatar_size, avatar_size), 0)
    ImageDraw.Draw(avatar_mask).ellipse((0, 0, avatar_size - 1, avatar_size - 1), fill=255)

    header_x = 60
    header_y = 72
    bg.paste(avatar, (header_x, header_y), avatar_mask)

    font_bot = _load_font(38, bold=True)
    font_user = _load_font(30, bold=False)

    text_x = header_x + avatar_size + 22
    draw.text((text_x, header_y + 4), BOT_DISPLAY_NAME, fill=(255, 255, 255, 240), font=font_bot)
    draw.text(
        (text_x, header_y + 50),
        f"{_truncate(user_name, 24)} está ouvindo",
        fill=(220, 220, 220, 220),
        font=font_user,
    )

    badge_size = 88
    badge = Image.new("RGBA", (badge_size, badge_size), (0, 0, 0, 0))
    bd = ImageDraw.Draw(badge)
    bd.ellipse((0, 0, badge_size - 1, badge_size - 1), fill=(14, 14, 14, 235))
    note_font = _load_font(44, bold=True)
    label = "♫"
    bbox = bd.textbbox((0, 0), label, font=note_font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    bd.text(((badge_size - tw) / 2, (badge_size - th) / 2 - 5), label, font=note_font, fill=(255, 255, 255, 235))
    bg.alpha_composite(badge, (44, H - 128))

    fg_size = 760
    fg = ImageOps.fit(cover, (fg_size, fg_size), method=RESAMPLE_LANCZOS, centering=(0.5, 0.5))
    fg = _rounded_image(fg, 56)

    x = (W - fg_size) // 2
    y = 300

    _add_shadow(bg, (x - 12, y - 12, x + fg_size + 12, y + fg_size + 12), radius=64)
    border = Image.new("RGBA", (fg_size + 14, fg_size + 14), (0, 0, 0, 0))
    bd = ImageDraw.Draw(border)
    bd.rounded_rectangle((0, 0, border.size[0] - 1, border.size[1] - 1), radius=60, outline=(255, 255, 255, 90), width=4)
    bg.alpha_composite(border, (x - 7, y - 7))
    bg.alpha_composite(fg, (x, y))

    card_w = int(W * 0.90)
    card_h = 230
    card_x = (W - card_w) // 2
    card_y = H - card_h - 120

    card = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 185))
    mask = Image.new("L", (card_w, card_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, card_w, card_h), radius=34, fill=255)
    card.putalpha(mask)
    bg.alpha_composite(card, (card_x, card_y))

    draw = ImageDraw.Draw(bg)

    title = _truncate(track.get("title") or "Unknown", 60)
    artist = _truncate((track.get("artist") or {}).get("name") or "Unknown", 44)
    album = _truncate((track.get("album") or {}).get("title") or "", 44)
    duration = _format_duration(track.get("duration"))

    font_title = _load_font(42, bold=True)
    font_artist = _load_font(34, bold=False)
    font_meta = _load_font(28, bold=False)
    font_time = _load_font(26, bold=False)

    pad = 30
    left_x = card_x + pad
    top_y = card_y + pad

    draw.text((left_x, top_y), f"🎧 {title}", fill=(255, 255, 255, 245), font=font_title)
    draw.text((left_x, top_y + 66), f"🎤 {artist}", fill=(225, 225, 225, 235), font=font_artist)

    meta_parts = [part for part in [album, duration] if part]
    if meta_parts:
        meta_line = " • ".join(meta_parts)
        draw.text((left_x, top_y + 116), meta_line, fill=(190, 190, 190, 220), font=font_meta)

    time_str = datetime.now().strftime("%H:%M")
    time_bbox = draw.textbbox((0, 0), time_str, font=font_time)
    time_w = time_bbox[2] - time_bbox[0]
    draw.text(
        (card_x + card_w - pad - time_w, card_y + card_h - pad - 6),
        time_str,
        fill=(180, 180, 180, 220),
        font=font_time,
    )

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


async def story_fetch_bot_avatar(context: ContextTypes.DEFAULT_TYPE) -> Optional[bytes]:
    try:
        photos = await context.bot.get_user_profile_photos(context.bot.id, limit=1)
        if not photos.photos:
            return None

        file_id = photos.photos[0][-1].file_id
        file = await context.bot.get_file(file_id)

        bio = BytesIO()
        await file.download_to_memory(out=bio)
        return bio.getvalue()
    except Exception as e:
        logger.warning("Erro ao obter avatar do bot: %s", e)
        return None


def _get_story_lock(key: str) -> asyncio.Lock:
    lock = STORY_RENDER_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        STORY_RENDER_LOCKS[key] = lock
    return lock


async def story_fetch_track(query: str) -> Optional[Dict[str, Any]]:
    cleaned = re.sub(r"\s+", " ", (query or "").strip())
    if not cleaned:
        return None

    tracks = await deezer_search(cleaned)
    if tracks:
        track = tracks[0]
        if track and track.get("id"):
            remember_track(track)
            return track

    track = await asyncio.to_thread(_itunes_search_sync, cleaned)
    if track and track.get("id"):
        remember_track(track)
        return track

    track = await asyncio.to_thread(_musicbrainz_search_sync, cleaned)
    if track and track.get("id"):
        remember_track(track)
        return track

    return None


async def story_fetch_cover(track: Dict[str, Any], query: Optional[str] = None) -> Image.Image:
    album = track.get("album") or {}
    urls: List[str] = []

    for key in ("cover_xl", "cover_big", "cover_medium", "cover", "cover_small"):
        value = album.get(key)
        if isinstance(value, str) and value.strip():
            urls.append(value.strip())

    artwork_url = track.get("artwork_url")
    if isinstance(artwork_url, str) and artwork_url.strip() and artwork_url not in urls:
        urls.append(artwork_url.strip())

    release_id = _redis_text(track.get("release_id") or "").strip()
    if release_id:
        urls.append(f"https://coverartarchive.org/release/{release_id}/front-500")
        urls.append(f"https://coverartarchive.org/release/{release_id}/front")

    has_high_res = bool(album.get("cover_xl")) or track.get("source") in {"itunes", "musicbrainz"}
    if query and not has_high_res:
        fallback_track = await asyncio.to_thread(_itunes_search_sync, query)
        if not fallback_track:
            fallback_track = await asyncio.to_thread(_musicbrainz_search_sync, query)

        if fallback_track:
            fallback_album = fallback_track.get("album") or {}
            for key in ("cover_xl", "cover_big", "cover_medium", "cover", "cover_small"):
                value = fallback_album.get(key)
                if isinstance(value, str) and value.strip() and value.strip() not in urls:
                    urls.insert(0, value.strip())

            fallback_artwork = fallback_track.get("artwork_url")
            if isinstance(fallback_artwork, str) and fallback_artwork.strip() and fallback_artwork.strip() not in urls:
                urls.insert(0, fallback_artwork.strip())

            fallback_release_id = _redis_text(fallback_track.get("release_id") or "").strip()
            if fallback_release_id:
                urls.insert(0, f"https://coverartarchive.org/release/{fallback_release_id}/front-500")
                urls.insert(0, f"https://coverartarchive.org/release/{fallback_release_id}/front")

    for url in urls:
        try:
            img = _download_image(url)
            if img is not None:
                return img
        except Exception as e:
            logger.warning("story_fetch_cover falhou para %s: %s", url, e)

    return _make_placeholder_cover(track)


async def story_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not update.effective_chat or not update.effective_user:
        return

    prompt = await msg.reply_text(
        "🎵 Responda esta mensagem com o nome da música.",
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(selective=True),
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

    normalized_query = normalize_query(reply_text, 200)
    if not normalized_query:
        await msg.reply_text("🎵 Envie um nome de música válido.")
        return

    query_lock_key = f"query:{normalized_query.lower()}"
    async with _get_story_lock(query_lock_key):
        track = await story_fetch_track(normalized_query)
        if track is None or not track.get("id"):
            tracks_probe = await deezer_search(normalized_query)
            if tracks_probe is None:
                await msg.reply_text("⚠️ Erro ao acessar Deezer. Tente novamente.")
            else:
                await msg.reply_text("🔎 Música não encontrada.")
            return

        track_id = _safe_track_id(track.get("id"))
        cached = await asyncio.to_thread(story_cache_get, track_id)
        if cached:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
                await msg.reply_photo(photo=str(cached))
                return
            except Exception as e:
                logger.warning("Falha ao enviar cache story %s: %s", track_id, e)

        track_lock_key = f"track:{track_id}"
        async with _get_story_lock(track_lock_key):
            cached = await asyncio.to_thread(story_cache_get, track_id)
            if cached:
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
                    await msg.reply_photo(photo=str(cached))
                    return
                except Exception as e:
                    logger.warning("Falha ao enviar cache story %s: %s", track_id, e)

            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
                cover = await story_fetch_cover(track, query=normalized_query)
                bot_avatar_bytes = await story_fetch_bot_avatar(context)
                user_name = update.effective_user.first_name or "Usuário"

                image_bytes = await asyncio.to_thread(
                    story_render_image,
                    track,
                    cover,
                    user_name,
                    bot_avatar_bytes,
                )

                cached_path = await asyncio.to_thread(
                    story_cache_set,
                    track,
                    image_bytes,
                    cover_url=((track.get("album") or {}).get("cover_xl")
                               or (track.get("album") or {}).get("cover_big")
                               or (track.get("album") or {}).get("cover_medium")
                               or (track.get("album") or {}).get("cover_small")
                               or track.get("artwork_url")
                               or ""),
                    user_name=user_name,
                    bot_avatar_present=bool(bot_avatar_bytes),
                )
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
        key_type = _redis_text(redis_client.type(key))
        ttl = redis_client.ttl(key)

        if key_type == "string":
            value = redis_client.get(key)
        elif key_type == "hash":
            value = redis_client.hgetall(key)
        elif key_type == "zset":
            value = redis_client.zrange(key, 0, -1, withscores=True)
        elif key_type == "set":
            value = sorted(list(redis_client.smembers(key)))
        elif key_type == "list":
            value = redis_client.lrange(key, 0, -1)
        else:
            value = None

        return {
            "type": key_type,
            "ttl": ttl,
            "value": _redis_jsonable(value),
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
            dump["keys"][_redis_text(key)] = _redis_jsonable(_serialize_redis_key(key))

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
        top_global = _redis_jsonable(redis_client.zrevrange("top:tracks", 0, 99, withscores=True))
        exported_users: Dict[str, Any] = {}

        for key in redis_client.scan_iter("top:user:*"):
            try:
                user_id = _redis_text(key).split("top:user:", 1)[1]
                exported_users[user_id] = _redis_jsonable(redis_client.zrevrange(key, 0, 99, withscores=True))
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

    if tracks is None:
        await update.message.reply_text("⚠️ Erro ao acessar Deezer. Tente novamente.")
        return

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

    if tracks is None:
        await update.message.reply_text("⚠️ Erro ao acessar Deezer. Tente novamente.")
        return

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
    if tracks is None:
        return

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
    entries = redis_client.zrevrange(
        f"top:user:{user_id}",
        0,
        9,
        withscores=True
    )

    if not entries:
        await update.message.reply_text("🎧 Você ainda não ouviu músicas.")
        return

    metas = await asyncio.gather(*(fetch_track_meta(_redis_text(track_id)) for track_id, _ in entries))

    lines = [
        f"📊 <b>Músicas mais ouvidas de {esc(user_first_name or 'Usuário')} no {BOT_DISPLAY_NAME}</b>",
        ""
    ]

    for i, ((track_id, score), meta) in enumerate(zip(entries, metas), 1):
        track_id_str = _redis_text(track_id)
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

    entries = redis_client.zrevrange(
        "top:tracks",
        0,
        9,
        withscores=True
    )

    if not entries:
        await update.message.reply_text("🎧 Ainda não há plays registrados.")
        return

    metas = await asyncio.gather(*(fetch_track_meta(_redis_text(track_id)) for track_id, _ in entries))

    lines = [
        f"📈 <b>Top global do {BOT_DISPLAY_NAME}</b>",
        ""
    ]

    for i, ((track_id, score), meta) in enumerate(zip(entries, metas), 1):
        track_id_str = _redis_text(track_id)
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
