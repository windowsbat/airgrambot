import asyncio
import logging
import sqlite3
import os  # Добавили для работы с переменными окружения
from datetime import datetime, timedelta
from dotenv import load_dotenv  # Добавили для загрузки .env файла
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.types import ChatPermissions
# Импорты для работы Вебхука на Render:
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# Загружаем переменные из файла .env (если он есть локально)
load_dotenv()

# ===== НАСТРОЙКИ ХОСТИНГА ДЛЯ ВЕБХУКА =====
# Render автоматически выдает URL твоего сервиса в переменную RENDER_EXTERNAL_URL
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL") 
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}" if RENDER_URL else None

# Порт, который Render выделяет для приложения (по умолчанию 10000)
PORT = int(os.getenv("PORT", 10000))

# ===== ТОКЕН (ТЕПЕРЬ ЗАЩИЩЕН) =====
# Бот сначала ищет токен в системе (на хостинге), а если не находит — берет из .env
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    exit("❌ Ошибка: Переменная окружения BOT_TOKEN не задана!")

# ===== АДМИНЫ =====
ADMIN_IDS = [8665223365, 1195470560]

# ===== НАСТРОЙКИ КД =====
COOLDOWN_APPLICATION = 300  # 5 минут
COOLDOWN_SUPPORT = 300      # 5 минут

# ===== ИНИЦИАЛИЗАЦИЯ =====
logging.basicConfig(level=logging.INFO)
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# ===== ХРАНИЛИЩЕ КД =====
user_cooldowns = {
    "application": {},
    "support": {}
}

# Путь к базе данных (вынесли в переменную, чтобы было удобно менять)
DB_PATH = 'airgram_bot.db'

# ===== БАЗА ДАННЫХ =====
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, user_name TEXT, date TEXT, status TEXT DEFAULT 'pending')''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, full_name TEXT, username TEXT, first_visit TEXT, last_visit TEXT, total_applications INTEGER DEFAULT 0, accepted_applications INTEGER DEFAULT 0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS muted_users (user_id INTEGER PRIMARY KEY, until_timestamp INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS blacklist (user_id INTEGER PRIMARY KEY, reason TEXT, date TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, value INTEGER DEFAULT 0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS support_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, user_name TEXT, message TEXT, file_id TEXT, file_type TEXT, date TEXT, status TEXT DEFAULT 'pending')''')
    cursor.execute('INSERT OR IGNORE INTO stats (key, value) VALUES ("accepted_count", 0)')
    conn.commit()
    conn.close()

# ===== ОБНОВЛЕНИЕ БАЗЫ =====
def update_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute('ALTER TABLE support_messages ADD COLUMN file_id TEXT')
    except:
        pass
    try:
        cursor.execute('ALTER TABLE support_messages ADD COLUMN file_type TEXT')
    except:
        pass
    try:
        cursor.execute('ALTER TABLE applications ADD COLUMN closed_by TEXT')
    except:
        pass
    conn.commit()
    conn.close()

init_db()
update_db()

# ===== ФУНКЦИИ КД =====
def check_cooldown(user_id: int, cooldown_type: str) -> tuple:
    if user_id in ADMIN_IDS:
        return True, 0
    cooldowns = user_cooldowns.get(cooldown_type, {})
    last_use = cooldowns.get(user_id, 0)
    current_time = datetime.now().timestamp()
    
    if cooldown_type == "application":
        cooldown_seconds = COOLDOWN_APPLICATION
    elif cooldown_type == "support":
        cooldown_seconds = COOLDOWN_SUPPORT
    else:
        return True, 0
    
    if current_time - last_use < cooldown_seconds:
        remaining = int(cooldown_seconds - (current_time - last_use))
        return False, remaining
    return True, 0

def set_cooldown(user_id: int, cooldown_type: str):
    if user_id in ADMIN_IDS:
        return
    if cooldown_type not in user_cooldowns:
        user_cooldowns[cooldown_type] = {}
    user_cooldowns[cooldown_type][user_id] = datetime.now().timestamp()

def format_cooldown_time(seconds: int) -> str:
    minutes = seconds // 60
    seconds_remain = seconds % 60
    if minutes > 0:
        return f"{minutes} мин {seconds_remain} сек"
    return f"{seconds_remain} сек"

# ===== ФУНКЦИИ БД =====
def get_accepted_count():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM stats WHERE key = "accepted_count"')
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

def add_application(user_id, username, user_name):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO applications (user_id, username, user_name, date) VALUES (?, ?, ?, ?)', (user_id, username, user_name, datetime.now().strftime("%d.%m.%Y %H:%M")))
    cursor.execute('INSERT INTO users (user_id, full_name, username, first_visit, last_visit, total_applications) VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(user_id) DO UPDATE SET last_visit = ?, total_applications = total_applications + 1', (user_id, user_name, username, datetime.now().strftime("%d.%m.%Y %H:%M"), datetime.now().strftime("%d.%m.%Y %H:%M"), datetime.now().strftime("%d.%m.%Y %H:%M")))
    conn.commit()
    conn.close()

def get_applications():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, username, user_name, date FROM applications WHERE status = "pending" ORDER BY id DESC')
    result = cursor.fetchall()
    conn.close()
    return result

def get_application(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, username, user_name, date FROM applications WHERE user_id = ? AND status = "pending" ORDER BY id DESC LIMIT 1', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result

def get_user_applications(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT date, username, status FROM applications WHERE user_id = ? ORDER BY id DESC LIMIT 10', (user_id,))
    result = cursor.fetchall()
    conn.close()
    return result

def update_application_status(user_id, status):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE applications SET status = ? WHERE user_id = ? AND status = "pending"', (status, user_id))
    if status == 'accepted':
        cursor.execute('UPDATE users SET accepted_applications = accepted_applications + 1 WHERE user_id = ?', (user_id,))
        cursor.execute('UPDATE stats SET value = value + 1 WHERE key = "accepted_count"')
    conn.commit()
    conn.close()

def add_to_blacklist(user_id, reason="Нарушение правил"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO blacklist (user_id, reason, date) VALUES (?, ?, ?)', (user_id, reason, datetime.now().strftime("%d.%m.%Y %H:%M")))
    conn.commit()
    conn.close()

def remove_from_blacklist(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM blacklist WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def is_in_blacklist(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM blacklist WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def add_mute(user_id, until_timestamp):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO muted_users (user_id, until_timestamp) VALUES (?, ?)', (user_id, until_timestamp))
    conn.commit()
    conn.close()

def remove_mute(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM muted_users WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def is_muted(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT until_timestamp FROM muted_users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    if not result:
        return False
    if result[0] < datetime.now().timestamp():
        remove_mute(user_id)
        return False
    return True

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users')
    result = cursor.fetchall()
    conn.close()
    return [row[0] for row in result]

def get_all_users_full():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, full_name, username FROM users ORDER BY user_id DESC')
    result = cursor.fetchall()
    conn.close()
    return result

def add_user_to_db(user_id, full_name, username):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id, full_name, username, first_visit, last_visit, total_applications) VALUES (?, ?, ?, ?, ?, 0)', (user_id, full_name, username, datetime.now().strftime("%d.%m.%Y %H:%M"), datetime.now().strftime("%d.%m.%Y %H:%M")))
    conn.commit()
    conn.close()

def get_user_stats(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT full_name, total_applications, accepted_applications FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result

def get_user_count():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM users')
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

def get_user_id_by_username(username):
    username = username.replace('@', '').strip()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users WHERE username = ?', (username,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def add_support_message(user_id, username, user_name, message, file_id=None, file_type=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO support_messages (user_id, username, user_name, message, file_id, file_type, date) VALUES (?, ?, ?, ?, ?, ?, ?)',
                   (user_id, username, user_name, message, file_id, file_type, datetime.now().strftime("%d.%m.%Y %H:%M")))
    conn.commit()
    conn.close()

def get_support_messages():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, user_id, username, user_name, message, file_id, file_type, date FROM support_messages WHERE status = "pending" ORDER BY id DESC')
    result = cursor.fetchall()
    conn.close()
    return result

def get_support_message_by_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, user_id, username, user_name, message, file_id, file_type, date FROM support_messages WHERE user_id = ? AND status = "pending" ORDER BY id DESC LIMIT 1', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result

def update_support_status(msg_id, status):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE support_messages SET status = ? WHERE id = ?', (status, msg_id))
    conn.commit()
    conn.close()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ===== КНОПКИ =====
apply_button = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📝 Отправить заявку")],
    [KeyboardButton(text="📊 Моя статистика")],
    [KeyboardButton(text="📋 Мои заявки")],
    [KeyboardButton(text="🆘 Техподдержка")]
], resize_keyboard=True)

admin_menu = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📋 Все заявки", callback_data="admin_applications")],
    [InlineKeyboardButton(text="📊 Полная статистика", callback_data="admin_stats")],
    [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
    [InlineKeyboardButton(text="📝 Управление ЧС", callback_data="admin_blacklist")],
    [InlineKeyboardButton(text="🆘 Сообщения в техподдержку", callback_data="admin_support")],
    [InlineKeyboardButton(text="👥 Список пользователей", callback_data="admin_users")],
    [InlineKeyboardButton(text="🔙 Закрыть меню", callback_data="admin_close")]
])

# ===== СОСТОЯНИЯ =====
class ApplicationState(StatesGroup):
    waiting_for_username = State()
    waiting_broadcast = State()
    waiting_support = State()
    waiting_support_reply = State()

# ===== КОМАНДА /START =====
@dp.message(Command("start"))
async def start_command(message: types.Message):
    user_id = message.from_user.id
    add_user_to_db(user_id, message.from_user.full_name, message.from_user.username or "Нет username")
    if is_in_blacklist(user_id):
        await message.answer("⛔ Вы заблокированы в этом боте.")
        return
    if is_muted(user_id):
        await message.answer("🔇 Вы замучены. Дождитесь снятия мута.")
        return
    accepted_count = get_accepted_count()
    pending_count = len(get_applications())
    total_users = get_user_count()
    welcome_text = f"🌟 *Добро пожаловать в AirgramBot!* 🌟\n\n📱 Пожалуйста, отправьте нам свой юзернейм в мессенджере *Airgram*.\n\n🎁 После проверки вы получите подарок!\n\n📊 *Всего принято заявок:* {accepted_count}\n⏳ *В очереди:* {pending_count}\n👥 *Всего пользователей:* {total_users}"
    await message.answer(welcome_text, reply_markup=apply_button, parse_mode="Markdown")

# ===== КОМАНДА /ADMIN =====
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("⛔ У вас нет доступа к админ-панели!")
        return
    
    support_count = len(get_support_messages())
    admin_text = f"👑 *АДМИН-ПАНЕЛЬ*\n════════════════\n\n📋 Все заявки - просмотр всех заявок\n📊 Статистика - полная статистика бота\n📢 Рассылка - отправить сообщение всем\n📝 Управление ЧС - работа с черным списком\n🆘 Техподдержка - {support_count} новых сообщений\n👥 Список пользователей - все пользователи бота\n\n👥 Всего пользователей: {get_user_count()}"
    await message.answer(admin_text, parse_mode="Markdown", reply_markup=admin_menu)

# ===== КОМАНДА /COME =====
@dp.message(Command("come"))
async def admin_commands_list(message: types.Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("⛔ Только для администратора!")
        return
    commands_text = (
        "📋 *СПИСОК КОМАНД ДЛЯ АДМИНА*\n"
        "═══════════════════════════\n\n"
        "👑 `/admin` - Открыть админ-панель\n"
        "📋 `/applications` - Показать все заявки\n"
        "📊 `/stats` - Полная статистика бота\n"
        "📢 `/broadcast [текст]` - Отправить рассылку ВСЕМ\n"
        "🔇 `/unmute [ID]` - Снять мут с пользователя\n"
        "🔓 `/unblock [ID]` - Разблокировать пользователя\n"
        "⛔ `/ban @username` - Забанить пользователя по юзернейму\n"
        "⛔ `/blacklist` - Показать черный список\n"
        "ℹ️ `/userinfo [ID]` - Информация о пользователе\n"
        "👥 `/users` - Список всех пользователей\n"
        "❓ `/come` - Показать это сообщение"
    )
    await message.answer(commands_text, parse_mode="Markdown")

# ===== КОМАНДА /BAN =====
@dp.message(Command("ban"))
async def ban_by_username(message: types.Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("⛔ Только для администратора!")
        return
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: `/ban @username`\nПример: `/ban @pugof22211`", parse_mode="Markdown")
        return
    
    username = args[1].replace('@', '').strip()
    target_user_id = get_user_id_by_username(username)
    
    if not target_user_id:
        await message.answer(f"❌ Пользователь @{username} не найден в базе данных.")
        return
    
    if target_user_id in ADMIN_IDS:
        await message.answer("❌ Нельзя забанить администратора!")
        return
    
    add_to_blacklist(target_user_id, "Забанен администратором")
    await message.answer(f"✅ Пользователь @{username} (`{target_user_id}`) забанен!")
    
    try:
        await bot.send_message(
            chat_id=target_user_id,
            text="⛔ *ВЫ ЗАБАНЕНЫ!*\n\nАдминистратор заблокировал вас в боте.",
            parse_mode="Markdown"
        )
    except:
        pass

# ===== КОМАНДА /USERS =====
@dp.message(Command("users"))
async def users_list_command(message: types.Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("⛔ Только для администратора!")
        return
    await show_users_list(message)

async def show_users_list(message: types.Message):
    users = get_all_users_full()
    if not users:
        await message.answer("👥 Нет пользователей.")
        return
    
    text = "👥 *СПИСОК ПОЛЬЗОВАТЕЛЕЙ*\n═══════════════════\n\n"
    for user_id, full_name, username in users[:20]:
        safe_username = username.replace('_', '\\_') if username else "Нет username"
        text += f"🆔 `{user_id}`\n"
        text += f"👤 {full_name}\n"
        text += f"📱 @{safe_username}\n"
        text += "─────────────\n"
    
    if len(users) > 20:
        text += f"\n... и ещё {len(users) - 20} пользователей"
    
    await message.answer(text, parse_mode="Markdown")

# ===== ТЕХПОДДЕРЖКА =====
@dp.message(F.text == "🆘 Техподдержка")
async def support_button(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if is_in_blacklist(user_id):
        await message.answer("⛔ Вы заблокированы.")
        return
    if is_muted(user_id):
        await message.answer("🔇 Вы замучены.")
        return
    
    can_use, remaining = check_cooldown(user_id, "support")
    if not can_use:
        time_str = format_cooldown_time(remaining)
        await message.answer(
            f"⏳ *Подождите!*\n\n"
            f"Вы слишком часто пишете в техподдержку.\n"
            f"Попробуйте снова через *{time_str}*.",
            parse_mode="Markdown"
        )
        return
    
    await message.answer(
        "🆘 *Техподдержка*\n\n"
        "Напишите ваше сообщение. Вы также можете прикрепить:\n"
        "📷 Фото\n"
        "🎥 Видео\n"
        "📄 Документ\n"
        "🎵 Аудио\n\n"
        "📌 Опишите проблему подробно.",
        parse_mode="Markdown"
    )
    await state.set_state(ApplicationState.waiting_support)

# ===== ПОЛУЧЕНИЕ СООБЩЕНИЯ =====
@dp.message(StateFilter(ApplicationState.waiting_support))
async def get_support_message(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_name = message.from_user.full_name
    username = message.from_user.username or "Нет username"
    
    file_id = None
    file_type = None
    msg_text = ""
    
    if message.text:
        msg_text = message.text
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
        msg_text = message.caption or "📷 Фото"
    elif message.video:
        file_id = message.video.file_id
        file_type = "video"
        msg_text = message.caption or "🎥 Видео"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"
        msg_text = message.caption or f"📄 Документ: {message.document.file_name}"
    elif message.audio:
        file_id = message.audio.file_id
        file_type = "audio"
        msg_text = message.caption or f"🎵 Аудио: {message.audio.file_name}"
    elif message.voice:
        file_id = message.voice.file_id
        file_type = "voice"
        msg_text = message.caption or "🎤 Голосовое сообщение"
    elif message.animation:
        file_id = message.animation.file_id
        file_type = "animation"
        msg_text = message.caption or "🔄 GIF"
    else:
        await message.answer("❌ Пожалуйста, отправьте текст или медиафайл.")
        return
    
    set_cooldown(user_id, "support")
    add_support_message(user_id, username, user_name, msg_text, file_id, file_type)
    
    await message.answer(
        "✅ *Ваше сообщение отправлено!*\n\n"
        "Мы свяжемся с вами в ближайшее время.\n"
        "⏳ Следующее сообщение можно отправить через 5 минут.",
        parse_mode="Markdown"
    )
    
    safe_username = username.replace('_', '\\_')
    admin_text = (
        f"🆘 *НОВОЕ СООБЩЕНИЕ В ТЕХПОДДЕРЖКУ!*\n\n"
        f"👤 Имя: {user_name}\n"
        f"🆔 ID: `{user_id}`\n"
        f"📱 Telegram: @{safe_username}\n"
        f"🕐 Время: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        f"📝 *Сообщение:*\n{msg_text}"
    )
    
    if file_type:
        admin_text += f"\n\n📎 Прикреплён файл: {file_type}"
    
    support_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ответить", callback_data=f"support_reply_{user_id}")],
        [InlineKeyboardButton(text="✅ Закрыть обращение", callback_data=f"support_close_{user_id}")]
    ])
    
    for admin_id in ADMIN_IDS:
        try:
            if file_id and file_type:
                if file_type == "photo":
                    await bot.send_photo(chat_id=admin_id, photo=file_id, caption=admin_text, parse_mode="Markdown", reply_markup=support_kb)
                elif file_type == "video":
                    await bot.send_video(chat_id=admin_id, video=file_id, caption=admin_text, parse_mode="Markdown", reply_markup=support_kb)
                elif file_type == "document":
                    await bot.send_document(chat_id=admin_id, document=file_id, caption=admin_text, parse_mode="Markdown", reply_markup=support_kb)
                elif file_type == "audio":
                    await bot.send_audio(chat_id=admin_id, audio=file_id, caption=admin_text, parse_mode="Markdown", reply_markup=support_kb)
                elif file_type == "voice":
                    await bot.send_voice(chat_id=admin_id, voice=file_id, caption=admin_text, parse_mode="Markdown", reply_markup=support_kb)
                elif file_type == "animation":
                    await bot.send_animation(chat_id=admin_id, animation=file_id, caption=admin_text, parse_mode="Markdown", reply_markup=support_kb)
            else:
                await bot.send_message(chat_id=admin_id, text=admin_text, parse_mode="Markdown", reply_markup=support_kb)
        except:
            pass
    
    await state.clear()

# ===== ОТВЕТ АДМИНА =====
@dp.callback_query(F.data.startswith("support_reply_"))
async def support_reply(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("⛔ Только для администратора!", show_alert=True)
        return
    
    target_user_id = int(callback.data.split("_")[2])
    
    await callback.message.answer(
        f"💬 Напишите ответ для пользователя `{target_user_id}`:\n"
        "Вы также можете прикрепить файл (фото, видео, документ).",
        parse_mode="Markdown"
    )
    await state.update_data(reply_user=target_user_id)
    await state.set_state(ApplicationState.waiting_support_reply)
    await callback.answer()

@dp.message(StateFilter(ApplicationState.waiting_support_reply))
async def send_support_reply(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    
    data = await state.get_data()
    target_user_id = data.get('reply_user')
    reply_text = message.text or message.caption or "📎 Файл от администратора"
    admin_name = message.from_user.full_name
    
    try:
        if message.photo:
            await bot.send_photo(chat_id=target_user_id, photo=message.photo[-1].file_id, caption=f"💬 *Ответ от администратора ({admin_name}):*\n\n{reply_text}", parse_mode="Markdown")
        elif message.video:
            await bot.send_video(chat_id=target_user_id, video=message.video.file_id, caption=f"💬 *Ответ от администратора ({admin_name}):*\n\n{reply_text}", parse_mode="Markdown")
        elif message.document:
            await bot.send_document(chat_id=target_user_id, document=message.document.file_id, caption=f"💬 *Ответ от администратора ({admin_name}):*\n\n{reply_text}", parse_mode="Markdown")
        elif message.audio:
            await bot.send_audio(chat_id=target_user_id, audio=message.audio.file_id, caption=f"💬 *Ответ от администратора ({admin_name}):*\n\n{reply_text}", parse_mode="Markdown")
        elif message.voice:
            await bot.send_voice(chat_id=target_user_id, voice=message.voice.file_id, caption=f"💬 *Ответ от администратора ({admin_name})*", parse_mode="Markdown")
        else:
            await bot.send_message(chat_id=target_user_id, text=f"💬 *Ответ от администратора ({admin_name}):*\n\n{message.text}", parse_mode="Markdown")
        
        await message.answer(f"✅ Ответ отправлен пользователю `{target_user_id}`")
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('UPDATE support_messages SET status = "answered" WHERE user_id = ? AND status = "pending"', (target_user_id,))
        conn.commit()
        conn.close()
        
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
    
    await state.clear()

# ===== ЗАКРЫТИЕ ОБРАЩЕНИЯ (БЕЗ ДУБЛИРОВАНИЯ) =====
@dp.callback_query(F.data.startswith("support_close_"))
async def support_close(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    if not is_admin(user_id):
        await callback.answer("⛔ Только для администратора!", show_alert=True)
        return
    
    target_user_id = int(callback.data.split("_")[2])
    admin_name = callback.from_user.full_name
    
    # Проверяем, есть ли активное обращение
    support_msg = get_support_message_by_user(target_user_id)
    if not support_msg:
        await callback.answer("❌ Обращение уже закрыто.", show_alert=True)
        try:
            new_text = callback.message.text + "\n\n⚠️ Обращение уже закрыто."
            await callback.message.edit_text(new_text)
            await callback.message.edit_reply_markup(reply_markup=None)
        except:
            pass
        return
    
    msg_id = support_msg[0]
    
    # Обновляем статус в БД
    update_support_status(msg_id, "closed")
    
    # Уведомляем пользователя
    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=f"❌ *Обращение в техподдержку закрыто*\n\n"
                 f"Администратор *{admin_name}* закрыл ваше обращение.\n\n"
                 f"Если у вас остались вопросы, вы можете создать новое обращение через кнопку 🆘 Техподдержка.",
            parse_mode="Markdown"
        )
    except:
        pass
    
    # Уведомляем второго админа
    for admin_id in ADMIN_IDS:
        if admin_id != callback.from_user.id:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"❌ *Обращение закрыто администратором {admin_name}*\n"
                         f"👤 Пользователь: `{target_user_id}`\n"
                         f"🕐 Время: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                    parse_mode="Markdown"
                )
            except:
                pass
    
    # Обновляем сообщение у админа (убираем кнопки)
    try:
        new_text = callback.message.text + f"\n\n✅ ЗАКРЫТО администратором {admin_name}"
        await callback.message.edit_text(new_text, parse_mode="Markdown")
        await callback.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    
    await callback.answer("✅ Обращение закрыто")

# ===== КОМАНДА /BROADCAST =====
@dp.message(Command("broadcast"))
async def broadcast_command(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора!")
        return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("❌ Вы не ввели текст для рассылки!\n\n📢 Использование:\n`/broadcast Ваше сообщение`", parse_mode="Markdown")
        return
    users = get_all_users()
    total_users = len(users)
    if total_users == 0:
        await message.answer("❌ Нет пользователей для рассылки.")
        return
    await message.answer(f"📢 Начинаю рассылку для {total_users} пользователей...")
    success = 0
    failed = 0
    for user_id in users:
        try:
            await bot.send_message(chat_id=user_id, text=f"📢 *Сообщение от администратора:*\n\n{text}", parse_mode="Markdown")
            success += 1
            await asyncio.sleep(0.05)
        except:
            failed += 1
    await message.answer(f"✅ Рассылка завершена!\n📤 Отправлено: {success}\n❌ Не доставлено: {failed}\n👥 Всего: {total_users}")

# ===== РАССЫЛКА ЧЕРЕЗ МЕНЮ =====
@dp.message(StateFilter(ApplicationState.waiting_broadcast))
async def broadcast_from_menu(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = message.text
    users = get_all_users()
    total_users = len(users)
    if total_users == 0:
        await message.answer("❌ Нет пользователей для рассылки.")
        await state.clear()
        return
    await message.answer(f"📢 Начинаю рассылку для {total_users} пользователей...")
    success = 0
    failed = 0
    for user_id in users:
        try:
            await bot.send_message(chat_id=user_id, text=f"📢 *Сообщение от администратора:*\n\n{text}", parse_mode="Markdown")
            success += 1
            await asyncio.sleep(0.05)
        except:
            failed += 1
    await message.answer(f"✅ Рассылка завершена!\n📤 Отправлено: {success}\n❌ Не доставлено: {failed}\n👥 Всего: {total_users}")
    await state.clear()

# ===== ОБРАБОТКА КНОПОК АДМИН МЕНЮ =====
@dp.callback_query(F.data.startswith("admin_"))
async def admin_menu_actions(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    
    if not is_admin(user_id):
        await callback.answer("⛔ Только для администратора!", show_alert=True)
        return
    
    action = callback.data
    
    if action == "admin_applications":
        await show_applications_command(callback.message)
        await callback.answer()
    elif action == "admin_stats":
        await admin_stats_command(callback.message)
        await callback.answer()
    elif action == "admin_broadcast":
        await callback.message.answer("📢 Введите текст для рассылки:")
        await state.set_state(ApplicationState.waiting_broadcast)
        await callback.answer()
    elif action == "admin_blacklist":
        await callback.message.answer(
            "📝 *Управление ЧС*\n\n"
            "Команды:\n"
            "`/unblock [ID]` - разблокировать по ID\n"
            "`/ban @username` - забанить по юзернейму\n"
            "`/blacklist` - список заблокированных\n"
            "`/unmute [ID]` - снять мут",
            parse_mode="Markdown"
        )
        await callback.answer()
    elif action == "admin_support":
        await show_support_messages(callback.message)
        await callback.answer()
    elif action == "admin_users":
        await show_users_list(callback.message)
        await callback.answer()
    elif action == "admin_close":
        await callback.message.delete()
        await callback.answer("Меню закрыто")

# ===== ПОКАЗАТЬ СООБЩЕНИЯ В ТЕХПОДДЕРЖКУ =====
async def show_support_messages(message: types.Message):
    messages = get_support_messages()
    if not messages:
        await message.answer("🆘 Нет новых сообщений в техподдержку.")
        return
    
    text = "🆘 *СООБЩЕНИЯ В ТЕХПОДДЕРЖКУ*\n═══════════════════\n\n"
    for msg in messages[:10]:
        msg_id, user_id, username, user_name, msg_text, file_id, file_type, date = msg
        safe_username = username.replace('_', '\\_') if username else "Нет username"
        text += f"👤 {user_name}\n"
        text += f"🆔 `{user_id}`\n"
        text += f"📱 @{safe_username}\n"
        text += f"🕐 {date}\n"
        text += f"📝 {msg_text[:50]}{'...' if len(msg_text) > 50 else ''}\n"
        if file_type:
            text += f"📎 {file_type}\n"
        text += "─────────────\n"
    
    await message.answer(text, parse_mode="Markdown")

# ===== ОТПРАВКА ЗАЯВКИ =====
@dp.message(F.text == "📝 Отправить заявку")
async def apply_button_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if is_in_blacklist(user_id):
        await message.answer("⛔ Вы заблокированы.")
        return
    if is_muted(user_id):
        await message.answer("🔇 Вы замучены.")
        return
    
    can_apply, remaining = check_cooldown(user_id, "application")
    if not can_apply:
        time_str = format_cooldown_time(remaining)
        await message.answer(
            f"⏳ *Подождите!*\n\n"
            f"Вы слишком часто отправляете заявки.\n"
            f"Попробуйте снова через *{time_str}*.",
            parse_mode="Markdown"
        )
        return
    
    await message.answer("✏️ Напишите ваш юзернейм в Airgram.\nПример: *@username* или просто *username*", parse_mode="Markdown")
    await state.set_state(ApplicationState.waiting_for_username)

# ===== ПОЛУЧЕНИЕ ЮЗЕРНЕЙМА =====
@dp.message(StateFilter(ApplicationState.waiting_for_username))
async def get_username(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    airgram_username = message.text.strip()
    
    if len(airgram_username) < 2:
        await message.answer("❌ Слишком короткое имя. Попробуйте снова.")
        return
    
    set_cooldown(user_id, "application")
    add_application(user_id, airgram_username, message.from_user.full_name)
    
    await message.answer(
        f"✅ *Ваша заявка принята!*\n"
        f"Юзернейм Airgram: *{airgram_username}*\n\n"
        "Ожидайте, администратор рассмотрит её.\n"
        "⏳ Следующую заявку можно отправить через 5 минут.",
        parse_mode="Markdown"
    )
    
    tg_username = message.from_user.username or "Нет username"
    safe_tg_username = tg_username.replace('_', '\\_') if tg_username != "Нет username" else "Нет username"
    safe_airgram_username = airgram_username.replace('_', '\\_')
    
    admin_text = (
        f"📩 *НОВАЯ ЗАЯВКА!*\n\n"
        f"👤 Имя: {message.from_user.full_name}\n"
        f"🆔 ID: `{user_id}`\n"
        f"📱 Telegram: @{safe_tg_username}\n"
        f"📱 Airgram: @{safe_airgram_username}\n"
        f"🕐 Время: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{user_id}"), 
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}")],
        [InlineKeyboardButton(text="🔇 Мут (1 час)", callback_data=f"mute_{user_id}"), 
         InlineKeyboardButton(text="⛔ В ЧС", callback_data=f"block_{user_id}")]
    ])
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                parse_mode="Markdown",
                reply_markup=admin_kb
            )
        except:
            pass
    
    await state.clear()

# ===== МОИ ЗАЯВКИ =====
@dp.message(F.text == "📋 Мои заявки")
async def my_applications(message: types.Message):
    user_id = message.from_user.id
    if is_in_blacklist(user_id):
        await message.answer("⛔ Вы заблокированы.")
        return
    apps = get_user_applications(user_id)
    if not apps:
        await message.answer("📭 У вас нет заявок.")
        return
    text = "📋 *МОИ ЗАЯВКИ*\n═══════════════\n\n"
    for date, username, status in apps[:5]:
        status_emoji = {'pending': '⏳ Ожидает', 'accepted': '✅ Принята', 'rejected': '❌ Отклонена'}.get(status, '❓ Неизвестно')
        safe_username = username.replace('_', '\\_')
        text += f"📱 @{safe_username}\n"
        text += f"📊 Статус: {status_emoji}\n"
        text += f"🕐 {date}\n"
        text += "─────────────\n"
    await message.answer(text, parse_mode="Markdown")

# ===== СТАТИСТИКА ПОЛЬЗОВАТЕЛЯ =====
@dp.message(F.text == "📊 Моя статистика")
async def my_stats(message: types.Message):
    user_id = message.from_user.id
    if is_in_blacklist(user_id):
        await message.answer("⛔ Вы заблокированы.")
        return
    stats = get_user_stats(user_id)
    if stats:
        name, total, accepted = stats
        text = f"📊 *Моя статистика*\n═══════════════\n\n👤 Имя: {name}\n📝 Всего заявок: {total}\n✅ Принято: {accepted}\n📊 Успешность: {round((accepted/total)*100) if total > 0 else 0}%"
    else:
        text = "📊 У вас пока нет заявок."
    await message.answer(text, parse_mode="Markdown")

# ===== ОБРАБОТКА ДЕЙСТВИЙ С ЗАЯВКАМИ =====
@dp.callback_query()
async def admin_actions(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    if not is_admin(user_id):
        await callback.answer("⛔ Только для администратора!", show_alert=True)
        return
    
    data = callback.data
    
    if data.startswith("admin_") or data.startswith("support_"):
        return
    
    target_user_id = int(data.split("_")[1])
    action = data.split("_")[0]
    
    app = get_application(target_user_id)
    if not app:
        await callback.answer("❌ Заявка уже обработана.", show_alert=True)
        await callback.message.edit_text("⚠️ Заявка уже обработана.")
        return
    
    airgram_username = app[1]
    user_name = app[2]
    safe_airgram = airgram_username.replace('_', '\\_')
    
    if action == "accept":
        update_application_status(target_user_id, 'accepted')
        await callback.answer("✅ Заявка принята!")
        await bot.send_message(
            chat_id=target_user_id,
            text=f"🎉 *Поздравляем, {user_name}!*\n\nВаша заявка на *{safe_airgram}* ОДОБРЕНА! ✅\n\n🎁 Подарок будет отправлен в ближайшее время.",
            parse_mode="Markdown"
        )
        await callback.message.edit_text(f"✅ ПРИНЯТО: @{safe_airgram} от {user_name}")
        
    elif action == "reject":
        update_application_status(target_user_id, 'rejected')
        await callback.answer("❌ Заявка отклонена")
        await bot.send_message(
            chat_id=target_user_id,
            text=f"😔 *Уважаемый(ая) {user_name}*\n\nЗаявка на *{safe_airgram}* ОТКЛОНЕНА.\n\nПопробуйте отправить другую.",
            parse_mode="Markdown"
        )
        await callback.message.edit_text(f"❌ ОТКЛОНЕНО: @{safe_airgram} от {user_name}")
        
    elif action == "mute":
        await callback.answer("🔇 Пользователь замучен на 1 час")
        mute_until = datetime.now() + timedelta(hours=1)
        add_mute(target_user_id, mute_until.timestamp())
        await bot.send_message(
            chat_id=target_user_id,
            text=f"🔇 *ВНИМАНИЕ!*\n\nАдминистратор замутил вас на 1 час.\n⏰ Разблокировка: {mute_until.strftime('%H:%M')}",
            parse_mode="Markdown"
        )
        update_application_status(target_user_id, 'rejected')
        await callback.message.edit_text(f"🔇 ЗАМУЧЕН: @{safe_airgram}")
        
    elif action == "block":
        await callback.answer("⛔ Пользователь в ЧС")
        add_to_blacklist(target_user_id)
        await bot.send_message(
            chat_id=target_user_id,
            text="⛔ *БЛОКИРОВКА!*\n\nВы заблокированы в боте.",
            parse_mode="Markdown"
        )
        update_application_status(target_user_id, 'rejected')
        await callback.message.edit_text(f"⛔ В ЧС: @{safe_airgram}")
    
    await callback.answer()

# ===== АДМИН КОМАНДЫ =====
@dp.message(Command("applications"))
async def show_applications_command(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора!")
        return
    apps = get_applications()
    if not apps:
        await message.answer("📭 Нет активных заявок.")
        return
    text = "📋 *СПИСОК ЗАЯВОК:*\n═══════════════\n\n"
    for app in apps:
        user_id, username, user_name, date = app
        safe_username = username.replace('_', '\\_')
        text += f"👤 {user_name}\n"
        text += f"🆔 `{user_id}`\n"
        text += f"📱 @{safe_username}\n"
        text += f"🕐 {date}\n"
        text += "─────────────\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("stats"))
async def admin_stats_command(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора!")
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM applications WHERE status = "pending"')
    pending = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM applications WHERE status = "accepted"')
    accepted = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM applications WHERE status = "rejected"')
    rejected = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM blacklist')
    blocked = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM muted_users')
    muted = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM support_messages WHERE status = "pending"')
    support_pending = cursor.fetchone()[0]
    conn.close()
    stats_text = f"📊 *ПОЛНАЯ СТАТИСТИКА*\n═══════════════════\n\n👥 Всего пользователей: {total_users}\n⏳ В очереди: {pending}\n✅ Принято: {accepted}\n❌ Отклонено: {rejected}\n⛔ В ЧС: {blocked}\n🔇 Замучено: {muted}\n🆘 В техподдержке: {support_pending}"
    await message.answer(stats_text, parse_mode="Markdown")

@dp.message(Command("userinfo"))
async def user_info(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора!")
        return
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("❌ Использование: /userinfo [ID]")
            return
        target_user_id = int(args[1])
        stats = get_user_stats(target_user_id)
        apps = get_user_applications(target_user_id)
        if not stats:
            await message.answer(f"❌ Пользователь {target_user_id} не найден.")
            return
        name, total, accepted = stats
        await message.answer(
            f"👤 *ИНФОРМАЦИЯ О ПОЛЬЗОВАТЕЛЕ*\n"
            f"═══════════════════════\n\n"
            f"🆔 ID: `{target_user_id}`\n"
            f"📝 Имя: {name}\n"
            f"📊 Всего заявок: {total}\n"
            f"✅ Принято: {accepted}\n"
            f"📊 Успешность: {round((accepted/total)*100) if total > 0 else 0}%\n"
            f"📋 Заявок: {len(apps)}",
            parse_mode="Markdown"
        )
    except:
        await message.answer("❌ Ошибка! Используйте: /userinfo [ID]")

@dp.message(Command("unmute"))
async def unmute_command(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора!")
        return
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("❌ Использование: /unmute [user_id]")
            return
        target_user_id = int(args[1])
        if is_muted(target_user_id):
            remove_mute(target_user_id)
            await message.answer(f"✅ Пользователь {target_user_id} размучен.")
            await bot.send_message(chat_id=target_user_id, text="🔊 Администратор снял с вас мут.")
        else:
            await message.answer("❌ Пользователь не в муте.")
    except:
        await message.answer("❌ Ошибка!")

@dp.message(Command("unblock"))
async def unblock_command(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора!")
        return
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("❌ Использование: /unblock [user_id]")
            return
        target_user_id = int(args[1])
        if is_in_blacklist(target_user_id):
            remove_from_blacklist(target_user_id)
            await message.answer(f"✅ Пользователь {target_user_id} разблокирован.")
            await bot.send_message(chat_id=target_user_id, text="🔓 Администратор разблокировал вас.")
        else:
            await message.answer("❌ Пользователь не в ЧС.")
    except:
        await message.answer("❌ Ошибка!")

@dp.message(Command("blacklist"))
async def show_blacklist(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора!")
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, reason, date FROM blacklist')
    users = cursor.fetchall()
    conn.close()
    if not users:
        await message.answer("📭 ЧС пуст.")
        return
    text = "⛔ *ЧЕРНЫЙ СПИСОК*\n═════════════\n\n"
    for user_id, reason, date in users:
        text += f"🆔 `{user_id}`\n"
        text += f"📝 {reason}\n"
        text += f"🕐 {date}\n"
        text += "─────────────\n"
    await message.answer(text, parse_mode="Markdown")


# ===== ЛОГИКА ЗАПУСКА ЧЕРЕЗ ВЕБХУК (ДЛЯ RENDER) =====

async def on_startup(bot: Bot):
    # Устанавливаем адрес вебхука на серверах Telegram
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        print(f"🚀 Вебхук успешно установлен на: {WEBHOOK_URL}")
    else:
        print("⚠️ WEBHOOK_URL не настроен! (Возможно, запуск локально на ПК)")

def main():
    # Создаем aiohttp веб-сервер
    app = web.Application()

    # Настраиваем обработчик запросов aiogram для пути /webhook
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    # Привязываем функцию on_startup к жизненному циклу aiogram
    dp.startup.register(on_startup)
    setup_application(app, dp, bot=bot)

    # Логируем текущее состояние бота перед запуском
    print("🤖 Бот AirgramBot запущен через Вебхук!")
    print(f"👑 Админы: {ADMIN_IDS}")
    print(f"⏳ КД на заявки: {COOLDOWN_APPLICATION} сек (5 мин)")
    print(f"⏳ КД на техподдержку: {COOLDOWN_SUPPORT} сек (5 мин)")
    print(f"👥 Всего пользователей: {get_user_count()}")
    print(f"📊 В очереди: {len(get_applications())}")
    print(f"🆘 В техподдержке: {len(get_support_messages())}")
    print("=" * 40)

    # Запускаем приложение на нужном порту
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()