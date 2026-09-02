import asyncio
import logging
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ================= НАСТРОЙКИ =================
BOT_TOKEN = "8651956926:AAG3ML1uGBPQOgrM5WAMl3kXaRLvVxTHCsw"
SHOP_NAME = "Kretros SMS Shop"
SUPPORT_USERNAME = "your_support_username"  # без @

DB_PATH = "shop.db"

logging.basicConfig(level=logging.INFO)

router = Router()


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
        # обновляем username, если поменялся
        if username and row["username"] != username:
            conn.execute(
                "UPDATE users SET username = ? WHERE user_id = ?", (username, user_id)
            )
            conn.commit()
    conn.close()
    return row


# ================= КЛАВИАТУРА =================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📞 Взять номер", callback_data="get_number")],
            [InlineKeyboardButton(text="💳 Баланс", callback_data="balance")],
            [InlineKeyboardButton(text="⚙️ Правила", callback_data="rules")],
            [InlineKeyboardButton(text="🛎 Поддержка", callback_data="support")],
        ]
    )


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
        ]
    )


# ================= ТЕКСТ ГЛАВНОГО МЕНЮ =================
def build_menu_text(user_row: sqlite3.Row) -> str:
    username = user_row["username"] or "—"
    return (
        f"<b>{SHOP_NAME}</b>\n"
        "―――――――――――――――――\n"
        f"| 👤 User: @{username} !\n"
        f"| 🆔 ID: <code>{user_row['user_id']}</code>\n"
        f"| 💰 Баланс: {user_row['balance']:.0f}$\n"
        f"| 🧾 Всего куплено: {user_row['total_bought']}\n"
        "―――――――――――――――――\n\n"
        "Кнопки :"
    )


# ================= ХЕНДЛЕРЫ =================
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
async def cb_get_number(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "📞 <b>Взять номер</b>\n\nВыберите сервис для получения номера (раздел в разработке).",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "balance")
async def cb_balance(callback: CallbackQuery) -> None:
    user_row = get_or_create_user(callback.from_user.id, callback.from_user.username)
    await callback.message.edit_text(
        f"💳 <b>Ваш баланс:</b> {user_row['balance']:.0f}$\n\n"
        "Пополнение доступно через раздел поддержки.",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "rules")
async def cb_rules(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "⚙️ <b>Правила пользования сервисом</b>\n\n"
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
        f"🛎 <b>Поддержка</b>\n\nПо всем вопросам пишите: @{SUPPORT_USERNAME}",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


# ================= ЗАПУСК =================
async def main() -> None:
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
