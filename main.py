import asyncio
import logging
import os
import random
import string
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

from google.cloud import firestore
from google.oauth2 import service_account
import json

load_dotenv()

# ===== НАСТРОЙКИ ХОСТИНГА =====
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}" if RENDER_URL else None
PORT = int(os.getenv("PORT", 10000))
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    exit("❌ BOT_TOKEN не задан!")

COOLDOWN_APPLICATION = 300
COOLDOWN_SUPPORT = 300

logging.basicConfig(level=logging.INFO)
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

user_cooldowns = {"application": {}, "support": {}}

# ===== FIRESTORE =====
FIREBASE_PROJECT = os.getenv("FIREBASE_PROJECT_ID", "gfsdksfg")

def init_firestore():
    creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON") or os.getenv("FIREBASE_CREDENTIALS")
    if creds_json:
        try:
            creds_info = json.loads(creds_json)
            credentials = service_account.Credentials.from_service_account_info(creds_info)
            project = creds_info.get("project_id", FIREBASE_PROJECT)
            return firestore.Client(project=project, credentials=credentials)
        except Exception as e:
            logging.error(f"Ошибка загрузки JSON-ключа: {e}")
    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if creds_path and os.path.exists(creds_path):
        try:
            return firestore.Client(project=FIREBASE_PROJECT)
        except Exception as e:
            logging.error(f"Ошибка загрузки файла учётных данных: {e}")
    try:
        return firestore.Client(project=FIREBASE_PROJECT)
    except Exception as e:
        logging.error(f"Ошибка инициализации Firestore: {e}")
        raise RuntimeError("Не удалось инициализировать Firestore. Проверьте учётные данные.")

db = init_firestore()

def s_id(user_id):
    return str(user_id)

# ===== АДМИНЫ ИЗ БД =====
ADMIN_IDS = []

async def load_admins():
    global ADMIN_IDS
    docs = db.collection("admins").stream()
    ADMIN_IDS = [int(doc.id) for doc in docs]
    logging.info(f"Загружены админы: {ADMIN_IDS}")

async def refresh_admins():
    while True:
        await load_admins()
        await asyncio.sleep(300)  # 5 минут

def is_admin_sync(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ===== КУЛДАУН =====
def check_cooldown(user_id: int, cooldown_type: str) -> tuple:
    if is_admin_sync(user_id):
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
    if is_admin_sync(user_id):
        return
    user_cooldowns[cooldown_type][user_id] = datetime.now().timestamp()

def format_cooldown_time(seconds: int) -> str:
    minutes = seconds // 60
    seconds_remain = seconds % 60
    return f"{minutes} мин {seconds_remain} сек" if minutes > 0 else f"{seconds_remain} сек"

# ===== АСИНХРОННЫЕ ФУНКЦИИ БАЗЫ =====
async def init_db():
    stats_ref = db.collection("stats").document("accepted_count")
    if not stats_ref.get().exists:
        stats_ref.set({"value": 0})
    await load_admins()
    print("🔥 Firebase инициализирована, админы загружены.")

async def get_accepted_count():
    doc = db.collection("stats").document("accepted_count").get()
    return doc.to_dict().get("value", 0) if doc.exists else 0

async def add_application(user_id, username, user_name):
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    doc_ref = db.collection("applications").add({
        "user_id": int(user_id),
        "username": username,
        "user_name": user_name,
        "date": now_str,
        "status": "pending"
    })
    # Получаем ID документа для будущего использования
    application_id = doc_ref.id

    user_ref = db.collection("users").document(s_id(user_id))
    user_doc = user_ref.get()
    if user_doc.exists:
        user_ref.update({
            "last_visit": now_str,
            "total_applications": firestore.Increment(1)
        })
    else:
        user_ref.set({
            "user_id": int(user_id),
            "full_name": user_name,
            "username": username,
            "first_visit": now_str,
            "last_visit": now_str,
            "total_applications": 1,
            "accepted_applications": 0
        })
    return application_id

async def get_applications():
    docs = db.collection("applications").where("status", "==", "pending").stream()
    return [[d.id, d.to_dict().get("user_id"), d.to_dict().get("username"), d.to_dict().get("user_name"), d.to_dict().get("date")] for d in docs]

async def get_application_by_id(app_id):
    doc = db.collection("applications").document(app_id).get()
    if doc.exists:
        data = doc.to_dict()
        data['id'] = app_id
        return data
    return None

async def get_application_by_user(user_id):
    docs = db.collection("applications").where("user_id", "==", int(user_id)).where("status", "==", "pending").limit(1).stream()
    for d in docs:
        return [d.id, d.to_dict().get("user_id"), d.to_dict().get("username"), d.to_dict().get("user_name"), d.to_dict().get("date")]
    return None

async def get_user_applications(user_id):
    docs = db.collection("applications").where("user_id", "==", int(user_id)).order_by("date", direction=firestore.Query.DESCENDING).limit(10).stream()
    return [[d.id, d.to_dict().get("date"), d.to_dict().get("username"), d.to_dict().get("status")] for d in docs]

async def update_application_status(app_id, status):
    db.collection("applications").document(app_id).update({"status": status})
    # Если статус accepted, обновляем счётчики
    if status == 'accepted':
        doc = db.collection("applications").document(app_id).get()
        if doc.exists:
            user_id = doc.to_dict().get("user_id")
            db.collection("users").document(s_id(user_id)).update({"accepted_applications": firestore.Increment(1)})
            db.collection("stats").document("accepted_count").update({"value": firestore.Increment(1)})

async def add_to_blacklist(user_id, reason="Нарушение правил"):
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    db.collection("blacklist").document(s_id(user_id)).set({
        "user_id": int(user_id),
        "reason": reason,
        "date": now_str
    })

async def remove_from_blacklist(user_id):
    db.collection("blacklist").document(s_id(user_id)).delete()

async def is_in_blacklist(user_id):
    return db.collection("blacklist").document(s_id(user_id)).get().exists

async def add_mute(user_id, until_timestamp):
    db.collection("muted_users").document(s_id(user_id)).set({
        "user_id": int(user_id),
        "until_timestamp": int(until_timestamp)
    })

async def remove_mute(user_id):
    db.collection("muted_users").document(s_id(user_id)).delete()

async def is_muted(user_id):
    doc = db.collection("muted_users").document(s_id(user_id)).get()
    if not doc.exists:
        return False
    if doc.to_dict().get("until_timestamp", 0) < datetime.now().timestamp():
        await remove_mute(user_id)
        return False
    return True

async def get_all_users():
    docs = db.collection("users").stream()
    return [int(d.id) for d in docs]

async def get_all_users_full():
    docs = db.collection("users").stream()
    return [[int(d.id), d.to_dict().get("full_name"), d.to_dict().get("username")] for d in docs]

async def add_user_to_db(user_id, full_name, username):
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    user_ref = db.collection("users").document(s_id(user_id))
    if not user_ref.get().exists:
        user_ref.set({
            "user_id": int(user_id),
            "full_name": full_name,
            "username": username,
            "first_visit": now_str,
            "last_visit": now_str,
            "total_applications": 0,
            "accepted_applications": 0
        })

async def get_user_stats(user_id):
    doc = db.collection("users").document(s_id(user_id)).get()
    if doc.exists:
        d = doc.to_dict()
        return [d.get("full_name"), d.get("total_applications", 0), d.get("accepted_applications", 0)]
    return None

async def get_user_count():
    return len(db.collection("users").get())

async def get_user_id_by_username(username):
    username = username.replace('@', '').strip()
    docs = db.collection("users").where("username", "==", username).limit(1).stream()
    for d in docs:
        return d.to_dict().get("user_id")
    return None

# ---- Техподдержка ----
async def add_support_message(user_id, username, user_name, message, file_id=None, file_type=None):
    doc_ref = db.collection("support_messages").add({
        "user_id": int(user_id),
        "username": username,
        "user_name": user_name,
        "message": message,
        "file_id": file_id,
        "file_type": file_type,
        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "status": "active"  # active / closed
    })
    return doc_ref.id

async def get_support_messages_active():
    docs = db.collection("support_messages").where("status", "==", "active").stream()
    return [[d.id, d.to_dict().get("user_id"), d.to_dict().get("username"), d.to_dict().get("user_name"), d.to_dict().get("message"), d.to_dict().get("file_id"), d.to_dict().get("file_type"), d.to_dict().get("date")] for d in docs]

async def get_support_message_by_user(user_id):
    docs = db.collection("support_messages").where("user_id", "==", int(user_id)).where("status", "==", "active").limit(1).stream()
    for d in docs:
        return [d.id, d.to_dict().get("user_id"), d.to_dict().get("username"), d.to_dict().get("user_name"), d.to_dict().get("message"), d.to_dict().get("file_id"), d.to_dict().get("file_type"), d.to_dict().get("date")]
    return None

async def get_user_support_messages(user_id):
    docs = db.collection("support_messages").where("user_id", "==", int(user_id)).order_by("date", direction=firestore.Query.DESCENDING).limit(10).stream()
    return [[d.id, d.to_dict().get("date"), d.to_dict().get("message"), d.to_dict().get("status")] for d in docs]

async def update_support_status(msg_id, status):
    db.collection("support_messages").document(str(msg_id)).update({"status": status})

# ===== КНОПКИ =====
apply_button = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📝 Отправить заявку")],
    [KeyboardButton(text="📊 Моя статистика"), KeyboardButton(text="📋 Мои заявки")],
    [KeyboardButton(text="🆘 Техподдержка"), KeyboardButton(text="📨 Мои обращения")]
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
    waiting_support_message_for_ticket = State()  # для ответа по конкретному обращению

# ===== ХЕНДЛЕРЫ =====

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
        "🌟 <b>Добро пожаловать в AirgramBot!</b>\n"
        "───────────────────────────\n"
        "📊 <b>Наша статистика:</b>\n"
        "• Принято заявок: <code>{accepted}</code>\n"
        "• В очереди: <code>{pending}</code>\n"
        "• Всего пользователей: <code>{users}</code>"
    ).format(accepted=accepted_count, pending=pending_count, users=total_users)
    await message.answer(welcome_text, reply_markup=apply_button, parse_mode="HTML")

# ------ АДМИН-ПАНЕЛЬ ------
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin_sync(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к админ-панели!")
        return
    support_messages = await get_support_messages_active()
    support_count = len(support_messages)
    user_count = await get_user_count()
    admin_text = (
        "👑 <b>ПАНЕЛЬ АДМИНИСТРАТОРА</b>\n"
        "───────────────────────────\n"
        "• Новых обращений в саппорт: <code>{support}</code>\n"
        "• Всего пользователей в БД: <code>{users}</code>"
    ).format(support=support_count, users=user_count)
    await message.answer(admin_text, parse_mode="HTML", reply_markup=admin_menu)

@dp.message(Command("come"))
async def admin_commands_list(message: types.Message):
    if not is_admin_sync(message.from_user.id):
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

@dp.message(Command("ban"))
async def ban_by_username(message: types.Message):
    if not is_admin_sync(message.from_user.id):
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
    if is_admin_sync(target_user_id):
        await message.answer("❌ Нельзя забанить администратора!")
        return
    await add_to_blacklist(target_user_id, "Забанен администратором")
    await message.answer(f"✅ Пользователь @{username} (<code>{target_user_id}</code>) успешно забанен!")
    try:
        await bot.send_message(chat_id=target_user_id, text="⛔ <b>ВЫ ЗАБАНЕНЫ!</b>\n\nАдминистратор ограничил вам доступ к боту.", parse_mode="HTML")
    except:
        pass

@dp.message(Command("users"))
async def users_list_command(message: types.Message):
    if not is_admin_sync(message.from_user.id):
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

# ------ ТЕХПОДДЕРЖКА (Пользователь) ------
@dp.message(F.text == "🆘 Техподдержка")
async def support_button(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if await is_in_blacklist(user_id) or await is_muted(user_id):
        return
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
    if message.text:
        msg_text = message.text
    elif message.photo:
        file_id, file_type, msg_text = message.photo[-1].file_id, "photo", message.caption or "📷 Фото"
    elif message.video:
        file_id, file_type, msg_text = message.video.file_id, "video", message.caption or "🎥 Видео"
    elif message.document:
        file_id, file_type, msg_text = message.document.file_id, "document", message.caption or "📄 Документ"
    elif message.audio:
        file_id, file_type, msg_text = message.audio.file_id, "audio", message.caption or "🎵 Аудио"
    elif message.voice:
        file_id, file_type, msg_text = message.voice.file_id, "voice", message.caption or "🎤 Голосовое сообщение"
    elif message.animation:
        file_id, file_type, msg_text = message.animation.file_id, "animation", message.caption or "🔄 GIF"
    else:
        await message.answer("❌ Отправьте текст или поддерживаемый медиафайл.")
        return

    set_cooldown(user_id, "support")
    ticket_id = await add_support_message(user_id, username, user_name, msg_text, file_id, file_type)
    await message.answer("✅ <b>Ваше сообщение доставлено!</b>\n\nКоманда поддержки рассмотрит его в ближайшее время.", parse_mode="HTML")

    admin_text = (
        "🆘 <b>НОВОЕ ОБРАЩЕНИЕ В ТЕХПОДДЕРЖКУ</b>\n"
        "───────────────────────────\n"
        "🆔 ID обращения: <code>{ticket_id}</code>\n"
        "👤 От: <b>{name}</b>\n"
        "🆔 ID: <code>{user_id}</code> | TG: @{username}\n"
        "🕐 Время: {time}\n"
        "───────────────────────────\n"
        "📝 <b>Сообщение:</b>\n{text}"
    ).format(ticket_id=ticket_id, name=user_name, user_id=user_id, username=username, time=datetime.now().strftime('%d.%m.%Y %H:%M'), text=msg_text)

    support_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ответить", callback_data=f"support_reply_{ticket_id}")],
        [InlineKeyboardButton(text="✅ Закрыть обращение", callback_data=f"support_close_{ticket_id}")]
    ])

    for admin_id in ADMIN_IDS:
        try:
            if file_id and file_type:
                if file_type == "photo":
                    await bot.send_photo(admin_id, file_id, caption=admin_text, parse_mode="HTML", reply_markup=support_kb)
                elif file_type == "video":
                    await bot.send_video(admin_id, file_id, caption=admin_text, parse_mode="HTML", reply_markup=support_kb)
                elif file_type == "document":
                    await bot.send_document(admin_id, file_id, caption=admin_text, parse_mode="HTML", reply_markup=support_kb)
                elif file_type == "audio":
                    await bot.send_audio(admin_id, file_id, caption=admin_text, parse_mode="HTML", reply_markup=support_kb)
                elif file_type == "voice":
                    await bot.send_voice(admin_id, file_id, caption=admin_text, parse_mode="HTML", reply_markup=support_kb)
                elif file_type == "animation":
                    await bot.send_animation(admin_id, file_id, caption=admin_text, parse_mode="HTML", reply_markup=support_kb)
            else:
                await bot.send_message(admin_id, text=admin_text, parse_mode="HTML", reply_markup=support_kb)
        except:
            pass
    await state.clear()

# -------- Ответ админа на обращение ----------
@dp.callback_query(F.data.startswith("support_reply_"))
async def support_reply(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin_sync(callback.from_user.id):
        return
    ticket_id = callback.data.split("_")[2]
    await callback.message.answer(f"💬 Введите ответ для обращения <code>{ticket_id}</code> (Текст или медиафайлы):", parse_mode="HTML")
    await state.update_data(reply_ticket=ticket_id)
    await state.set_state(ApplicationState.waiting_support_reply)
    await callback.answer()

@dp.message(StateFilter(ApplicationState.waiting_support_reply))
async def send_support_reply(message: types.Message, state: FSMContext):
    if not is_admin_sync(message.from_user.id):
        return
    data = await state.get_data()
    ticket_id = data.get('reply_ticket')
    if not ticket_id:
        await message.answer("❌ Ошибка: не найдено обращение.")
        await state.clear()
        return

    # Получаем данные обращения
    doc = db.collection("support_messages").document(ticket_id).get()
    if not doc.exists:
        await message.answer("❌ Обращение не найдено.")
        await state.clear()
        return
    ticket_data = doc.to_dict()
    target_user_id = ticket_data.get("user_id")
    if not target_user_id:
        await message.answer("❌ Ошибка: пользователь не найден.")
        await state.clear()
        return

    reply_text = message.text or message.caption or "<i>Файл от техподдержки</i>"
    admin_name = message.from_user.full_name

    header_text = f"💬 <b>Ответ от техподдержки ({admin_name}):</b>\n\n{reply_text}"
    try:
        if message.photo:
            await bot.send_photo(target_user_id, message.photo[-1].file_id, caption=header_text, parse_mode="HTML")
        elif message.video:
            await bot.send_video(target_user_id, message.video.file_id, caption=header_text, parse_mode="HTML")
        elif message.document:
            await bot.send_document(target_user_id, message.document.file_id, caption=header_text, parse_mode="HTML")
        elif message.audio:
            await bot.send_audio(target_user_id, message.audio.file_id, caption=header_text, parse_mode="HTML")
        elif message.voice:
            await bot.send_voice(target_user_id, message.voice.file_id, caption=f"💬 <b>Голосовой ответ от техподдержки ({admin_name})</b>", parse_mode="HTML")
        else:
            await bot.send_message(target_user_id, text=f"💬 <b>Ответ от техподдержки ({admin_name}):</b>\n\n{message.text}", parse_mode="HTML")

        await message.answer(f"✅ Ответ успешно доставлен пользователю <code>{target_user_id}</code>", parse_mode="HTML")
        # Не меняем статус обращения, остаётся активным
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить ответ: {str(e)}")
    await state.clear()

# -------- Закрытие обращения админом ----------
@dp.callback_query(F.data.startswith("support_close_"))
async def support_close(callback: types.CallbackQuery):
    if not is_admin_sync(callback.from_user.id):
        return
    ticket_id = callback.data.split("_")[2]
    admin_name = callback.from_user.full_name

    doc = db.collection("support_messages").document(ticket_id).get()
    if not doc.exists:
        await callback.answer("❌ Обращение не найдено.", show_alert=True)
        return
    ticket_data = doc.to_dict()
    if ticket_data.get("status") == "closed":
        await callback.answer("❌ Обращение уже закрыто.", show_alert=True)
        return

    await update_support_status(ticket_id, "closed")
    try:
        user_id = ticket_data.get("user_id")
        if user_id:
            await bot.send_message(user_id, "❌ <b>Ваше обращение в техподдержку было закрыто администратором.</b>", parse_mode="HTML")
    except:
        pass
    await callback.message.edit_text(callback.message.text + f"\n\n✅ <b>ЗАКРЫТО администратором {admin_name}</b>", parse_mode="HTML", reply_markup=None)
    await callback.answer("Обращение закрыто")

# -------- Открытие обращения админом (дополнительно) ----------
@dp.callback_query(F.data.startswith("support_open_"))
async def support_open(callback: types.CallbackQuery):
    if not is_admin_sync(callback.from_user.id):
        return
    ticket_id = callback.data.split("_")[2]
    admin_name = callback.from_user.full_name

    doc = db.collection("support_messages").document(ticket_id).get()
    if not doc.exists:
        await callback.answer("❌ Обращение не найдено.", show_alert=True)
        return
    ticket_data = doc.to_dict()
    if ticket_data.get("status") == "active":
        await callback.answer("❌ Обращение уже активно.", show_alert=True)
        return

    await update_support_status(ticket_id, "active")
    try:
        user_id = ticket_data.get("user_id")
        if user_id:
            await bot.send_message(user_id, "🔄 <b>Ваше обращение в техподдержку было снова открыто администратором.</b>", parse_mode="HTML")
    except:
        pass
    await callback.message.edit_text(callback.message.text + f"\n\n✅ <b>ОТКРЫТО администратором {admin_name}</b>", parse_mode="HTML", reply_markup=None)
    await callback.answer("Обращение открыто")

# -------- Мои обращения ----------
@dp.message(F.text == "📨 Мои обращения")
async def my_support_messages(message: types.Message):
    if await is_in_blacklist(message.from_user.id):
        return
    msgs = await get_user_support_messages(message.from_user.id)
    if not msgs:
        await message.answer("📭 У вас пока нет обращений в техподдержку.")
        return
    text = "📨 <b>ИСТОРИЯ ОБРАЩЕНИЙ (До 10 шт)</b>\n"
    text += "───────────────────────────\n"
    for msg_id, date, msg_text, status in msgs:
        status_emoji = {'active': '🟢 Активно', 'closed': '🔴 Закрыто'}.get(status, '❓ Неизвестно')
        text += f"• <code>{msg_id}</code> | {status_emoji}\n<pre>Дата: {date}\nСообщение: {msg_text[:50]}{'...' if len(msg_text)>50 else ''}</pre>\n"
    await message.answer(text, parse_mode="HTML")

# ------ ЗАЯВКИ ------
@dp.message(F.text == "📝 Отправить заявку")
async def apply_button_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if await is_in_blacklist(user_id) or await is_muted(user_id):
        return

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
    app_id = await add_application(user_id, airgram_username, message.from_user.full_name)
    await message.answer("✅ <b>Ваша заявка успешно принята в обработку!</b>\nОжидайте вердикта администратора.", parse_mode="HTML")

    admin_text = (
        "📩 <b>НОВАЯ ЗАЯВКА</b>\n"
        "───────────────────────────\n"
        "🆔 ID заявки: <code>{app_id}</code>\n"
        "👤 Пользователь: {name}\n"
        "🆔 ID: <code>{user_id}</code> | TG: @{tg}\n"
        "📱 Airgram Юзернейм: <b>@{airgram}</b>\n"
        "🕐 Подано: {time}"
    ).format(app_id=app_id, name=message.from_user.full_name, user_id=user_id, tg=message.from_user.username or "нет", airgram=airgram_username, time=datetime.now().strftime('%d.%m.%Y %H:%M'))

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{app_id}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{app_id}")],
        [InlineKeyboardButton(text="🔇 Мут (1ч)", callback_data=f"mute_{app_id}"), InlineKeyboardButton(text="⛔ В ЧС", callback_data=f"block_{app_id}")]
    ])

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text=admin_text, parse_mode="HTML", reply_markup=admin_kb)
        except:
            pass
    await state.clear()

# ------ Обработка заявок (админ) ------
@dp.callback_query(lambda c: c.data.split('_')[0] in ['accept', 'reject', 'mute', 'block'])
async def admin_actions(callback: types.CallbackQuery):
    if not is_admin_sync(callback.from_user.id):
        return
    action, app_id = callback.data.split("_")[0], callback.data.split("_")[1]
    app_data = await get_application_by_id(app_id)
    if not app_data:
        await callback.answer("❌ Заявка уже была обработана или не существует.", show_alert=True)
        await callback.message.edit_text("⚠️ Заявка уже обработана.")
        return

    user_id = app_data.get("user_id")
    airgram_username = app_data.get("username")
    user_name = app_data.get("user_name")

    if action == "accept":
        await update_application_status(app_id, 'accepted')
        try:
            await bot.send_message(user_id, f"🎉 <b>Поздравляем, {user_name}!</b>\n\nВаша заявка на юзернейм <b>@{airgram_username}</b> была успешно одобрена! Подарок будет отправлен совсем скоро. 🎁", parse_mode="HTML")
        except:
            pass
        await callback.message.edit_text(f"✅ <b>ОДОБРЕНО:</b> @{airgram_username} для {user_name} (ID: {app_id})", parse_mode="HTML")
    elif action == "reject":
        await update_application_status(app_id, 'rejected')
        try:
            await bot.send_message(user_id, f"😔 <b>Уважаемый(ая) {user_name},</b>\n\nК сожалению, ваша заявка на аккаунт <b>@{airgram_username}</b> отклонена администратором.", parse_mode="HTML")
        except:
            pass
        await callback.message.edit_text(f"❌ <b>ОТКЛОНЕНО:</b> @{airgram_username} (ID: {app_id})", parse_mode="HTML")
    elif action == "mute":
        mute_until = datetime.now() + timedelta(hours=1)
        await add_mute(user_id, mute_until.timestamp())
        await update_application_status(app_id, 'rejected')
        try:
            await bot.send_message(user_id, f"🔇 <b>Вы получили ограничение на отправку сообщений (Мут) на 1 час.</b>", parse_mode="HTML")
        except:
            pass
        await callback.message.edit_text(f"🔇 <b>ЗАМУЧЕН:</b> @{airgram_username} (ID: {app_id})", parse_mode="HTML")
    elif action == "block":
        await add_to_blacklist(user_id, "Заблокирован через панель заявок")
        await update_application_status(app_id, 'rejected')
        try:
            await bot.send_message(user_id, "⛔ <b>Вы внесены в черный список бота.</b>", parse_mode="HTML")
        except:
            pass
        await callback.message.edit_text(f"⛔ <b>В ЧЕРНОМ СПИСКЕ:</b> @{airgram_username} (ID: {app_id})", parse_mode="HTML")
    await callback.answer()

# ------ Мои заявки ------
@dp.message(F.text == "📋 Мои заявки")
async def my_applications(message: types.Message):
    if await is_in_blacklist(message.from_user.id):
        return
    apps = await get_user_applications(message.from_user.id)
    if not apps:
        await message.answer("📭 У вас пока нет созданных заявок.")
        return
    text = "📋 <b>ИСТОРИЯ ВАШИХ ЗАЯВОК (До 10 шт)</b>\n"
    text += "───────────────────────────\n"
    for app_id, date, username, status in apps:
        status_emoji = {'pending': '⏳ В очереди', 'accepted': '✅ Одобрена', 'rejected': '❌ Отклонена'}.get(status, '❓ Неизвестно')
        text += f"• <code>{app_id}</code> | @{username} | Статус: <b>{status_emoji}</b>\n<pre>Дата подачи: {date}</pre>\n"
    await message.answer(text, parse_mode="HTML")

# ------ Статистика ------
@dp.message(F.text == "📊 Моя статистика")
async def my_stats(message: types.Message):
    if await is_in_blacklist(message.from_user.id):
        return
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

# ------ Админ-меню (кнопки) ------
@dp.callback_query(F.data.startswith("admin_"))
async def admin_menu_actions(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin_sync(callback.from_user.id):
        return
    action = callback.data

    if action == "admin_applications":
        apps = await get_applications()
        if not apps:
            await callback.message.answer("📭 Нет активных заявок в очереди.")
        else:
            text = "📋 <b>АКТИВНЫЕ ЗАЯВКИ В ОЧЕРЕДИ:</b>\n───────────────────────────\n"
            for app_id, user_id, username, user_name, date in apps:
                text += f"🆔 <code>{app_id}</code> | {user_name} | @{username} ({date})\n"
            await callback.message.answer(text, parse_mode="HTML")
        await callback.answer()

    elif action == "admin_stats":
        pending = len(await get_applications())
        accepted = await get_accepted_count()
        users = await get_user_count()
        stats_text = (
            "📊 <b>ПОЛНАЯ СТАТИСТИКА БОТА (FIRESTORE)</b>\n"
            "───────────────────────────\n"
            "👥 Всего пользователей: <code>{u}</code>\n"
            "⏳ Заявок в обработке: <code>{p}</code>\n"
            "✅ Успешно принятых: <code>{a}</code>"
        ).format(u=users, p=pending, a=accepted)
        await callback.message.answer(stats_text, parse_mode="HTML")
        await callback.answer()

    elif action == "admin_broadcast":
        await callback.message.answer("📢 <b>Введите текст сообщения для рассылки всем юзерам:</b>", parse_mode="HTML")
        await state.set_state(ApplicationState.waiting_broadcast)
        await callback.answer()

    elif action == "admin_blacklist":
        docs = db.collection("blacklist").stream()
        rows = [d.to_dict() for d in docs]
        if not rows:
            await callback.message.answer("📭 Черный список на данный момент пуст.")
        else:
            text = "⛔ <b>СПИСОК ЗАБЛОКИРОВАННЫХ:</b>\n───────────────────────────\n"
            for r in rows:
                text += f"🆔 <code>{r.get('user_id')}</code> | Причина: {r.get('reason')} ({r.get('date')})\n"
            await callback.message.answer(text, parse_mode="HTML")
        await callback.answer()

    elif action == "admin_support":
        msgs = await get_support_messages_active()
        if not msgs:
            await callback.message.answer("🆘 Активных обращений в поддержку нет.")
        else:
            text = "🆘 <b>АКТИВНЫЕ ОБРАЩЕНИЯ:</b>\n───────────────────────────\n"
            for msg_id, user_id, username, user_name, msg_text, file_id, file_type, date in msgs:
                text += f"🆔 <code>{msg_id}</code> | {user_name} (@{username}) | {date}\n"
            await callback.message.answer(text, parse_mode="HTML")
        await callback.answer()

    elif action == "admin_users":
        await show_users_list(callback.message)
        await callback.answer()
    elif action == "admin_close":
        await callback.message.delete()
        await callback.answer()

# ------ Рассылка ------
@dp.message(StateFilter(ApplicationState.waiting_broadcast))
async def process_broadcast(message: types.Message, state: FSMContext):
    if not is_admin_sync(message.from_user.id):
        return
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

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if not is_admin_sync(message.from_user.id):
        return
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
        except:
            pass
    await message.answer(f"✅ Успешно доставлено: {success} пользователям.")

# ------ userinfo, unmute, unblock ------
@dp.message(Command("userinfo"))
async def user_info(message: types.Message):
    if not is_admin_sync(message.from_user.id):
        return
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
    if not is_admin_sync(message.from_user.id):
        return
    try:
        target_id = int(message.text.split()[1])
        await remove_mute(target_id)
        await message.answer(f"✅ Ограничения (мут) с пользователя <code>{target_id}</code> успешно сняты.", parse_mode="HTML")
        try:
            await bot.send_message(target_id, "🔊 <b>С вас снято ограничение на отправку сообщений!</b>", parse_mode="HTML")
        except:
            pass
    except:
        await message.answer("❌ Ошибка ввода. Формат: /unmute [ID]")

@dp.message(Command("unblock"))
async def unblock_command(message: types.Message):
    if not is_admin_sync(message.from_user.id):
        return
    try:
        target_id = int(message.text.split()[1])
        await remove_from_blacklist(target_id)
        await message.answer(f"✅ Пользователь <code>{target_id}</code> успешно разблокирован.", parse_mode="HTML")
        try:
            await bot.send_message(target_id, "🔓 <b>Администратор разблокировал ваш профиль в боте.</b>", parse_mode="HTML")
        except:
            pass
    except:
        await message.answer("❌ Ошибка ввода. Формат: /unblock [ID]")

# ===== ЗАПУСК =====
async def on_startup(bot: Bot):
    await init_db()
    asyncio.create_task(refresh_admins())
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        print(f"🚀 Вебхук установлен на: {WEBHOOK_URL}")
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

    print("🤖 Бот AirgramBot запущен с улучшенной системой заявок и поддержки!")
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
