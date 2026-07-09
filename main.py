import asyncio
import logging
import aiosqlite  # Заменили синхронный sqlite3 на асинхронный
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# Загружаем переменные из .env
load_dotenv()

# ===== НАСТРОЙКИ ХОСТИНГА И ТОКЕНЫ =====
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL") 
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}" if RENDER_URL else None
PORT = int(os.getenv("PORT", 10000))
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    exit("❌ Ошибка: Переменная окружения BOT_TOKEN не задана!")

# ===== АДМИНЫ (теперь можно читать из .env строкой через запятую или использовать дефолт) =====
ADMINS_RAW = os.getenv("ADMIN_IDS", "6241802278,1195470560")
ADMIN_IDS = [int(x.strip()) for x in ADMINS_RAW.split(",") if x.strip().isdigit()]

# ===== НАСТРОЙКИ КД =====
COOLDOWN_APPLICATION = 300  
COOLDOWN_SUPPORT = 300      

# ===== ИНИЦИАЛИЗАЦИЯ =====
logging.basicConfig(level=logging.INFO)
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# ===== ХРАНИЛИЩЕ КД =====
user_cooldowns = {"application": {}, "support": {}}
DB_PATH = 'airgram_bot.db'

# ===== АСИНХРОННАЯ БАЗА ДАННЫХ =====
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, user_name TEXT, date TEXT, status TEXT DEFAULT 'pending')''')
        await db.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, full_name TEXT, username TEXT, first_visit TEXT, last_visit TEXT, total_applications INTEGER DEFAULT 0, accepted_applications INTEGER DEFAULT 0)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS muted_users (user_id INTEGER PRIMARY KEY, until_timestamp INTEGER)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS blacklist (user_id INTEGER PRIMARY KEY, reason TEXT, date TEXT)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, value INTEGER DEFAULT 0)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS support_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, user_name TEXT, message TEXT, file_id TEXT, file_type TEXT, date TEXT, status TEXT DEFAULT 'pending')''')
        await db.execute('INSERT OR IGNORE INTO stats (key, value) VALUES ("accepted_count", 0)')
        
        # Сразу проведем миграции (апдейт базы) безопасности ради
        try: await db.execute('ALTER TABLE support_messages ADD COLUMN file_id TEXT')
        except: pass
        try: await db.execute('ALTER TABLE support_messages ADD COLUMN file_type TEXT')
        except: pass
        try: await db.execute('ALTER TABLE applications ADD COLUMN closed_by TEXT')
        except: pass
        await db.commit()

# ===== ФУНКЦИИ КД =====
def check_cooldown(user_id: int, cooldown_type: str) -> tuple:
    if user_id in ADMIN_IDS:
        return True, 0
    cooldowns = user_cooldowns.get(cooldown_type, {})
    last_use = cooldowns.get(user_id, 0)
    current_time = datetime.now().timestamp()
    
    cooldown_seconds = COOLDOWN_APPLICATION if cooldown_type == "application" else COOLDOWN_SUPPORT
    
    if current_time - last_use < cooldown_seconds:
        remaining = int(cooldown_seconds - (current_time - last_use))
        return False, remaining
    return True, 0

def set_cooldown(user_id: int, cooldown_type: str):
    if user_id in ADMIN_IDS:
        return
    user_cooldowns[cooldown_type][user_id] = datetime.now().timestamp()

def format_cooldown_time(seconds: int) -> str:
    minutes = seconds // 60
    seconds_remain = seconds % 60
    return f"{minutes} мин {seconds_remain} сек" if minutes > 0 else f"{seconds_remain} сек"

# ===== АСИНХРОННЫЕ ФУНКЦИИ БД =====
async def get_accepted_count():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT value FROM stats WHERE key = "accepted_count"') as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def add_application(user_id, username, user_name):
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT INTO applications (user_id, username, user_name, date) VALUES (?, ?, ?, ?)', (user_id, username, user_name, now_str))
        await db.execute('INSERT INTO users (user_id, full_name, username, first_visit, last_visit, total_applications) VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(user_id) DO UPDATE SET last_visit = ?, total_applications = total_applications + 1', (user_id, user_name, username, now_str, now_str, now_str))
        await db.commit()

async def get_applications():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id, username, user_name, date FROM applications WHERE status = "pending" ORDER BY id DESC') as cursor:
            return await cursor.fetchall()

async def get_application(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id, username, user_name, date FROM applications WHERE user_id = ? AND status = "pending" ORDER BY id DESC LIMIT 1', (user_id,)) as cursor:
            return await cursor.fetchone()

async def get_user_applications(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT date, username, status FROM applications WHERE user_id = ? ORDER BY id DESC LIMIT 10', (user_id,)) as cursor:
            return await cursor.fetchall()

async def update_application_status(user_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE applications SET status = ? WHERE user_id = ? AND status = "pending"', (status, user_id))
        if status == 'accepted':
            await db.execute('UPDATE users SET accepted_applications = accepted_applications + 1 WHERE user_id = ?', (user_id,))
            await db.execute('UPDATE stats SET value = value + 1 WHERE key = "accepted_count"')
        await db.commit()

async def add_to_blacklist(user_id, reason="Нарушение правил"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT OR REPLACE INTO blacklist (user_id, reason, date) VALUES (?, ?, ?)', (user_id, reason, datetime.now().strftime("%d.%m.%Y %H:%M")))
        await db.commit()

async def remove_from_blacklist(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM blacklist WHERE user_id = ?', (user_id,))
        await db.commit()

async def is_in_blacklist(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id FROM blacklist WHERE user_id = ?', (user_id,)) as cursor:
            return await cursor.fetchone() is not None

async def add_mute(user_id, until_timestamp):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT OR REPLACE INTO muted_users (user_id, until_timestamp) VALUES (?, ?)', (user_id, until_timestamp))
        await db.commit()

async def remove_mute(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM muted_users WHERE user_id = ?', (user_id,))
        await db.commit()

async def is_muted(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT until_timestamp FROM muted_users WHERE user_id = ?', (user_id,)) as cursor:
            result = await cursor.fetchone()
            if not result:
                return False
            if result[0] < datetime.now().timestamp():
                await remove_mute(user_id)
                return False
            return True

async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id FROM users') as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

async def get_all_users_full():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id, full_name, username FROM users ORDER BY user_id DESC') as cursor:
            return await cursor.fetchall()

async def add_user_to_db(user_id, full_name, username):
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT OR IGNORE INTO users (user_id, full_name, username, first_visit, last_visit, total_applications) VALUES (?, ?, ?, ?, ?, 0)', (user_id, full_name, username, now_str, now_str))
        await db.commit()

async def get_user_stats(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT full_name, total_applications, accepted_applications FROM users WHERE user_id = ?', (user_id,)) as cursor:
            return await cursor.fetchone()

async def get_user_count():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT COUNT(*) FROM users') as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_user_id_by_username(username):
    username = username.replace('@', '').strip()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id FROM users WHERE username = ?', (username,)) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else None

async def add_support_message(user_id, username, user_name, message, file_id=None, file_type=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT INTO support_messages (user_id, username, user_name, message, file_id, file_type, date) VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (user_id, username, user_name, message, file_id, file_type, datetime.now().strftime("%d.%m.%Y %H:%M")))
        await db.commit()

async def get_support_messages():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT id, user_id, username, user_name, message, file_id, file_type, date FROM support_messages WHERE status = "pending" ORDER BY id DESC') as cursor:
            return await cursor.fetchall()

async def get_support_message_by_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT id, user_id, username, user_name, message, file_id, file_type, date FROM support_messages WHERE user_id = ? AND status = "pending" ORDER BY id DESC LIMIT 1', (user_id,)) as cursor:
            return await cursor.fetchone()

async def update_support_status(msg_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE support_messages SET status = ? WHERE id = ?', (status, msg_id))
        await db.commit()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ===== КНОПКИ БОТА =====
apply_button = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📝 Отправить заявку")],
    [KeyboardButton(text="📊 Моя статистика"), KeyboardButton(text="📋 Мои заявки")],
    [KeyboardButton(text="🆘 Техподдержка")]
], resize_keyboard=True)

admin_menu = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📋 Все заявки", callback_data="admin_applications"), InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
    [InlineKeyboardButton(text="📢 Создать рассылку", callback_data="admin_broadcast")],
    [InlineKeyboardButton(text="📝 Управление ЧС", callback_data="admin_blacklist"), InlineKeyboardButton(text="🆘 Техподдержка", callback_data="admin_support")],
    [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
    [InlineKeyboardButton(text="🔙 Закрыть меню", callback_data="admin_close")]
])

# ===== СОСТОЯНИЯ =====
class ApplicationState(StatesGroup):
    waiting_for_username = State()
    waiting_broadcast = State()
    waiting_support = State()
    waiting_support_reply = State()

# ===== КОМАНДА /START (НОВЫЙ ДИЗАЙН ЧЕРЕЗ HTML) =====
@dp.message(Command("start"))
async def start_command(message: types.Message):
    user_id = message.from_user.id
    await add_user_to_db(user_id, message.from_user.full_name, message.from_user.username or "Нет username")
    
    if await is_in_blacklist(user_id):
        await message.answer("⛔ <b>Вы заблокированы в этом боте.</b>", parse_mode="HTML")
        return
    if await is_muted(user_id):
        await message.answer("🔇 <b>Вы замучены.</b> Дождитесь снятия ограничения.", parse_mode="HTML")
        return
        
    accepted_count = await get_accepted_count()
    pending_apps = await get_applications()
    pending_count = len(pending_apps)
    total_users = await get_user_count()
    
    welcome_text = (
        "🌟 <b>Добро пожаловать в AirgramBot! донат, юз в аир: @botair</b>\n"
        "───────────────────────────\n"
        "📱 Пожалуйста, отправьте нам свой юзернейм в мессенджере <b>Airgram</b>.\n"
        "🎁 После успешной проверки вы получите подарок!\n\n"
        "📊 <b>Наша статистика:</b>\n"
        "• Принято заявок: <code>{accepted}</code>\n"
        "• В очереди обработки: <code>{pending}</code>\n"
        "• Всего пользователей: <code>{users}</code>"
    ).format(accepted=accepted_count, pending=pending_count, users=total_users)
    
    await message.answer(welcome_text, reply_markup=apply_button, parse_mode="HTML")

# ===== АДМИН-ПАНЕЛЬ =====
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к админ-панели!")
        return
    
    support_messages = await get_support_messages()
    support_count = len(support_messages)
    user_count = await get_user_count()
    
    admin_text = (
        "👑 <b>ПАНЕЛЬ АДМИНИСТРАТОРА</b>\n"
        "───────────────────────────\n"
        "• Ипользуйте кнопки ниже для управления.\n"
        "• Новых обращений в саппорт: <code>{support}</code>\n"
        "• Всего пользователей в БД: <code>{users}</code>"
    ).format(support=support_count, users=user_count)
    await message.answer(admin_text, parse_mode="HTML", reply_markup=admin_menu)

# ===== КОМАНДА /COME =====
@dp.message(Command("come"))
async def admin_commands_list(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора!")
        return
    commands_text = (
        "📋 <b>СПИСОК КОМАНД АДМИНИСТРАТОРА</b>\n"
        "───────────────────────────\n"
        "👑 <code>/admin</code> — Открыть админ-панель\n"
        "📋 <code>/applications</code> — Показать текущие заявки\n"
        "📊 <code>/stats</code> — Полная статистика бота\n"
        "📢 <code>/broadcast [текст]</code> — Быстрая рассылка\n"
        "🔇 <code>/unmute [ID]</code> — Снять мут с пользователя\n"
        "🔓 <code>/unblock [ID]</code> — Разблокировать пользователя\n"
        "⛔ <code>/ban @username</code> — Забанить по юзернейму\n"
        "⛔ <code>/blacklist</code> — Показать черный список\n"
        "ℹ️ <code>/userinfo [ID]</code> — Инфо о пользователе\n"
        "👥 <code>/users</code> — Список пользователей\n"
        "❓ <code>/come</code> — Показать эту справку"
    )
    await message.answer(commands_text, parse_mode="HTML")

# ===== БАН ПО ЮЗЕРНЕЙМУ =====
@dp.message(Command("ban"))
async def ban_by_username(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора!")
        return
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Использование: <code>/ban @username</code>", parse_mode="HTML")
        return
    
    username = args[1].replace('@', '').strip()
    target_user_id = await get_user_id_by_username(username)
    
    if not target_user_id:
        await message.answer(f"❌ Пользователь @{username} не найден в базе данных.")
        return
    
    if target_user_id in ADMIN_IDS:
        await message.answer("❌ Нельзя забанить администратора!")
        return
    
    await add_to_blacklist(target_user_id, "Забанен администратором")
    await message.answer(f"✅ Пользователь @{username} (<code>{target_user_id}</code>) успешно забанен!")
    
    try:
        await bot.send_message(chat_id=target_user_id, text="⛔ <b>ВЫ ЗАБАНЕНЫ!</b>\n\nАдминистратор ограничил вам доступ к боту.", parse_mode="HTML")
    except: pass

# ===== СПИСОК ПОЛЬЗОВАТЕЛЕЙ =====
@dp.message(Command("users"))
async def users_list_command(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора!")
        return
    await show_users_list(message)

async def show_users_list(message: types.Message):
    users = await get_all_users_full()
    if not users:
        await message.answer("👥 Список пользователей пуст.")
        return
    
    text = "👥 <b>СПИСОК ПОЛЬЗОВАТЕЛЕЙ (Последние 20)</b>\n"
    text += "───────────────────────────\n"
    for user_id, full_name, username in users[:20]:
        username_str = f"@{username}" if username and username != "Нет username" else "Нет юзернейма"
        text += f"🆔 <code>{user_id}</code> | {full_name} ({username_str})\n"
    
    if len(users) > 20:
        text += f"\n<i>...и ещё {len(users) - 20} пользователей.</i>"
    
    await message.answer(text, parse_mode="HTML")

# ===== ТЕХПОДДЕРЖКА КЛИЕНТ =====
@dp.message(F.text == "🆘 Техподдержка")
async def support_button(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if await is_in_blacklist(user_id): return
    if await is_muted(user_id): return
    
    can_use, remaining = check_cooldown(user_id, "support")
    if not can_use:
        await message.answer(f"⏳ <b>Подождите!</b>\n\nВы слишком часто обращаетесь в поддержку. Попробуйте через <b>{format_cooldown_time(remaining)}</b>.", parse_mode="HTML")
        return
    
    await message.answer("🆘 <b>Техподдержка Airgram</b>\n\nОпишите вашу проблему в одном сообщении. Вы можете прикрепить к тексту фото, видео, файлы или голосовое сообщение.", parse_mode="HTML")
    await state.set_state(ApplicationState.waiting_support)

@dp.message(StateFilter(ApplicationState.waiting_support))
async def get_support_message(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_name = message.from_user.full_name
    username = message.from_user.username or "Нет username"
    
    file_id, file_type, msg_text = None, None, ""
    
    if message.text: msg_text = message.text
    elif message.photo: file_id, file_type, msg_text = message.photo[-1].file_id, "photo", message.caption or "📷 Фото"
    elif message.video: file_id, file_type, msg_text = message.video.file_id, "video", message.caption or "🎥 Видео"
    elif message.document: file_id, file_type, msg_text = message.document.file_id, "document", message.caption or "📄 Документ"
    elif message.audio: file_id, file_type, msg_text = message.audio.file_id, "audio", message.caption or "🎵 Аудио"
    elif message.voice: file_id, file_type, msg_text = message.voice.file_id, "voice", message.caption or "🎤 Голосовое сообщение"
    elif message.animation: file_id, file_type, msg_text = message.animation.file_id, "animation", message.caption or "🔄 GIF"
    else:
        await message.answer("❌ Отправьте текст или поддерживаемый медиафайл.")
        return
    
    set_cooldown(user_id, "support")
    await add_support_message(user_id, username, user_name, msg_text, file_id, file_type)
    
    await message.answer("✅ <b>Ваше сообщение доставлено!</b>\n\nКоманда поддержки рассмотрит его в ближайшее время.", parse_mode="HTML")
    
    admin_text = (
        "🆘 <b>НОВОЕ ОБРАЩЕНИЕ В ТЕХПОДДЕРЖКУ</b>\n"
        "───────────────────────────\n"
        "👤 От: <b>{name}</b>\n"
        "🆔 ID: <code>{user_id}</code> | TG: @{username}\n"
        "🕐 Время: {time}\n"
        "───────────────────────────\n"
        "📝 <b>Сообщение:</b>\n{text}"
    ).format(name=user_name, user_id=user_id, username=username, time=datetime.now().strftime('%d.%m.%Y %H:%M'), text=msg_text)
    
    support_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ответить", callback_data=f"support_reply_{user_id}")],
        [InlineKeyboardButton(text="✅ Закрыть обращение", callback_data=f"support_close_{user_id}")]
    ])
    
    for admin_id in ADMIN_IDS:
        try:
            if file_id and file_type:
                if file_type == "photo": await bot.send_photo(admin_id, file_id, caption=admin_text, parse_mode="HTML", reply_markup=support_kb)
                elif file_type == "video": await bot.send_video(admin_id, file_id, caption=admin_text, parse_mode="HTML", reply_markup=support_kb)
                elif file_type == "document": await bot.send_document(admin_id, file_id, caption=admin_text, parse_mode="HTML", reply_markup=support_kb)
                elif file_type == "audio": await bot.send_audio(admin_id, file_id, caption=admin_text, parse_mode="HTML", reply_markup=support_kb)
                elif file_type == "voice": await bot.send_voice(admin_id, file_id, caption=admin_text, parse_mode="HTML", reply_markup=support_kb)
                elif file_type == "animation": await bot.send_animation(admin_id, file_id, caption=admin_text, parse_mode="HTML", reply_markup=support_kb)
            else:
                await bot.send_message(admin_id, text=admin_text, parse_mode="HTML", reply_markup=support_kb)
        except: pass
    await state.clear()

# ===== ОТВЕТ АДМИНИСТРАТОРА СУППОРТ =====
@dp.callback_query(F.data.startswith("support_reply_"))
async def support_reply(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    target_user_id = int(callback.data.split("_")[2])
    await callback.message.answer(f"💬 Введите ответ для пользователя <code>{target_user_id}</code> (Текст или медиафайлы):", parse_mode="HTML")
    await state.update_data(reply_user=target_user_id)
    await state.set_state(ApplicationState.waiting_support_reply)
    await callback.answer()

@dp.message(StateFilter(ApplicationState.waiting_support_reply))
async def send_support_reply(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    
    data = await state.get_data()
    target_user_id = data.get('reply_user')
    reply_text = message.text or message.caption or "<i>Файл от техподдержки</i>"
    admin_name = message.from_user.full_name
    
    header_text = f"💬 <b>Ответ от техподдержки ({admin_name}):</b>\n\n{reply_text}"
    
    try:
        if message.photo: await bot.send_photo(target_user_id, message.photo[-1].file_id, caption=header_text, parse_mode="HTML")
        elif message.video: await bot.send_video(target_user_id, message.video.file_id, caption=header_text, parse_mode="HTML")
        elif message.document: await bot.send_document(target_user_id, message.document.file_id, caption=header_text, parse_mode="HTML")
        elif message.audio: await bot.send_audio(target_user_id, message.audio.file_id, caption=header_text, parse_mode="HTML")
        elif message.voice: await bot.send_voice(target_user_id, message.voice.file_id, caption=f"💬 <b>Голосовой ответ от техподдержки ({admin_name})</b>", parse_mode="HTML")
        else: await bot.send_message(target_user_id, text=f"💬 <b>Ответ от техподдержки ({admin_name}):</b>\n\n{message.text}", parse_mode="HTML")
        
        await message.answer(f"✅ Ответ успешно доставлен пользователю <code>{target_user_id}</code>", parse_mode="HTML")
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('UPDATE support_messages SET status = "answered" WHERE user_id = ? AND status = "pending"', (target_user_id,))
            await db.commit()
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить ответ: {str(e)}")
    await state.clear()

@dp.callback_query(F.data.startswith("support_close_"))
async def support_close(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id): return
    target_user_id = int(callback.data.split("_")[2])
    admin_name = callback.from_user.full_name
    
    support_msg = await get_support_message_by_user(target_user_id)
    if not support_msg:
        await callback.answer("❌ Обращение уже закрыто или не найдено.", show_alert=True)
        return
    
    await update_support_status(support_msg[0], "closed")
    
    try:
        await bot.send_message(target_user_id, "❌ <b>Ваше обращение в техподдержку было закрыто администратором.</b>", parse_mode="HTML")
    except: pass
    
    await callback.message.edit_text(callback.message.text + f"\n\n✅ <b>ЗАКРЫТО администратором {admin_name}</b>", parse_mode="HTML", reply_markup=None)
    await callback.answer("Обращение закрыто")

# ===== МЕНЮ ЗАЯВОК (КЛИЕНТ) =====
@dp.message(F.text == "📝 Отправить заявку")
async def apply_button_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if await is_in_blacklist(user_id) or await is_muted(user_id): return
    
    can_apply, remaining = check_cooldown(user_id, "application")
    if not can_apply:
        await message.answer(f"⏳ <b>Подождите!</b>\n\nВы сможете отправить следующую заявку через <b>{format_cooldown_time(remaining)}</b>.", parse_mode="HTML")
        return
    
    await message.answer("✏️ Пожалуйста, введите ваш юзернейм в системе <b>Airgram</b>:\n<i>Пример: @username или username</i>", parse_mode="HTML")
    await state.set_state(ApplicationState.waiting_for_username)

@dp.message(StateFilter(ApplicationState.waiting_for_username))
async def get_username(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    airgram_username = message.text.strip().replace("@", "")
    
    if len(airgram_username) < 2:
        await message.answer("❌ Слишком короткое имя. Введите корректный юзернейм.")
        return
    
    set_cooldown(user_id, "application")
    await add_application(user_id, airgram_username, message.from_user.full_name)
    
    await message.answer("✅ <b>Ваша заявка успешно принята в обработку!</b>\nОжидайте вердикта администратора.", parse_mode="HTML")
    
    admin_text = (
        "📩 <b>НОВАЯ ЗАЯВКА НА ПРОВЕРКУ</b>\n"
        "───────────────────────────\n"
        "👤 Пользователь: {name}\n"
        "🆔 ID: <code>{user_id}</code> | TG: @{tg}\n"
        "📱 Airgram Юзернейм: <b>@{airgram}</b>\n"
        "🕐 Подано: {time}"
    ).format(name=message.from_user.full_name, user_id=user_id, tg=message.from_user.username or "нет", airgram=airgram_username, time=datetime.now().strftime('%d.%m.%Y %H:%M'))
    
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{user_id}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}")],
        [InlineKeyboardButton(text="🔇 Мут (1ч)", callback_data=f"mute_{user_id}"), InlineKeyboardButton(text="⛔ В ЧС", callback_data=f"block_{user_id}")]
    ])
    
    for admin_id in ADMIN_IDS:
        try: await bot.send_message(admin_id, text=admin_text, parse_mode="HTML", reply_markup=admin_kb)
        except: pass
    await state.clear()

# ===== ДЕЙСТВИЯ С ЗАЯВКАМИ ДЛЯ АДМИНА =====
@dp.callback_query(lambda c: c.data.split('_')[0] in ['accept', 'reject', 'mute', 'block'])
async def admin_actions(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id): return
    
    action, target_user_id = callback.data.split("_")[0], int(callback.data.split("_")[1])
    app = await get_application(target_user_id)
    
    if not app:
        await callback.answer("❌ Заявка уже была обработана кем-то другим.", show_alert=True)
        await callback.message.edit_text("⚠️ Заявка уже обработана.")
        return
    
    airgram_username, user_name = app[1], app[2]
    
    if action == "accept":
        await update_application_status(target_user_id, 'accepted')
        try: await bot.send_message(target_user_id, f"🎉 <b>Поздравляем, {user_name}!</b>\n\nВаша заявка на юзернейм <b>@{airgram_username}</b> была успешно одобрена! Подарок будет отправлен совсем скоро. 🎁", parse_mode="HTML")
        except: pass
        await callback.message.edit_text(f"✅ <b>ОДОБРЕНО:</b> @{airgram_username} для {user_name}", parse_mode="HTML")
        
    elif action == "reject":
        await update_application_status(target_user_id, 'rejected')
        try: await bot.send_message(target_user_id, f"😔 <b>Уважаемый(ая) {user_name},</b>\n\nК сожалению, ваша заявка на аккаунт <b>@{airgram_username}</b> отклонена администратором.", parse_mode="HTML")
        except: pass
        await callback.message.edit_text(f"❌ <b>ОТКЛОНЕНО:</b> @{airgram_username}", parse_mode="HTML")
        
    elif action == "mute":
        mute_until = datetime.now() + timedelta(hours=1)
        await add_mute(target_user_id, mute_until.timestamp())
        await update_application_status(target_user_id, 'rejected')
        try: await bot.send_message(target_user_id, f"🔇 <b>Вы получили ограничение на отправку сообщений (Мут) на 1 час.</b>", parse_mode="HTML")
        except: pass
        await callback.message.edit_text(f"🔇 <b>ЗАМУЧЕН:</b> @{airgram_username}", parse_mode="HTML")
        
    elif action == "block":
        await add_to_blacklist(target_user_id, "Заблокирован через панель заявок")
        await update_application_status(target_user_id, 'rejected')
        try: await bot.send_message(target_user_id, "⛔ <b>Вы внесены в черный список бота.</b>", parse_mode="HTML")
        except: pass
        await callback.message.edit_text(f"⛔ <b>В ЧЕРНОМ СПИСКЕ:</b> @{airgram_username}", parse_mode="HTML")
        
    await callback.answer()

# ===== ПРОСМОТР МОИХ ЗАЯВОК (КЛИЕНТ) =====
@dp.message(F.text == "📋 Мо мои заявки")
@dp.message(F.text == "📋 Мои заявки")
async def my_applications(message: types.Message):
    if await is_in_blacklist(message.from_user.id): return
    apps = await get_user_applications(message.from_user.id)
    if not apps:
        await message.answer("📭 У вас пока нет созданных заявок.")
        return
    
    text = "📋 <b>ИСТОРИЯ ВАШИХ ЗАЯВОК (До 10 шт)</b>\n"
    text += "───────────────────────────\n"
    for date, username, status in apps:
        status_emoji = {'pending': '⏳ В очереди', 'accepted': '✅ Одобрена', 'rejected': '❌ Отклонена'}.get(status, '❓ Неизвестно')
        text += f"• @{username} | Статус: <b>{status_emoji}</b>\n<pre>Дата подачи: {date}</pre>\n"
    await message.answer(text, parse_mode="HTML")

# ===== СТАТИСТИКА КЛИЕНТА =====
@dp.message(F.text == "📊 Моя статистика")
async def my_stats(message: types.Message):
    if await is_in_blacklist(message.from_user.id): return
    stats = await get_user_stats(message.from_user.id)
    if stats:
        name, total, accepted = stats
        winrate = round((accepted / total) * 100) if total > 0 else 0
        text = (
            "📊 <b>Ваша личная статистика:</b>\n"
            "───────────────────────────\n"
            "👤 Имя профиля: {name}\n"
            "📝 Подано заявок: <code>{total}</code>\n"
            "✅ Из них принято: <code>{accepted}</code>\n"
            "📈 Процент одобрений: <code>{wr}%</code>"
        ).format(name=name, total=total, accepted=accepted, wr=winrate)
    else:
        text = "📊 У вас пока нет зарегистрированной статистики."
    await message.answer(text, parse_mode="HTML")

# ===== КНОПКИ АДМИН МЕНЮ ВЫЗОВЫ =====
@dp.callback_query(F.data.startswith("admin_"))
async def admin_menu_actions(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    action = callback.data
    
    if action == "admin_applications":
        apps = await get_applications()
        if not apps:
            await callback.message.answer("📭 Нет активных заявок в очереди.")
        else:
            text = "📋 <b>АКТИВНЫЕ ЗАЯВКИ В ОЧЕРЕДИ:</b>\n───────────────────────────\n"
            for app in apps:
                text += f"👤 {app[2]} | ID: <code>{app[0]}</code> | @{app[1]} ({app[3]})\n"
            await callback.message.answer(text, parse_mode="HTML")
        await callback.answer()
        
    elif action == "admin_stats":
        async with aiosqlite.connect(DB_PATH) as db:
            pending = (await (await db.execute('SELECT COUNT(*) FROM applications WHERE status = "pending"')).fetchone())[0]
            accepted = (await (await db.execute('SELECT COUNT(*) FROM applications WHERE status = "accepted"')).fetchone())[0]
            rejected = (await (await db.execute('SELECT COUNT(*) FROM applications WHERE status = "rejected"')).fetchone())[0]
            users = (await (await db.execute('SELECT COUNT(*) FROM users')).fetchone())[0]
            blocked = (await (await db.execute('SELECT COUNT(*) FROM blacklist')).fetchone())[0]
            muted = (await (await db.execute('SELECT COUNT(*) FROM muted_users')).fetchone())[0]
            support = (await (await db.execute('SELECT COUNT(*) FROM support_messages WHERE status = "pending"')).fetchone())[0]
        
        stats_text = (
            "📊 <b>ПОЛНАЯ СТАТИСТИКА БОТА</b>\n"
            "───────────────────────────\n"
            "👥 Всего пользователей: <code>{u}</code>\n"
            "⏳ Заявок в обработке: <code>{p}</code>\n"
            "✅ Успешно принятых: <code>{a}</code>\n"
            "❌ Отклоненных системой: <code>{r}</code>\n"
            "⛔ Пользователей в ЧС: <code>{b}</code>\n"
            "🔇 Замученных аккаунтов: <code>{m}</code>\n"
            "🆘 Неотвеченных тикетов: <code>{s}</code>"
        ).format(u=users, p=pending, a=accepted, r=rejected, b=blocked, m=muted, s=support)
        await callback.message.answer(stats_text, parse_mode="HTML")
        await callback.answer()
        
    elif action == "admin_broadcast":
        await callback.message.answer("📢 <b>Введите текст сообщения для рассылки всем юзерам:</b>", parse_mode="HTML")
        await state.set_state(ApplicationState.waiting_broadcast)
        await callback.answer()
        
    elif action == "admin_blacklist":
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute('SELECT user_id, reason, date FROM blacklist') as cursor:
                rows = await cursor.fetchall()
        if not rows:
            await callback.message.answer("📭 Черный список на данный момент пуст.")
        else:
            text = "⛔ <b>СПИСОК ЗАБЛОКИРОВАННЫХ:</b>\n───────────────────────────\n"
            for r in rows: text += f"🆔 <code>{r[0]}</code> | Причина: {r[1]} ({r[2]})\n"
            await callback.message.answer(text, parse_mode="HTML")
        await callback.answer()
        
    elif action == "admin_support":
        msgs = await get_support_messages()
        if not msgs:
            await callback.message.answer("🆘 Активных диалогов поддержки нет.")
        else:
            text = "🆘 <b>АКТИВНЫЕ ОБРАЩЕНИЯ:</b>\n───────────────────────────\n"
            for m in msgs[:10]: text += f"👤 {m[3]} (<code>{m[1]}</code>): {m[4][:30]}... [{m[7]}]\n"
            await callback.message.answer(text, parse_mode="HTML")
        await callback.answer()
        
    elif action == "admin_users":
        await show_users_list(callback.message)
        await callback.answer()
        
    elif action == "admin_close":
        await callback.message.delete()
        await callback.answer()

# ===== РАССЫЛКА ДЛЯ ВСЕХ ПОЛЬЗОВАТЕЛЕЙ =====
@dp.message(StateFilter(ApplicationState.waiting_broadcast))
async def process_broadcast(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    text = message.text
    await state.clear()
    
    users = await get_all_users()
    if not users:
        await message.answer("❌ В базе данных нет пользователей.")
        return
        
    await message.answer(f"📢 Запущена рассылка на <b>{len(users)}</b> пользователей...", parse_mode="HTML")
    success, failed = 0, 0
    
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 <b>Уведомление от администратора!</b>\n\n{text}", parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.05)
        except:
            failed += 1
            
    await message.answer(f"✅ <b>Рассылка завершена!</b>\n• Доставлено: <code>{success}</code>\n• Ошибки: <code>{failed}</code>", parse_mode="HTML")

# ===== КЛАССИЧЕСКАЯ СИСТЕМНАЯ РАССЫЛКА КОМАНДОЙ =====
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if not is_admin(message.from_user.id): return
    text = message.text.replace("/broadcast", "").strip()
    if not text:
        await message.answer("❌ Ошибка. Используйте: <code>/broadcast Ваше сообщение</code>", parse_mode="HTML")
        return
    
    users = await get_all_users()
    await message.answer(f"📢 Запущена рассылка на <b>{len(users)}</b> пользователей...", parse_mode="HTML")
    success = 0
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 <b>Сообщение от Администратора:</b>\n\n{text}", parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.05)
        except: pass
    await message.answer(f"✅ Успешно доставлено: {success} пользователям.")

# ===== ПОЛУЧЕНИЕ ИНФОРМАЦИИ О ЮЗЕРЕ ДЛЯ АДМИНА =====
@dp.message(Command("userinfo"))
async def user_info(message: types.Message):
    if not is_admin(message.from_user.id): return
    try:
        args = message.text.split()
        if len(args) < 2:
            await message.answer("❌ Используйте: <code>/userinfo [ID]</code>", parse_mode="HTML")
            return
        target_user_id = int(args[1])
        stats = await get_user_stats(target_user_id)
        if not stats:
            await message.answer("❌ Такого пользователя нет в статистике базы данных.")
            return
        apps = await get_user_applications(target_user_id)
        
        text = (
            "ℹ️ <b>ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ</b>\n"
            "───────────────────────────\n"
            "🆔 Telegram ID: <code>{uid}</code>\n"
            "👤 Имя: {name}\n"
            "📊 Заявок отправлено: {total}\n"
            "✅ Из них одобрено: {acc}\n"
            "📈 Рейтинг доверия: {wr}%"
        ).format(uid=target_user_id, name=stats[0], total=stats[1], acc=stats[2], wr=(round((stats[2]/stats[1])*100) if stats[1] > 0 else 0))
        await message.answer(text, parse_mode="HTML")
    except:
        await message.answer("❌ Произошла системная ошибка при парсинге ID.")

@dp.message(Command("unmute"))
async def unmute_command(message: types.Message):
    if not is_admin(message.from_user.id): return
    try:
        target_id = int(message.text.split()[1])
        await remove_mute(target_id)
        await message.answer(f"✅ Ограничения (мут) с пользователя <code>{target_id}</code> успешно сняты.", parse_mode="HTML")
        try: await bot.send_message(target_id, "🔊 <b>С вас снято ограничение на отправку сообщений!</b>", parse_mode="HTML")
        except: pass
    except: await message.answer("❌ Ошибка ввода. Формат: /unmute [ID]")

@dp.message(Command("unblock"))
async def unblock_command(message: types.Message):
    if not is_admin(message.from_user.id): return
    try:
        target_id = int(message.text.split()[1])
        await remove_from_blacklist(target_id)
        await message.answer(f"✅ Пользователь <code>{target_id}</code> успешно разблокирован.", parse_mode="HTML")
        try: await bot.send_message(target_id, "🔓 <b>Администратор разблокировал ваш профиль в боте.</b>", parse_mode="HTML")
        except: pass
    except: await message.answer("❌ Ошибка ввода. Формат: /unblock [ID]")

# ===== ВЕБХУК ЗАПУСК ДЛЯ RENDER СЕРВЕРА =====
async def on_startup(bot: Bot):
    await init_db()  # Инициализация и автомиграция асинхронной БД
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        print(f"🚀 Вебхук успешно установлен на: {WEBHOOK_URL}")
    else:
        print("⚠️ WEBHOOK_URL не настроен! Запуск без вебхука.")

async def health_check(request):
    return web.Response(text="Я живой и я работаю!", status=200)

def main():
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    app.router.add_get('/', health_check)

    dp.startup.register(on_startup)
    setup_application(app, dp, bot=bot)

    print("🤖 Бот AirgramBot оптимизирован и запущен!")
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
