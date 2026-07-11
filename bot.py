import asyncio
import os
import json
import time
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher()

STATS_FILE = "stats.json"
TEAM_FILE = "team.json"
USER_STATS_FILE = "user_statistics.json"

COOLDOWN_SECONDS = 60
MPLUS_TIMEOUT_SECONDS = 600  # 10 минут таймаут для М+
TAG_TIMEOUT_SECONDS = 1800   # 30 минут таймаут для очистки списка /tag

# Стартовый состав на случай, если файла team.json еще нет
DEFAULT_TEAM = [
    "@user1", "@user2", "@user3", "@user4", "@user5", "@user6", "@user7", "@user8"
]

current_mplus = {}

# --- Функции для работы со списком команды ---
def load_team():
    if os.path.exists(TEAM_FILE):
        with open(TEAM_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    with open(TEAM_FILE, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_TEAM, f, ensure_ascii=False, indent=4)
    return DEFAULT_TEAM

def save_team(team_list):
    with open(TEAM_FILE, "w", encoding="utf-8") as f:
        json.dump(team_list, f, ensure_ascii=False, indent=4)

# --- Функции для общей статистики ---
def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r") as f:
            stats = json.load(f)
            if "current_gathering" not in stats:
                stats["current_gathering"] = {}
            if "mplus_groups" not in stats:
                stats["mplus_groups"] = 0
            if "texxera" not in stats:
                stats["texxera"] = 0
            return stats
    return {
        "slavik": 0,
        "texxera": 0,
        "team": 0,
        "unauthorized": 0,
        "last_tag_time": 0.0,
        "current_gathering": {},
        "mplus_groups": 0
    }

def save_stats(stats):
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f)

# --- Функции для личной статистики (Топ пуляторов) ---
def load_user_stats():
    if os.path.exists(USER_STATS_FILE):
        with open(USER_STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_user_stats(stats):
    with open(USER_STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=4)

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
    global current_mplus
    while True:
        now = datetime.now()
        target = now.replace(hour=3, minute=0, second=0, microsecond=0)

        if now >= target:
            target += timedelta(days=1)

        sleep_seconds = (target - now).total_seconds()
        await asyncio.sleep(sleep_seconds)

        stats = load_stats()
        stats["current_gathering"] = {}
        save_stats(stats)
        
        current_mplus.clear()
        print(f"[{datetime.now()}] Все списки опросов автоматически очищены.")

# --- Автоочистка списка /tag через 30 минут неактивности ---
async def tag_timeout_checker(chat_id: int):
    await asyncio.sleep(TAG_TIMEOUT_SECONDS)
    
    stats = load_stats()
    current_time = time.time()
    last_tag_time = stats.get("last_tag_time", 0.0)
    
    if current_time - last_tag_time >= TAG_TIMEOUT_SECONDS:
        current_gathering = stats.get("current_gathering", {})
        if current_gathering:
            stats["current_gathering"] = {}
            save_stats(stats)
            try:
                await bot.send_message(
                    chat_id=chat_id, 
                    text="⏳ Прошло 30 минут с последнего сбора. Список плюсов и минусов очищен!"
                )
            except Exception as e:
                print(f"Ошибка при отправке сообщения таймаута /tag: {e}")

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
    team = load_team()
    
    if any(u.lower() == user.lower() for u in team):
        await message.reply("Ты уже есть в списке напуляторов! 🔥")
    else:
        team.append(user)
        save_team(team)
        await message.reply("Успешно добавлен в список напуляторов! Бот будет тегать тебя при сборе. ⚔️")

@dp.message(Command("removeme", ignore_mention=True))
async def remove_me(message: types.Message):
    if not message.from_user.username:
        return
    
    user = f"@{message.from_user.username}"
    team = load_team()
    
    new_team = [u for u in team if u.lower() != user.lower()]
    
    if len(new_team) == len(team):
        await message.reply("Тебя и так нет в списке напуляторов. 🤔")
    else:
        save_team(new_team)
        await message.reply("Удален из списка. Больше тебя тегать не будут. 🫡")

@dp.message(Command("team", ignore_mention=True))
async def show_team(message: types.Message):
    team = load_team()
    if not team:
        await message.answer("Список напуляторов пуст. Добавь себя через /addme")
        return
    
    clean_names = [user.replace("@", "") for user in team]
    
    text = "👥 <b>Все зарегистрированные в Deadlock:</b>\n\n"
    text += "\n".join(f"• {name}" for name in clean_names)
    await message.answer(text, parse_mode="HTML")

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
    team = load_team()
    team_lower = [u.lower() for u in team]

    stats = load_stats()
    current_time = time.time()

    is_slavik = (current_user == "@slaanesh")
    is_texxera = (current_user == "@texxera")
    in_team = (current_user in team_lower)

    # 1. Защита от левых пользователей
    if not is_slavik and not is_texxera and not in_team:
        stats["unauthorized"] += 1
        save_stats(stats)
        await message.reply("403")
        return

    # 2. Если Славик, но НЕ в команде — срабатывает старая пасхалка
    if is_slavik and not in_team:
        stats["slavik"] += 1
        save_stats(stats)
        count = stats["slavik"]
        word = get_raz_word(count)
        await message.reply(f"Славик напулял (уже {count} {word})")
        return

    # 2.1 Если Тексер, но НЕ в команде — срабатывает пасхалка
    if is_texxera and not in_team:
        stats["texxera"] += 1
        save_stats(stats)
        count = stats["texxera"]
        word = get_raz_word(count)
        await message.reply(f"Тексер напулял (уже {count} {word})")
        return

    # 3. Если пользователь В КОМАНДЕ (включая Славика, если он туда добавился)
    if in_team:
        # Проверка КД
        time_since_last = current_time - stats.get("last_tag_time", 0)
        if time_since_last < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - time_since_last)
            mins, secs = divmod(remaining, 60)
            await message.reply(f"⏳ КД. Подожди еще {mins} мин. {secs} сек.")
            return

        # Засчитываем +1 в топ пуляторов
        user_stats = load_user_stats()
        user_stats[current_user] = user_stats.get(current_user, 0) + 1
        save_user_stats(user_stats)

        # Обновляем статистику сборов
        stats["team"] += 1
        stats["last_tag_time"] = current_time
        save_stats(stats)

        current_gathering = stats.get("current_gathering", {})
        users_to_tag = [user for user in team if current_gathering.get(user.lower(), {}).get("vote") != "+"]

        for i in range(0, len(users_to_tag), 4):
            chunk = users_to_tag[i:i+4]
            await message.answer(" ".join(chunk))

        if not users_to_tag:
            await message.answer("Все напуляторы уже подписались! 🔥")

        await message.answer(
            get_gathering_text(current_gathering),
            reply_markup=get_keyboard(),
            parse_mode="HTML"
        )
        
        asyncio.create_task(tag_timeout_checker(chat_id))

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
        
        stats = load_stats()
        stats["mplus_groups"] = stats.get("mplus_groups", 0) + 1
        save_stats(stats)
        
        current_mplus[chat_id].pop(initiator_key, None)

@dp.callback_query(F.data.in_({"vote_plus", "vote_minus"}))
async def handle_vote(callback: types.CallbackQuery):
    if not callback.from_user.username:
        await callback.answer("У тебя нет username!", show_alert=True)
        return

    current_user = f"@{callback.from_user.username}".lower()
    team = load_team()
    team_lower = [u.lower() for u in team]

    if current_user not in team_lower:
        await callback.answer("403: Тебя нет в списке!", show_alert=True)
        return

    stats = load_stats()
    current_gathering = stats.get("current_gathering", {})

    vote = "+" if callback.data == "vote_plus" else "-"
    display_name = callback.from_user.username

    user_record = current_gathering.get(current_user)
    if user_record and user_record["vote"] == vote:
        await callback.answer("Уже учтено!")
        return

    # Записываем решение
    current_gathering[current_user] = {"display": display_name, "vote": vote}
    stats["current_gathering"] = current_gathering
    save_stats(stats)

    await callback.message.edit_text(
        get_gathering_text(current_gathering),
        reply_markup=get_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer("Принято!")

# Очищенная функция без личной статистики
def get_gathering_text(current_gathering):
    text = "🚨 <b>Напуляем! Кто идет?</b>\n\n"
    
    pluses = [data["display"] for data in current_gathering.values() if data["vote"] == "+"]
    minuses = [data["display"] for data in current_gathering.values() if data["vote"] == "-"]
            
    if pluses:
        text += "✅ <b>Пуляют:</b>\n" + "\n".join(pluses) + "\n\n"
    if minuses:
        text += "❌ <b>Гейчики:</b>\n" + "\n".join(minuses) + "\n\n"
    if not pluses and not minuses:
        text += "Пока никто не отметился. Жмите кнопки!"
    return text

def get_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Пуляю", callback_data="vote_plus"),
            InlineKeyboardButton(text="➖ Я ГЕЙ", callback_data="vote_minus")
        ]
    ])

@dp.message(Command("stats", ignore_mention=True))
async def show_stats(message: types.Message):
    stats = load_stats()
    team = load_team()
    user_stats = load_user_stats()
    
    s_count = stats.get('slavik', 0)
    tx_count = stats.get('texxera', 0)
    t_count = stats.get('team', 0)
    u_count = stats.get('unauthorized', 0)
    m_count = stats.get('mplus_groups', 0)
    team_count = len(team)

    text = (
        f"📊 <b>Общая Статистика:</b>\n\n"
        f"👥 Напуляторов в базе: {team_count}\n"
        f"🔫 Славик напулял (Пасхалка): {s_count} {get_raz_word(s_count)}\n"
        f"🔫 Тексер напулял (Пасхалка): {tx_count} {get_raz_word(tx_count)}\n"
        f"✅ Напуляторов тегали: {t_count} {get_raz_word(t_count)}\n"
        f"❌ Отказано в напуле: {u_count} {get_raz_word(u_count)}\n"
        f"⚔️ Успешных сборов в М+: {m_count} {get_raz_word(m_count)}\n\n"
        f"🏆 <b>Топ пуляторов:</b>\n"
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
    asyncio.create_task(reset_gathering_at_three_am())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())