import asyncio
import logging
import os
import sqlite3
from datetime import datetime

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
SUPPORT_USERNAME = "your_support_username"

ADMIN_CHAT_ID = 8118184388

REQUEST_TIMEOUT_SECONDS = 3 * 60
PENALTY_AMOUNT = 0.5
DB_PATH = "shop.db"

logging.basicConfig(level=logging.INFO)

router = Router()
active_timers: dict[int, asyncio.Task] = {}

# ================= КАСТОМНЫЕ ЭМОДЗИ =================
# Эмодзи из скриншотов
EMOJI_STAR = "⭐"  # 5906581476639513176
EMOJI_SMALL_STAR = "⭐"  # 5445353829304387411
EMOJI_SMALL_STAR_2 = "⭐"  # 6078158956188930337
EMOJI_FOLDER = "🗃"  # 5877316724830768997
EMOJI_PHONE = "📞"  # 5897567714674741148
EMOJI_GEAR = "⚙️"  # 5341715473882955310
EMOJI_USER = "👤"  # 5848400681416793625
EMOJI_CROSS = "❌"  # 5210952531676504517
EMOJI_WARNING = "‼️"  # 5440660757194744323
EMOJI_PHONE_2 = "📞"  # 5104966345267610825
EMOJI_MONEY = "💰"  # 5116648080787112958
EMOJI_CHECK = "✔️"  # 5206607081334906820
EMOJI_KEY = "🔑"  # 5307843983102204243
EMOJI_GLOBE = "🌐"  # 5447410659077661506

# ================= FSM СОСТОЯНИЯ АДМИНА =================
class AdminStates(StatesGroup):
    waiting_number = State()
    waiting_code = State()

# ================= БАЗА ДАННЫХ =================
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = db_connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 0,
            total_bought INTEGER DEFAULT 0,
            created_at TEXT
        )
        """
    )
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
            issued_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()

def get_or_create_user(user_id: int, username: str | None) -> sqlite3.Row:
    conn = db_connect()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users (user_id, username, balance, total_bought, created_at) "
            "VALUES (?, ?, 0, 0, ?)",
            (user_id, username, datetime.utcnow().isoformat()),
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
        (user_id, username, datetime.utcnow().isoformat()),
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

# ================= КЛАВИАТУРЫ =================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{EMOJI_PHONE} Взять номер", callback_data="get_number")],
            [InlineKeyboardButton(text=f"{EMOJI_MONEY} Баланс", callback_data="balance")],
            [InlineKeyboardButton(text=f"{EMOJI_GEAR} Правила", callback_data="rules")],
            [InlineKeyboardButton(text=f"{EMOJI_FOLDER} Поддержка", callback_data="support")],
        ]
    )

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{EMOJI_CROSS} Назад", callback_data="back_to_menu")]
        ]
    )

def user_searching_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{EMOJI_CROSS} Отменить", callback_data=f"usercancel:{req_id}")]
        ]
    )

def user_issued_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{EMOJI_CHECK} Код отправлен!", callback_data=f"usercodesent:{req_id}")]
        ]
    )

def admin_new_request_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{EMOJI_CHECK} Выдать номер", callback_data=f"issue:{req_id}")],
            [InlineKeyboardButton(text=f"{EMOJI_CROSS} Отклонить", callback_data=f"reject:{req_id}")],
        ]
    )

def admin_waiting_sms_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{EMOJI_KEY} Ввести код", callback_data=f"entercode:{req_id}")],
        ]
    )

def admin_confirm_code_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{EMOJI_CHECK} Отправить пользователю", callback_data=f"confirmsend:{req_id}")],
            [InlineKeyboardButton(text=f"{EMOJI_KEY} Ввести заново", callback_data=f"entercode:{req_id}")],
        ]
    )

# ================= ТЕКСТ ГЛАВНОГО МЕНЮ =================
def build_menu_text(user_row: sqlite3.Row) -> str:
    username = user_row["username"] or "—"
    return (
        f"{EMOJI_STAR}{EMOJI_SMALL_STAR}{EMOJI_SMALL_STAR_2} <b>{SHOP_NAME}</b>\n"
        "―――――――――――――――――\n"
        f"{EMOJI_USER} User: @{username} !\n"
        f"{EMOJI_FOLDER} ID: <code>{user_row['user_id']}</code>\n"
        f"{EMOJI_MONEY} Баланс: {user_row['balance']:.0f}$\n"
        f"{EMOJI_FOLDER} Всего куплено: {user_row['total_bought']}\n"
        "―――――――――――――――――\n\n"
        "Кнопки :"
    )

def build_waiting_admin_text(req: sqlite3.Row) -> str:
    return (
        f"{EMOJI_CHECK} <b>Вы отметили: код отправлен!</b>\n"
        "―――――――――――――――――\n"
        f"┣ Номер: <code>{req['phone_number']}</code>\n"
        "┗ ⏳ Ожидайте, администратор вводит код...\n"
    )

def build_issued_text(req: sqlite3.Row) -> str:
    return (
        f"{EMOJI_CHECK} <b>Номер получен!</b>\n"
        "―――――――――――――――――\n"
        f"┣ Номер: <code>{req['phone_number']}</code>\n"
        "┣ Формат: СМС\n"
        f"┗ {EMOJI_MONEY} Остаток: 0.0000$\n\n"
        "⏳ Ожидаю СМС, отправьте код в течение 3 минут"
    )

# ================= ТАЙМЕРЫ =================
async def schedule_timeout(bot: Bot, req_id: int) -> None:
    try:
        await asyncio.sleep(REQUEST_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        return

    req = get_request(req_id)
    if req is None or req["status"] != "issued":
        return

    update_request(req_id, status="expired")
    new_balance = adjust_balance(req["user_id"], -PENALTY_AMOUNT)

    if req["user_msg_chat_id"] and req["user_msg_id"]:
        try:
            await bot.edit_message_text(
                chat_id=req["user_msg_chat_id"],
                message_id=req["user_msg_id"],
                text=(
                    f"{EMOJI_WARNING} <b>СМС не пришло</b> {EMOJI_WARNING}\n\n"
                    f"{EMOJI_PHONE_2} Номер был возвращён в сток\n\n"
                    f"{EMOJI_GLOBE} Штраф: {PENALTY_AMOUNT}$\n"
                    f"{EMOJI_MONEY} Ваш баланс: {new_balance:.2f}$"
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
                    f"Штраф {PENALTY_AMOUNT}$ списан с баланса пользователя."
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

# ================= ПОЛЬЗОВАТЕЛЬСКИЕ ХЕНДЛЕРЫ =================
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_row = get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(
        build_menu_text(user_row),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )

@router.callback_query(F.data == "back_to_menu")
async def cb_back_to_menu(callback: CallbackQuery) -> None:
    user_row = get_or_create_user(callback.from_user.id, callback.from_user.username)
    await callback.message.edit_text(
        build_menu_text(user_row),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(F.data == "get_number")
async def cb_get_number(callback: CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    req_id = create_request(user.id, user.username)

    await callback.message.edit_text(
        f"{EMOJI_PHONE_2} <b>В поиске номера</b>, ожидайте в течение 3 минут",
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
        f"{EMOJI_CROSS} Заявка отменена.", reply_markup=None
    )

    if req["admin_msg_chat_id"] and req["admin_msg_id"]:
        try:
            await bot.edit_message_text(
                chat_id=req["admin_msg_chat_id"],
                message_id=req["admin_msg_id"],
                text=f"{EMOJI_CROSS} Заявка #{req_id} отменена пользователем @{req['username'] or req['user_id']}.",
            )
        except Exception:
            logging.exception("Не удалось отредактировать сообщение админа (user cancel)")

    await callback.answer()

@router.callback_query(F.data == "balance")
async def cb_balance(callback: CallbackQuery) -> None:
    user_row = get_or_create_user(callback.from_user.id, callback.from_user.username)
    await callback.message.edit_text(
        f"{EMOJI_MONEY} <b>Ваш баланс:</b> {user_row['balance']:.0f}$\n\n"
        "Пополнение доступно через раздел поддержки.",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(F.data == "rules")
async def cb_rules(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        f"{EMOJI_GEAR} <b>Правила пользования сервисом</b>\n\n"
        "1. Номер выдаётся на ограниченное время.\n"
        "2. Средства не возвращаются после успешной активации.\n"
        "3. Запрещена перепродажа номеров третьим лицам.",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(F.data == "support")
async def cb_support(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        f"{EMOJI_FOLDER} <b>Поддержка</b>\n\nПо всем вопросам пишите: @{SUPPORT_USERNAME}",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()

# ================= АДМИНСКИЕ ХЕНДЛЕРЫ =================
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
        f"{EMOJI_KEY} Введите номер телефона для заявки #{req_id} (например, +79991112233):"
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
        f"{EMOJI_CROSS} Заявка #{req_id} отклонена.", reply_markup=None
    )

    if req["user_msg_chat_id"] and req["user_msg_id"]:
        try:
            await bot.edit_message_text(
                chat_id=req["user_msg_chat_id"],
                message_id=req["user_msg_id"],
                text=f"{EMOJI_CROSS} Ваша заявка отклонена администратором.",
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
    update_request(
        req_id,
        status="issued",
        phone_number=phone_number,
        issued_at=datetime.utcnow().isoformat(),
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
        f"{EMOJI_CHECK} Номер <code>{phone_number}</code> выдан @{req['username'] or req['user_id']}. "
        "Жду СМС на свой телефон.\n⏳ Таймер: 3 минуты.",
        reply_markup=admin_waiting_sms_kb(req_id),
        parse_mode="HTML",
    )

    if req["admin_msg_chat_id"] and req["admin_msg_id"]:
        try:
            await bot.edit_message_text(
                chat_id=req["admin_msg_chat_id"],
                message_id=req["admin_msg_id"],
                text=f"{EMOJI_CHECK} Заявка #{req_id}: номер <code>{phone_number}</code> выдан.",
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
        f"{EMOJI_KEY} Введите СМС-код для номера <code>{req['phone_number']}</code>:",
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
    update_request(req_id, status="completed")
    increment_total_bought(req["user_id"])

    try:
        await bot.send_message(
            req["user_id"],
            f"{EMOJI_KEY} <b>Код для номера {req['phone_number']}:</b>\n<code>{req['sms_code']}</code>",
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
                    f"{EMOJI_CHECK} <b>Номер {req['phone_number']}</b>\n"
                    f"Код отправлен: <code>{req['sms_code']}</code>"
                ),
                parse_mode="HTML",
            )
        except Exception:
            logging.exception("Не удалось отредактировать сообщение пользователя (send code)")

    status_note = "отправлен" if sent_ok else "НЕ отправлен (ошибка, см. логи)"
    await callback.message.edit_text(
        f"{EMOJI_CHECK} Заявка #{req_id} завершена. Код {status_note} пользователю "
        f"@{req['username'] or req['user_id']}.",
        reply_markup=None,
    )
    await callback.answer()

# ================= ПОЛЬЗОВАТЕЛЬ НАЖИМАЕТ «КОД ОТПРАВЛЕН!» =================
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
                    f"{EMOJI_KEY} <b>Пользователь отправил код по заявке #{req_id}</b>\n"
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
