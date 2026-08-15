import asyncio
import logging
import sqlite3
import re
import os
from datetime import datetime, timedelta
from contextlib import contextmanager

import aiohttp

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton,
    Message, CallbackQuery, FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = "8651956926:AAG3ML1uGBPQOgrM5WAMl3kXaRLvVxTHCsw"  # Замените на ваш токен

# --- CryptoBot (Crypto Pay API) ---
# Токен приложения берётся у @CryptoBot -> Crypto Pay -> Create App
CRYPTO_PAY_TOKEN = "YOUR_CRYPTO_PAY_TOKEN"  # Замените на токен вашего приложения
CRYPTO_PAY_API_BASE = "https://pay.crypt.bot/api"  # для теста: https://testnet-pay.crypt.bot/api
CRYPTO_ASSET = "USDT"  # актив, в котором создаются чеки (вывод) и инвойсы (пополнение)

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# --- Состояния FSM ---

class UserStates(StatesGroup):
    entering_phone = State()
    selecting_queue = State()
    confirm_number = State()
    selecting_withdrawal = State()
    worker_select_method = State()
    worker_waiting_code = State()
    worker_waiting_photo = State()
    worker_waiting_qr = State()
    worker_confirm_stand = State()
    admin_add_worker = State()
    admin_edit_price = State()
    admin_set_photo = State()
    admin_broadcast = State()
    admin_treasury = State()

# --- Работа с базой данных ---

DB_PATH = "babrito.db"

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        conn.close()

def ensure_columns(conn, table: str, columns: dict):
    """Добавляет отсутствующие колонки в уже существующую таблицу
    (нужно, если база была создана более старой версией бота и
    CREATE TABLE IF NOT EXISTS не тронул её структуру)."""
    existing = {row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    for col_name, col_def in columns.items():
        if col_name in existing:
            continue
        try:
            if 'CURRENT_TIMESTAMP' in col_def.upper():
                # SQLite запрещает ALTER TABLE ADD COLUMN с не-константным
                # DEFAULT (CURRENT_TIMESTAMP), если в таблице уже есть строки.
                # Поэтому добавляем колонку без DEFAULT, а существующие
                # строки задним числом заполняем текущим временем.
                base_type = col_def.upper().split('DEFAULT')[0].strip() or 'DATETIME'
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {col_name} {base_type}')
                conn.execute(f'UPDATE {table} SET {col_name} = CURRENT_TIMESTAMP WHERE {col_name} IS NULL')
            else:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {col_name} {col_def}')
            logger.info(f"Migration: added column {col_name} to {table}")
        except sqlite3.OperationalError as e:
            logger.error(f"Migration error for {table}.{col_name}: {e}")

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS numbers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                phone_number TEXT NOT NULL,
                queue_type TEXT NOT NULL,
                price REAL NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value TEXT
            )
        ''')
        
        # Миграция: добираем недостающие колонки у уже существующих таблиц
        # (актуально для баз, созданных старой версией бота)
        ensure_columns(conn, 'users', {
            'username': 'TEXT',
            'balance': 'REAL DEFAULT 0',
            'is_admin': 'INTEGER DEFAULT 0',
            'is_worker': 'INTEGER DEFAULT 0',
            'is_banned': 'INTEGER DEFAULT 0',
            'registered_at': 'DATETIME DEFAULT CURRENT_TIMESTAMP',
        })
        ensure_columns(conn, 'numbers', {
            'status': "TEXT DEFAULT 'waiting'",
            'worker_id': 'INTEGER',
            'created_at': 'DATETIME DEFAULT CURRENT_TIMESTAMP',
            'started_at': 'DATETIME',
            'finished_at': 'DATETIME',
            'is_paid': 'INTEGER DEFAULT 0',
        })
        ensure_columns(conn, 'withdrawals', {
            'check_id': 'TEXT',
            'check_url': 'TEXT',
            'asset': 'TEXT',
            'status': "TEXT DEFAULT 'pending'",
            'created_at': 'DATETIME DEFAULT CURRENT_TIMESTAMP',
            'expires_at': 'DATETIME',
            'completed_at': 'DATETIME',
        })
        
        default_settings = [
            ('price', '3.0'),
            ('vip_price', '1.5'),
            ('treasury_balance', '0'),
            ('min_withdrawal', '10'),
            ('welcome_photo', '')
        ]
        
        for key, value in default_settings:
            cursor.execute(
                'INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)',
                (key, value)
            )
        
        # Создаем администратора (замените на свой ID)
        cursor.execute('''
            INSERT OR IGNORE INTO users (telegram_id, username, is_admin, registered_at)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
        ''', (123456789, 'admin'))

# --- CryptoBot: Crypto Pay API клиент ---

class CryptoPayError(Exception):
    pass

async def cryptopay_request(method: str, params: dict = None) -> dict:
    """Низкоуровневый вызов метода Crypto Pay API."""
    url = f"{CRYPTO_PAY_API_BASE}/{method}"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=params or {}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
    if not data.get("ok"):
        error = data.get("error", {})
        logger.error(f"CryptoPay API error [{method}]: {error}")
        raise CryptoPayError(error.get("name") or str(error) or "unknown_error")
    return data["result"]

async def cryptopay_create_check(amount: float, asset: str = None) -> dict:
    """Создаёт чек CryptoBot на выплату пользователю (метод createCheck)."""
    return await cryptopay_request("createCheck", {
        "asset": asset or CRYPTO_ASSET,
        "amount": f"{amount:.2f}",
    })

async def cryptopay_get_checks(check_ids: list = None) -> list:
    params = {}
    if check_ids:
        params["check_ids"] = ",".join(str(c) for c in check_ids)
    result = await cryptopay_request("getChecks", params)
    return result.get("items", [])

# --- Вспомогательные функции ---

def get_setting(key: str) -> str:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
        row = cursor.fetchone()
        return row['value'] if row else None

def set_setting(key: str, value: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
            (key, value)
        )

def get_user(telegram_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,))
        return cursor.fetchone()

def get_user_by_id(user_id: int):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
        return cursor.fetchone()

def get_or_create_user(telegram_id: int, username: str = None):
    with get_db() as conn:
        cursor = conn.cursor()
        user = cursor.execute(
            'SELECT * FROM users WHERE telegram_id = ?', (telegram_id,)
        ).fetchone()
        
        if not user:
            cursor.execute(
                'INSERT INTO users (telegram_id, username, registered_at) VALUES (?, ?, CURRENT_TIMESTAMP)',
                (telegram_id, username)
            )
            conn.commit()
            user = cursor.execute(
                'SELECT * FROM users WHERE telegram_id = ?', (telegram_id,)
            ).fetchone()
        
        return user

def get_user_numbers(user_id: int, status: str = None):
    with get_db() as conn:
        cursor = conn.cursor()
        if status:
            cursor.execute(
                'SELECT * FROM numbers WHERE user_id = ? AND status = ? ORDER BY created_at DESC',
                (user_id, status)
            )
        else:
            cursor.execute(
                'SELECT * FROM numbers WHERE user_id = ? ORDER BY created_at DESC',
                (user_id,)
            )
        return cursor.fetchall()

def get_worker_numbers(worker_id: int, status: str = None):
    with get_db() as conn:
        cursor = conn.cursor()
        if status:
            cursor.execute(
                'SELECT * FROM numbers WHERE worker_id = ? AND status = ?',
                (worker_id, status)
            )
        else:
            cursor.execute(
                'SELECT * FROM numbers WHERE worker_id = ?',
                (worker_id,)
            )
        return cursor.fetchall()

def get_next_number():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM numbers 
            WHERE status = 'waiting'
            ORDER BY 
                CASE queue_type 
                    WHEN 'vip' THEN 1 
                    WHEN 'regular' THEN 2 
                END,
                created_at ASC 
            LIMIT 1
        ''')
        return cursor.fetchone()

def add_balance(telegram_id: int, amount: float):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE users SET balance = balance + ? WHERE telegram_id = ?',
            (amount, telegram_id)
        )

def deduct_balance(telegram_id: int, amount: float) -> bool:
    """Атомарно списывает баланс, только если средств достаточно. Возвращает успех."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE users SET balance = balance - ? WHERE telegram_id = ? AND balance >= ?',
            (amount, telegram_id, amount)
        )
        return cursor.rowcount > 0

def get_user_role(user):
    if not user:
        return "Пользователь"
    if user['is_admin']:
        return "Администратор"
    if user['is_worker']:
        return "Работяга"
    return "Пользователь"

# --- Клавиатуры ---

def get_main_keyboard(telegram_id: int):
    user = get_user(telegram_id)
    buttons = [
        [KeyboardButton(text="📱 Сдать номер")],
        [KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="📋 Моя очередь")],
        [KeyboardButton(text="🆘 Тех поддержка")]
    ]
    
    if user and user['is_worker']:
        buttons.append([KeyboardButton(text="🔧 Панель работяги")])
    
    if user and user['is_admin']:
        buttons.append([KeyboardButton(text="⚙️ Админ панель")])
    
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True
    )

def get_admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="👥 Пользователи")],
            [KeyboardButton(text="💰 Балансы")],
            [KeyboardButton(text="💰 Пополнить казну")],
            [KeyboardButton(text="📨 Рассылка")],
            [KeyboardButton(text="📊 История выводов")],
            [KeyboardButton(text="📅 Номера за сегодня")],
            [KeyboardButton(text="💥 Расчет оплат")],
            [KeyboardButton(text="📝 Редактировать прайс")],
            [KeyboardButton(text="🖼 Установить фото")],
            [KeyboardButton(text="➕ Добавить работягу")],
            [KeyboardButton(text="📋 Список работяг")],
            [KeyboardButton(text="🔙 Закрыть")]
        ],
        resize_keyboard=True
    )

def get_worker_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Взять номер")],
            [KeyboardButton(text="📊 Моя статистика")],
            [KeyboardButton(text="❌ Закрыть")]
        ],
        resize_keyboard=True
    )

def get_withdrawal_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="10$", callback_data="withdraw_10"),
        InlineKeyboardButton(text="25$", callback_data="withdraw_25"),
        InlineKeyboardButton(text="50$", callback_data="withdraw_50")
    )
    builder.row(
        InlineKeyboardButton(text="100$", callback_data="withdraw_100"),
        InlineKeyboardButton(text="Весь баланс", callback_data="withdraw_all")
    )
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="withdraw_back")
    )
    return builder.as_markup()

def get_queue_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⚡ VIP (вне очереди) 1.5$", callback_data="queue_vip"),
        InlineKeyboardButton(text="💰 Обычная 3$", callback_data="queue_regular")
    )
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="queue_back")
    )
    return builder.as_markup()

def get_confirm_keyboard(phone: str, queue_type: str, price: float):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_{phone}_{queue_type}_{price}")
    )
    builder.row(
        InlineKeyboardButton(text="❌ Отмена", callback_data="confirm_cancel")
    )
    return builder.as_markup()

def get_worker_method_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔄 Перенос (Код)", callback_data="method_transfer"),
        InlineKeyboardButton(text="🔗 Связ", callback_data="method_link")
    )
    builder.row(
        InlineKeyboardButton(text="📱 Кюар", callback_data="method_qr"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="method_cancel")
    )
    return builder.as_markup()

def get_worker_stand_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Встал", callback_data="stand_yes"),
        InlineKeyboardButton(text="❌ Ошибка", callback_data="stand_no")
    )
    return builder.as_markup()

def get_worker_fly_keyboard(number_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="💥 Слет", callback_data=f"fly_{number_id}")
    )
    return builder.as_markup()

def get_user_action_keyboard(number_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Сделал", callback_data=f"user_done_{number_id}")
    )
    return builder.as_markup()

# --- Обработчики команд ---

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    
    user = get_or_create_user(
        message.from_user.id,
        message.from_user.username
    )
    
    price = get_setting('price') or '3.0'
    welcome_photo = get_setting('welcome_photo')
    
    role = get_user_role(user)
    
    welcome_text = (
        f"👋 Добро пожаловать в Babrito WA!\n\n"
        f"👤 Username: @{message.from_user.username or 'Не указан'}\n"
        f"💰 Текущая цена за аккаунт: {price}$\n"
        f"💵 Баланс: {user['balance']}$\n"
        f"🎭 Роль: {role}\n\n"
        "Выберите действие:"
    )
    
    if welcome_photo and os.path.exists(welcome_photo):
        try:
            photo = FSInputFile(welcome_photo)
            await message.answer_photo(
                photo=photo,
                caption=welcome_text,
                reply_markup=get_main_keyboard(message.from_user.id)
            )
            return
        except Exception as e:
            logger.error(f"Error sending welcome photo: {e}")
    
    await message.answer(
        welcome_text,
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user or not user['is_admin']:
        await message.answer("⛔ У вас нет доступа к админ-панели.")
        return
    
    await state.clear()
    await message.answer(
        "⚙️ Админ панель\nВыберите действие:",
        reply_markup=get_admin_keyboard()
    )

@dp.message(Command("rabotyaga"))
async def cmd_worker(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user or not user['is_worker']:
        await message.answer("⛔ У вас нет доступа к панели работяги.")
        return
    
    await state.clear()
    await message.answer(
        "🔧 Панель работяги\nВыберите действие:",
        reply_markup=get_worker_keyboard()
    )

@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "❌ Действие отменено.",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

# --- Обработчики главного меню ---

@dp.message(F.text == "📱 Сдать номер")
async def sell_number(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("❌ Ошибка: пользователь не найден.")
        return
    if user['is_banned']:
        await message.answer("⛔ Вы заблокированы.")
        return
    
    await state.set_state(UserStates.entering_phone)
    await message.answer(
        "📱 Введите номер телефона.\n\n"
        "Поддерживаемые форматы:\n"
        "• 11 цифр: 7XXXXXXXXXX или 8XXXXXXXXXX\n"
        "• 10 цифр: 9XXXXXXXXX → автоматически 7XXXXXXXXXX\n"
        "• С поддержкой символов: +, пробелы, скобки\n\n"
        "Примеры: +79123456789, 79123456789, 9123456789\n\n"
        "Для отмены отправьте /cancel"
    )

@dp.message(UserStates.entering_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    cleaned = re.sub(r'[+\s()\-]', '', phone)
    
    if len(cleaned) == 11 and cleaned.startswith('7'):
        formatted = cleaned
    elif len(cleaned) == 11 and cleaned.startswith('8'):
        formatted = '7' + cleaned[1:]
    elif len(cleaned) == 10 and cleaned.startswith('9'):
        formatted = '7' + cleaned
    else:
        await message.answer(
            "❌ Неверный формат номера.\n"
            "Пожалуйста, введите номер в одном из поддерживаемых форматов.\n\n"
            "Для отмены отправьте /cancel"
        )
        return
    
    await state.update_data(phone=formatted)
    await state.set_state(UserStates.selecting_queue)
    
    await message.answer(
        f"📱 Номер: +{formatted}\n\n"
        "Выберите тип очереди:",
        reply_markup=get_queue_keyboard()
    )

@dp.callback_query(F.data.startswith("queue_"))
async def select_queue(callback: CallbackQuery, state: FSMContext):
    queue_type = callback.data.split("_")[1]
    
    if queue_type == "back":
        await state.clear()
        await callback.message.delete()
        await callback.message.answer(
            "Главное меню",
            reply_markup=get_main_keyboard(callback.from_user.id)
        )
        await callback.answer()
        return
    
    data = await state.get_data()
    phone = data.get('phone')
    
    if queue_type == 'vip':
        price = float(get_setting('vip_price') or '1.5')
    else:
        price = float(get_setting('price') or '3.0')
    
    await state.update_data(queue_type=queue_type, price=price)
    await state.set_state(UserStates.confirm_number)
    
    queue_names = {
        'vip': '⚡ VIP (вне очереди)',
        'regular': '💰 Обычная'
    }
    
    text = (
        f"📱 Номер: +{phone}\n"
        f"📋 Тип: {queue_names[queue_type]}\n"
        f"💵 Цена: {price}$ за 10+ минут\n"
        f"📊 Статус: Ожидание\n\n"
        "Подтвердите добавление номера:"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=get_confirm_keyboard(phone, queue_type, price)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_"))
async def confirm_number(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    phone = parts[1]
    queue_type = parts[2]
    price = float(parts[3])
    
    user = get_user(callback.from_user.id)
    if not user:
        await callback.message.answer("❌ Ошибка: пользователь не найден.")
        await callback.answer()
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO numbers (user_id, phone_number, queue_type, price, status, created_at)
            VALUES (?, ?, ?, ?, 'waiting', CURRENT_TIMESTAMP)
        ''', (user['id'], phone, queue_type, price))
        conn.commit()
    
    await state.clear()
    
    await callback.message.edit_text(
        f"✅ Номер +{phone} успешно добавлен в очередь!\n"
        f"💵 Цена: {price}$\n"
        f"📊 Статус: Ожидание",
        reply_markup=None
    )
    await callback.message.answer(
        "Главное меню",
        reply_markup=get_main_keyboard(callback.from_user.id)
    )
    await callback.answer()

@dp.callback_query(F.data == "confirm_cancel")
async def confirm_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "❌ Добавление номера отменено.",
        reply_markup=None
    )
    await callback.message.answer(
        "Главное меню",
        reply_markup=get_main_keyboard(callback.from_user.id)
    )
    await callback.answer()

# --- Профиль ---

@dp.message(F.text == "👤 Профиль")
async def profile(message: Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("❌ Ошибка: пользователь не найден.")
        return
    
    role = get_user_role(user)
    
    text = (
        f"👤 Профиль\n\n"
        f"🔹 Username: @{message.from_user.username or 'Не указан'}\n"
        f"💰 Цена за аккаунт: {get_setting('price') or '3.0'}$\n"
        f"💵 Баланс: {user['balance']}$\n"
        f"📅 Дата регистрации: {user['registered_at']}\n"
        f"🎭 Роль: {role}"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data="profile_stats"),
        InlineKeyboardButton(text="💸 Вывод", callback_data="profile_withdraw")
    )
    builder.row(
        InlineKeyboardButton(text="📋 Моя очередь", callback_data="profile_queue"),
        InlineKeyboardButton(text="🔙 Назад", callback_data="profile_back")
    )
    
    await message.answer(text, reply_markup=builder.as_markup())

@dp.message(F.text == "📋 Моя очередь")
async def my_queue(message: Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("❌ Ошибка: пользователь не найден.")
        return
    
    numbers = get_user_numbers(user['id'])
    
    if not numbers:
        await message.answer("📭 У вас нет номеров в очереди.")
        return
    
    text = "📋 Моя очередь:\n\n"
    queue_names = {'vip': '⚡ VIP', 'regular': '💰 Обычная'}
    status_names = {
        'waiting': '⏳ Ожидает',
        'in_progress': '🔄 В работе',
        'completed': '✅ Отстоял',
        'failed': '❌ Слет'
    }
    
    for num in numbers[:10]:
        text += (
            f"📱 +{num['phone_number']}\n"
            f"📋 {queue_names.get(num['queue_type'], num['queue_type'])}\n"
            f"💵 {num['price']}$\n"
            f"📊 {status_names.get(num['status'], num['status'])}\n"
            f"🕐 {num['created_at']}\n\n"
        )
    
    if len(numbers) > 10:
        text += f"\n... и еще {len(numbers) - 10} номеров"
    
    await message.answer(text)

@dp.message(F.text == "🆘 Тех поддержка")
async def support(message: Message):
    await message.answer(
        "🆘 Тех поддержка\n\n"
        "Если у вас возникли вопросы или проблемы, обратитесь в нашу службу поддержки:\n"
        "👤 @support_bot\n\n"
        "Мы всегда рады помочь! 😊"
    )

# --- Профиль callback ---

@dp.callback_query(F.data == "profile_back")
async def profile_back(callback: CallbackQuery):
    await callback.message.delete()
    await callback.message.answer(
        "Главное меню",
        reply_markup=get_main_keyboard(callback.from_user.id)
    )
    await callback.answer()

@dp.callback_query(F.data == "profile_queue")
async def profile_queue(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await callback.message.answer("❌ Ошибка: пользователь не найден.")
        await callback.answer()
        return
    
    numbers = get_user_numbers(user['id'])
    
    if not numbers:
        await callback.message.answer("📭 У вас нет номеров в очереди.")
        await callback.answer()
        return
    
    text = "📋 Моя очередь:\n\n"
    queue_names = {'vip': '⚡ VIP', 'regular': '💰 Обычная'}
    status_names = {
        'waiting': '⏳ Ожидает',
        'in_progress': '🔄 В работе',
        'completed': '✅ Отстоял',
        'failed': '❌ Слет'
    }
    
    for num in numbers[:10]:
        text += (
            f"📱 +{num['phone_number']}\n"
            f"📋 {queue_names.get(num['queue_type'], num['queue_type'])}\n"
            f"💵 {num['price']}$\n"
            f"📊 {status_names.get(num['status'], num['status'])}\n"
            f"🕐 {num['created_at']}\n\n"
        )
    
    if len(numbers) > 10:
        text += f"\n... и еще {len(numbers) - 10} номеров"
    
    await callback.message.answer(text)
    await callback.answer()

@dp.callback_query(F.data == "profile_stats")
async def profile_stats(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        await callback.message.answer("❌ Ошибка: пользователь не найден.")
        await callback.answer()
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute('''
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
            FROM numbers 
            WHERE user_id = ? AND DATE(created_at) = ?
        ''', (user['id'], today))
        today_stats = cursor.fetchone()
        
        cursor.execute('''
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
            FROM numbers 
            WHERE user_id = ?
        ''', (user['id'],))
        all_stats = cursor.fetchone()
        
        cursor.execute('''
            SELECT COALESCE(SUM(price), 0) as profit
            FROM numbers 
            WHERE user_id = ? AND status = 'completed' AND is_paid = 1
        ''', (user['id'],))
        profit = cursor.fetchone()
    
    today_text = f"За сегодня:\n• Взято: {today_stats['total']}\n• Отстояло: {today_stats['completed']}\n• Слёт: {today_stats['failed']}"
    all_text = f"За всё время:\n• Взято: {all_stats['total']}\n• Отстояло: {all_stats['completed']}\n• Слёт: {all_stats['failed']}\n• Прибыль: {profit['profit']}$"
    
    await callback.message.answer(
        f"📊 Статистика\n\n{today_text}\n\n{all_text}"
    )
    await callback.answer()

@dp.callback_query(F.data == "profile_withdraw")
async def profile_withdraw(callback: CallbackQuery, state: FSMContext):
    user = get_user(callback.from_user.id)
    if not user:
        await callback.message.answer("❌ Ошибка: пользователь не найден.")
        await callback.answer()
        return
    
    min_withdrawal = float(get_setting('min_withdrawal') or '10')
    
    if user['balance'] < min_withdrawal:
        await callback.message.answer(
            f"❌ Минимальная сумма вывода: {min_withdrawal}$\n"
            f"Ваш баланс: {user['balance']}$"
        )
        await callback.answer()
        return
    
    await state.set_state(UserStates.selecting_withdrawal)
    
    text = (
        f"💸 Вывод средств\n\n"
        f"💰 Ваш баланс: {user['balance']}$\n"
        f"📉 Минимальная сумма: {min_withdrawal}$\n\n"
        "Выберите сумму:"
    )
    
    await callback.message.answer(text, reply_markup=get_withdrawal_keyboard())
    await callback.answer()

# --- Вывод средств ---

@dp.callback_query(F.data.startswith("withdraw_"), UserStates.selecting_withdrawal)
async def process_withdrawal(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split("_")[1]
    user = get_user(callback.from_user.id)
    
    if not user:
        await callback.message.answer("❌ Ошибка: пользователь не найден.")
        await callback.answer()
        return
    
    if action == "back":
        await state.clear()
        await callback.message.delete()
        await callback.message.answer(
            "Главное меню",
            reply_markup=get_main_keyboard(callback.from_user.id)
        )
        await callback.answer()
        return
    
    if action == "all":
        amount = user['balance']
    else:
        amount = float(action)
    
    if amount > user['balance']:
        await callback.message.answer(
            f"❌ Недостаточно средств.\n"
            f"Ваш баланс: {user['balance']}$\n"
            f"Запрошено: {amount}$"
        )
        await callback.answer()
        return
    
    # Списываем баланс сразу, атомарно и с проверкой — деньги реально уйдут через CryptoBot
    if not deduct_balance(user['telegram_id'], amount):
        await callback.message.answer("❌ Недостаточно средств.")
        await callback.answer()
        return
    
    try:
        check = await cryptopay_create_check(amount, CRYPTO_ASSET)
    except (CryptoPayError, aiohttp.ClientError, asyncio.TimeoutError) as e:
        # Не удалось создать чек в CryptoBot — возвращаем деньги пользователю
        add_balance(user['telegram_id'], amount)
        logger.error(f"Failed to create CryptoBot check for user {user['telegram_id']}: {e}")
        await callback.message.answer(
            "❌ Не удалось создать чек на вывод через CryptoBot. Средства не списаны, попробуйте позже."
        )
        await callback.answer()
        return
    
    check_id = str(check["check_id"])
    check_url = check.get("bot_check_url") or f"https://t.me/CryptoBot?start=C-{check.get('hash', '')}"
    expires_at = datetime.now() + timedelta(hours=1)
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO withdrawals (user_id, amount, check_id, check_url, asset, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (user['id'], amount, check_id, check_url, CRYPTO_ASSET, expires_at))
        conn.commit()
        withdrawal_id = cursor.lastrowid
    
    await state.clear()
    
    text = (
        f"✅ Чек CryptoBot создан!\n\n"
        f"💰 Сумма: {amount} {CRYPTO_ASSET}\n"
        f"🆔 ID чека: {check_id}\n\n"
        f"Нажмите кнопку ниже, чтобы получить средства через @CryptoBot."
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔗 Получить в CryptoBot", url=check_url),
        InlineKeyboardButton(text="✅ Проверить статус", callback_data=f"check_status_{withdrawal_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="check_back")
    )
    
    await callback.message.answer(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("check_status_"))
async def check_withdrawal_status(callback: CallbackQuery):
    withdrawal_id = callback.data.rsplit("_", 1)[1]
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM withdrawals WHERE id = ?', (withdrawal_id,))
        withdrawal = cursor.fetchone()
    
    if not withdrawal:
        await callback.message.answer("❌ Чек не найден.")
        await callback.answer()
        return
    
    # Подтягиваем актуальный статус чека напрямую из CryptoBot, если он ещё не финализирован
    if withdrawal['status'] == 'pending' and withdrawal['check_id']:
        try:
            items = await cryptopay_get_checks([withdrawal['check_id']])
            if items:
                remote_status = items[0].get("status")  # 'active' | 'activated'
                if remote_status == "activated":
                    with get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            "UPDATE withdrawals SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (withdrawal_id,)
                        )
                    withdrawal = dict(withdrawal)
                    withdrawal['status'] = 'completed'
        except (CryptoPayError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"Failed to fetch CryptoBot check status: {e}")
    
    status_names = {
        'pending': '⏳ Ожидание получения',
        'completed': '✅ Получено',
        'expired': '❌ Истек'
    }
    
    await callback.message.answer(
        f"📊 Статус чека:\n\n"
        f"🆔 ID: {withdrawal['check_id']}\n"
        f"💰 Сумма: {withdrawal['amount']} {withdrawal['asset'] or CRYPTO_ASSET}\n"
        f"📊 Статус: {status_names.get(withdrawal['status'], withdrawal['status'])}\n"
        f"📅 Создан: {withdrawal['created_at']}"
    )
    await callback.answer()

@dp.callback_query(F.data == "check_back")
async def check_back(callback: CallbackQuery):
    await callback.message.delete()
    await callback.message.answer(
        "Главное меню",
        reply_markup=get_main_keyboard(callback.from_user.id)
    )
    await callback.answer()

# --- Админ панель ---

@dp.message(F.text == "⚙️ Админ панель")
async def admin_panel(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user or not user['is_admin']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    await state.clear()
    await message.answer(
        "⚙️ Админ панель\nВыберите действие:",
        reply_markup=get_admin_keyboard()
    )

@dp.message(F.text == "🔙 Закрыть")
async def close_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Главное меню",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

# --- Админ: Статистика ---

@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    user = get_user(message.from_user.id)
    if not user or not user['is_admin']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) as count FROM users')
        total_users = cursor.fetchone()['count']
        
        cursor.execute('SELECT COUNT(*) as count FROM users WHERE is_banned = 1')
        banned_users = cursor.fetchone()['count']
        
        cursor.execute('SELECT COALESCE(SUM(balance), 0) as total FROM users')
        total_balance = cursor.fetchone()['total']
        
        treasury = float(get_setting('treasury_balance') or '0')
        
        cursor.execute('''
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
            FROM numbers
        ''')
        stats = cursor.fetchone()
        
        cursor.execute('SELECT COALESCE(SUM(price), 0) as profit FROM numbers WHERE status = "completed" AND is_paid = 1')
        profit = cursor.fetchone()['profit']
    
    text = (
        f"📊 Статистика\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"🔒 Заблокировано: {banned_users}\n"
        f"💰 Общий баланс: {total_balance}$\n"
        f"🏦 Казнa: {treasury}$\n\n"
        f"📱 Номера:\n"
        f"• Всего взято: {stats['total']}\n"
        f"• Отстояло: {stats['completed']}\n"
        f"• Слетело: {stats['failed']}\n"
        f"• Прибыль: {profit}$"
    )
    
    await message.answer(text)

# --- Админ: Пользователи ---

@dp.message(F.text == "👥 Пользователи")
async def admin_users(message: Message):
    user = get_user(message.from_user.id)
    if not user or not user['is_admin']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM users 
            ORDER BY registered_at DESC 
            LIMIT 20
        ''')
        users = cursor.fetchall()
    
    if not users:
        await message.answer("👥 Пользователей нет.")
        return
    
    text = "👥 Пользователи (последние 20):\n\n"
    for u in users:
        status = "✅ активен" if not u['is_banned'] else "🔒 заблокирован"
        role = "👑 админ" if u['is_admin'] else "🛠 работяга" if u['is_worker'] else "👤 пользователь"
        text += f"• {status} | {role} | @{u['username'] or 'Не указан'} | {u['balance']}$\n"
    
    await message.answer(text)

# --- Админ: Балансы ---

@dp.message(F.text == "💰 Балансы")
async def admin_balances(message: Message):
    user = get_user(message.from_user.id)
    if not user or not user['is_admin']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT username, balance 
            FROM users 
            ORDER BY balance DESC 
            LIMIT 10
        ''')
        users = cursor.fetchall()
    
    if not users:
        await message.answer("💰 Нет пользователей с балансом.")
        return
    
    text = "💰 Топ-10 по балансу:\n\n"
    for i, u in enumerate(users, 1):
        text += f"{i}. @{u['username'] or 'Не указан'} - {u['balance']}$\n"
    
    await message.answer(text)

# --- Админ: Пополнить казну ---

@dp.message(F.text == "💰 Пополнить казну")
async def admin_treasury(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user or not user['is_admin']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    treasury = float(get_setting('treasury_balance') or '0')
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="10$", callback_data="treasury_10"),
        InlineKeyboardButton(text="25$", callback_data="treasury_25"),
        InlineKeyboardButton(text="50$", callback_data="treasury_50")
    )
    builder.row(
        InlineKeyboardButton(text="100$", callback_data="treasury_100"),
        InlineKeyboardButton(text="500$", callback_data="treasury_500"),
        InlineKeyboardButton(text="1000$", callback_data="treasury_1000")
    )
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="treasury_back")
    )
    
    await message.answer(
        f"🏦 Текущий баланс казны: {treasury}$\n\n"
        "Выберите сумму для пополнения:",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("treasury_"))
async def process_treasury(callback: CallbackQuery):
    action = callback.data.split("_")[1]
    
    if action == "back":
        await callback.message.delete()
        await callback.message.answer(
            "Админ панель",
            reply_markup=get_admin_keyboard()
        )
        await callback.answer()
        return
    
    amount = float(action)
    treasury = float(get_setting('treasury_balance') or '0')
    new_treasury = treasury + amount
    set_setting('treasury_balance', str(new_treasury))
    
    await callback.message.edit_text(
        f"✅ Казнa пополнена на {amount}$!\n"
        f"🏦 Новый баланс: {new_treasury}$",
        reply_markup=None
    )
    await callback.answer()

# --- Админ: Рассылка ---

@dp.message(F.text == "📨 Рассылка")
async def admin_broadcast(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user or not user['is_admin']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    await state.set_state(UserStates.admin_broadcast)
    await message.answer(
        "📨 Отправьте текст для рассылки.\n\n"
        "Для отмены отправьте /cancel"
    )

@dp.message(UserStates.admin_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    text = message.text
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT telegram_id FROM users WHERE is_banned = 0')
        users = cursor.fetchall()
    
    sent = 0
    errors = 0
    
    for user in users:
        try:
            await bot.send_message(user['telegram_id'], text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Broadcast error to {user['telegram_id']}: {e}")
            errors += 1
    
    await state.clear()
    await message.answer(
        f"✅ Рассылка завершена!\n"
        f"📨 Отправлено: {sent}\n"
        f"❌ Ошибок: {errors}"
    )

# --- Админ: История выводов ---

@dp.message(F.text == "📊 История выводов")
async def admin_withdrawals(message: Message):
    user = get_user(message.from_user.id)
    if not user or not user['is_admin']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT w.*, u.username 
            FROM withdrawals w
            JOIN users u ON w.user_id = u.id
            ORDER BY w.created_at DESC 
            LIMIT 20
        ''')
        withdrawals = cursor.fetchall()
    
    if not withdrawals:
        await message.answer("📊 История выводов пуста.")
        return
    
    text = "📊 Последние 20 выводов:\n\n"
    status_icons = {'pending': '⏳', 'completed': '✅', 'expired': '❌'}
    
    for w in withdrawals:
        text += (
            f"{status_icons.get(w['status'], '❓')} "
            f"@{w['username'] or 'Не указан'} | "
            f"{w['amount']}$ | "
            f"{w['created_at']}\n"
        )
    
    await message.answer(text)

# --- Админ: Номера за сегодня ---

@dp.message(F.text == "📅 Номера за сегодня")
async def admin_numbers_today(message: Message):
    user = get_user(message.from_user.id)
    if not user or not user['is_admin']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT n.*, u.username as worker_username
            FROM numbers n
            JOIN users u ON n.worker_id = u.id
            WHERE n.worker_id IS NOT NULL
              AND DATE(n.started_at) = DATE('now')
            ORDER BY n.started_at DESC
        ''')
        numbers = cursor.fetchall()
    
    if not numbers:
        await message.answer("📅 Сегодня работяги ещё не брали номеров.")
        return
    
    queue_names = {'vip': '⚡ VIP', 'regular': '💰 Обычная'}
    status_names = {
        'waiting': '⏳ Ожидает',
        'in_progress': '🔄 В работе',
        'completed': '✅ Отстоял',
        'failed': '❌ Слет'
    }
    
    def hhmm(dt_str):
        if not dt_str:
            return None
        try:
            return dt_str.split(' ')[1][:5]
        except (IndexError, AttributeError):
            return dt_str
    
    chunks = [f"📅 Номера за сегодня (взято работягами): {len(numbers)}\n\n"]
    
    for num in numbers:
        time_line = ""
        if num['started_at']:
            time_line += f"🟢 Встал: {hhmm(num['started_at'])}"
        else:
            time_line += "🟢 Встал: —"
        
        if num['finished_at']:
            time_line += f" | 🏁 Слёт: {hhmm(num['finished_at'])}"
            try:
                started = datetime.strptime(num['started_at'], '%Y-%m-%d %H:%M:%S')
                finished = datetime.strptime(num['finished_at'], '%Y-%m-%d %H:%M:%S')
                duration = (finished - started).total_seconds() / 60
                time_line += f" ({duration:.0f} мин)"
            except (ValueError, TypeError):
                pass
        elif num['status'] == 'in_progress':
            time_line += " | 🔄 ещё в работе"
        
        entry = (
            f"📱 +{num['phone_number']}\n"
            f"👤 Работяга: @{num['worker_username'] or 'Не указан'}\n"
            f"📋 {queue_names.get(num['queue_type'], num['queue_type'])} | "
            f"{status_names.get(num['status'], num['status'])}\n"
            f"💵 {num['price']}$\n"
            f"{time_line}\n\n"
        )
        if len(chunks[-1]) + len(entry) > 3500:
            chunks.append(entry)
        else:
            chunks[-1] += entry
    
    for chunk in chunks:
        await message.answer(chunk)

# --- Админ: Расчет оплат ---

@dp.message(F.text == "💥 Расчет оплат")
async def admin_calculate_payments(message: Message):
    user = get_user(message.from_user.id)
    if not user or not user['is_admin']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT n.*, u.telegram_id 
            FROM numbers n
            JOIN users u ON n.user_id = u.id
            WHERE n.queue_type = 'regular' 
            AND n.status = 'completed' 
            AND n.is_paid = 0
        ''')
        numbers = cursor.fetchall()

        if not numbers:
            await message.answer("💥 Нет номеров для оплаты.")
            return

        total_amount = 0
        paid_count = 0

        for num in numbers:
            add_balance(num['telegram_id'], num['price'])
            cursor.execute('UPDATE numbers SET is_paid = 1 WHERE id = ?', (num['id'],))
            total_amount += num['price']
            paid_count += 1

            try:
                await bot.send_message(
                    num['telegram_id'],
                    f"💰 Начислено {num['price']}$ за номер +{num['phone_number']}\n"
                    f"Тип: Обычная"
                )
            except Exception as e:
                logger.error(f"Error notifying user {num['telegram_id']}: {e}")
    
    await message.answer(
        f"✅ Расчет оплат завершен!\n"
        f"💳 Оплачено номеров: {paid_count}\n"
        f"💰 Общая сумма: {total_amount}$"
    )

# --- Админ: Редактировать прайс ---

@dp.message(F.text == "📝 Редактировать прайс")
async def admin_edit_price(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user or not user['is_admin']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    current_price = get_setting('price') or '3.0'
    current_vip_price = get_setting('vip_price') or '1.5'
    
    await state.set_state(UserStates.admin_edit_price)
    await message.answer(
        f"📝 Редактирование прайса\n\n"
        f"💰 Текущая цена (обычная): {current_price}$\n"
        f"⚡ Текущая цена (VIP): {current_vip_price}$\n\n"
        f"Введите новую цену для обычной очереди:\n"
        f"Для отмены отправьте /cancel"
    )

@dp.message(UserStates.admin_edit_price)
async def process_edit_price(message: Message, state: FSMContext):
    try:
        price = float(message.text)
        if price <= 0:
            await message.answer("❌ Цена должна быть положительным числом.")
            return
        
        set_setting('price', str(price))
        await state.clear()
        await message.answer(f"✅ Цена обновлена!\n💰 Новая цена: {price}$")
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число.")

# --- Админ: Установить фото ---

@dp.message(F.text == "🖼 Установить фото")
async def admin_set_photo(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user or not user['is_admin']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    await state.set_state(UserStates.admin_set_photo)
    await message.answer(
        "🖼 Отправьте фото для приветственного сообщения.\n\n"
        "Фото будет отображаться при запуске бота.\n"
        "Для отмены отправьте /cancel"
    )

@dp.message(UserStates.admin_set_photo, F.photo)
async def process_set_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    
    file_path = f"welcome_photo.jpg"
    await bot.download_file(file.file_path, file_path)
    
    set_setting('welcome_photo', file_path)
    await state.clear()
    
    await message.answer("✅ Приветственное фото установлено!")

@dp.message(UserStates.admin_set_photo)
async def set_photo_invalid(message: Message):
    await message.answer("❌ Пожалуйста, отправьте фото.\n\nДля отмены отправьте /cancel")

# --- Админ: Добавить работягу ---

@dp.message(F.text == "➕ Добавить работягу")
async def admin_add_worker(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user or not user['is_admin']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    await state.set_state(UserStates.admin_add_worker)
    await message.answer(
        "➕ Добавление работяги\n\n"
        "Введите Telegram ID пользователя:\n"
        "Для отмены отправьте /cancel"
    )

@dp.message(UserStates.admin_add_worker)
async def process_add_worker(message: Message, state: FSMContext):
    try:
        telegram_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректный Telegram ID (число).")
        return
    
    user = get_user(telegram_id)
    if not user:
        await message.answer("❌ Пользователь не найден. Убедитесь, что он запустил бота.")
        return
    
    if user['is_worker']:
        await message.answer("❌ Пользователь уже является работягой.")
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET is_worker = 1 WHERE telegram_id = ?', (telegram_id,))
    
    await state.clear()
    await message.answer(f"✅ Пользователь @{user['username'] or 'Не указан'} назначен работягой!")
    
    try:
        await bot.send_message(
            telegram_id,
            "🎉 Вас назначили работягой!\n"
            "Теперь вы можете использовать команду /rabotyaga"
        )
    except Exception as e:
        logger.error(f"Error notifying worker: {e}")

# --- Админ: Список работяг ---

@dp.message(F.text == "📋 Список работяг")
async def admin_workers_list(message: Message):
    user = get_user(message.from_user.id)
    if not user or not user['is_admin']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE is_worker = 1')
        workers = cursor.fetchall()
    
    if not workers:
        await message.answer("📋 Нет работяг.")
        return
    
    text = "📋 Список работяг:\n\n"
    for w in workers:
        text += (
            f"• @{w['username'] or 'Не указан'}\n"
            f"  🆔 ID: {w['telegram_id']}\n"
            f"  💰 Баланс: {w['balance']}$\n"
            f"  {'👑 Админ' if w['is_admin'] else '🛠 Работяга'}\n\n"
        )
    
    await message.answer(text)

# --- Работяга ---

@dp.message(F.text == "🔧 Панель работяги")
async def worker_panel(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user or not user['is_worker']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    await state.clear()
    await message.answer(
        "🔧 Панель работяги\nВыберите действие:",
        reply_markup=get_worker_keyboard()
    )

@dp.message(F.text == "❌ Закрыть")
async def close_worker(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Главное меню",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.message(F.text == "📱 Взять номер")
async def worker_take_number(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user or not user['is_worker']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    active_numbers = get_worker_numbers(user['id'], 'in_progress')
    if active_numbers:
        await message.answer("❌ У вас уже есть активный номер!\nСначала завершите работу с текущим номером.")
        return
    
    number = get_next_number()
    if not number:
        await message.answer("📭 Очередь пуста. Нет номеров для взятия.")
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE numbers 
            SET worker_id = ?, status = 'in_progress', started_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (user['id'], number['id']))
    
    await state.update_data(number_id=number['id'])
    await state.set_state(UserStates.worker_select_method)
    
    queue_names = {'vip': '⚡ VIP', 'regular': '💰 Обычная'}
    
    await message.answer(
        f"📱 Номер: +{number['phone_number']}\n"
        f"📋 Тип: {queue_names.get(number['queue_type'], number['queue_type'])}\n"
        f"💵 Цена: {number['price']}$\n\n"
        "Выберите способ конекта:",
        reply_markup=get_worker_method_keyboard()
    )

@dp.callback_query(F.data.startswith("method_"), UserStates.worker_select_method)
async def worker_select_method(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split("_")[1]
    
    if method == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Взятие номера отменено.", reply_markup=None)
        await callback.message.answer(
            "Главное меню",
            reply_markup=get_main_keyboard(callback.from_user.id)
        )
        await callback.answer()
        return
    
    data = await state.get_data()
    number_id = data.get('number_id')
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM numbers WHERE id = ?', (number_id,))
        number = cursor.fetchone()
        if number:
            cursor.execute('SELECT * FROM users WHERE id = ?', (number['user_id'],))
            user = cursor.fetchone()
        else:
            user = None
    
    if not number or not user:
        await callback.message.answer("❌ Ошибка: номер не найден.")
        await callback.answer()
        return
    
    if method == "transfer":
        await state.update_data(method='transfer')
        await state.set_state(UserStates.worker_waiting_code)
        
        await callback.message.edit_text(
            f"🔄 Перенос (Код)\n\n"
            f"📱 Номер: +{number['phone_number']}\n\n"
            "⏳ Ожидание кода от пользователя...\n"
            "Пользователь получил запрос на ввод кода."
        )
        
        try:
            await bot.send_message(
                user['telegram_id'],
                f"📱 Для номера +{number['phone_number']} требуется код переноса.\n\n"
                "Пожалуйста, введите код:"
            )
        except Exception as e:
            logger.error(f"Error sending code request to user: {e}")
        
        await callback.answer()
        return
    
    elif method == "link":
        await state.update_data(method='link')
        await state.set_state(UserStates.worker_waiting_photo)
        
        await callback.message.edit_text(
            f"🔗 Связ\n\n"
            f"📱 Номер: +{number['phone_number']}\n\n"
            "Отправьте фото для связки:"
        )
        await callback.answer()
        return
    
    elif method == "qr":
        await state.update_data(method='qr')
        await state.set_state(UserStates.worker_waiting_qr)
        
        await callback.message.edit_text(
            f"📱 Кюар\n\n"
            f"📱 Номер: +{number['phone_number']}\n\n"
            "Отправьте QR-код:"
        )
        await callback.answer()
        return

# --- Работяга: Перенос (Код) ---

@dp.message(UserStates.worker_waiting_code)
async def worker_receive_code(message: Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    number_id = data.get('number_id')
    
    if not code or len(code) < 4:
        await message.answer("❌ Пожалуйста, введите корректный код.")
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM numbers WHERE id = ?', (number_id,))
        number = cursor.fetchone()
    
    if not number:
        await message.answer("❌ Номер не найден.")
        return
    
    await message.answer(
        f"✅ Код получен!\n"
        f"📱 Номер: +{number['phone_number']}\n"
        f"🔑 Код: {code}\n\n"
        "Нажмите 'Встал' после успешного переноса или 'Ошибка':",
        reply_markup=get_worker_stand_keyboard()
    )
    
    await state.update_data(code=code)
    await state.set_state(UserStates.worker_confirm_stand)

# --- Работяга: Связ (Фото) ---

@dp.message(UserStates.worker_waiting_photo, F.photo)
async def worker_receive_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    number_id = data.get('number_id')
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM numbers WHERE id = ?', (number_id,))
        number = cursor.fetchone()
        if number:
            cursor.execute('SELECT * FROM users WHERE id = ?', (number['user_id'],))
            user = cursor.fetchone()
        else:
            user = None
    
    if not number or not user:
        await message.answer("❌ Ошибка: номер не найден.")
        return
    
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_path = f"link_photo_{number_id}.jpg"
    await bot.download_file(file.file_path, file_path)
    
    try:
        await bot.send_photo(
            user['telegram_id'],
            photo=FSInputFile(file_path),
            caption=f"📱 Для номера +{number['phone_number']}\n\n"
                    "Пожалуйста, подтвердите выполнение:",
            reply_markup=get_user_action_keyboard(number_id)
        )
    except Exception as e:
        logger.error(f"Error sending photo to user: {e}")
    
    await message.answer(
        f"✅ Фото отправлено пользователю!\n"
        f"📱 Номер: +{number['phone_number']}\n\n"
        "Ожидайте подтверждения от пользователя..."
    )
    
    await state.set_state(UserStates.worker_confirm_stand)

@dp.message(UserStates.worker_waiting_photo)
async def worker_photo_invalid(message: Message):
    await message.answer("❌ Пожалуйста, отправьте фото для связки.")

# --- Работяга: Кюар ---

@dp.message(UserStates.worker_waiting_qr, F.photo)
async def worker_receive_qr(message: Message, state: FSMContext):
    data = await state.get_data()
    number_id = data.get('number_id')
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM numbers WHERE id = ?', (number_id,))
        number = cursor.fetchone()
        if number:
            cursor.execute('SELECT * FROM users WHERE id = ?', (number['user_id'],))
            user = cursor.fetchone()
        else:
            user = None
    
    if not number or not user:
        await message.answer("❌ Ошибка: номер не найден.")
        return
    
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_path = f"qr_{number_id}.jpg"
    await bot.download_file(file.file_path, file_path)
    
    try:
        await bot.send_photo(
            user['telegram_id'],
            photo=FSInputFile(file_path),
            caption=f"📱 Для номера +{number['phone_number']}\n\n"
                    "Пожалуйста, отсканируйте QR-код и подтвердите выполнение:",
            reply_markup=get_user_action_keyboard(number_id)
        )
    except Exception as e:
        logger.error(f"Error sending QR to user: {e}")
    
    await message.answer(
        f"✅ QR-код отправлен пользователю!\n"
        f"📱 Номер: +{number['phone_number']}\n\n"
        "Ожидайте подтверждения от пользователя..."
    )
    
    await state.set_state(UserStates.worker_confirm_stand)

@dp.message(UserStates.worker_waiting_qr)
async def worker_qr_invalid(message: Message):
    await message.answer("❌ Пожалуйста, отправьте QR-код (фото).")

# --- Пользователь: Подтверждение выполнения ---

@dp.callback_query(F.data.startswith("user_done_"))
async def user_done(callback: CallbackQuery):
    number_id = callback.data.rsplit("_", 1)[1]
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM numbers WHERE id = ?', (number_id,))
        number = cursor.fetchone()
    
    if not number:
        await callback.message.answer("❌ Номер не найден.")
        await callback.answer()
        return
    
    worker = get_user_by_id(number['worker_id'])
    if worker:
        try:
            await bot.send_message(
                worker['telegram_id'],
                f"✅ Пользователь подтвердил выполнение!\n"
                f"📱 Номер: +{number['phone_number']}\n\n"
                "Нажмите 'Встал' если номер успешно встал или 'Ошибка':",
                reply_markup=get_worker_stand_keyboard()
            )
        except Exception as e:
            logger.error(f"Error notifying worker: {e}")
    
    await callback.message.edit_text(
        f"✅ Вы подтвердили выполнение для номера +{number['phone_number']}.\n"
        "Работяга получил уведомление.",
        reply_markup=None
    )
    await callback.answer()

# --- Работяга: Подтверждение "Встал" ---

@dp.callback_query(F.data == "stand_yes", UserStates.worker_confirm_stand)
async def worker_stand_yes(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    number_id = data.get('number_id')
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE numbers SET started_at = CURRENT_TIMESTAMP WHERE id = ?', (number_id,))
        cursor.execute('SELECT * FROM numbers WHERE id = ?', (number_id,))
        number = cursor.fetchone()
    
    await state.clear()
    
    await callback.message.edit_text(
        f"✅ Номер встал!\n"
        f"📱 Номер: +{number['phone_number']}\n"
        f"🕐 Время: {datetime.now().strftime('%H:%M:%S')}\n\n"
        "Нажмите 'Слет' после завершения работы:",
        reply_markup=get_worker_fly_keyboard(number_id)
    )
    await callback.answer()

@dp.callback_query(F.data == "stand_no", UserStates.worker_confirm_stand)
async def worker_stand_no(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    number_id = data.get('number_id')
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE numbers 
            SET status = 'waiting', worker_id = NULL, started_at = NULL
            WHERE id = ?
        ''', (number_id,))
    
    await state.clear()
    
    await callback.message.edit_text(
        "❌ Ошибка! Номер не встал.\n"
        "Номер возвращен в очередь.",
        reply_markup=None
    )
    await callback.message.answer(
        "Главное меню",
        reply_markup=get_worker_keyboard()
    )
    await callback.answer()

# --- Слет номера ---

@dp.callback_query(F.data.startswith("fly_"))
async def worker_fly(callback: CallbackQuery, state: FSMContext):
    _, number_id = callback.data.split("_")
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM numbers WHERE id = ?', (number_id,))
        number = cursor.fetchone()
        
        if not number:
            await callback.message.answer("❌ Номер не найден.")
            await callback.answer()
            return
        
        cursor.execute('SELECT * FROM users WHERE id = ?', (number['user_id'],))
        owner = cursor.fetchone()
        owner_telegram_id = owner['telegram_id'] if owner else None
        
        if number['started_at']:
            started = datetime.strptime(number['started_at'], '%Y-%m-%d %H:%M:%S')
            finished = datetime.now()
            minutes = (finished - started).total_seconds() / 60
        else:
            minutes = 0
        
        if minutes >= 10:
            status = 'completed'
            if number['queue_type'] == 'vip':
                if owner_telegram_id is not None:
                    add_balance(owner_telegram_id, number['price'])
                cursor.execute('UPDATE numbers SET is_paid = 1 WHERE id = ?', (number_id,))
                
                if owner_telegram_id is not None:
                    try:
                        await bot.send_message(
                            owner_telegram_id,
                            f"💰 Начислено {number['price']}$ за номер +{number['phone_number']}\n"
                            f"⏱ Время: {minutes:.1f} минут\n"
                            f"📋 Тип: ⚡ VIP"
                        )
                    except Exception as e:
                        logger.error(f"Error notifying user: {e}")
        else:
            status = 'failed'
        
        cursor.execute('''
            UPDATE numbers 
            SET status = ?, finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (status, number_id))
    
    status_text = "✅ Отстоял" if status == 'completed' else "❌ Слет"
    price_text = f"💰 Начислено: {number['price']}$" if status == 'completed' else "💰 Начислено: 0$"
    
    await callback.message.edit_text(
        f"📱 Номер: +{number['phone_number']}\n"
        f"📊 Статус: {status_text}\n"
        f"⏱ Время: {minutes:.1f} минут\n"
        f"{price_text}\n\n"
        f"{'✅ Номер успешно отстоял!' if status == 'completed' else '❌ Номер слетел (< 10 минут)'}",
        reply_markup=None
    )
    
    if status == 'failed' and owner_telegram_id is not None:
        try:
            await bot.send_message(
                owner_telegram_id,
                f"❌ Номер +{number['phone_number']} слетел\n"
                f"⏱ Время: {minutes:.1f} минут\n"
                f"💰 Начислено: 0$ (менее 10 минут)"
            )
        except Exception as e:
            logger.error(f"Error notifying user about fly: {e}")
    
    await callback.message.answer(
        "🔧 Панель работяги",
        reply_markup=get_worker_keyboard()
    )
    await callback.answer()

# --- Работяга: Статистика ---

@dp.message(F.text == "📊 Моя статистика")
async def worker_stats(message: Message):
    user = get_user(message.from_user.id)
    if not user or not user['is_worker']:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    with get_db() as conn:
        cursor = conn.cursor()
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute('''
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
            FROM numbers 
            WHERE worker_id = ? AND DATE(created_at) = ?
        ''', (user['id'], today))
        today_stats = cursor.fetchone()
        
        cursor.execute('''
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
            FROM numbers 
            WHERE worker_id = ?
        ''', (user['id'],))
        all_stats = cursor.fetchone()
        
        cursor.execute('''
            SELECT COALESCE(SUM(price), 0) as earnings
            FROM numbers 
            WHERE worker_id = ? AND status = 'completed'
        ''', (user['id'],))
        earnings = cursor.fetchone()
    
    today_text = f"За сегодня:\n• Взято: {today_stats['total']}\n• Отстояло: {today_stats['completed']}\n• Слетело: {today_stats['failed']}"
    all_text = f"За всё время:\n• Взято: {all_stats['total']}\n• Отстояло: {all_stats['completed']}\n• Слетело: {all_stats['failed']}\n• Заработано: {earnings['earnings']}$"
    
    await message.answer(f"📊 Моя статистика\n\n{today_text}\n\n{all_text}")

# --- Фоновая проверка чеков на вывод CryptoBot ---

async def cryptopay_checks_watcher():
    """Раз в 30 секунд опрашивает выданные чеки на вывод и помечает активированные,
    уведомляя пользователя, когда он забрал средства из CryptoBot."""
    while True:
        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT w.id, w.check_id, w.user_id, w.amount, w.asset
                    FROM withdrawals w
                    WHERE w.status = 'pending' AND w.check_id IS NOT NULL
                ''')
                pending = cursor.fetchall()
            
            if pending:
                check_ids = [row['check_id'] for row in pending]
                by_check = {row['check_id']: row for row in pending}
                items = await cryptopay_get_checks(check_ids)
                for item in items:
                    check_id = str(item.get("check_id"))
                    row = by_check.get(check_id)
                    if row and item.get("status") == "activated":
                        with get_db() as conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                "UPDATE withdrawals SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'",
                                (row['id'],)
                            )
                            updated = cursor.rowcount > 0
                        if updated:
                            owner = get_user_by_id(row['user_id'])
                            if owner:
                                try:
                                    await bot.send_message(
                                        owner['telegram_id'],
                                        f"✅ Чек на {row['amount']} {row['asset'] or CRYPTO_ASSET} получен через CryptoBot!"
                                    )
                                except Exception as e:
                                    logger.error(f"Error notifying user about check activation: {e}")
        except (CryptoPayError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"cryptopay_checks_watcher error: {e}")
        except Exception as e:
            logger.error(f"cryptopay_checks_watcher unexpected error: {e}")
        
        await asyncio.sleep(30)

# --- Запуск бота ---

async def main():
    init_db()
    logger.info("Starting bot...")
    asyncio.create_task(cryptopay_checks_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
