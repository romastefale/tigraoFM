import os
import re
import html
import json
import logging
import asyncio
import time
import io
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple

import requests
import redis
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageEnhance

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQueryResultPhoto,
    ForceReply,
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

session = requests.Session()
session.headers.update(
    {
        "User-Agent": f"{BOT_DISPLAY_NAME}/1.0 (Telegram bot)",
        "Accept": "application/json,text/plain,*/*",
    }
)

# =========================
# ESTADOS ISOLADOS
# =========================

# /play existente
PENDING_REPLIES: Dict[Tuple[int, int], Dict[str, Any]] = {}
REPLY_TIMEOUT = 900  # 15 minutos

# /story isolado
STORY_PENDING: Dict[Tuple[int, int], Dict[str, Any]] = {}
STORY_TIMEOUT = 900  # 15 minutos
STORY_CACHE_DIR = Path(BACKUP_PATH) / "story_cache"
STORY_CACHE_TTL = 60 * 60 * 24 * 30  # 30 dias

# Locks para evitar render duplicado por track
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
            decode_responses=True,
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

def normalize_query(text: Any, limit: int = 120) -> str:
    if text is None:
        return ""
    value = re.sub(r"\s+", " ", str(text)).strip()
    return value[:limit]

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
    if redis_client:
        try:
            meta = redis_client.hgetall(f"trackmeta:{track_id}")
            if meta and meta.get("title"):
                return meta
        except Exception:
            pass

    track = await deezer_track(track_id)
    if track and track.get("id"):
        remember_track(track)
        if redis_client:
            try:
                meta = redis_client.hgetall(f"trackmeta:{track_id}")
                if meta and meta.get("title"):
                    return meta
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
# BACKUP / STATS EXPORT
# =========================

def _serialize_redis_key(key: str) -> Dict[str, Any]:
    assert redis_client is not None

    try:
        key_type = redis_client.type(key)
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
            dump["keys"][key] = _serialize_redis_key(key)

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
        top_global = redis_client.zrevrange("top:tracks", 0, 99, withscores=True)
        exported_users: Dict[str, Any] = {}

        for key in redis_client.scan_iter("top:user:*"):
            try:
                user_id = key.split("top:user:", 1)[1]
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
# STORY - CACHE / RESILIÊNCIA / RENDER
# =========================

def _story_key(chat_id: int, user_id: int) -> Tuple[int, int]:
    return (chat_id, user_id)

def _cleanup_dict_state(state: Dict[Tuple[int, int], Dict[str, Any]], timeout: int, now: Optional[float] = None) -> None:
    now = now or time.time()
    expired = []
    for key, data in state.items():
        ts = float(data.get("ts", 0))
        if now - ts > timeout:
            expired.append(key)
    for key in expired:
        state.pop(key, None)

def cleanup_story_pending(now: Optional[float] = None) -> None:
    _cleanup_dict_state(STORY_PENDING, STORY_TIMEOUT, now)

def cleanup_pending(now: Optional[float] = None) -> None:
    _cleanup_dict_state(PENDING_REPLIES, REPLY_TIMEOUT, now)

def _story_cache_path(track_id: str) -> Path:
    safe_track_id = re.sub(r"[^0-9A-Za-z_.-]", "_", str(track_id))
    return STORY_CACHE_DIR / f"story_{safe_track_id}.png"

def _story_cache_redis_key(track_id: str) -> str:
    return f"storycache:{track_id}"

async def story_cache_get(track_id: str) -> Optional[bytes]:
    path = _story_cache_path(track_id)

    if redis_client:
        try:
            cached = redis_client.hgetall(_story_cache_redis_key(track_id))
            cached_path = cached.get("path") if cached else None
            if cached_path:
                cached_file = Path(cached_path)
                if cached_file.exists():
                    return await asyncio.to_thread(cached_file.read_bytes)
        except Exception as e:
            logger.warning("story_cache_get redis falhou (%s): %s", track_id, e)

    if path.exists():
        try:
            data = await asyncio.to_thread(path.read_bytes)
            if redis_client:
                try:
                    redis_client.hset(
                        _story_cache_redis_key(track_id),
                        mapping={
                            "path": str(path),
                            "created_at": datetime.utcnow().isoformat() + "Z",
                        },
                    )
                    redis_client.expire(_story_cache_redis_key(track_id), STORY_CACHE_TTL)
                except Exception:
                    pass
            return data
        except Exception as e:
            logger.warning("story_cache_get disco falhou (%s): %s", track_id, e)

    return None

async def story_cache_set(track_id: str, image_bytes: bytes, track: Optional[Dict[str, Any]] = None) -> None:
    path = _story_cache_path(track_id)
    try:
        STORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, image_bytes)

        if redis_client:
            try:
                payload = {
                    "path": str(path),
                    "created_at": datetime.utcnow().isoformat() + "Z",
                    "size": str(len(image_bytes)),
                }
                if track:
                    payload["title"] = str(track.get("title") or "")
                    payload["artist"] = str((track.get("artist") or {}).get("name") or "")
                redis_client.hset(_story_cache_redis_key(track_id), mapping=payload)
                redis_client.expire(_story_cache_redis_key(track_id), STORY_CACHE_TTL)
            except Exception as e:
                logger.warning("story_cache_set redis falhou (%s): %s", track_id, e)
    except Exception as e:
        logger.warning("story_cache_set disco falhou (%s): %s", track_id, e)

def _normalize_string(value: Any) -> str:
    value = normalize_query(value, 200)
    return value

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

def _upgrade_artwork_url(url: str, size: int) -> str:
    if not url:
        return ""
    if "100x100" in url:
        return url.replace("100x100bb", f"{size}x{size}bb").replace("100x100", f"{size}x{size}")
    if "60x60" in url:
        return url.replace("60x60bb", f"{size}x{size}bb").replace("60x60", f"{size}x{size}")
    return url

async def story_fetch_track(query: str) -> Optional[Dict[str, Any]]:
    normalized = _normalize_string(query)
    if not normalized:
        return None

    track = await asyncio.to_thread(_itunes_search_sync, normalized)
    if track and track.get("id"):
        return track

    track = await asyncio.to_thread(_musicbrainz_search_sync, normalized)
    if track and track.get("id"):
        return track

    return None

def _placeholder_cover(title: str, artist: str) -> bytes:
    canvas = Image.new("RGBA", (1000, 1000), (18, 18, 24, 255))
    draw = ImageDraw.Draw(canvas)

    # Fundo simples com gradiente horizontal e algumas manchas
    for x in range(1000):
        alpha = int(20 + (x / 1000) * 40)
        draw.line((x, 0, x, 1000), fill=(30, 30, 40, alpha))

    font_big = ImageFont.load_default()
    font_small = ImageFont.load_default()

    label_title = _normalize_string(title) or "Unknown"
    label_artist = _normalize_string(artist) or "Unknown"

    draw.rounded_rectangle((80, 80, 920, 920), radius=48, outline=(255, 255, 255, 60), width=4)

    # Texto centralizado sem depender de fontes externas
    bbox_title = draw.textbbox((0, 0), label_title, font=font_big)
    w_title = bbox_title[2] - bbox_title[0]
    h_title = bbox_title[3] - bbox_title[1]

    bbox_artist = draw.textbbox((0, 0), label_artist, font=font_small)
    w_artist = bbox_artist[2] - bbox_artist[0]
    h_artist = bbox_artist[3] - bbox_artist[1]

    draw.text(((1000 - w_title) / 2, 470 - h_title), label_title, font=font_big, fill=(235, 235, 235, 255))
    draw.text(((1000 - w_artist) / 2, 530), label_artist, font=font_small, fill=(210, 210, 210, 220))

    bio = io.BytesIO()
    canvas.convert("RGB").save(bio, format="JPEG", quality=92)
    return bio.getvalue()

async def story_fetch_cover(track: Dict[str, Any]) -> bytes:
    title = str(track.get("title") or "Unknown")
    artist = str((track.get("artist") or {}).get("name") or "Unknown")

    urls: List[str] = []
    artwork = str(track.get("artwork_url") or "")
    if artwork:
        urls.append(artwork)

    album = track.get("album") or {}
    for key in ("cover_big", "cover_small"):
        value = str(album.get(key) or "")
        if value and value not in urls:
            urls.append(value)

    release_id = str(track.get("release_id") or "")
    if release_id:
        urls.append(f"https://coverartarchive.org/release/{release_id}/front-500")
        urls.append(f"https://coverartarchive.org/release/{release_id}/front")

    for url in urls:
        try:
            if not url:
                continue
            response = await asyncio.to_thread(session.get, url, timeout=10, stream=False)
            response.raise_for_status()
            content = response.content
            if content and len(content) > 1024:
                return content
        except Exception as e:
            logger.warning("story_fetch_cover falhou (%s): %s", url, e)

    return _placeholder_cover(title, artist)

def _rounded_mask(size: Tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask

def _render_story_image_sync(cover_bytes: bytes) -> bytes:
    canvas_size = (1080, 1920)
    fg_size = 820
    fg_top = 540
    fg_left = (canvas_size[0] - fg_size) // 2

    try:
        base = Image.open(io.BytesIO(cover_bytes)).convert("RGBA")
    except Exception:
        base = Image.open(io.BytesIO(_placeholder_cover("Unknown", "Unknown"))).convert("RGBA")

    background = ImageOps.fit(base, canvas_size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    background = background.filter(ImageFilter.GaussianBlur(radius=24))
    background = ImageEnhance.Color(background).enhance(0.92)

    overlay = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle((0, 0, canvas_size[0], canvas_size[1]), fill=(0, 0, 0, 110))
    background = Image.alpha_composite(background, overlay)

    foreground = ImageOps.fit(base, (fg_size, fg_size), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))

    # Sombra suave
    shadow_layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    shadow = Image.new("RGBA", (fg_size + 36, fg_size + 36), (0, 0, 0, 160))
    shadow_mask = _rounded_mask((fg_size + 36, fg_size + 36), radius=54)
    shadow.putalpha(shadow_mask)
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=18))
    shadow_layer.alpha_composite(shadow, (fg_left - 18, fg_top - 8))

    # Card principal
    card = Image.new("RGBA", (fg_size, fg_size), (0, 0, 0, 0))
    mask = _rounded_mask((fg_size, fg_size), radius=48)
    card.paste(foreground, (0, 0))
    card.putalpha(mask)

    # Borda discreta
    border = Image.new("RGBA", (fg_size, fg_size), (0, 0, 0, 0))
    border_draw = ImageDraw.Draw(border)
    border_draw.rounded_rectangle(
        (4, 4, fg_size - 5, fg_size - 5),
        radius=44,
        outline=(255, 255, 255, 55),
        width=4,
    )

    final = background.copy()
    final = Image.alpha_composite(final, shadow_layer)
    final.alpha_composite(card, (fg_left, fg_top))
    final.alpha_composite(border, (fg_left, fg_top))

    # Ajuste final de contraste mínimo
    final = ImageEnhance.Contrast(final).enhance(1.02)

    bio = io.BytesIO()
    final.convert("RGB").save(bio, format="JPEG", quality=92, optimize=True)
    return bio.getvalue()

async def story_render_image(cover_bytes: bytes) -> bytes:
    return await asyncio.to_thread(_render_story_image_sync, cover_bytes)

def _get_story_lock(track_id: str) -> asyncio.Lock:
    lock = STORY_RENDER_LOCKS.get(track_id)
    if lock is None:
        lock = asyncio.Lock()
        STORY_RENDER_LOCKS[track_id] = lock
    return lock

# =========================
# /STORY
# =========================

async def story_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    now = time.time()
    cleanup_story_pending(now)
    key = _story_key(chat.id, user.id)

    prompt = await msg.reply_text(
        "🎬 Responda esta mensagem com o nome da música para gerar o story.",
        reply_markup=ForceReply(selective=True),
    )

    STORY_PENDING[key] = {
        "ts": now,
        "prompt_id": prompt.message_id,
        "chat_id": chat.id,
        "user_id": user.id,
    }

def _is_pending_story_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return False

    key = _story_key(chat.id, user.id)
    pending = STORY_PENDING.get(key)
    if not pending:
        return False

    now = time.time()
    if now - float(pending.get("ts", 0)) > STORY_TIMEOUT:
        STORY_PENDING.pop(key, None)
        return False

    reply_to = msg.reply_to_message
    if not reply_to or not reply_to.from_user:
        return False

    if reply_to.from_user.id != context.bot.id:
        return False

    if int(reply_to.message_id) != int(pending.get("prompt_id", -1)):
        return False

    return True

async def story_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user

    if not msg or not chat or not user:
        return False

    key = _story_key(chat.id, user.id)
    pending = STORY_PENDING.get(key)
    if not pending:
        return False

    now = time.time()
    if now - float(pending.get("ts", 0)) > STORY_TIMEOUT:
        STORY_PENDING.pop(key, None)
        await msg.reply_text("⏱️ Tempo expirado. Use /story novamente.")
        return True

    if not _is_pending_story_reply(update, context):
        await msg.reply_text("Responda diretamente à mensagem do /story com o nome da música.")
        return True

    query = normalize_query(msg.text)
    if not query:
        STORY_PENDING.pop(key, None)
        await msg.reply_text("🔎 Nome da música vazio.")
        return True

    lock = _get_story_lock("query:" + query.lower())
    async with lock:
        try:
            cached_bytes = None
            track = await story_fetch_track(query)
            if not track or not track.get("id"):
                STORY_PENDING.pop(key, None)
                await msg.reply_text("🔎 Não encontrei essa música.")
                return True

            track_id = str(track.get("id"))
            cached_bytes = await story_cache_get(track_id)
            if cached_bytes is None:
                cover_bytes = await story_fetch_cover(track)
                if not cover_bytes:
                    STORY_PENDING.pop(key, None)
                    await msg.reply_text("⚠️ Não foi possível obter a capa da música.")
                    return True

                await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.UPLOAD_PHOTO)
                image_bytes = await story_render_image(cover_bytes)
                if not image_bytes:
                    STORY_PENDING.pop(key, None)
                    await msg.reply_text("⚠️ Não foi possível gerar a imagem do story.")
                    return True

                await story_cache_set(track_id, image_bytes, track)
                cached_bytes = image_bytes

            photo = io.BytesIO(cached_bytes)
            photo.name = f"story_{track_id}.jpg"
            await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.UPLOAD_PHOTO)
            await msg.reply_photo(photo=photo)

            STORY_PENDING.pop(key, None)
            return True
        except Exception as e:
            logger.warning("story_reply_handler falhou: %s", e)
            STORY_PENDING.pop(key, None)
            await msg.reply_text("⚠️ Falha ao gerar o story.")
            return True

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
        f"/story — gerar story em 1080x1920"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# =========================
# BUSCA NORMAL
# =========================

async def search_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await story_reply_handler(update, context):
        return

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

async def group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ["group", "supergroup"]:
        return

    if await story_reply_handler(update, context):
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
        prompt = await msg.reply_text(
            "🎧Responda aqui o nome de uma música ou use "
            f"{BOT_USERNAME} para pesquisar <i>inline</i>",
            parse_mode=ParseMode.HTML
        )
        PENDING_REPLIES[key] = {
            "ts": now,
            "prompt_id": prompt.message_id,
            "chat_id": chat.id,
            "user_id": user.id,
            "mode": "play",
        }
        return

    pending = PENDING_REPLIES.get(key)
    if is_reply_to_bot and pending and pending.get("mode") == "play":
        if now - float(pending.get("ts", 0)) > REPLY_TIMEOUT:
            PENDING_REPLIES.pop(key, None)
            await msg.reply_text("⏱️ Tempo expirado. Use /play novamente.")
            return

        if int(msg.reply_to_message.message_id) != int(pending.get("prompt_id", -1)):
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
        prompt = await update.message.reply_text(
            "🎧Responda aqui o nome de uma música ou use "
            f"{BOT_USERNAME} para pesquisar <i>inline</i>",
            parse_mode=ParseMode.HTML
        )
        PENDING_REPLIES[key] = {
            "ts": now,
            "prompt_id": prompt.message_id,
            "chat_id": chat.id,
            "user_id": user.id,
            "mode": "play",
        }
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
    entries: List[Tuple[str, float]] = redis_client.zrevrange(
        f"top:user:{user_id}",
        0,
        9,
        withscores=True
    )

    if not entries:
        await update.message.reply_text("🎧 Você ainda não ouviu músicas.")
        return

    metas = await asyncio.gather(*(fetch_track_meta(track_id) for track_id, _ in entries))

    lines = [
        f"📊 <b>Músicas mais ouvidas de {esc(user_first_name or 'Usuário')} no {BOT_DISPLAY_NAME}</b>",
        ""
    ]

    for i, ((track_id, score), meta) in enumerate(zip(entries, metas), 1):
        title = sanitize(meta.get("title") or f"Track {track_id}")
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

    entries: List[Tuple[str, float]] = redis_client.zrevrange(
        "top:tracks",
        0,
        9,
        withscores=True
    )

    if not entries:
        await update.message.reply_text("🎧 Ainda não há plays registrados.")
        return

    metas = await asyncio.gather(*(fetch_track_meta(track_id) for track_id, _ in entries))

    lines = [
        f"📈 <b>Top global do {BOT_DISPLAY_NAME}</b>",
        ""
    ]

    for i, ((track_id, score), meta) in enumerate(zip(entries, metas), 1):
        title = sanitize(meta.get("title") or f"Track {track_id}")
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
    app.add_handler(CommandHandler("story", story_command))
    app.add_handler(CommandHandler("play", play))
    app.add_handler(CommandHandler("charts", stats))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("log", log_cmd))

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