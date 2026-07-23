import asyncio
import copy
import html
import os
import json
import re
import sys
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    sys.exit(
        "BOT_TOKEN не задан. Укажи токен в .env (BOT_TOKEN=...) "
        "или в переменной окружения перед запуском."
    )

# Все файлы с данными живут отдельно от кода, чтобы их можно было
# примонтировать томом, не перекрывая сам bot.py
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

STATE_FILE = os.path.join(DATA_DIR, "state.json")
# Журнал исходящих сообщений для /cleanup — отдельно от основного состояния,
# чтобы частые записи не дергали state.json
BOT_MESSAGES_FILE = os.path.join(DATA_DIR, "bot_messages.json")

# Файлы старого плоского формата — переносятся в state.json при первом запуске
LEGACY_STATS_FILE = os.path.join(DATA_DIR, "stats.json")
LEGACY_TEAM_FILE = os.path.join(DATA_DIR, "team.json")
LEGACY_USER_STATS_FILE = os.path.join(DATA_DIR, "user_statistics.json")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Журнал исходящих сообщений для /cleanup. Telegram не отдает боту список его
# сообщений, поэтому запоминаем id сами. Журнал живет в отдельном файле
# BOT_MESSAGES_FILE и сбрасывается на диск фоново, чтобы не писать на каждое
# сообщение. После рестарта поднимаем его обратно.
_bot_messages = {}       # chat_id -> [(message_id, unix_ts), ...]
_bot_messages_dirty = False  # есть несохраненные изменения


def record_bot_message(chat_id: int, message_id: int):
    global _bot_messages_dirty
    now = time.time()
    log = _bot_messages.setdefault(chat_id, [])
    log.append((message_id, now))
    if len(log) > 1:
        cutoff = now - BOT_MSG_KEEP
        _bot_messages[chat_id] = [(mid, ts) for mid, ts in log if ts >= cutoff]
    _bot_messages_dirty = True


async def record_outgoing(make_request, called_bot, method):
    """Middleware сессии: ловит каждое отправленное/отредактированное сообщение."""
    result = await make_request(called_bot, method)
    try:
        if isinstance(result, types.Message):
            record_bot_message(result.chat.id, result.message_id)
    except Exception as e:
        print(f"Не удалось записать сообщение в журнал /cleanup: {e}")
    return result


bot.session.middleware(record_outgoing)


def load_bot_messages():
    """Поднимает журнал с диска, отсеивая записи старше срока хранения."""
    global _bot_messages
    data = _read_json(BOT_MESSAGES_FILE, {}) or {}
    cutoff = time.time() - BOT_MSG_KEEP
    restored = {}
    for chat_id, log in data.items():
        kept = [(int(mid), float(ts)) for mid, ts in log if float(ts) >= cutoff]
        if kept:
            restored[int(chat_id)] = kept
    _bot_messages = restored


def _write_bot_messages_sync(data):
    tmp_path = BOT_MESSAGES_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, BOT_MESSAGES_FILE)


async def flush_bot_messages():
    """Сбрасывает журнал на диск, если были изменения."""
    global _bot_messages_dirty
    if not _bot_messages_dirty:
        return
    # Снимок строим синхронно (без await) — значит, консистентный
    snapshot = {str(cid): [[mid, ts] for mid, ts in log]
                for cid, log in _bot_messages.items() if log}
    _bot_messages_dirty = False
    await asyncio.to_thread(_write_bot_messages_sync, snapshot)


async def bot_messages_flush_loop():
    while True:
        await asyncio.sleep(BOT_MSG_FLUSH_INTERVAL)
        try:
            await flush_bot_messages()
        except OSError as e:
            print(f"Не удалось сохранить журнал сообщений: {e}")

COOLDOWN_SECONDS = 60        # кулдаун /tag по умолчанию, сек
DEFAULT_RESET_TIME = "03:00" # когда по умолчанию чистится список сбора

# --- Статистика Deadlock через api.deadlock-api.com ---
# Ключ Steam нужен только чтобы разворачивать vanity-ссылки (steamcommunity.com/id/имя).
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "").strip()
STEAMID64_BASE = 76561197960265728  # SteamID64 = account_id + это число
DEADLOCK_API = "https://api.deadlock-api.com"
API_TIMEOUT = 12          # сек на запрос к API
RECENT_MATCHES = 30       # по скольким последним матчам считаем винрейт/KDA
MH_CACHE_TTL = 900        # match-history тяжелый (~1.5 МБ), держим 15 мин

# Чат, в котором работают пасхалки. В остальных чатах счетчики выключены,
# а /tag от постороннего отвечает «403».
EGG_CHAT_ID = os.getenv("EGG_CHAT_ID", "1179357258").strip()

# Кому доступны админские команды (/clear, /set): список user_id через запятую
# или пробел. Если пусто — сгодится любой админ чата, но в чате, где админы все,
# это не ограничение, поэтому ADMIN_ID и стоит задать.
ADMIN_IDS = {
    part.strip().lstrip("-")
    for part in os.getenv("ADMIN_ID", "").replace(",", " ").split()
    if part.strip()
}

ADMIN_WHO = "владельцы бота" if ADMIN_IDS else "админы чата"
CLEAR_DENY = f"Список сбора чистят только {ADMIN_WHO}. 🚫"
SET_DENY = f"Настройки меняют только {ADMIN_WHO}. 🚫"
CLEANUP_DENY = f"Сообщения бота чистят только {ADMIN_WHO}. 🚫"

CLEANUP_WINDOW = 24 * 3600  # /cleanup удаляет сообщения бота за это время, сек
BOT_MSG_KEEP = 48 * 3600    # дольше держать в журнале нет смысла: Telegram
                            # не дает ботам удалять сообщения старше 48 часов
BOT_MSG_FLUSH_INTERVAL = 60 # как часто сбрасывать журнал на диск, сек

SET_USAGE = (
    f"⚙️ <b>Настройки чата</b> — меняют только {ADMIN_WHO}\n\n"
    "<b>Автосбор</b> — ежедневный /tag в заданное время (пояс сервера):\n"
    "• <code>/set autotag ЧЧ:ММ</code> — включить, напр. <code>/set autotag 20:00</code>\n"
    "• <code>/set autotag disable</code> — выключить\n\n"
    "<b>Состав</b>:\n"
    "• <code>/set team add @ник</code> — добавить игрока\n"
    "• <code>/set team remove @ник</code> — убрать игрока\n\n"
    "<b>Прочее</b>:\n"
    "• <code>/set cooldown СЕК</code> — кулдаун /tag (0 — выключить)\n"
    "• <code>/set reset_time ЧЧ:ММ</code> — когда чистить список сбора"
)


def chat_id_variants(value) -> set:
    """Одна и та же группа записывается по-разному: 1179357258, -1179357258,
    -1001179357258. Приводим к сравнимому виду, чтобы префикс -100 и минус
    в конфиге не имели значения."""
    digits = str(value).strip().lstrip("-")
    variants = {digits}
    if digits.startswith("100"):
        variants.add(digits[3:])
    return variants


EGG_CHAT_VARIANTS = chat_id_variants(EGG_CHAT_ID)

# Старые пасхалки были прибиты к username. Когда человек с совпадающим именем
# впервые зовет /tag, счетчик переезжает на его user_id и обнуляется в старом поле.
LEGACY_EGG_FIELDS = {
    "славик": "slavik",
    "slavik": "slavik",
    "тексер": "texxera",
    "texxera": "texxera",
}

# --- Хранилище ---
# Всё состояние держим в памяти под одним локом и пишем на диск атомарно,
# чтобы одновременные нажатия кнопок не затирали правки друг друга.
_state = {"version": 2, "chats": {}}
_state_lock = asyncio.Lock()


def new_chat_state():
    return {
        "team": [],
        "user_stats": {},
        "eggs": {},          # user_id -> {"name": ..., "count": ...}
        "slavik": 0,         # непривязанные счетчики старых пасхалок
        "texxera": 0,
        "tags": 0,
        "unauthorized": 0,
        "last_tag_time": 0.0,
        "current_gathering": {},
        "autotag": None,               # "ЧЧ:ММ" — ежедневный автосбор, или None
        "reset_time": DEFAULT_RESET_TIME,  # "ЧЧ:ММ" — когда чистить список сбора
        "cooldown": COOLDOWN_SECONDS,  # кулдаун /tag, сек
        "gay_stats": {},               # @ник -> сколько раз нажал «Я ГЕЙ»
        "links": {},                   # user_id -> Deadlock account_id (SteamID3)
    }


def eggs_enabled(chat_id: int) -> bool:
    return bool(chat_id_variants(chat_id) & EGG_CHAT_VARIANTS)


def adopt_legacy_counter(chat: dict, name: str) -> int:
    """Забирает счетчик прежней захардкоженной пасхалки с тем же именем."""
    field = LEGACY_EGG_FIELDS.get(name.lower())
    if not field or chat.get(field, 0) <= 0:
        return 0
    adopted = chat[field]
    chat[field] = 0
    return adopted


def ensure_egg(chat: dict, user: types.User) -> dict:
    """Пасхалка на каждого не-игрока: счетчик заводится сам при первом /tag.
    Привязка к user_id, а не к @username, — ник можно сменить, id остается."""
    key = str(user.id)
    egg = chat["eggs"].get(key)
    if egg is None:
        name = user.first_name or user.username or f"id{user.id}"
        egg = {"name": name, "count": adopt_legacy_counter(chat, name)}
        chat["eggs"][key] = egg
    return egg


async def is_admin(message: types.Message) -> bool:
    """Заданный ADMIN_ID перекрывает проверку прав в чате: там, где админы все,
    она никого не отсекает."""
    if ADMIN_IDS:
        return str(message.from_user.id) in ADMIN_IDS
    return await is_chat_admin(message)


async def is_chat_admin(message: types.Message) -> bool:
    # В личке администраторов нет — там хозяин чата сам собеседник
    if message.chat.type == "private":
        return True
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    except Exception as e:
        print(f"Не удалось проверить права в чате {message.chat.id}: {e}")
        return False
    return member.status in ("creator", "administrator")


def _read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Не удалось прочитать {path}: {e}")
        return default


def _collect_legacy():
    """Собирает данные старого формата (три отдельных файла) в одно состояние чата."""
    old_stats = _read_json(LEGACY_STATS_FILE)
    old_team = _read_json(LEGACY_TEAM_FILE)
    old_user_stats = _read_json(LEGACY_USER_STATS_FILE)

    if old_stats is None and old_team is None and old_user_stats is None:
        return None

    chat = new_chat_state()
    chat["team"] = old_team or []
    chat["user_stats"] = old_user_stats or {}
    if old_stats:
        chat["slavik"] = old_stats.get("slavik", 0)
        chat["texxera"] = old_stats.get("texxera", 0)
        chat["tags"] = old_stats.get("team", 0)
        chat["unauthorized"] = old_stats.get("unauthorized", 0)
        chat["last_tag_time"] = old_stats.get("last_tag_time", 0.0)
        chat["current_gathering"] = old_stats.get("current_gathering", {})
    return chat


def load_state():
    global _state
    saved = _read_json(STATE_FILE)
    if saved and isinstance(saved.get("chats"), dict):
        _state = saved
        _state.setdefault("version", 2)
        return

    _state = {"version": 2, "chats": {}}
    legacy = _collect_legacy()
    if legacy is not None:
        # Chat_id старых данных неизвестен, поэтому они достаются первому чату,
        # который обратится к боту после обновления.
        _state["legacy"] = legacy
        print(f"[{datetime.now()}] Найдены данные старого формата, ждут привязки к чату.")


def _write_state_sync(data):
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, STATE_FILE)


async def save_state():
    # Запись в отдельном потоке, чтобы не блокировать event loop
    await asyncio.to_thread(_write_state_sync, _state)


def _ensure_chat(chat_id: int) -> dict:
    chats = _state.setdefault("chats", {})
    key = str(chat_id)
    if key not in chats:
        legacy = _state.pop("legacy", None)
        chats[key] = legacy if legacy is not None else new_chat_state()
        if legacy is not None:
            print(f"[{datetime.now()}] Данные старого формата привязаны к чату {key}.")
    # Ключи, появившиеся позже, — чтобы не спотыкаться на состоянии старых чатов
    for field, default in new_chat_state().items():
        chats[key].setdefault(field, default)
    return chats[key]


@asynccontextmanager
async def edit_chat(chat_id: int):
    """Меняет состояние чата под локом и сохраняет его на диск."""
    async with _state_lock:
        try:
            yield _ensure_chat(chat_id)
        finally:
            await save_state()


async def read_chat(chat_id: int) -> dict:
    """Снимок состояния чата — безопасно читать после выхода из лока."""
    async with _state_lock:
        return copy.deepcopy(_ensure_chat(chat_id))


def get_raz_word(count: int) -> str:
    last_two_digits = count % 100
    last_digit = count % 10

    if 11 <= last_two_digits <= 14:
        return "раз"
    if 2 <= last_digit <= 4:
        return "раза"
    return "раз"


def parse_hhmm(text: str):
    """'20:00', '9:5' -> '20:00' / '09:05'; мусор -> None."""
    try:
        t = datetime.strptime(text.strip(), "%H:%M")
    except ValueError:
        return None
    return f"{t.hour:02d}:{t.minute:02d}"


async def broadcast_gathering(chat_id: int, users_to_tag: list, current_gathering: dict):
    """Тегает игроков пачками по 4 и постит список сбора с кнопками."""
    for i in range(0, len(users_to_tag), 4):
        await bot.send_message(chat_id, " ".join(users_to_tag[i:i + 4]))
    await bot.send_message(
        chat_id,
        get_gathering_text(current_gathering),
        reply_markup=get_keyboard(),
        parse_mode="HTML",
    )

# --- Автосбор: раз в сутки тегаем состав в заданное для чата время ---
async def fire_autotag(chat_id: int):
    async with edit_chat(chat_id) as chat:
        team = list(chat["team"])
        current_gathering = copy.deepcopy(chat["current_gathering"])
        users_to_tag = [
            user for user in team
            if current_gathering.get(user.lower(), {}).get("vote") != "+"
        ]
        # Отмечаем тег, только если реально есть кого звать
        if users_to_tag:
            chat["tags"] += 1
            chat["last_tag_time"] = time.time()

    if not users_to_tag:
        return  # состав пуст или все уже отметились — не шумим

    try:
        await bot.send_message(chat_id, "⏰ <b>Автосбор!</b> Пора напуляться.", parse_mode="HTML")
        await broadcast_gathering(chat_id, users_to_tag, current_gathering)
    except Exception as e:
        print(f"Ошибка автотега в чате {chat_id}: {e}")


async def minute_scheduler():
    """Раз в минуту: у каждого чата свое время ночной очистки и автосбора."""
    while True:
        now = datetime.now()
        # Просыпаемся к началу следующей минуты, чтобы проверять ЧЧ:ММ один раз
        await asyncio.sleep(max(1.0, 60 - now.second - now.microsecond / 1_000_000))

        hhmm = datetime.now().strftime("%H:%M")
        autotag_due = []
        cleared = False
        async with _state_lock:
            for key, chat in _state.get("chats", {}).items():
                # Сначала очистка: если совпадет с автосбором, тот увидит пустой список
                if chat.get("reset_time", DEFAULT_RESET_TIME) == hhmm and chat.get("current_gathering"):
                    chat["current_gathering"] = {}
                    cleared = True
                if chat.get("autotag") == hhmm:
                    autotag_due.append(int(key))
            if cleared:
                await save_state()

        if cleared:
            print(f"[{datetime.now()}] Списки сбора очищены по расписанию ({hhmm}).")
        for chat_id in autotag_due:
            await fire_autotag(chat_id)

# --- Статистика Deadlock ---
_http_session = None
_asset_cache = {}   # "ranks"/"heroes" -> {id: name}
_mh_cache = {}      # account_id -> (expiry_ts, matches)


async def _get_session():
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(headers={"User-Agent": "napul-bot"})
    return _http_session


async def api_json(url: str, params: dict = None):
    """GET JSON; None при любой ошибке или не-200 — вызывающий решает, что сказать."""
    try:
        session = await _get_session()
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as r:
            if r.status != 200:
                return None
            return await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"Ошибка запроса {url}: {e}")
        return None


async def _load_asset_names(kind: str) -> dict:
    """kind: 'ranks' (tier->name) или 'heroes' (id->name). Кэшируется на весь процесс."""
    if kind not in _asset_cache:
        data = await api_json(f"{DEADLOCK_API}/v1/assets/{kind}")
        if not data:
            return {}
        key = "tier" if kind == "ranks" else "id"
        _asset_cache[kind] = {item[key]: item["name"] for item in data}
    return _asset_cache.get(kind, {})


async def resolve_account_id(text: str):
    """Ссылка Steam / tracklock / голый id -> (account_id, None) либо (None, код_ошибки)."""
    text = text.strip()

    m = re.search(r"tracklock\.gg/players/(\d+)", text)
    if m:
        return int(m.group(1)), None

    m = re.search(r"steamcommunity\.com/profiles/(\d{17})", text)
    if m:
        return int(m.group(1)) - STEAMID64_BASE, None

    m = re.search(r"steamcommunity\.com/id/([^/?\s]+)", text)
    if m:
        return await _resolve_vanity(m.group(1))

    if text.isdigit():
        n = int(text)
        # 17-значный SteamID64 приводим к account_id, короткое число — уже account_id
        return (n - STEAMID64_BASE if n > STEAMID64_BASE else n), None

    return None, "unknown"


async def _resolve_vanity(name: str):
    if not STEAM_API_KEY:
        return None, "no_key"
    data = await api_json(
        "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/",
        {"key": STEAM_API_KEY, "vanityurl": name})
    resp = (data or {}).get("response", {})
    if resp.get("success") == 1 and resp.get("steamid"):
        return int(resp["steamid"]) - STEAMID64_BASE, None
    return None, "vanity_not_found"


async def fetch_player_summary(account_id: int):
    """Дешевые вызовы: текущий ранг (mmr) и Steam-профиль. Любой может быть None."""
    mmr, steam = await asyncio.gather(
        api_json(f"{DEADLOCK_API}/v1/players/mmr", {"account_ids": str(account_id)}),
        api_json(f"{DEADLOCK_API}/v1/players/steam", {"account_ids": str(account_id)}),
    )
    return (mmr[0] if mmr else None), (steam[0] if steam else None)


async def fetch_recent_stats(account_id: int):
    """Винрейт/KDA/топ-герой по последним матчам. match-history тяжелый — кэшируем."""
    now = time.time()
    cached = _mh_cache.get(account_id)
    if cached and cached[0] > now:
        matches = cached[1]
    else:
        matches = await api_json(f"{DEADLOCK_API}/v1/players/{account_id}/match-history")
        if matches is None:
            return None
        _mh_cache[account_id] = (now + MH_CACHE_TTL, matches)

    last = matches[:RECENT_MATCHES]
    if not last:
        return None

    n = len(last)
    wins = sum(1 for m in last if m["match_result"] == m["player_team"])
    top_hero_id = Counter(m["hero_id"] for m in last).most_common(1)[0][0]
    return {
        "n": n,
        "winrate": wins / n * 100,
        "kills": sum(m["player_kills"] for m in last) / n,
        "deaths": sum(m["player_deaths"] for m in last) / n,
        "assists": sum(m["player_assists"] for m in last) / n,
        "top_hero_id": top_hero_id,
    }


async def render_deadlock_stats(account_id: int) -> str:
    """Блок статистики Deadlock для /me. Пустая строка, если совсем нет данных."""
    (mmr, steam), recent = await asyncio.gather(
        fetch_player_summary(account_id),
        fetch_recent_stats(account_id),
    )

    if not mmr and not steam and not recent:
        return ("\n\n🎮 <b>Deadlock:</b> данных нет — профиль приватный "
                "или Steam-статистика отключена.")

    lines = ["\n\n🎮 <b>Deadlock</b>"]

    if steam and steam.get("personaname"):
        lines.append(f"👤 {html.escape(steam['personaname'])}")

    if mmr and mmr.get("division") is not None:
        ranks = await _load_asset_names("ranks")
        rank_name = ranks.get(mmr["division"], f"Ранг {mmr['division']}")
        subrank = mmr.get("division_tier", "")
        score = mmr.get("player_score")
        line = f"🏅 {html.escape(rank_name)} {subrank}".rstrip()
        if score is not None:
            line += f" ({round(score)})"
        lines.append(line)

    if recent:
        heroes = await _load_asset_names("heroes")
        hero = heroes.get(recent["top_hero_id"], f"#{recent['top_hero_id']}")
        lines.append(
            f"📈 За {recent['n']} игр: винрейт {recent['winrate']:.0f}%, "
            f"KDA {recent['kills']:.1f}/{recent['deaths']:.1f}/{recent['assists']:.1f}")
        lines.append(f"🦸 Чаще играет: {html.escape(hero)}")

    if steam and steam.get("matches_played_last_30d") is not None:
        lines.append(f"🗓 Матчей за 30 дней: {steam['matches_played_last_30d']}")

    return "\n".join(lines)


LINK_USAGE = (
    "🔗 <b>Привязка профиля Deadlock</b>\n\n"
    "Пришли ссылку на свой Steam или tracklock:\n"
    "• <code>/link https://steamcommunity.com/profiles/7656...</code>\n"
    "• <code>/link https://steamcommunity.com/id/твой_ник</code>\n"
    "• <code>/link https://tracklock.gg/players/12345678</code>\n\n"
    "После привязки статы Deadlock появятся в /me.\n"
    "Отвязать: <code>/link remove</code>"
)


@dp.message(Command("link", ignore_mention=True))
async def link_profile(message: types.Message, command: CommandObject):
    arg = (command.args or "").strip()

    if not arg:
        await message.reply(LINK_USAGE, parse_mode="HTML")
        return

    if arg.lower() in ("remove", "del", "delete", "off", "убрать"):
        async with edit_chat(message.chat.id) as chat:
            existed = chat["links"].pop(str(message.from_user.id), None)
        await message.reply("Профиль отвязан." if existed else "У тебя и не было привязки. 🤔")
        return

    account_id, err = await resolve_account_id(arg)
    if err == "no_key":
        await message.reply(
            "Ссылки вида /id/имя пока не поддержаны. Пришли числовую: "
            "<code>steamcommunity.com/profiles/7656...</code> "
            "(открой профиль → «Редактировать» → там виден числовой URL).",
            parse_mode="HTML")
        return
    if err == "vanity_not_found" or (err is None and account_id is None):
        await message.reply("Не нашел такой Steam-профиль. Проверь ссылку. 🤔")
        return
    if account_id is None:
        await message.reply("Не понял ссылку. Формат — см. /link без аргументов.")
        return

    mmr, steam = await fetch_player_summary(account_id)

    async with edit_chat(message.chat.id) as chat:
        chat["links"][str(message.from_user.id)] = account_id

    name = steam.get("personaname") if steam else None
    who = f" ({html.escape(name)})" if name else ""
    await message.reply(
        f"🔗 Профиль привязан{who}. Смотри /me.", parse_mode="HTML")

# --- Обработчики команд ---
@dp.message(Command("addme", ignore_mention=True))
async def add_me(message: types.Message):
    if not message.from_user.username:
        await message.reply("Для этого нужен @username в настройках Telegram!")
        return

    user = f"@{message.from_user.username}"

    async with edit_chat(message.chat.id) as chat:
        already_in = any(u.lower() == user.lower() for u in chat["team"])
        if not already_in:
            chat["team"].append(user)

    if already_in:
        await message.reply("Ты уже есть в списке игроков! 🔥")
    else:
        await message.reply("Успешно добавлен в список игроков! Бот будет тегать тебя при сборе. ⚔️")

@dp.message(Command("removeme", ignore_mention=True))
async def remove_me(message: types.Message):
    if not message.from_user.username:
        return

    user = f"@{message.from_user.username}"

    async with edit_chat(message.chat.id) as chat:
        new_team = [u for u in chat["team"] if u.lower() != user.lower()]
        removed = len(new_team) != len(chat["team"])
        chat["team"] = new_team

    if removed:
        await message.reply("Удален из списка. Больше тебя тегать не будут. 🫡")
    else:
        await message.reply("Тебя и так нет в списке игроков. 🤔")

@dp.message(Command("team", ignore_mention=True))
async def show_team(message: types.Message):
    chat = await read_chat(message.chat.id)
    team = chat["team"]
    if not team:
        await message.answer("Список игроков пуст. Добавь себя через /addme")
        return

    clean_names = [user.replace("@", "") for user in team]

    text = "👥 <b>Все зарегистрированные в Deadlock:</b>\n\n"
    text += "\n".join(f"• {name}" for name in clean_names)
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("whoami", ignore_mention=True))
async def who_am_i(message: types.Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    await message.reply(f"user_id: <code>{target.id}</code>\nchat_id: <code>{message.chat.id}</code>",
                        parse_mode="HTML")

@dp.message(Command("tag", ignore_mention=True))
async def mention_team(message: types.Message):
    # Без @username в состав не попасть, а вот пасхалку завести можно — она по user_id
    username = message.from_user.username
    current_user = f"@{username}".lower() if username else None
    chat_id = message.chat.id
    current_time = time.time()

    async with edit_chat(chat_id) as chat:
        team = list(chat["team"])
        in_team = current_user is not None and current_user in [u.lower() for u in team]

        # 1. Не игрок — заводим ему пасхалку (если ее еще нет) и считаем напул
        if not in_team and eggs_enabled(chat_id):
            egg = ensure_egg(chat, message.from_user)
            egg["count"] += 1
            action, egg_name, count = "egg", egg["name"], egg["count"]

        # 2. Защита от левых пользователей там, где пасхалки выключены
        elif not in_team:
            chat["unauthorized"] += 1
            action = "denied"

        # 3. Пользователь В КОМАНДЕ (включая пасхалочных, если они туда добавились)
        else:
            cooldown = chat.get("cooldown", COOLDOWN_SECONDS)
            time_since_last = current_time - chat.get("last_tag_time", 0.0)
            if time_since_last < cooldown:
                action = "cooldown"
                remaining = int(cooldown - time_since_last)
            else:
                action = "tag"
                # Засчитываем +1 в топ игроков
                chat["user_stats"][current_user] = chat["user_stats"].get(current_user, 0) + 1
                chat["tags"] += 1
                chat["last_tag_time"] = current_time

                current_gathering = copy.deepcopy(chat["current_gathering"])
                users_to_tag = [
                    user for user in team
                    if current_gathering.get(user.lower(), {}).get("vote") != "+"
                ]

    # Сеть — уже вне лока
    if action == "denied":
        await message.reply("403")
        return

    if action == "egg":
        await message.reply(f"{egg_name} напулял (уже {count} {get_raz_word(count)})")
        return

    if action == "cooldown":
        mins, secs = divmod(remaining, 60)
        await message.reply(f"⏳ КД. Подожди еще {mins} мин. {secs} сек.")
        return

    if not users_to_tag:
        await message.answer("Все игроки уже подписались! 🔥")

    await broadcast_gathering(chat_id, users_to_tag, current_gathering)

@dp.message(Command("clear", ignore_mention=True))
async def clear_gathering(message: types.Message):
    """Ручная версия ночного сброса: чистит список плюсов и минусов до 03:00."""
    if not await is_admin(message):
        await message.reply(CLEAR_DENY)
        return

    async with edit_chat(message.chat.id) as chat:
        had_votes = bool(chat["current_gathering"])
        chat["current_gathering"] = {}
        # Заодно снимаем КД: список чистят, чтобы сразу начать сбор заново
        chat["last_tag_time"] = 0.0

    if had_votes:
        await message.reply("🧹 Список плюсов и минусов очищен. Можно собираться заново!")
    else:
        await message.reply("Список и так пуст. 🤔")

@dp.message(Command("cleanup", ignore_mention=True))
async def cleanup_messages(message: types.Message):
    """Удаляет сообщения бота за последние сутки (по журналу в памяти)."""
    if not await is_admin(message):
        await message.reply(CLEANUP_DENY)
        return

    chat_id = message.chat.id
    cutoff = time.time() - CLEANUP_WINDOW
    log = _bot_messages.get(chat_id, [])
    to_delete = [mid for mid, ts in log if ts >= cutoff]

    if not to_delete:
        await message.reply(
            "Нечего чистить — за сутки бот тут ничего не слал "
            "(или журнал сбросился после перезапуска). 🤔")
        return

    deleted = 0
    failed = 0
    for mid in to_delete:
        try:
            await bot.delete_message(chat_id, mid)
            deleted += 1
        except Exception:
            # старше 48ч, уже удалено или нет прав — пропускаем
            failed += 1

    # Убираем из журнала все, что пытались удалить (успех или нет)
    global _bot_messages_dirty
    tried = set(to_delete)
    _bot_messages[chat_id] = [(mid, ts) for mid, ts in log if mid not in tried]
    _bot_messages_dirty = True
    await flush_bot_messages()

    text = f"🧹 Удалено сообщений бота: {deleted}."
    if failed:
        text += f" Не удалось: {failed} (старше 48ч или уже удалены)."
    await message.reply(text)

@dp.message(Command("set", ignore_mention=True))
async def set_config(message: types.Message, command: CommandObject):
    if not await is_admin(message):
        await message.reply(SET_DENY)
        return

    args = (command.args or "").split()
    section = args[0].lower() if args else ""
    rest = args[1:]

    if section == "autotag":
        await set_autotag(message, rest)
    elif section == "team":
        await set_team(message, rest)
    elif section == "cooldown":
        await set_cooldown(message, rest)
    elif section in ("reset_time", "reset"):
        await set_reset_time(message, rest)
    else:
        await message.answer(SET_USAGE, parse_mode="HTML")


async def set_autotag(message: types.Message, rest: list):
    chat_id = message.chat.id

    if not rest:
        chat = await read_chat(chat_id)
        cur = chat.get("autotag")
        if cur:
            await message.reply(
                f"⏰ Автосбор включен на {cur} (по времени сервера).\n"
                "Выключить: <code>/set autotag disable</code>", parse_mode="HTML")
        else:
            await message.reply(
                "Автосбор выключен.\nВключить: <code>/set autotag ЧЧ:ММ</code>", parse_mode="HTML")
        return

    if rest[0].lower() in ("disable", "off", "выкл", "выключить", "0"):
        async with edit_chat(chat_id) as chat:
            was = chat.get("autotag")
            chat["autotag"] = None
        await message.reply("⏰ Автосбор выключен." if was else "Автосбор и так был выключен. 🤔")
        return

    hhmm = parse_hhmm(rest[0])
    if not hhmm:
        await message.reply(
            "Неверное время. Формат ЧЧ:ММ, напр. <code>/set autotag 20:00</code>.", parse_mode="HTML")
        return

    async with edit_chat(chat_id) as chat:
        chat["autotag"] = hhmm
    await message.reply(
        f"⏰ Автосбор включен на {hhmm} (по времени сервера). "
        "Каждый день бот сам тегнет тех, кто еще не отметил «+».")


async def set_team(message: types.Message, rest: list):
    op = rest[0].lower() if rest else ""
    nick = rest[1].lstrip("@") if len(rest) > 1 else ""

    if op not in ("add", "remove", "rm", "del") or not nick:
        await message.answer(SET_USAGE, parse_mode="HTML")
        return

    user = f"@{nick}"
    chat_id = message.chat.id

    if op == "add":
        async with edit_chat(chat_id) as chat:
            exists = any(u.lower() == user.lower() for u in chat["team"])
            if not exists:
                chat["team"].append(user)
        await message.reply(
            f"{user} уже в списке игроков. 🔥" if exists
            else f"{user} добавлен в список игроков. ⚔️")
    else:
        async with edit_chat(chat_id) as chat:
            new_team = [u for u in chat["team"] if u.lower() != user.lower()]
            removed = len(new_team) != len(chat["team"])
            chat["team"] = new_team
        await message.reply(
            f"{user} убран из списка игроков. 🫡" if removed
            else f"{user} и так нет в списке. 🤔")


async def set_cooldown(message: types.Message, rest: list):
    chat_id = message.chat.id

    if not rest:
        chat = await read_chat(chat_id)
        cur = chat.get("cooldown", COOLDOWN_SECONDS)
        await message.reply(
            f"Кулдаун /tag: {cur} сек." if cur else "Кулдаун /tag выключен.")
        return

    if not rest[0].isdigit():
        await message.reply(
            "Укажи число секунд: <code>/set cooldown 30</code> (0 — выключить).",
            parse_mode="HTML")
        return

    seconds = min(int(rest[0]), 86400)  # больше суток смысла не имеет
    async with edit_chat(chat_id) as chat:
        chat["cooldown"] = seconds
    await message.reply(
        f"⏳ Кулдаун /tag теперь {seconds} сек." if seconds
        else "⏳ Кулдаун /tag выключен.")


async def set_reset_time(message: types.Message, rest: list):
    chat_id = message.chat.id

    if not rest:
        chat = await read_chat(chat_id)
        await message.reply(
            f"🧹 Список сбора чистится в {chat.get('reset_time', DEFAULT_RESET_TIME)} "
            "(по времени сервера).")
        return

    hhmm = parse_hhmm(rest[0])
    if not hhmm:
        await message.reply(
            "Неверное время. Формат ЧЧ:ММ, напр. <code>/set reset_time 04:00</code>.",
            parse_mode="HTML")
        return

    async with edit_chat(chat_id) as chat:
        chat["reset_time"] = hhmm
    await message.reply(f"🧹 Список сбора будет чиститься в {hhmm} (по времени сервера).")

# --- Обработчики кнопок (callback) ---
async def answer_callback(callback: types.CallbackQuery, text: str, show_alert: bool = False):
    """На нажатие Telegram ждет ответа меньше минуты. Кнопки старого сбора и
    нажатия, накопившиеся за простой бота, отвечать уже поздно — это штатная
    ситуация, а не повод ронять обработчик."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as e:
        print(f"Не удалось ответить на нажатие: {e}")


@dp.callback_query(F.data.in_({"vote_plus", "vote_minus"}))
async def handle_vote(callback: types.CallbackQuery):
    if not callback.from_user.username:
        await answer_callback(callback, "У тебя нет username!", show_alert=True)
        return

    current_user = f"@{callback.from_user.username}".lower()
    chat_id = callback.message.chat.id
    vote = "+" if callback.data == "vote_plus" else "-"
    display_name = callback.from_user.username

    async with edit_chat(chat_id) as chat:
        if current_user not in [u.lower() for u in chat["team"]]:
            result = "not_in_team"
        else:
            user_record = chat["current_gathering"].get(current_user)
            if user_record and user_record["vote"] == vote:
                result = "duplicate"
            else:
                # Вечный счетчик гейства: считаем только новое «−», не дубль
                if vote == "-":
                    chat["gay_stats"][current_user] = chat["gay_stats"].get(current_user, 0) + 1
                # Записываем решение
                chat["current_gathering"][current_user] = {"display": display_name, "vote": vote}
                result = "accepted"
            current_gathering = copy.deepcopy(chat["current_gathering"])

    if result == "not_in_team":
        await answer_callback(callback, "403: Тебя нет в списке!", show_alert=True)
        return

    if result == "duplicate":
        await answer_callback(callback, "Уже учтено!")
        return

    # Сначала закрываем нажатие, потом перерисовываем: на ответ есть меньше минуты,
    # а редактирование сообщения может и не успеть в это окно
    await answer_callback(callback, "Принято!")

    try:
        await callback.message.edit_text(
            get_gathering_text(current_gathering),
            reply_markup=get_keyboard(),
            parse_mode="HTML"
        )
    except TelegramBadRequest as e:
        # Сообщение могли удалить или оно уже с таким же текстом — голос все равно учтен
        print(f"Не удалось обновить список сбора: {e}")

# Очищенная функция без личной статистики
def get_gathering_text(current_gathering):
    text = "🚨 <b>Играем! Кто идет?</b>\n\n"

    pluses = [data["display"] for data in current_gathering.values() if data["vote"] == "+"]
    minuses = [data["display"] for data in current_gathering.values() if data["vote"] == "-"]

    if pluses:
        text += "✅ <b>Играют:</b>\n" + "\n".join(pluses) + "\n\n"
    if minuses:
        text += "❌ <b>Гейчики:</b>\n" + "\n".join(minuses) + "\n\n"
    if not pluses and not minuses:
        text += "Пока никто не отметился. Жмите кнопки!"
    return text

def get_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Играю", callback_data="vote_plus"),
            InlineKeyboardButton(text="➖ Я ГЕЙ", callback_data="vote_minus")
        ]
    ])

TOP_SIZE = 3


def format_top(pairs) -> str:
    """Топ-3 из пар (имя, счетчик); пустые счетчики в топ не идут."""
    top = sorted((p for p in pairs if p[1] > 0), key=lambda p: p[1], reverse=True)[:TOP_SIZE]
    if not top:
        return "Пока никого нет.\n"
    return "".join(
        f"{idx}. {html.escape(name)} — {count} {get_raz_word(count)}\n"
        for idx, (name, count) in enumerate(top, 1)
    )


@dp.message(Command("stats", ignore_mention=True))
async def show_stats(message: types.Message):
    chat = await read_chat(message.chat.id)

    t_count = chat.get("tags", 0)
    u_count = chat.get("unauthorized", 0)
    team_count = len(chat.get("team", []))
    eggs = chat.get("eggs", {})

    # Зарегистрированные пасхалки + еще не привязанные счетчики старого формата
    egg_pairs = [(egg["name"], egg["count"]) for egg in eggs.values()]
    egg_pairs += [(title, chat.get(field, 0))
                  for title, field in (("Славик", "slavik"), ("Тексер", "texxera"))]

    # Собачка в юзернейме нужна только для тегов, в топе она лишняя
    tag_pairs = [(user.replace("@", ""), count)
                 for user, count in chat.get("user_stats", {}).items()]

    text = (
        f"📊 <b>Общая Статистика:</b>\n\n"
        f"👥 Игроков в базе: {team_count}\n"
        f"🥚 Напулявших в базе: {len(eggs)}\n"
        f"✅ Игроков тегали: {t_count} {get_raz_word(t_count)}\n"
    )
    if u_count:
        text += f"❌ Отказано в игре: {u_count} {get_raz_word(u_count)}\n"

    text += "\n🔫 <b>Топ-3 напулявших:</b>\n" + format_top(egg_pairs)
    text += "\n🏆 <b>Топ-3 тегавших:</b>\n" + format_top(tag_pairs)

    await message.answer(text, parse_mode="HTML")

@dp.message(Command("gaystats", ignore_mention=True))
async def show_gaystats(message: types.Message):
    chat = await read_chat(message.chat.id)
    gay_stats = chat.get("gay_stats", {})

    ranked = sorted(
        ((user.replace("@", ""), count) for user, count in gay_stats.items() if count > 0),
        key=lambda p: p[1], reverse=True,
    )
    if not ranked:
        await message.answer("🌈 Пока никто не киданул.")
        return

    lines = ["🌈 <b>Топ гейчиков</b> (нажатий «Я ГЕЙ»):\n"]
    for idx, (name, count) in enumerate(ranked, 1):
        lines.append(f"{idx}. {html.escape(name)} — {count} {get_raz_word(count)}")
    await message.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("me", ignore_mention=True))
async def show_me(message: types.Message):
    user = message.from_user
    username = user.username
    key = f"@{username}".lower() if username else None
    chat = await read_chat(message.chat.id)

    in_team = key is not None and key in [u.lower() for u in chat.get("team", [])]
    tag_count = chat.get("user_stats", {}).get(key, 0) if key else 0
    gay_count = chat.get("gay_stats", {}).get(key, 0) if key else 0
    egg = chat.get("eggs", {}).get(str(user.id))

    display = html.escape(user.first_name or username or "Игрок")
    lines = [
        f"🪪 <b>{display}</b>",
        ("✅ В составе" if in_team else "➖ Не в составе (добавься через /addme)"),
        f"🏆 Тегал: {tag_count} {get_raz_word(tag_count)}",
    ]
    if egg:
        lines.append(f"🔫 Напулял: {egg['count']} {get_raz_word(egg['count'])}")
    if gay_count:
        lines.append(f"🌈 Гейнул: {gay_count} {get_raz_word(gay_count)}")

    account_id = chat.get("links", {}).get(str(user.id))
    if account_id is not None:
        try:
            lines.append(await render_deadlock_stats(account_id))
        except Exception as e:
            print(f"Ошибка статистики Deadlock для {account_id}: {e}")
            lines.append("\n\n🎮 <b>Deadlock:</b> не удалось получить статы, попробуй позже.")
    else:
        lines.append("\n🎮 Привяжи Steam через /link — покажу статы Deadlock.")

    await message.reply("\n".join(lines), parse_mode="HTML")

async def main():
    load_state()
    load_bot_messages()
    asyncio.create_task(minute_scheduler())
    asyncio.create_task(bot_messages_flush_loop())
    # Копившиеся за простой апдейты выбрасываем: отвечать на нажатия,
    # сделанные во время перезапуска, Telegram уже не позволит
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
