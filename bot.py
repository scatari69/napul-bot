import asyncio
import copy
import html
import os
import json
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
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

# Файлы старого плоского формата — переносятся в state.json при первом запуске
LEGACY_STATS_FILE = os.path.join(DATA_DIR, "stats.json")
LEGACY_TEAM_FILE = os.path.join(DATA_DIR, "team.json")
LEGACY_USER_STATS_FILE = os.path.join(DATA_DIR, "user_statistics.json")

bot = Bot(token=TOKEN)
dp = Dispatcher()

COOLDOWN_SECONDS = 60
MPLUS_TIMEOUT_SECONDS = 600  # 10 минут таймаут для М+

# Чат, в котором работают пасхалки. В остальных чатах /egg и счетчики выключены.
EGG_CHAT_ID = os.getenv("EGG_CHAT_ID", "1179357258").strip()


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

# Старые пасхалки были прибиты к username. При регистрации через /egg
# счетчик с совпадающим именем переезжает на user_id и обнуляется в старом поле.
LEGACY_EGG_FIELDS = {
    "славик": "slavik",
    "slavik": "slavik",
    "тексер": "texxera",
    "texxera": "texxera",
}

current_mplus = {}

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
        "mplus_groups": 0,
        "last_tag_time": 0.0,
        "current_gathering": {},
    }


def eggs_enabled(chat_id: int) -> bool:
    return bool(chat_id_variants(chat_id) & EGG_CHAT_VARIANTS)


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
        chat["mplus_groups"] = old_stats.get("mplus_groups", 0)
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

# --- Фоновая задача для сброса в 3 часа ночи ---
async def reset_gathering_at_three_am():
    while True:
        now = datetime.now()
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)

        if now >= target:
            target += timedelta(days=1)

        sleep_seconds = (target - now).total_seconds()
        await asyncio.sleep(sleep_seconds)

        async with _state_lock:
            for chat in _state.get("chats", {}).values():
                chat["current_gathering"] = {}
            await save_state()

        current_mplus.clear()
        print(f"[{datetime.now()}] Все списки опросов автоматически очищены.")

# --- Автоочистка группы М+ по таймауту ---
async def mplus_timeout_checker(chat_id: int, initiator: str, message_id: int):
    await asyncio.sleep(MPLUS_TIMEOUT_SECONDS)

    if chat_id in current_mplus and initiator in current_mplus[chat_id]:
        chat_data = current_mplus[chat_id][initiator]

        if chat_data.get("message_id") == message_id:
            try:
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"⌛ Время вышло. Группа от @{chat_data['initiator_display']} в М+ не была собрана."
                )
            except Exception as e:
                print(f"Ошибка при таймауте: {e}")
            finally:
                current_mplus[chat_id].pop(initiator, None)

# --- Функции для М+ опроса ---
def get_mplus_text(chat_data):
    initiator_display = chat_data.get("initiator_display", "Неизвестно")
    text = f"🔮 <b>Собираем пати в М+!</b> (Сбор от @{initiator_display})\n\n"

    tanks = chat_data["tank"]
    heals = chat_data["heal"]
    dds = chat_data["dd"]

    text += f"🛡️ <b>Танк ({len(tanks)}/1):</b> {tanks[0] if tanks else '—'}\n"
    text += f"💚 <b>Хил ({len(heals)}/1):</b> {heals[0] if heals else '—'}\n"

    text += f"⚔️ <b>ДД ({len(dds)}/3):</b>\n"
    if dds:
        for dd in dds:
            text += f"  • {dd}\n"
    else:
        text += "  • —\n"

    return text

def get_mplus_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🛡️ Танк", callback_data="mp_tank"),
            InlineKeyboardButton(text="💚 Хил", callback_data="mp_heal"),
            InlineKeyboardButton(text="⚔️ ДД", callback_data="mp_dd")
        ]
    ])

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

EGG_USAGE = (
    "🥚 <b>Управление пасхалками</b> (только для админов чата)\n\n"
    "• <code>/egg list</code> — показать все пасхалки\n"
    "• <code>/egg add Имя</code> — в ответ на сообщение нужного человека\n"
    "• <code>/egg add &lt;user_id&gt; Имя</code> — если человек сейчас не пишет\n"
    "• <code>/egg del &lt;user_id&gt;</code> — убрать пасхалку\n\n"
    "user_id можно узнать командой /whoami (в том числе в ответ на сообщение)."
)

@dp.message(Command("egg", ignore_mention=True))
async def manage_eggs(message: types.Message, command: CommandObject):
    if not eggs_enabled(message.chat.id):
        await message.reply(
            "В этом чате пасхалки не включены.\n"
            f"chat_id этого чата: <code>{message.chat.id}</code> — "
            "укажи его в <code>EGG_CHAT_ID</code>, если пасхалки нужны здесь.",
            parse_mode="HTML"
        )
        return

    args = (command.args or "").split()
    action = args[0].lower() if args else ""

    if action == "list":
        chat = await read_chat(message.chat.id)
        eggs = chat.get("eggs", {})
        lines = ["🥚 <b>Пасхалки этого чата:</b>\n"]
        if eggs:
            for user_id, egg in sorted(eggs.items(), key=lambda i: i[1]["count"], reverse=True):
                name = html.escape(egg["name"])
                lines.append(f"• {name} — <code>{user_id}</code>, "
                             f"{egg['count']} {get_raz_word(egg['count'])}")
        else:
            lines.append("Пока ни одной. Добавь через <code>/egg add Имя</code>.")

        unclaimed = [(title, chat.get(field, 0))
                     for title, field in (("Славик", "slavik"), ("Тексер", "texxera"))
                     if chat.get(field, 0) > 0]
        if unclaimed:
            lines.append("\n📦 <b>Непривязанные счетчики (старый формат):</b>")
            for title, count in unclaimed:
                lines.append(f"• {title} — {count} {get_raz_word(count)}")
            lines.append("Добавь пасхалку с тем же именем — счетчик переедет на нее.")

        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    if action not in ("add", "del"):
        await message.answer(EGG_USAGE, parse_mode="HTML")
        return

    if not await is_chat_admin(message):
        await message.reply("Пасхалками управляют только админы чата. 🚫")
        return

    # Цель — либо явный user_id аргументом, либо автор сообщения, на которое ответили
    rest = args[1:]
    target_id = None
    if rest and rest[0].lstrip("-").isdigit():
        target_id = rest[0].lstrip("-")
        rest = rest[1:]
    elif message.reply_to_message:
        target_id = str(message.reply_to_message.from_user.id)

    if target_id is None:
        await message.answer(EGG_USAGE, parse_mode="HTML")
        return

    if action == "del":
        async with edit_chat(message.chat.id) as chat:
            removed = chat["eggs"].pop(target_id, None)
        if removed:
            await message.reply(f"Пасхалка «{removed['name']}» убрана.")
        else:
            await message.reply("Такой пасхалки тут нет. 🤔")
        return

    name = " ".join(rest).strip()
    if not name and message.reply_to_message:
        replied = message.reply_to_message.from_user
        name = replied.first_name or replied.username or ""
    if not name:
        await message.reply("Укажи имя для пасхалки: <code>/egg add Имя</code>", parse_mode="HTML")
        return

    async with edit_chat(message.chat.id) as chat:
        existing = chat["eggs"].get(target_id)
        count = existing["count"] if existing else 0

        # Забираем счетчик старой пасхалки с тем же именем
        legacy_field = LEGACY_EGG_FIELDS.get(name.lower())
        adopted = 0
        if legacy_field and chat.get(legacy_field, 0) > 0:
            adopted = chat[legacy_field]
            count += adopted
            chat[legacy_field] = 0

        chat["eggs"][target_id] = {"name": name, "count": count}

    text = f"🥚 Пасхалка «{html.escape(name)}» привязана к <code>{target_id}</code>."
    if adopted:
        text += f"\nСтарый счетчик перенесен: {adopted} {get_raz_word(adopted)}."
    elif existing:
        text += f"\nСчетчик сохранен: {count} {get_raz_word(count)}."
    await message.reply(text, parse_mode="HTML")

@dp.message(Command("mplus", ignore_mention=True))
async def start_mplus(message: types.Message):
    if not message.from_user.username:
        await message.reply("Для использования команды нужен @username в Telegram!")
        return

    chat_id = message.chat.id
    current_time = time.time()
    initiator = f"@{message.from_user.username}".lower()
    initiator_display = message.from_user.username

    if chat_id not in current_mplus:
        current_mplus[chat_id] = {}

    if initiator in current_mplus[chat_id]:
        chat_data = current_mplus[chat_id][initiator]
        time_since_start = current_time - chat_data.get("start_time", 0)

        if time_since_start < MPLUS_TIMEOUT_SECONDS:
            remaining = int(MPLUS_TIMEOUT_SECONDS - time_since_start)
            mins, secs = divmod(remaining, 60)
            await message.reply(f"⚠️ Ты уже начал сбор! Он будет автоматически закончен через {mins} мин. {secs} сек.")
            return

    chat_data = {
        "tank": [],
        "heal": [],
        "dd": [],
        "start_time": current_time,
        "initiator_display": initiator_display
    }

    sent_message = await message.answer(
        get_mplus_text(chat_data),
        reply_markup=get_mplus_keyboard(),
        parse_mode="HTML"
    )

    chat_data["message_id"] = sent_message.message_id
    current_mplus[chat_id][initiator] = chat_data

    asyncio.create_task(mplus_timeout_checker(chat_id, initiator, sent_message.message_id))

@dp.message(Command("tag", ignore_mention=True))
async def mention_team(message: types.Message):
    if not message.from_user.username:
        return

    current_user = f"@{message.from_user.username}".lower()
    chat_id = message.chat.id
    current_time = time.time()

    async with edit_chat(chat_id) as chat:
        team = list(chat["team"])
        in_team = current_user in [u.lower() for u in team]

        # Пасхалка ищется по user_id, а не по username: ник можно сменить, id — нет
        egg = None
        if not in_team and eggs_enabled(chat_id):
            egg = chat["eggs"].get(str(message.from_user.id))

        # 1. Пасхалочный пользователь вне команды
        if egg is not None:
            egg["count"] += 1
            action, egg_name, count = "egg", egg["name"], egg["count"]

        # 2. Защита от левых пользователей
        elif not in_team:
            chat["unauthorized"] += 1
            action = "denied"

        # 3. Пользователь В КОМАНДЕ (включая пасхалочных, если они туда добавились)
        else:
            time_since_last = current_time - chat.get("last_tag_time", 0.0)
            if time_since_last < COOLDOWN_SECONDS:
                action = "cooldown"
                remaining = int(COOLDOWN_SECONDS - time_since_last)
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

    for i in range(0, len(users_to_tag), 4):
        chunk = users_to_tag[i:i+4]
        await message.answer(" ".join(chunk))

    if not users_to_tag:
        await message.answer("Все игроки уже подписались! 🔥")

    await message.answer(
        get_gathering_text(current_gathering),
        reply_markup=get_keyboard(),
        parse_mode="HTML"
    )

# --- Обработчики кнопок (callback) ---
@dp.callback_query(F.data.startswith("mp_"))
async def handle_mplus_click(callback: types.CallbackQuery):
    if not callback.from_user.username:
        await callback.answer("У тебя нет username!", show_alert=True)
        return

    chat_id = callback.message.chat.id
    message_id = callback.message.message_id
    display_name = callback.from_user.username
    target_role = callback.data.split("_")[1]

    initiator_key = None
    if chat_id in current_mplus:
        for key, data in current_mplus[chat_id].items():
            if data.get("message_id") == message_id:
                initiator_key = key
                break

    if not initiator_key:
        await callback.answer("Этот сбор уже не актуален!", show_alert=True)
        return

    chat_data = current_mplus[chat_id][initiator_key]

    if display_name in chat_data[target_role]:
        chat_data[target_role].remove(display_name)
        await callback.answer("Вы покинули роль.")
    else:
        limit = 1 if target_role in ["tank", "heal"] else 3
        if len(chat_data[target_role]) >= limit:
            await callback.answer("Слот для этой роли уже занят!", show_alert=True)
            return

        for role in ["tank", "heal", "dd"]:
            if display_name in chat_data[role]:
                chat_data[role].remove(display_name)

        chat_data[target_role].append(display_name)
        await callback.answer("Роль успешно выбрана!")

    await callback.message.edit_text(
        get_mplus_text(chat_data),
        reply_markup=get_mplus_keyboard(),
        parse_mode="HTML"
    )

    if len(chat_data["tank"]) == 1 and len(chat_data["heal"]) == 1 and len(chat_data["dd"]) == 3:
        await callback.message.edit_reply_markup(reply_markup=None)

        party_list = (
            f"🛡️ Танк: {chat_data['tank'][0]}\n"
            f"💚 Хил: {chat_data['heal'][0]}\n"
            f"⚔️ ДД: {', '.join(chat_data['dd'])}"
        )

        await callback.message.answer(
            f"🎉 <b>Группа от @{chat_data['initiator_display']} собрана!</b>\n\n{party_list}",
            parse_mode="HTML"
        )

        async with edit_chat(chat_id) as chat:
            chat["mplus_groups"] += 1

        current_mplus[chat_id].pop(initiator_key, None)

@dp.callback_query(F.data.in_({"vote_plus", "vote_minus"}))
async def handle_vote(callback: types.CallbackQuery):
    if not callback.from_user.username:
        await callback.answer("У тебя нет username!", show_alert=True)
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
                # Записываем решение
                chat["current_gathering"][current_user] = {"display": display_name, "vote": vote}
                result = "accepted"
            current_gathering = copy.deepcopy(chat["current_gathering"])

    if result == "not_in_team":
        await callback.answer("403: Тебя нет в списке!", show_alert=True)
        return

    if result == "duplicate":
        await callback.answer("Уже учтено!")
        return

    await callback.message.edit_text(
        get_gathering_text(current_gathering),
        reply_markup=get_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer("Принято!")

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

@dp.message(Command("stats", ignore_mention=True))
async def show_stats(message: types.Message):
    chat = await read_chat(message.chat.id)

    t_count = chat.get("tags", 0)
    u_count = chat.get("unauthorized", 0)
    m_count = chat.get("mplus_groups", 0)
    team_count = len(chat.get("team", []))
    user_stats = chat.get("user_stats", {})

    # Зарегистрированные пасхалки + еще не привязанные счетчики старого формата
    egg_lines = [
        f"🔫 {html.escape(egg['name'])} напулял: {egg['count']} {get_raz_word(egg['count'])}\n"
        for egg in sorted(chat.get("eggs", {}).values(), key=lambda e: e["count"], reverse=True)
    ]
    for title, field in (("Славик", "slavik"), ("Тексер", "texxera")):
        count = chat.get(field, 0)
        if count > 0:
            egg_lines.append(f"🔫 {title} напулял: {count} {get_raz_word(count)}\n")

    text = (
        f"📊 <b>Общая Статистика:</b>\n\n"
        f"👥 Игроков в базе: {team_count}\n"
        + "".join(egg_lines) +
        f"✅ Игроков тегали: {t_count} {get_raz_word(t_count)}\n"
        f"❌ Отказано в игре: {u_count} {get_raz_word(u_count)}\n"
        f"⚔️ Успешных сборов в М+: {m_count} {get_raz_word(m_count)}\n\n"
        f"🏆 <b>Топ игроков:</b>\n"
    )

    if user_stats:
        sorted_users = sorted(user_stats.items(), key=lambda item: item[1], reverse=True)
        for idx, (username, count) in enumerate(sorted_users, 1):
            # Убираем собачку из юзернейма для чистого вывода в топе
            clean_username = username.replace("@", "")
            text += f"{idx}. {clean_username} — {count} {get_raz_word(count)}\n"
    else:
        text += "Пока никого нет.\n"

    await message.answer(text, parse_mode="HTML")

async def main():
    load_state()
    asyncio.create_task(reset_gathering_at_three_am())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
