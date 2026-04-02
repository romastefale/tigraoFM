import os
import re
import html
import json
import logging
import asyncio
import time
import io
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple

import requests
import redis

from PIL import Image, ImageFilter, ImageOps

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.constants import ParseMode
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

# =========================
# ESTADO DO GRUPO
# =========================

PENDING_REPLIES: Dict[Tuple[int, int], float] = {}
PENDING_ACTIONS: Dict[Tuple[int, int], str] = {}
REPLY_TIMEOUT = 900  # 15 minutos

STORY_SIZE = (1080, 1920)
STORY_FRONT_RATIO = 0.70
STORY_BLUR_RADIUS = 34
STORY_MIN_COVER_SIZE = 600
STORY_TARGET_COVER_SIZE = 1400

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
# SANITIZE / TRADUÇÃO / RANKING
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

def normalize_text_basic(text: Any) -> str:
    if text is None:
        return ""
    text = str(text).strip()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text




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

def tokenize(text: Any) -> List[str]:
    text_norm = normalize_text_basic(text)
    if not text_norm:
        return []
    return [tok for tok in re.split(r"[^a-z0-9]+", text_norm) if tok]




def _track_search_text(track: Dict[str, Any]) -> str:
    title = str(track.get("title") or "")
    artist = str((track.get("artist") or {}).get("name") or "")
    album = str((track.get("album") or {}).get("title") or "")
    stored_meta = str(track.get("_meta_search_text") or "")

    combined = " ".join(part for part in [stored_meta, title, artist, album] if part).strip()
    return normalize_text_basic(combined)


def score_track_match(query: str, track: Dict[str, Any]) -> float:
    q_text = normalize_query(query)
    q_norm = normalize_text_basic(q_text)
    q_tokens = tokenize(q_text)

    title = normalize_text_basic(track.get("title") or "")
    artist = normalize_text_basic((track.get("artist") or {}).get("name") or "")
    album = normalize_text_basic((track.get("album") or {}).get("title") or "")
    meta = _track_search_text(track)

    score = 0.0

    if not q_norm:
        return 0.0

    if q_norm == title:
        score += 12.0
    if q_norm == artist:
        score += 4.0
    if q_norm == album:
        score += 2.0
    if q_norm == meta and meta:
        score += 8.0

    if title.startswith(q_norm):
        score += 2.0
    if artist.startswith(q_norm):
        score += 1.0
    if album.startswith(q_norm):
        score += 0.5

    if q_norm in title:
        score += 7.5
    if q_norm in artist:
        score += 4.5
    if q_norm in album:
        score += 1.2
    if meta and q_norm in meta:
        score += 5.0

    if q_tokens:
        title_hits = sum(1 for tok in q_tokens if tok in title)
        artist_hits = sum(1 for tok in q_tokens if tok in artist)
        album_hits = sum(1 for tok in q_tokens if tok in album)
        meta_hits = sum(1 for tok in q_tokens if tok in meta)

        score += title_hits * 1.8
        score += artist_hits * 1.1
        score += album_hits * 0.5
        score += meta_hits * 0.4

        if title_hits == len(q_tokens):
            score += 2.5
        if artist_hits == len(q_tokens):
            score += 1.0
        if album_hits == len(q_tokens):
            score += 0.7

        if all((tok in title or tok in artist or tok in album or tok in meta) for tok in q_tokens):
            score += 2.0

        joined = " ".join(q_tokens)
        if joined and joined in meta:
            score += 1.5

    track_id = normalize_text_basic(track.get("id") or "")
    if track_id and q_norm == track_id:
        score += 10.0
    elif track_id and q_norm and q_norm in track_id:
        score += 1.0

    return score


def rank_tracks(query: str, tracks: List[Dict[str, Any]]) -> List[Tuple[float, Dict[str, Any]]]:
    ranked: List[Tuple[float, Dict[str, Any]]] = []
    seen_ids = set()

    for t in tracks or []:
        try:
            track_id = str(t.get("id") or "")
            if track_id and track_id in seen_ids:
                continue
            if track_id:
                seen_ids.add(track_id)

            score = score_track_match(query, t)
            ranked.append((score, t))
        except Exception as e:
            logger.warning("Falha ao pontuar track: %s", e)

    ranked.sort(
        key=lambda item: (
            item[0],
            normalize_text_basic(item[1].get("title") or ""),
            normalize_text_basic((item[1].get("artist") or {}).get("name") or ""),
        ),
        reverse=True,
    )
    return ranked




def select_best_track(query: str, tracks: List[Dict[str, Any]], min_score: float = 0.0) -> Optional[Dict[str, Any]]:
    ranked = rank_tracks(query, tracks)
    if not ranked:
        return None

    best_score, best_track = ranked[0]
    if best_score < min_score:
        return None

    best_track["_match_score"] = best_score
    return best_track

def _resample_lanczos():
    return getattr(Image, "Resampling", Image).LANCZOS

def _unique_nonempty(values: List[str]) -> List[str]:
    seen = set()
    out = []
    for value in values:
        value = (value or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out

def _promote_deezer_cover_url(url: str, size: int) -> str:
    if not url:
        return url

    promoted = re.sub(r"/\d+x\d+-", f"/{size}x{size}-", url, count=1)
    promoted = re.sub(r"size=(small|medium|big|xl)", f"size={size}x{size}", promoted, flags=re.I)
    return promoted

def _cover_candidates(track: Dict[str, Any]) -> List[str]:
    album = track.get("album") or {}
    urls = [
        album.get("cover_xl"),
        album.get("cover_big"),
        album.get("cover_medium"),
        album.get("cover_small"),
        album.get("cover"),
    ]

    candidates: List[str] = []
    for url in _unique_nonempty([str(u) for u in urls if u]):
        candidates.append(url)
        candidates.append(_promote_deezer_cover_url(url, STORY_TARGET_COVER_SIZE))
        candidates.append(_promote_deezer_cover_url(url, 1000))
        candidates.append(_promote_deezer_cover_url(url, STORY_MIN_COVER_SIZE))

    return _unique_nonempty(candidates)

def _download_image_bytes(url: str) -> Optional[bytes]:
    try:
        r = session.get(url, timeout=8)
        r.raise_for_status()
        content_type = r.headers.get("content-type", "").lower()
        if content_type and not content_type.startswith("image/"):
            return None
        if not r.content:
            return None
        return r.content
    except Exception as e:
        logger.warning("Falha ao baixar capa %s: %s", url, e)
        return None

def _open_image_from_bytes(data: bytes) -> Optional[Image.Image]:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception:
        return None

def _best_cover_image(track: Dict[str, Any]) -> Optional[Image.Image]:
    best_img = None
    best_score = -1
    for url in _cover_candidates(track):
        data = _download_image_bytes(url)
        if not data:
            continue
        img = _open_image_from_bytes(data)
        if img is None:
            continue

        score = min(img.size)
        if score > best_score:
            best_score = score
            best_img = img.copy()

        if score >= STORY_MIN_COVER_SIZE:
            return img.copy()

    return best_img

def _render_story_image(track: Dict[str, Any]) -> Optional[bytes]:
    cover = _best_cover_image(track)
    if cover is None:
        return None

    try:
        cover = cover.convert("RGB")
        bg = ImageOps.fit(cover, STORY_SIZE, method=_resample_lanczos())
        bg = bg.filter(ImageFilter.GaussianBlur(radius=STORY_BLUR_RADIUS))

        front_w = int(STORY_SIZE[0] * STORY_FRONT_RATIO)
        front = ImageOps.contain(cover, (front_w, front_w), method=_resample_lanczos())

        canvas = bg.copy()
        x = (STORY_SIZE[0] - front.size[0]) // 2
        y = (STORY_SIZE[1] - front.size[1]) // 2
        canvas.paste(front, (x, y))

        buffer = io.BytesIO()
        canvas.save(buffer, format="JPEG", quality=95, optimize=True, progressive=True)
        buffer.seek(0)
        return buffer.getvalue()
    except Exception as e:
        logger.warning("Falha ao gerar story: %s", e)
        return None

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
    album = track.get("album") or {}
    title = str(track.get("title") or "Unknown")
    artist = str((track.get("artist") or {}).get("name") or "Unknown")
    album_title = str(album.get("title") or "")
    meta_search_text = " ".join(part for part in [title, artist, album_title] if part).strip()

    return {
        "title": title,
        "artist": artist,
        "cover": str(album.get("cover") or ""),
        "cover_small": str(album.get("cover_small") or ""),
        "cover_medium": str(album.get("cover_medium") or ""),
        "cover_big": str(album.get("cover_big") or ""),
        "cover_xl": str(album.get("cover_xl") or ""),
        "title_norm": normalize_text_basic(title),
        "artist_norm": normalize_text_basic(artist),
        "meta_search_text": normalize_text_basic(meta_search_text),
        "source": str(track.get("source") or "deezer"),
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
        "cover": "",
        "cover_small": "",
        "cover_medium": "",
        "cover_big": "",
        "cover_xl": "",
        "title_norm": "",
        "artist_norm": "",
        "meta_search_text": "",
        "source": "unknown",
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
            "cover": meta.get("cover", ""),
            "cover_small": meta.get("cover_small", ""),
            "cover_medium": meta.get("cover_medium", ""),
            "cover_big": meta.get("cover_big", ""),
            "cover_xl": meta.get("cover_xl", ""),
        }
    }


async def deezer_search(query: str):
    query = normalize_query(query)
    if not query:
        return []

    try:
        r = await asyncio.to_thread(
            session.get,
            "https://api.deezer.com/search",
            params={"q": query, "limit": 10},
            timeout=6,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("data", [])
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
# START / HELP
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"🎶 <b>{BOT_DISPLAY_NAME}</b>\n"
        f"🎧 Digite o nome de uma música ou use <code>{BOT_USERNAME} nome</code>\n\n"
        f"📌 Comandos:\n"
        f"/charts — suas músicas mais ouvidas\n"
        f"/top — ranking global\n"
        f"/play — enviar uma música pelo grupo\n"
        f"/story — gerar story da música"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# =========================
# BUSCA NORMAL
# =========================

async def show_search_results(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str, mode: str = "play"):
    query = (query or "").strip()
    if not query:
        await update.message.reply_text("🎤 Digite o nome de uma música.")
        return

    tracks = await deezer_search(query)

    if not tracks:
        await update.message.reply_text("🔎 Nada encontrado.")
        return

    ranked = rank_tracks(query, tracks)
    keyboard = []

    for score, t in ranked[:5]:
        try:
            track_id = str(t["id"])
            remember_track(t)

            title = sanitize(t.get("title"))
            artist = sanitize((t.get("artist") or {}).get("name"))

            keyboard.append([
                InlineKeyboardButton(
                    f"🎵 {title} — {artist}",
                    callback_data=f"{mode}:{track_id}"
                )
            ])
        except Exception as e:
            logger.warning("Erro montando botão: %s", e)

    if not keyboard:
        await update.message.reply_text("🔎 Nada encontrado.")
        return

    await update.message.reply_text(
        "🎧 Escolha:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def search_music(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str = "play"):
    query = (update.message.text or "").strip()
    await show_search_results(update, context, query, mode=mode)

# =========================
# NOVO: GRUPO EXCLUSIVO E INTELIGENTE
# =========================

def cleanup_pending(now: float) -> None:
    expired = [k for k, ts in PENDING_REPLIES.items() if now - ts > REPLY_TIMEOUT]
    for k in expired:
        PENDING_REPLIES.pop(k, None)
        PENDING_ACTIONS.pop(k, None)

def set_pending_action(chat_id: int, user_id: int, mode: str) -> None:
    key = (chat_id, user_id)
    PENDING_REPLIES[key] = time.time()
    PENDING_ACTIONS[key] = mode

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

    is_command = text.startswith("/play") or text.startswith("/story")
    is_mention = BOT_USERNAME.lower() in text.lower()

    is_reply_to_bot = (
        msg.reply_to_message
        and msg.reply_to_message.from_user
        and msg.reply_to_message.from_user.id == context.bot.id
    )

    if is_command or is_mention:
        set_pending_action(chat.id, user.id, "story" if text.startswith("/story") else "play")
        await msg.reply_text(
            "🎧 Responda aqui o nome de uma música ou use "
            f"{BOT_USERNAME} para pesquisar <i>inline</i>",
            parse_mode=ParseMode.HTML
        )
        return

    if is_reply_to_bot and key in PENDING_REPLIES:
        if now - PENDING_REPLIES[key] > REPLY_TIMEOUT:
            mode = PENDING_ACTIONS.pop(key, "play")
            PENDING_REPLIES.pop(key, None)
            await msg.reply_text(f"⏱️ Tempo expirado. Use /{mode} novamente.")
            return

        mode = PENDING_ACTIONS.pop(key, "play")
        PENDING_REPLIES.pop(key, None)
        await search_music(update, context, mode=mode)
        return

    return

async def _direct_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user
    now = time.time()

    query = " ".join(context.args).strip()

    if msg.reply_to_message:
        target_msg = msg.reply_to_message
        query_text = (target_msg.text or target_msg.caption or "").strip()

        is_own_bot = (
            (target_msg.from_user and target_msg.from_user.id == context.bot.id) or
            (target_msg.via_bot and target_msg.via_bot.id == context.bot.id)
        )

        if is_own_bot and "🎧" in query_text and "🎤" in query_text:
            title, artist = "", ""
            for line in query_text.split('\n'):
                if "🎧" in line:
                    title = line.replace("🎧", "").strip()
                elif "🎤" in line:
                    artist = line.replace("🎤", "").strip()
            
            if title or artist:
                query = f"{title} {artist}".strip()
        
        elif query_text and not query:
            query = query_text

    if query:
        await show_search_results(update, context, query, mode=mode)
        return

    if chat.type in ["group", "supergroup"]:
        cleanup_pending(now)
        set_pending_action(chat.id, user.id, mode)

        await update.message.reply_text(
            "🎧 Responda aqui o nome de uma música ou use "
            f"{BOT_USERNAME} para pesquisar <i>inline</i>",
            parse_mode=ParseMode.HTML
        )
        return

    await update.message.reply_text("🎤 Digite o nome de uma música.")

async def play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _direct_search_command(update, context, mode="play")

async def story(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _direct_search_command(update, context, mode="story")

# =========================
# CLICK DO CHAT
# =========================

async def click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()

    try:
        action, track_id = cb.data.split(":", 1)
    except Exception:
        await cb.answer("⚠️ Ação inválida.", show_alert=True)
        return

    if action not in {"play", "story"}:
        action = "play"

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

    cover_urls = _cover_candidates(t)
    photo = cover_urls[0] if cover_urls else (t.get("album") or {}).get("cover_big")

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

        if action == "story":
            msg_status = await cb.message.reply_text("⏳ <i>Gerando imagem do Story, aguarde...</i>", parse_mode=ParseMode.HTML)
            
            story_bytes = await asyncio.to_thread(_render_story_image, t)
            
            await msg_status.delete()
            
            if story_bytes:
                await cb.message.reply_photo(
                    photo=story_bytes,
                )
            else:
                await cb.message.reply_text("⚠️ Não foi possível gerar o story.")
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

    if not tracks:
        return

    results = []
    ranked = rank_tracks(query, tracks)

    for score, t in ranked[:10]:
        try:
            track_id = str(t["id"])
            title = sanitize(t.get("title"))
            artist = sanitize((t.get("artist") or {}).get("name"))
            album_name = sanitize((t.get("album") or {}).get("title") or "Desconhecido")
            cover_urls = _cover_candidates(t)
            cover_big = cover_urls[0] if cover_urls else (t.get("album") or {}).get("cover_big")
            cover_small = (t.get("album") or {}).get("cover_small") or cover_big

            if not cover_big:
                continue

            remember_track(t)
            current_count = get_play_count(user.id, track_id)

            caption = build_caption(
                title=title,
                artist=artist,
                plays=current_count,
                user_first_name=user.first_name,
            )

            results.append(
                InlineQueryResultArticle(
                    id=f"track:{track_id}",
                    title=f"🎵 {title}",
                    description=f"{artist} — {album_name}",
                    thumbnail_url=cover_small or cover_big,
                    input_message_content=InputTextMessageContent(
                        message_text=f"<a href='{cover_big}'>&#8203;</a>\n{caption}",
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=False
                    )
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
                    "cover": meta.get("cover", ""),
                    "cover_small": meta.get("cover_small", ""),
                    "cover_medium": meta.get("cover_medium", ""),
                    "cover_big": meta.get("cover_big", ""),
                    "cover_xl": meta.get("cover_xl", ""),
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
    app.add_handler(CommandHandler("play", play))
    app.add_handler(CommandHandler("story", story))
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
