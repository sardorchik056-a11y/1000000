import asyncio
import logging
import os
import sqlite3
import aiohttp
import json
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8651956926:AAG3ML1uGBPQOgrM5WAMl3kXaRLvVxTHCsw")

SHOP_NAME = "Kretros SMS Shop"
SUPPORT_USERNAME = "DATROQ"

ADMIN_CHAT_ID = 8118184388  # ID админа (ваш ID)
ADMIN_USERNAME = "sa"  # ваш юзернейм

REQUEST_TIMEOUT_SECONDS = 3 * 60
PENALTY_AMOUNT = 0.5
PRICE_PER_NUMBER = 1.0  # цена за один номер
DB_PATH = "shop.db"
MIN_DEPOSIT = 1

# ================= НАСТРОЙКИ CRYPTOBOT API =================
CRYPTOBOT_API_TOKEN = "582363:AALEf7JOugnrQyrkMHzH5UrO7pdOjjYnTQy"
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"
PAY_ASSET = "USDT"

logging.basicConfig(level=logging.INFO)

router = Router()
active_timers: dict[int, asyncio.Task] = {}

# ================= КАСТОМНЫЕ ТЕЛЕГРАМ ПРЕМИУМ ЭМОДЗИ =================
EMOJI_STAR_ID = "5906581476639513176"
EMOJI_SMALL_STAR_ID = "5445353829304387411"
EMOJI_SMALL_STAR_2_ID = "6078158956188930337"
EMOJI_FOLDER_ID = "5877316724830768997"
EMOJI_PHONE_ID = "5897567714674741148"
EMOJI_GEAR_ID = "5341715473882955310"
EMOJI_USER_ID = "5848400681416793625"
EMOJI_MONEY_ID = "5116648080787112958"
EMOJI_CROSS_ID = "5210952531676504517"
EMOJI_WARNING_ID = "5440660757194744323"
EMOJI_PHONE_2_ID = "5104966345267610825"
EMOJI_CHECK_ID = "5206607081334906820"
EMOJI_KEY_ID = "5307843983102204243"
EMOJI_GLOBE_ID = "5447410659077661506"

def ce(emoji_id: str, fallback: str = "⭐") -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

STAR = ce(EMOJI_STAR_ID, "⭐")
SMALL_STAR = ce(EMOJI_SMALL_STAR_ID, "⭐")
SMALL_STAR_2 = ce(EMOJI_SMALL_STAR_2_ID, "⭐")
FOLDER = ce(EMOJI_FOLDER_ID, "🗃")
PHONE = ce(EMOJI_PHONE_ID, "📞")
GEAR = ce(EMOJI_GEAR_ID, "⚙️")
USER = ce(EMOJI_USER_ID, "👤")
MONEY = ce(EMOJI_MONEY_ID, "💰")
CROSS = ce(EMOJI_CROSS_ID, "❌")
WARNING = ce(EMOJI_WARNING_ID, "‼️")
PHONE_2 = ce(EMOJI_PHONE_2_ID, "📞")
CHECK = ce(EMOJI_CHECK_ID, "✔️")
KEY = ce(EMOJI_KEY_ID, "🔑")
GLOBE = ce(EMOJI_GLOBE_ID, "🌐")

# ================= FSM СОСТОЯНИЯ =================
class AdminStates(StatesGroup):
    waiting_number = State()
    waiting_code = State()
    waiting_price = State()
    waiting_penalty = State()
    waiting_timeout = State()
    waiting_broadcast = State()
    waiting_ban = State()
    waiting_unban = State()

class DepositStates(StatesGroup):
    waiting_custom_amount = State()

# ================= БАЗА ДАННЫХ С МИГРАЦИЕЙ =================
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def migrate_database() -> None:
    """Миграция базы данных - добавление недостающих колонок"""
    conn = db_connect()
    cursor = conn.cursor()
    
    # Проверяем таблицу users
    cursor.execute("PRAGMA table_info(users)")
    columns = [column[1] for column in cursor.fetchall()]
    
    # Добавляем колонку is_banned если её нет
    if 'is_banned' not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN is_banned BOOLEAN DEFAULT 0")
        logging.info("✅ Колонка is_banned добавлена в таблицу users")
    
    # Добавляем колонку completed_at в таблицу requests если её нет
    if 'completed_at' not in columns:
        cursor.execute("ALTER TABLE requests ADD COLUMN completed_at TEXT")
        logging.info("✅ Колонка completed_at добавлена в таблицу requests")
    
    conn.commit()
    conn.close()

def init_db() -> None:
    conn = db_connect()
    
    # Таблица пользователей
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 0,
            total_bought INTEGER DEFAULT 0,
            created_at TEXT,
            is_banned BOOLEAN DEFAULT 0
        )
        """
    )
    
    # Таблица заявок
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            status TEXT DEFAULT 'pending',
            phone_number TEXT,
            sms_code TEXT,
            user_msg_chat_id INTEGER,
            user_msg_id INTEGER,
            admin_msg_chat_id INTEGER,
            admin_msg_id INTEGER,
            created_at TEXT,
            issued_at TEXT,
            completed_at TEXT
        )
        """
    )
    
    # Таблица депозитов
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            invoice_id TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            completed_at TEXT
        )
        """
    )
    
    # Таблица настроек
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    
    # Добавляем настройки по умолчанию
    settings = [
        ("price_per_number", str(PRICE_PER_NUMBER)),
        ("penalty_amount", str(PENALTY_AMOUNT)),
        ("timeout_seconds", str(REQUEST_TIMEOUT_SECONDS)),
    ]
    
    for key, value in settings:
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
    
    conn.commit()
    conn.close()
    
    # Запускаем миграцию
    migrate_database()

def get_setting(key: str) -> str:
    conn = db_connect()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None

def set_setting(key: str, value: str) -> None:
    conn = db_connect()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, value)
    )
    conn.commit()
    conn.close()

def get_or_create_user(user_id: int, username: str | None) -> sqlite3.Row:
    conn = db_connect()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users (user_id, username, balance, total_bought, created_at, is_banned) "
            "VALUES (?, ?, 0, 0, ?, 0)",
            (user_id, username, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    else:
        if username and row["username"] != username:
            conn.execute(
                "UPDATE users SET username = ? WHERE user_id = ?", (username, user_id)
            )
            conn.commit()
    conn.close()
    return row

def create_request(user_id: int, username: str | None) -> int:
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO requests (user_id, username, status, created_at) VALUES (?, ?, 'pending', ?)",
        (user_id, username, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    req_id = cur.lastrowid
    conn.close()
    return req_id

def get_request(req_id: int) -> sqlite3.Row | None:
    conn = db_connect()
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
    conn.close()
    return row

def update_request(req_id: int, **fields) -> None:
    if not fields:
        return
    conn = db_connect()
    columns = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [req_id]
    conn.execute(f"UPDATE requests SET {columns} WHERE id = ?", values)
    conn.commit()
    conn.close()

def increment_total_bought(user_id: int) -> None:
    conn = db_connect()
    conn.execute(
        "UPDATE users SET total_bought = total_bought + 1 WHERE user_id = ?", (user_id,)
    )
    conn.commit()
    conn.close()

def adjust_balance(user_id: int, delta: float) -> float:
    conn = db_connect()
    conn.execute(
        "UPDATE users SET balance = balance + ? WHERE user_id = ?", (delta, user_id)
    )
    conn.commit()
    row = conn.execute(
        "SELECT balance FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["balance"] if row else 0.0

def create_deposit(user_id: int, amount: float, invoice_id: str) -> int:
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO deposits (user_id, amount, invoice_id, status, created_at) "
        "VALUES (?, ?, ?, 'pending', ?)",
        (user_id, amount, invoice_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    deposit_id = cur.lastrowid
    conn.close()
    return deposit_id

def update_deposit_status(invoice_id: str, status: str) -> None:
    conn = db_connect()
    conn.execute(
        "UPDATE deposits SET status = ?, completed_at = ? WHERE invoice_id = ?",
        (status, datetime.now(timezone.utc).isoformat(), invoice_id)
    )
    conn.commit()
    conn.close()

def get_deposit_by_id(deposit_id: int) -> sqlite3.Row | None:
    conn = db_connect()
    row = conn.execute(
        "SELECT * FROM deposits WHERE id = ?", (deposit_id,)
    ).fetchone()
    conn.close()
    return row

def get_all_users() -> list:
    conn = db_connect()
    rows = conn.execute("SELECT * FROM users WHERE is_banned = 0").fetchall()
    conn.close()
    return rows

def ban_user(user_id: int) -> None:
    conn = db_connect()
    conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def unban_user(user_id: int) -> None:
    conn = db_connect()
    conn.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def is_user_banned(user_id: int) -> bool:
    conn = db_connect()
    row = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row["is_banned"] == 1 if row else False

# ================= CRYPTOBOT API =================
class CryptoBotAPI:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Crypto-Pay-API-Token": token,
            "Content-Type": "application/json"
        }
    
    async def create_invoice(self, amount: float, asset: str = "USDT", description: str = "Пополнение баланса") -> dict:
        url = f"{CRYPTOBOT_API_URL}/createInvoice"
        payload = {
            "asset": asset,
            "amount": str(amount),
            "description": description,
            "hidden_message": "Спасибо за пополнение!",
            "paid_btn_name": "openChannel",
            "paid_btn_url": "https://t.me/Kretros_sms_bot"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("ok"):
                        return data.get("result")
                    else:
                        logging.error(f"CryptoBot API error: {data}")
                        return None
                else:
                    logging.error(f"CryptoBot API HTTP error: {response.status}")
                    return None
    
    async def get_invoice_status(self, invoice_id: str) -> dict:
        url = f"{CRYPTOBOT_API_URL}/getInvoices"
        payload = {"invoice_ids": [invoice_id]}
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("ok") and data.get("result"):
                        return data["result"]["items"][0] if data["result"]["items"] else None
                return None

crypto_api = CryptoBotAPI(CRYPTOBOT_API_TOKEN)

# ================= КЛАВИАТУРЫ =================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Взять номер",
                    callback_data="get_number",
                    icon_custom_emoji_id=EMOJI_PHONE_ID
                )
            ],
            [
                InlineKeyboardButton(
                    text="Баланс",
                    callback_data="balance",
                    icon_custom_emoji_id=EMOJI_MONEY_ID
                )
            ],
            [
                InlineKeyboardButton(
                    text="Правила",
                    callback_data="rules",
                    icon_custom_emoji_id=EMOJI_GEAR_ID
                )
            ],
            [
                InlineKeyboardButton(
                    text="Поддержка",
                    callback_data="support",
                    icon_custom_emoji_id=EMOJI_FOLDER_ID
                )
            ],
        ]
    )

def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data="admin_stats"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💰 Управление ценами",
                    callback_data="admin_prices"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📢 Рассылка",
                    callback_data="admin_broadcast"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Пользователи",
                    callback_data="admin_users"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="back_to_menu",
                    icon_custom_emoji_id=EMOJI_CROSS_ID
                )
            ],
        ]
    )

def admin_prices_kb() -> InlineKeyboardMarkup:
    price = get_setting("price_per_number") or PRICE_PER_NUMBER
    penalty = get_setting("penalty_amount") or PENALTY_AMOUNT
    timeout = get_setting("timeout_seconds") or REQUEST_TIMEOUT_SECONDS
    
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Цена номера: {price}$",
                    callback_data="admin_edit_price"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Штраф: {penalty}$",
                    callback_data="admin_edit_penalty"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Таймаут: {timeout}с",
                    callback_data="admin_edit_timeout"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="admin_back",
                    icon_custom_emoji_id=EMOJI_CROSS_ID
                )
            ],
        ]
    )

def admin_users_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Список пользователей",
                    callback_data="admin_user_list"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Забанить пользователя",
                    callback_data="admin_ban_user"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Разбанить пользователя",
                    callback_data="admin_unban_user"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="admin_back",
                    icon_custom_emoji_id=EMOJI_CROSS_ID
                )
            ],
        ]
    )

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="back_to_menu",
                    icon_custom_emoji_id=EMOJI_CROSS_ID
                )
            ]
        ]
    )

def balance_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Пополнить",
                    callback_data="deposit",
                    icon_custom_emoji_id=EMOJI_MONEY_ID
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="back_to_menu",
                    icon_custom_emoji_id=EMOJI_CROSS_ID
                )
            ],
        ]
    )

def deposit_amount_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="5$",
                    callback_data="deposit_amount:5",
                    icon_custom_emoji_id=EMOJI_MONEY_ID
                ),
                InlineKeyboardButton(
                    text="10$",
                    callback_data="deposit_amount:10",
                    icon_custom_emoji_id=EMOJI_MONEY_ID
                ),
                InlineKeyboardButton(
                    text="25$",
                    callback_data="deposit_amount:25",
                    icon_custom_emoji_id=EMOJI_MONEY_ID
                )
            ],
            [
                InlineKeyboardButton(
                    text="50$",
                    callback_data="deposit_amount:50",
                    icon_custom_emoji_id=EMOJI_MONEY_ID
                ),
                InlineKeyboardButton(
                    text="150$",
                    callback_data="deposit_amount:150",
                    icon_custom_emoji_id=EMOJI_MONEY_ID
                )
            ],
            [
                InlineKeyboardButton(
                    text="Ввести вручную",
                    callback_data="deposit_custom",
                    icon_custom_emoji_id=EMOJI_KEY_ID
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="back_to_balance",
                    icon_custom_emoji_id=EMOJI_CROSS_ID
                )
            ],
        ]
    )

def deposit_confirm_kb(deposit_id: int, invoice_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Оплатить 💳",
                    url=invoice_url
                )
            ],
            [
                InlineKeyboardButton(
                    text="Проверить оплату ✅",
                    callback_data=f"deposit_check:{deposit_id}",
                    icon_custom_emoji_id=EMOJI_CHECK_ID
                )
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="back_to_deposit",
                    icon_custom_emoji_id=EMOJI_CROSS_ID
                )
            ],
        ]
    )

def user_searching_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отменить",
                    callback_data=f"usercancel:{req_id}",
                    icon_custom_emoji_id=EMOJI_CROSS_ID
                )
            ]
        ]
    )

def user_issued_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Код отправлен!",
                    callback_data=f"usercodesent:{req_id}",
                    icon_custom_emoji_id=EMOJI_CHECK_ID
                )
            ]
        ]
    )

def admin_new_request_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Выдать номер",
                    callback_data=f"issue:{req_id}",
                    icon_custom_emoji_id=EMOJI_CHECK_ID
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отклонить",
                    callback_data=f"reject:{req_id}",
                    icon_custom_emoji_id=EMOJI_CROSS_ID
                )
            ],
        ]
    )

def admin_waiting_sms_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ввести код",
                    callback_data=f"entercode:{req_id}",
                    icon_custom_emoji_id=EMOJI_KEY_ID
                )
            ]
        ]
    )

def admin_confirm_code_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отправить пользователю",
                    callback_data=f"confirmsend:{req_id}",
                    icon_custom_emoji_id=EMOJI_CHECK_ID
                )
            ],
            [
                InlineKeyboardButton(
                    text="Ввести заново",
                    callback_data=f"entercode:{req_id}",
                    icon_custom_emoji_id=EMOJI_KEY_ID
                )
            ],
        ]
    )

# ================= ТЕКСТЫ =================
def build_menu_text(user_row: sqlite3.Row) -> str:
    username = user_row["username"] or "—"
    return (
        f"{STAR}{SMALL_STAR}{SMALL_STAR_2} <b>{SHOP_NAME}</b>\n"
        "―――――――――――――――――\n"
        f"{USER} User: @{username} !\n"
        f"{FOLDER} ID: <code>{user_row['user_id']}</code>\n"
        f"{MONEY} Баланс: {user_row['balance']:.0f}$\n"
        f"{FOLDER} Всего куплено: {user_row['total_bought']}\n"
        "―――――――――――――――――\n\n"
        "Кнопки :"
    )

def build_balance_text(user_row: sqlite3.Row) -> str:
    return (
        f"{MONEY} <b>Ваш баланс</b>\n"
        "―――――――――――――――――\n"
        f"Баланс: {user_row['balance']:.2f}$\n"
        "―――――――――――――――――\n\n"
        "Пополните баланс для продолжения работы с сервисом."
    )

def build_deposit_text(amount: float, invoice: dict, deposit_id: int) -> str:
    return (
        f"{MONEY} <b>Пополнение баланса</b>\n"
        "―――――――――――――――――\n"
        f"Сумма: {amount:.2f} {invoice.get('asset', 'USDT')}\n"
        f"Счет: #{deposit_id}\n\n"
        f"Нажмите кнопку <b>«Оплатить 💳»</b> для оплаты\n"
        "После оплаты нажмите <b>«Проверить оплату ✅»</b>\n\n"
        f"<b>Статус:</b> ⏳ Ожидание оплаты"
    )

def build_deposit_success_text(amount: float, new_balance: float) -> str:
    return (
        f"{CHECK} <b>Пополнение успешно!</b>\n"
        "―――――――――――――――――\n"
        f"Сумма: {amount:.2f}$\n"
        f"Новый баланс: {new_balance:.2f}$\n\n"
        "Спасибо за пополнение!"
    )

def build_waiting_admin_text(req: sqlite3.Row) -> str:
    return (
        f"{CHECK} <b>Вы отметили: код отправлен!</b>\n"
        "―――――――――――――――――\n"
        f"┣ Номер: <code>{req['phone_number']}</code>\n"
        "┗ ⏳ Ожидайте, администратор вводит код...\n"
    )

def build_issued_text(req: sqlite3.Row) -> str:
    price = float(get_setting("price_per_number") or PRICE_PER_NUMBER)
    return (
        f"{CHECK} <b>Номер получен!</b>\n"
        "―――――――――――――――――\n"
        f"┣ Номер: <code>{req['phone_number']}</code>\n"
        "┣ Формат: СМС\n"
        f"┗ {MONEY} Стоимость: {price:.2f}$\n\n"
        "⏳ Ожидаю СМС, отправьте код в течение 3 минут"
    )

# ================= ТАЙМЕРЫ =================
async def schedule_timeout(bot: Bot, req_id: int) -> None:
    try:
        penalty = float(get_setting("penalty_amount") or PENALTY_AMOUNT)
        await asyncio.sleep(REQUEST_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        return

    req = get_request(req_id)
    if req is None or req["status"] != "issued":
        return

    update_request(req_id, status="expired")
    new_balance = adjust_balance(req["user_id"], -penalty)

    if req["user_msg_chat_id"] and req["user_msg_id"]:
        try:
            await bot.edit_message_text(
                chat_id=req["user_msg_chat_id"],
                message_id=req["user_msg_id"],
                text=(
                    f"{WARNING} <b>СМС не пришло</b> {WARNING}\n\n"
                    f"{PHONE_2} Номер был возвращён в сток\n\n"
                    f"{GLOBE} Штраф: {penalty:.2f}$\n"
                    f"{MONEY} Ваш баланс: {new_balance:.2f}$"
                ),
                parse_mode="HTML",
            )
        except Exception:
            logging.exception("Не удалось отредактировать сообщение пользователя (timeout)")

    if req["admin_msg_chat_id"] and req["admin_msg_id"]:
        try:
            await bot.edit_message_text(
                chat_id=req["admin_msg_chat_id"],
                message_id=req["admin_msg_id"],
                text=(
                    f"⌛ <b>Заявка #{req_id} просрочена.</b>\n"
                    f"Номер <code>{req['phone_number']}</code> для @{req['username'] or req['user_id']} "
                    "— пользователь не нажал «Код отправлен!» вовремя.\n"
                    f"Штраф {penalty:.2f}$ списан с баланса пользователя."
                ),
                parse_mode="HTML",
            )
        except Exception:
            logging.exception("Не удалось отредактировать сообщение админа (timeout)")

    active_timers.pop(req_id, None)

def start_timer(bot: Bot, req_id: int) -> None:
    cancel_timer(req_id)
    active_timers[req_id] = asyncio.create_task(schedule_timeout(bot, req_id))

async def schedule_admin_timeout(bot: Bot, req_id: int) -> None:
    try:
        await asyncio.sleep(REQUEST_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        return

    req = get_request(req_id)
    if req is None or req["status"] != "code_pending":
        return

    update_request(req_id, status="issued")
    req = get_request(req_id)

    if req["user_msg_chat_id"] and req["user_msg_id"]:
        try:
            await bot.edit_message_text(
                chat_id=req["user_msg_chat_id"],
                message_id=req["user_msg_id"],
                text=build_issued_text(req),
                reply_markup=user_issued_kb(req_id),
                parse_mode="HTML",
            )
        except Exception:
            logging.exception("Не удалось вернуть сообщение пользователя (admin timeout)")

    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"⌛ <b>Заявка #{req_id}:</b> вы не ввели код за 3 минуты.\n"
            "Заявка возвращена пользователю, ждём повторного нажатия «Код отправлен!».",
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("Не удалось уведомить админа о таймауте ввода кода")

    active_timers.pop(req_id, None)
    active_timers[req_id] = asyncio.create_task(schedule_timeout(bot, req_id))

def start_admin_timer(bot: Bot, req_id: int) -> None:
    cancel_timer(req_id)
    active_timers[req_id] = asyncio.create_task(schedule_admin_timeout(bot, req_id))

def cancel_timer(req_id: int) -> None:
    task = active_timers.pop(req_id, None)
    if task and not task.done():
        task.cancel()

def is_admin_chat(chat_id: int) -> bool:
    return chat_id == ADMIN_CHAT_ID

def is_admin_user(user_id: int) -> bool:
    return user_id == ADMIN_CHAT_ID

# ================= ПОЛЬЗОВАТЕЛЬСКИЕ ХЕНДЛЕРЫ =================
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id
    
    # Проверка бана
    if is_user_banned(user_id):
        await message.answer(
            "❌ Вы забанены в боте. Обратитесь к администратору @sardorchik056.",
            parse_mode="HTML",
        )
        return
    
    user_row = get_or_create_user(message.from_user.id, message.from_user.username)
    
    # Если это админ - показываем админ-меню
    if is_admin_user(user_id):
        await message.answer(
            f"{GEAR} <b>Админ-панель</b>\n\n"
            "Добро пожаловать в админ-панель бота!",
            reply_markup=admin_menu_kb(),
            parse_mode="HTML",
        )
        return
    
    await message.answer(
        build_menu_text(user_row),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )

@router.callback_query(F.data == "back_to_menu")
async def cb_back_to_menu(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    
    if is_admin_user(user_id):
        await callback.message.edit_text(
            f"{GEAR} <b>Админ-панель</b>\n\n"
            "Добро пожаловать в админ-панель бота!",
            reply_markup=admin_menu_kb(),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    
    user_row = get_or_create_user(user_id, callback.from_user.username)
    await callback.message.edit_text(
        build_menu_text(user_row),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(F.data == "admin_back")
async def cb_admin_back(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        f"{GEAR} <b>Админ-панель</b>\n\n"
        "Добро пожаловать в админ-панель бота!",
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

# ================= АДМИН-ПАНЕЛЬ =================
@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    
    conn = db_connect()
    
    # Статистика пользователей
    users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    banned_count = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1").fetchone()[0]
    
    # Статистика заявок
    total_requests = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    pending_requests = conn.execute("SELECT COUNT(*) FROM requests WHERE status = 'pending'").fetchone()[0]
    completed_requests = conn.execute("SELECT COUNT(*) FROM requests WHERE status = 'completed'").fetchone()[0]
    expired_requests = conn.execute("SELECT COUNT(*) FROM requests WHERE status = 'expired'").fetchone()[0]
    
    # Статистика депозитов
    total_deposits = conn.execute("SELECT COUNT(*) FROM deposits").fetchone()[0]
    total_amount = conn.execute("SELECT SUM(amount) FROM deposits WHERE status = 'completed'").fetchone()[0] or 0
    
    conn.close()
    
    await callback.message.edit_text(
        f"{FOLDER} <b>Статистика бота</b>\n"
        "―――――――――――――――――\n"
        f"{USER} <b>Пользователи:</b>\n"
        f"┣ Всего: {users_count}\n"
        f"┗ Забанено: {banned_count}\n\n"
        f"{PHONE} <b>Заявки:</b>\n"
        f"┣ Всего: {total_requests}\n"
        f"┣ В обработке: {pending_requests}\n"
        f"┣ Выполнено: {completed_requests}\n"
        f"┗ Просрочено: {expired_requests}\n\n"
        f"{MONEY} <b>Финансы:</b>\n"
        f"┣ Депозитов: {total_deposits}\n"
        f"┗ Собрано: {total_amount:.2f}$",
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(F.data == "admin_prices")
async def cb_admin_prices(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    
    await callback.message.edit_text(
        f"{GEAR} <b>Управление ценами</b>\n\n"
        "Настройте параметры сервиса:",
        reply_markup=admin_prices_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(F.data == "admin_edit_price")
async def cb_admin_edit_price(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    
    await state.set_state(AdminStates.waiting_price)
    await callback.message.edit_text(
        f"{KEY} <b>Введите новую цену за номер</b>\n"
        "―――――――――――――――――\n"
        f"Текущая цена: {get_setting('price_per_number') or PRICE_PER_NUMBER}$\n\n"
        "Введите цену цифрами (например: 1.5, 2, 3.25):",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.message(AdminStates.waiting_price)
async def process_price_change(message: Message, state: FSMContext) -> None:
    if not is_admin_user(message.from_user.id):
        await message.answer("Недоступно")
        return
    
    try:
        price = float(message.text.replace(",", "."))
        if price < 0:
            await message.answer("❌ Цена не может быть отрицательной!")
            return
        
        set_setting("price_per_number", str(price))
        await message.answer(
            f"{CHECK} Цена за номер установлена: {price:.2f}$",
            reply_markup=admin_prices_kb(),
            parse_mode="HTML",
        )
        await state.clear()
    except ValueError:
        await message.answer(
            "❌ Введите корректное число (например: 1.5, 2, 3.25)",
            parse_mode="HTML",
        )

@router.callback_query(F.data == "admin_edit_penalty")
async def cb_admin_edit_penalty(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    
    await state.set_state(AdminStates.waiting_penalty)
    await callback.message.edit_text(
        f"{KEY} <b>Введите новый размер штрафа</b>\n"
        "―――――――――――――――――\n"
        f"Текущий штраф: {get_setting('penalty_amount') or PENALTY_AMOUNT}$\n\n"
        "Введите сумму штрафа цифрами (например: 0.5, 1, 2):",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.message(AdminStates.waiting_penalty)
async def process_penalty_change(message: Message, state: FSMContext) -> None:
    if not is_admin_user(message.from_user.id):
        await message.answer("Недоступно")
        return
    
    try:
        penalty = float(message.text.replace(",", "."))
        if penalty < 0:
            await message.answer("❌ Штраф не может быть отрицательным!")
            return
        
        set_setting("penalty_amount", str(penalty))
        await message.answer(
            f"{CHECK} Штраф установлен: {penalty:.2f}$",
            reply_markup=admin_prices_kb(),
            parse_mode="HTML",
        )
        await state.clear()
    except ValueError:
        await message.answer(
            "❌ Введите корректное число (например: 0.5, 1, 2)",
            parse_mode="HTML",
        )

@router.callback_query(F.data == "admin_edit_timeout")
async def cb_admin_edit_timeout(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    
    await state.set_state(AdminStates.waiting_timeout)
    await callback.message.edit_text(
        f"{KEY} <b>Введите новый таймаут</b>\n"
        "―――――――――――――――――\n"
        f"Текущий таймаут: {get_setting('timeout_seconds') or REQUEST_TIMEOUT_SECONDS} секунд\n\n"
        "Введите время в секундах (например: 180, 300, 600):",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.message(AdminStates.waiting_timeout)
async def process_timeout_change(message: Message, state: FSMContext) -> None:
    if not is_admin_user(message.from_user.id):
        await message.answer("Недоступно")
        return
    
    try:
        timeout = int(message.text)
        if timeout < 30:
            await message.answer("❌ Таймаут не может быть меньше 30 секунд!")
            return
        
        set_setting("timeout_seconds", str(timeout))
        global REQUEST_TIMEOUT_SECONDS
        REQUEST_TIMEOUT_SECONDS = timeout
        
        await message.answer(
            f"{CHECK} Таймаут установлен: {timeout} секунд",
            reply_markup=admin_prices_kb(),
            parse_mode="HTML",
        )
        await state.clear()
    except ValueError:
        await message.answer(
            "❌ Введите корректное число (например: 180, 300, 600)",
            parse_mode="HTML",
        )

# ================= АДМИН-РАССЫЛКА =================
@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    
    await state.set_state(AdminStates.waiting_broadcast)
    await callback.message.edit_text(
        f"{PHONE} <b>Рассылка сообщений</b>\n"
        "―――――――――――――――――\n"
        "Введите текст для рассылки всем пользователям.\n\n"
        "Поддерживается HTML-разметка.\n\n"
        "Для отмены нажмите кнопку «Назад»:",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.message(AdminStates.waiting_broadcast)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin_user(message.from_user.id):
        await message.answer("Недоступно")
        return
    
    text = message.text
    users = get_all_users()
    
    await message.answer(
        f"{CHECK} <b>Начинаю рассылку...</b>\n"
        f"Пользователей: {len(users)}",
        parse_mode="HTML",
    )
    
    success = 0
    fail = 0
    
    for user in users:
        try:
            await bot.send_message(
                user["user_id"],
                f"{PHONE} <b>Рассылка от администрации</b>\n\n{text}",
                parse_mode="HTML",
            )
            success += 1
            await asyncio.sleep(0.1)
        except Exception:
            fail += 1
    
    await message.answer(
        f"{CHECK} <b>Рассылка завершена!</b>\n"
        "―――――――――――――――――\n"
        f"✅ Отправлено: {success}\n"
        f"❌ Не доставлено: {fail}",
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )
    await state.clear()

# ================= АДМИН-ПОЛЬЗОВАТЕЛИ =================
@router.callback_query(F.data == "admin_users")
async def cb_admin_users(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    
    await callback.message.edit_text(
        f"{USER} <b>Управление пользователями</b>\n\n"
        "Выберите действие:",
        reply_markup=admin_users_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(F.data == "admin_user_list")
async def cb_admin_user_list(callback: CallbackQuery) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    
    conn = db_connect()
    users = conn.execute(
        "SELECT user_id, username, balance, total_bought, is_banned FROM users ORDER BY balance DESC LIMIT 50"
    ).fetchall()
    conn.close()
    
    text = f"{USER} <b>Список пользователей (топ 50)</b>\n"
    text += "―――――――――――――――――\n"
    
    for i, user in enumerate(users, 1):
        status = "🔴 Забанен" if user["is_banned"] else "🟢 Активен"
        text += (
            f"{i}. @{user['username'] or user['user_id']}\n"
            f"   💰 {user['balance']:.2f}$ | 📱 {user['total_bought']}\n"
            f"   {status}\n\n"
        )
    
    await callback.message.edit_text(
        text,
        reply_markup=admin_users_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(F.data == "admin_ban_user")
async def cb_admin_ban_user(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    
    await state.set_state(AdminStates.waiting_ban)
    await callback.message.edit_text(
        f"{CROSS} <b>Бан пользователя</b>\n"
        "―――――――――――――――――\n"
        "Введите ID пользователя для бана (можно скопировать из списка пользователей):\n\n"
        "Пример: 123456789",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(F.data == "admin_unban_user")
async def cb_admin_unban_user(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Недоступно", show_alert=True)
        return
    
    await state.set_state(AdminStates.waiting_unban)
    await callback.message.edit_text(
        f"{CHECK} <b>Разбан пользователя</b>\n"
        "―――――――――――――――――\n"
        "Введите ID пользователя для разбана:\n\n"
        "Пример: 123456789",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

# Обработчик для бана/разбана (текстовый ввод после нажатия кнопок)
@router.message(F.text & ~F.text.startswith('/'))
async def handle_admin_ban_unban(message: Message, state: FSMContext) -> None:
    if not is_admin_user(message.from_user.id):
        return
    
    current_state = await state.get_state()
    
    try:
        user_id = int(message.text.strip())
        
        if current_state == "AdminStates:waiting_ban":
            ban_user(user_id)
            await message.answer(
                f"{CROSS} Пользователь {user_id} забанен.",
                reply_markup=admin_users_kb(),
                parse_mode="HTML",
            )
            await state.clear()
        
        elif current_state == "AdminStates:waiting_unban":
            unban_user(user_id)
            await message.answer(
                f"{CHECK} Пользователь {user_id} разбанен.",
                reply_markup=admin_users_kb(),
                parse_mode="HTML",
            )
            await state.clear()
            
    except ValueError:
        await message.answer(
            "❌ Введите корректный ID пользователя (только цифры)",
            parse_mode="HTML",
        )

# ================= ОСТАЛЬНЫЕ ХЕНДЛЕРЫ =================
@router.callback_query(F.data == "balance")
async def cb_balance(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    
    if is_user_banned(user_id):
        await callback.answer("Вы забанены!", show_alert=True)
        return
    
    user_row = get_or_create_user(user_id, callback.from_user.username)
    await callback.message.edit_text(
        build_balance_text(user_row),
        reply_markup=balance_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(F.data == "deposit")
async def cb_deposit(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        f"{MONEY} <b>Выберите сумму пополнения</b>\n"
        "―――――――――――――――――\n"
        f"Минимальная сумма: {MIN_DEPOSIT}$\n\n"
        "Выберите сумму или введите вручную:",
        reply_markup=deposit_amount_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(F.data == "back_to_balance")
async def cb_back_to_balance(callback: CallbackQuery) -> None:
    user_row = get_or_create_user(callback.from_user.id, callback.from_user.username)
    await callback.message.edit_text(
        build_balance_text(user_row),
        reply_markup=balance_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(F.data == "back_to_deposit")
async def cb_back_to_deposit(callback: CallbackQuery) -> None:
    user_row = get_or_create_user(callback.from_user.id, callback.from_user.username)
    await callback.message.edit_text(
        build_balance_text(user_row),
        reply_markup=deposit_amount_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

# ================= ПОПОЛНЕНИЕ =================
@router.callback_query(F.data.startswith("deposit_amount:"))
async def cb_deposit_amount(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    
    if is_user_banned(user_id):
        await callback.answer("Вы забанены!", show_alert=True)
        return
    
    amount = float(callback.data.split(":")[1])
    
    invoice = await crypto_api.create_invoice(amount, PAY_ASSET, f"Пополнение баланса пользователя {user_id}")
    
    if not invoice:
        await callback.message.edit_text(
            "❌ Ошибка при создании счета. Пожалуйста, попробуйте позже.",
            reply_markup=back_kb(),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    
    deposit_id = create_deposit(user_id, amount, invoice["invoice_id"])
    
    await callback.message.edit_text(
        build_deposit_text(amount, invoice, deposit_id),
        reply_markup=deposit_confirm_kb(deposit_id, invoice["pay_url"]),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(F.data == "deposit_custom")
async def cb_deposit_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(DepositStates.waiting_custom_amount)
    await callback.message.edit_text(
        f"{KEY} <b>Введите сумму пополнения</b>\n"
        "―――――――――――――――――\n"
        f"Минимальная сумма: {MIN_DEPOSIT}$\n\n"
        "Введите сумму цифрами (например: 7, 15, 30):",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.message(DepositStates.waiting_custom_amount)
async def process_custom_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = float(message.text.replace(",", "."))
        if amount < MIN_DEPOSIT:
            await message.answer(
                f"❌ Минимальная сумма пополнения: {MIN_DEPOSIT}$\n"
                "Пожалуйста, введите сумму больше.",
                parse_mode="HTML",
            )
            return
        
        user_id = message.from_user.id
        
        invoice = await crypto_api.create_invoice(amount, PAY_ASSET, f"Пополнение баланса пользователя {user_id}")
        
        if not invoice:
            await message.answer(
                "❌ Ошибка при создании счета. Пожалуйста, попробуйте позже.",
                reply_markup=back_kb(),
                parse_mode="HTML",
            )
            await state.clear()
            return
        
        deposit_id = create_deposit(user_id, amount, invoice["invoice_id"])
        
        await message.answer(
            build_deposit_text(amount, invoice, deposit_id),
            reply_markup=deposit_confirm_kb(deposit_id, invoice["pay_url"]),
            parse_mode="HTML",
        )
        await state.clear()
        
    except ValueError:
        await message.answer(
            "❌ Пожалуйста, введите корректное число (например: 7, 15, 30)",
            parse_mode="HTML",
        )

@router.callback_query(F.data.startswith("deposit_check:"))
async def cb_deposit_check(callback: CallbackQuery, bot: Bot) -> None:
    deposit_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    deposit = get_deposit_by_id(deposit_id)
    
    if deposit is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    
    if deposit["user_id"] != user_id:
        await callback.answer("Это не ваша заявка", show_alert=True)
        return
    
    if deposit["status"] != "pending":
        await callback.answer("Эта заявка уже обработана", show_alert=True)
        return
    
    invoice_status = await crypto_api.get_invoice_status(deposit["invoice_id"])
    
    if not invoice_status:
        await callback.answer("Ошибка проверки статуса. Попробуйте позже.", show_alert=True)
        return
    
    if invoice_status.get("status") == "paid":
        new_balance = adjust_balance(user_id, deposit["amount"])
        update_deposit_status(deposit["invoice_id"], "completed")
        
        await callback.message.edit_text(
            build_deposit_success_text(deposit["amount"], new_balance),
            reply_markup=back_kb(),
            parse_mode="HTML",
        )
        await callback.answer("✅ Оплата подтверждена!")
        
        user = await bot.get_chat(user_id)
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"{MONEY} <b>Пополнение баланса</b>\n"
            f"Пользователь: @{user.username or user_id}\n"
            f"Сумма: {deposit['amount']:.2f}$\n"
            f"Новый баланс: {new_balance:.2f}$",
            parse_mode="HTML",
        )
    else:
        await callback.answer("⏳ Счет еще не оплачен. Попробуйте позже.", show_alert=True)

# ================= ОСТАЛЬНЫЕ ХЕНДЛЕРЫ =================
@router.callback_query(F.data == "get_number")
async def cb_get_number(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    
    if is_user_banned(user_id):
        await callback.answer("Вы забанены!", show_alert=True)
        return
    
    user = callback.from_user
    req_id = create_request(user.id, user.username)

    await callback.message.edit_text(
        f"{PHONE_2} <b>В поиске номера</b>, ожидайте в течение 3 минут",
        reply_markup=user_searching_kb(req_id),
        parse_mode="HTML",
    )
    update_request(
        req_id,
        user_msg_chat_id=callback.message.chat.id,
        user_msg_id=callback.message.message_id,
    )

    if ADMIN_CHAT_ID:
        admin_msg = await bot.send_message(
            ADMIN_CHAT_ID,
            f"🆕 <b>Новая заявка #{req_id}</b>\nОт: @{user.username or user.id}",
            reply_markup=admin_new_request_kb(req_id),
            parse_mode="HTML",
        )
        update_request(
            req_id,
            admin_msg_chat_id=admin_msg.chat.id,
            admin_msg_id=admin_msg.message_id,
        )
    else:
        logging.warning("ADMIN_CHAT_ID не настроен — заявка не будет отправлена админу")

    await callback.answer()

@router.callback_query(F.data == "rules")
async def cb_rules(callback: CallbackQuery) -> None:
    price = float(get_setting("price_per_number") or PRICE_PER_NUMBER)
    penalty = float(get_setting("penalty_amount") or PENALTY_AMOUNT)
    timeout = int(get_setting("timeout_seconds") or REQUEST_TIMEOUT_SECONDS)
    
    await callback.message.edit_text(
        f"{GEAR} <b>Правила пользования сервисом</b>\n\n"
        f"1. Стоимость номера: {price:.2f}$\n"
        f"2. Время на получение СМС: {timeout} секунд\n"
        f"3. Штраф за просрочку: {penalty:.2f}$\n"
        f"4. Средства не возвращаются после успешной активации\n"
        f"5. Запрещена перепродажа номеров третьим лицам\n"
        f"6. При нарушении правил - бан без возврата средств",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(F.data == "support")
async def cb_support(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        f"{FOLDER} <b>Поддержка</b>\n\n"
        f"По всем вопросам пишите: @{SUPPORT_USERNAME}\n\n"
        f"Пополнение через CryptoBot:\n"
        f"• USDT (TRC-20)\n"
        f"• BTC\n"
        f"• TON\n"
        f"• ETH\n"
        f"• LTC\n"
        f"• BNB\n"
        f"• TRX",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

# ================= АДМИНСКИЕ ХЕНДЛЕРЫ ЗАЯВОК =================
@router.callback_query(F.data.startswith("usercancel:"))
async def cb_user_cancel(callback: CallbackQuery, bot: Bot) -> None:
    req_id = int(callback.data.split(":")[1])
    req = get_request(req_id)

    if req is None or req["status"] != "pending":
        await callback.answer("Эту заявку уже нельзя отменить", show_alert=True)
        return

    if callback.from_user.id != req["user_id"]:
        await callback.answer("Это не ваша заявка", show_alert=True)
        return

    update_request(req_id, status="cancelled")

    await callback.message.edit_text(
        f"{CROSS} Заявка отменена.", reply_markup=None
    )

    if req["admin_msg_chat_id"] and req["admin_msg_id"]:
        try:
            await bot.edit_message_text(
                chat_id=req["admin_msg_chat_id"],
                message_id=req["admin_msg_id"],
                text=f"{CROSS} Заявка #{req_id} отменена пользователем @{req['username'] or req['user_id']}.",
            )
        except Exception:
            logging.exception("Не удалось отредактировать сообщение админа (user cancel)")

    await callback.answer()

@router.callback_query(F.data.startswith("issue:"))
async def cb_issue_number(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_chat(callback.message.chat.id):
        await callback.answer("Недоступно", show_alert=True)
        return

    req_id = int(callback.data.split(":")[1])
    req = get_request(req_id)
    if req is None or req["status"] != "pending":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_number)
    await state.update_data(req_id=req_id)

    await callback.message.answer(
        f"{KEY} Введите номер телефона для заявки #{req_id} (например, +79991112233):"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("reject:"))
async def cb_reject_request(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin_chat(callback.message.chat.id):
        await callback.answer("Недоступно", show_alert=True)
        return

    req_id = int(callback.data.split(":")[1])
    req = get_request(req_id)
    if req is None or req["status"] != "pending":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    update_request(req_id, status="rejected")

    await callback.message.edit_text(
        f"{CROSS} Заявка #{req_id} отклонена.", reply_markup=None
    )

    if req["user_msg_chat_id"] and req["user_msg_id"]:
        try:
            await bot.edit_message_text(
                chat_id=req["user_msg_chat_id"],
                message_id=req["user_msg_id"],
                text=f"{CROSS} Ваша заявка отклонена администратором.",
            )
        except Exception:
            logging.exception("Не удалось отредактировать сообщение пользователя (reject)")

    await callback.answer()

@router.message(StateFilter(AdminStates.waiting_number), F.chat.id == ADMIN_CHAT_ID)
async def process_number_input(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    req_id = data.get("req_id")
    req = get_request(req_id) if req_id else None

    if req is None or req["status"] != "pending":
        await message.answer("Заявка больше не активна.")
        await state.clear()
        return

    phone_number = message.text.strip()
    
    # Списание средств
    price = float(get_setting("price_per_number") or PRICE_PER_NUMBER)
    new_balance = adjust_balance(req["user_id"], -price)
    
    if new_balance < 0:
        await message.answer(
            f"❌ Недостаточно средств! Баланс пользователя: {new_balance + price:.2f}$"
        )
        await state.clear()
        return
    
    update_request(
        req_id,
        status="issued",
        phone_number=phone_number,
        issued_at=datetime.now(timezone.utc).isoformat(),
    )
    req = get_request(req_id)

    if req["user_msg_chat_id"] and req["user_msg_id"]:
        try:
            await bot.edit_message_text(
                chat_id=req["user_msg_chat_id"],
                message_id=req["user_msg_id"],
                text=build_issued_text(req),
                reply_markup=user_issued_kb(req_id),
                parse_mode="HTML",
            )
        except Exception:
            logging.exception("Не удалось отредактировать сообщение пользователя (issue)")

    await message.answer(
        f"{CHECK} Номер <code>{phone_number}</code> выдан @{req['username'] or req['user_id']}. "
        f"Списано: {price:.2f}$\n"
        "Жду СМС на свой телефон.\n⏳ Таймер: 3 минуты.",
        reply_markup=admin_waiting_sms_kb(req_id),
        parse_mode="HTML",
    )

    if req["admin_msg_chat_id"] and req["admin_msg_id"]:
        try:
            await bot.edit_message_text(
                chat_id=req["admin_msg_chat_id"],
                message_id=req["admin_msg_id"],
                text=f"{CHECK} Заявка #{req_id}: номер <code>{phone_number}</code> выдан. Списано: {price:.2f}$",
                parse_mode="HTML",
            )
        except Exception:
            logging.exception("Не удалось отредактировать исходное сообщение админа")

    start_timer(bot, req_id)
    await state.clear()

@router.callback_query(F.data.startswith("entercode:"))
async def cb_enter_code(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin_chat(callback.message.chat.id):
        await callback.answer("Недоступно", show_alert=True)
        return

    req_id = int(callback.data.split(":")[1])
    req = get_request(req_id)
    if req is None or req["status"] not in ("issued", "code_pending"):
        await callback.answer("Заявка не в статусе ожидания SMS", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_code)
    await state.update_data(req_id=req_id)

    await callback.message.answer(
        f"{KEY} Введите СМС-код для номера <code>{req['phone_number']}</code>:",
        parse_mode="HTML",
    )
    await callback.answer()

@router.message(StateFilter(AdminStates.waiting_code), F.chat.id == ADMIN_CHAT_ID)
async def process_code_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    req_id = data.get("req_id")
    req = get_request(req_id) if req_id else None

    if req is None or req["status"] not in ("issued", "code_pending"):
        await message.answer("Заявка больше не активна.")
        await state.clear()
        return

    code = message.text.strip()
    update_request(req_id, sms_code=code)

    await message.answer(
        f"Код: <code>{code}</code>\nПодтвердите отправку пользователю:",
        reply_markup=admin_confirm_code_kb(req_id),
        parse_mode="HTML",
    )
    await state.clear()

@router.callback_query(F.data.startswith("confirmsend:"))
async def cb_confirm_send(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin_chat(callback.message.chat.id):
        await callback.answer("Недоступно", show_alert=True)
        return

    req_id = int(callback.data.split(":")[1])
    req = get_request(req_id)

    if req is None or req["status"] not in ("issued", "code_pending") or not req["sms_code"]:
        await callback.answer("Нечего отправлять", show_alert=True)
        return

    cancel_timer(req_id)
    update_request(req_id, status="completed", completed_at=datetime.now(timezone.utc).isoformat())
    increment_total_bought(req["user_id"])

    try:
        await bot.send_message(
            req["user_id"],
            f"{KEY} <b>Код для номера {req['phone_number']}:</b>\n<code>{req['sms_code']}</code>",
            parse_mode="HTML",
        )
        sent_ok = True
    except Exception:
        logging.exception("Не удалось отправить код пользователю")
        sent_ok = False

    if req["user_msg_chat_id"] and req["user_msg_id"]:
        try:
            await bot.edit_message_text(
                chat_id=req["user_msg_chat_id"],
                message_id=req["user_msg_id"],
                text=(
                    f"{CHECK} <b>Номер {req['phone_number']}</b>\n"
                    f"Код отправлен: <code>{req['sms_code']}</code>"
                ),
                parse_mode="HTML",
            )
        except Exception:
            logging.exception("Не удалось отредактировать сообщение пользователя (send code)")

    status_note = "отправлен" if sent_ok else "НЕ отправлен (ошибка, см. логи)"
    await callback.message.edit_text(
        f"{CHECK} Заявка #{req_id} завершена. Код {status_note} пользователю "
        f"@{req['username'] or req['user_id']}.",
        reply_markup=None,
    )
    await callback.answer()

@router.callback_query(F.data.startswith("usercodesent:"))
async def cb_user_code_sent(callback: CallbackQuery, bot: Bot) -> None:
    req_id = int(callback.data.split(":")[1])
    req = get_request(req_id)

    if req is None or req["status"] != "issued":
        await callback.answer("Эта заявка уже неактуальна", show_alert=True)
        return

    if callback.from_user.id != req["user_id"]:
        await callback.answer("Это не ваша заявка", show_alert=True)
        return

    update_request(req_id, status="code_pending")
    req = get_request(req_id)

    try:
        await callback.message.edit_text(
            build_waiting_admin_text(req),
            reply_markup=None,
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("Не удалось отредактировать сообщение пользователя (code sent)")

    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(
                ADMIN_CHAT_ID,
                (
                    f"{KEY} <b>Пользователь отправил код по заявке #{req_id}</b>\n"
                    f"От: @{req['username'] or req['user_id']}\n"
                    f"Номер: <code>{req['phone_number']}</code>\n\n"
                    "✍️ Введите код в течение 3 минут, иначе заявка будет возвращена пользователю."
                ),
                reply_markup=admin_waiting_sms_kb(req_id),
                parse_mode="HTML",
            )
        except Exception:
            logging.exception("Не удалось уведомить админа о нажатии «Код отправлен!»")

    start_admin_timer(bot, req_id)
    await callback.answer()

# ================= ЗАПУСК =================
async def main() -> None:
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
