"""
Telegram-бот: главное меню + пополнение через xRocket и CryptoBot/@send (aiogram 3.x + aiosqlite)

Установка:
    pip install -U aiogram aiosqlite aiohttp

Переменные окружения (на Render → Environment, только эти три):
    BOT_TOKEN            — токен бота из @BotFather
    XROCKET_API_KEY      — ключ xRocket Pay
    CRYPTOBOT_API_KEY    — ключ Crypto Pay (@send / @CryptoBot)
Всё остальное (поддержка, админы, название, комиссия) — в блоке НАСТРОЙКИ ниже.
Render сам выдаёт PORT и RENDER_EXTERNAL_URL — их задавать не нужно.
Start Command: python main.py
"""

import asyncio
import hashlib
import html
import logging
import math
import os
import time
from datetime import datetime
from urllib.parse import quote

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramConflictError
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан — добавьте его в Environment Variables на Render")

# ── Webhook (Render) ───────────────────────────────────────────
# Render сам выставляет RENDER_EXTERNAL_URL и PORT. Если URL нет — работаем через polling (локально).
WEBHOOK_BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
# Путь и секрет выводятся из токена, поэтому отдельные переменные не нужны (но можно задать WEBHOOK_SECRET)
_token_hash = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()
WEBHOOK_PATH = f"/webhook/{_token_hash[:24]}"
WEBHOOK_SECRET = _token_hash[24:56]
SUPPORT_USERNAME = "support_xyli_shop"  # ← впишите username поддержки без @
# Ссылка кнопки «Тех поддержка» (по умолчанию — t.me/<SUPPORT_USERNAME>)
SUPPORT_URL = f"https://t.me/{SUPPORT_USERNAME}"
# Админы: ID через запятую, например ADMIN_IDS="123456789,987654321"
ADMIN_IDS = {8854740379, 8118184388}  # ← ID админов через запятую: {111, 222}
SHOP_NAME = "XYLI SHOP"
DB_PATH = "bot.db"  # на Render с Persistent Disk поменяйте на "/data/bot.db"
REF_PERCENT = 10.0  # % от пополнений реферала
CURRENCY = "$"

# ── xRocket (оплата) ───────────────────────────────────────────
XROCKET_API_KEY = os.getenv("XROCKET_API_KEY", "PASTE_XROCKET_API_KEY")
XROCKET_URL = "https://pay.xrocket.tg"  # для testnet укажите URL из документации xRocket
PAY_CURRENCY = "USDT"   # валюта счёта (1 USDT = 1 $)

# ── CryptoBot / @send (Crypto Pay API) ─────────────────────────
CRYPTOBOT_API_KEY = os.getenv("CRYPTOBOT_API_KEY", "PASTE_CRYPTOBOT_API_KEY")
# mainnet: https://pay.crypt.bot/api   |   testnet: https://testnet-pay.crypt.bot/api
CRYPTOBOT_URL = "https://pay.crypt.bot/api"

# Способы пополнения: код -> название на кнопке
PROVIDERS = {"xr": "xRocket", "cb": "CryptoBot (@send)"}
TOPUP_AMOUNTS = [5, 10, 25, 50]  # кнопки быстрых сумм (остальное — «Другая сумма»)
MIN_TOPUP = 1.0
MAX_TOPUP = 1000.0
INVOICE_TTL = 3600     # срок жизни счёта, сек
POLL_INTERVAL = 8      # как часто проверять оплату, сек

# Товары каталога: код -> название и цена (цены можно менять в админ-панели).
PRODUCTS = {
    "fresh": {"title": "Новореги", "price": 0.50},
    "warm": {"title": "Фишы", "price": 1.50},
}

# Статусы: (минимум покупок, название)
STATUSES = [
    (0, "🌱 Новичок"),
    (5, "⭐ Покупатель"),
    (25, "🔥 Постоянный клиент"),
    (100, "💎 VIP"),
]

# Кастомные эмодзи (в <tg-emoji> внутри — запасной обычный эмодзи)
EMOJI_PROFILE = '<tg-emoji emoji-id="5452085950022707790">😎</tg-emoji>'
NUM_1 = '<tg-emoji emoji-id="5830126888357468979">1️⃣</tg-emoji>'
NUM_2 = '<tg-emoji emoji-id="5830254543375441108">2️⃣</tg-emoji>'
NUM_3 = '<tg-emoji emoji-id="5827786453303696733">3️⃣</tg-emoji>'
NUM_4 = '<tg-emoji emoji-id="5830434773088083875">4️⃣</tg-emoji>'
EMOJI_REFS = '<tg-emoji emoji-id="4960891456869893259">💠</tg-emoji>'
EMOJI_STATS = '<tg-emoji emoji-id="5854798142578368552">📊</tg-emoji>'
EMOJI_LINK = '<tg-emoji emoji-id="5271604874419647061">🔗</tg-emoji>'
EMOJI_SHOP = '<tg-emoji emoji-id="5199874732983353088">💫</tg-emoji>'
EMOJI_BALANCE = '<tg-emoji emoji-id="5224257782013769471">💰</tg-emoji>'
EMOJI_MIN = '<tg-emoji emoji-id="5447183459602669338">🔽</tg-emoji>'
EMOJI_MAX = '<tg-emoji emoji-id="5449683594425410231">🔼</tg-emoji>'
EMOJI_CUSTOM = '<tg-emoji emoji-id="5197269100878907942">✍️</tg-emoji>'
# иконки на кнопках способов оплаты
PROVIDER_ICONS = {"xr": "5798534328698805312", "cb": "5798650400189980129"}
EMOJI_PAY_AMOUNT = '<tg-emoji emoji-id="5409048419211682843">💵</tg-emoji>'
EMOJI_INVOICE = '<tg-emoji emoji-id="5197288647275071607">🛡</tg-emoji>'
EMOJI_TTL = '<tg-emoji emoji-id="5386367538735104399">⌛</tg-emoji>'
EMOJI_STATUS = '<tg-emoji emoji-id="5397782960512444700">📌</tg-emoji>'
EMOJI_FRESH = '<tg-emoji emoji-id="5850317551090800862">⏰</tg-emoji>'
EMOJI_WARM = '<tg-emoji emoji-id="5881806211195605908">📸</tg-emoji>'
EMOJI_FINANCE = '<tg-emoji emoji-id="5402186569006210455">💱</tg-emoji>'

router = Router()
SEP = "━━━━━━━━━━━━━━━━━━━━"


# ══════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT,
                full_name    TEXT,
                balance      REAL    NOT NULL DEFAULT 0,
                purchased    INTEGER NOT NULL DEFAULT 0,
                spent        REAL    NOT NULL DEFAULT 0,
                deposited    REAL    NOT NULL DEFAULT 0,
                referrer_id  INTEGER,
                ref_earned   REAL    NOT NULL DEFAULT 0,
                reg_date     TEXT    NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                invoice_id  TEXT    PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                amount      REAL    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'pending',
                link        TEXT,
                chat_id     INTEGER,
                message_id  INTEGER,
                created_at  INTEGER NOT NULL,
                expires_at  INTEGER NOT NULL
            )
            """
        )
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        await db.commit()


async def get_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def create_user(user_id: int, username: str | None, full_name: str, referrer_id: int | None) -> bool:
    """Возвращает True, если пользователь новый."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cur:
            if await cur.fetchone():
                return False
        await db.execute(
            "INSERT INTO users (user_id, username, full_name, referrer_id, reg_date) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, full_name, referrer_id, datetime.now().strftime("%d.%m.%Y")),
        )
        await db.commit()
        return True


async def update_profile(user_id: int, username: str | None, full_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
            (username, full_name, user_id),
        )
        await db.commit()


async def count_referrals(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def _credit(db: aiosqlite.Connection, user_id: int, amount: float) -> None:
    """Зачисляет сумму и реферальный бонус (внутри уже открытой транзакции)."""
    await db.execute(
        "UPDATE users SET balance = balance + ?, deposited = deposited + ? WHERE user_id = ?",
        (amount, amount, user_id),
    )
    async with db.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    if row and row[0]:
        bonus = round(amount * REF_PERCENT / 100, 2)
        await db.execute(
            "UPDATE users SET balance = balance + ?, ref_earned = ref_earned + ? WHERE user_id = ?",
            (bonus, bonus, row[0]),
        )


async def add_balance(user_id: int, amount: float) -> None:
    """Ручное зачисление: сумма + реферальный бонус пригласившему."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _credit(db, user_id, amount)
        await db.commit()


async def create_payment(
    invoice_id: str, user_id: int, amount: float, link: str, chat_id: int, message_id: int
) -> None:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO payments (invoice_id, user_id, amount, link, chat_id, message_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (invoice_id, user_id, amount, link, chat_id, message_id, now, now + INVOICE_TTL),
        )
        await db.commit()


async def get_payment(invoice_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM payments WHERE invoice_id = ?", (invoice_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_payment_status(invoice_id: str, status: str) -> bool:
    """Меняет статус только у ожидающего платежа. True — если статус сменился."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE payments SET status = ? WHERE invoice_id = ? AND status = 'pending'",
            (status, invoice_id),
        )
        await db.commit()
        return cur.rowcount == 1


async def credit_payment(invoice_id: str) -> bool:
    """
    Атомарно: помечает платёж оплаченным и зачисляет баланс.
    Повторный вызов ничего не зачислит (защита от двойного начисления).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE payments SET status = 'paid' WHERE invoice_id = ? AND status = 'pending'",
            (invoice_id,),
        )
        if cur.rowcount != 1:
            await db.rollback()
            return False
        async with db.execute(
            "SELECT user_id, amount FROM payments WHERE invoice_id = ?", (invoice_id,)
        ) as c:
            user_id, amount = await c.fetchone()
        await _credit(db, user_id, amount)
        await db.commit()
        return True


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def load_settings() -> None:
    """Подтягивает сохранённые админом цены и минимум пополнения."""
    global MIN_TOPUP
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
    for key, value in rows:
        try:
            if key == "min_topup":
                MIN_TOPUP = float(value)
            elif key.startswith("price:") and key[6:] in PRODUCTS:
                PRODUCTS[key[6:]]["price"] = float(value)
        except ValueError:
            logging.warning("Некорректная настройка %s=%r", key, value)


async def find_user(ref: str) -> dict | None:
    """Ищет пользователя по ID или @username."""
    ref = ref.strip().lstrip("@")
    if not ref:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if ref.isdigit():
            query, params = "SELECT * FROM users WHERE user_id = ?", (int(ref),)
        else:
            query, params = "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (ref,)
        async with db.execute(query, params) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def admin_give(user_id: int, amount: float) -> float:
    """Выдача баланса админом (без учёта в «Пополнено» и без реф. бонуса). Возвращает новый баланс."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()
    user = await get_user(user_id)
    return user["balance"]


async def user_payments(user_id: int, limit: int = 8) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM payments WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def pending_payments() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT invoice_id FROM payments WHERE status = 'pending'") as cur:
            return [r[0] for r in await cur.fetchall()]


# ══════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНОЕ
# ══════════════════════════════════════════════════════════════
def money(value: float) -> str:
    return f"{CURRENCY}{value:,.2f}"


def get_status(purchased: int) -> tuple[str, int | None, int]:
    """(название, порог следующего статуса или None, порог текущего)"""
    current = STATUSES[0]
    nxt = None
    for i, (threshold, name) in enumerate(STATUSES):
        if purchased >= threshold:
            current = (threshold, name)
            nxt = STATUSES[i + 1][0] if i + 1 < len(STATUSES) else None
    return current[1], nxt, current[0]


def progress_bar(purchased: int, start: int, target: int | None, size: int = 10) -> str:
    if target is None:
        return "▰" * size + " MAX"
    span = max(target - start, 1)
    filled = min(size, int((purchased - start) / span * size))
    return "▰" * filled + "▱" * (size - filled) + f" {purchased}/{target}"


def display_name(user: dict) -> str:
    if user.get("username"):
        return f"@{html.escape(user['username'])}"
    return html.escape(user.get("full_name") or "Без имени")


async def build_menu_text(user: dict) -> str:
    status, _, _ = get_status(user["purchased"])

    return (
        f"{EMOJI_SHOP} <b>{html.escape(SHOP_NAME)}</b>\n"
        f"<i>Быстро  •  Надёжно  •  Автоматически</i>\n"
        f"{SEP}\n\n"
        f"{EMOJI_PROFILE} <b>Профиль</b>\n"
        f"├ Никнейм: <b>{display_name(user)}</b>\n"
        f"├ ID: <code>{user['user_id']}</code>\n"
        f"├ Статус: <b>{status}</b>\n"
        f"└ Регистрация: <code>{user['reg_date']}</code>\n\n"
        f"{EMOJI_FINANCE} <b>Финансы</b>\n"
        f"├ Баланс: <b>{money(user['balance'])}</b>\n"
        f"└ Пополнено: <code>{money(user['deposited'])}</code>\n"
        f"<i>Выберите нужный раздел ниже 👇</i>"
    )


# ══════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════════
def btn(text: str, style: str = "primary", **kwargs) -> InlineKeyboardButton:
    """Кнопка со стилем (Bot API 9.4): primary = синяя, success = зелёная, danger = красная."""
    return InlineKeyboardButton(text=text, style=style, **kwargs)


def back_btn(callback_data: str = "menu") -> InlineKeyboardButton:
    """Кнопка «Назад» с кастомным эмодзи."""
    return btn(text="Назад", callback_data=callback_data, icon_custom_emoji_id="5258236805890710909")


def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(btn(text="Купить", style="success", callback_data="buy", icon_custom_emoji_id="4990307318513009602"))
    kb.row(
        btn(text="Рефералы", callback_data="refs", icon_custom_emoji_id="4960891456869893259"),
        btn(text="Финансы", callback_data="finance", icon_custom_emoji_id="5417924076503062111"),
    )
    kb.row(
        btn(text="Инструкция", callback_data="guide", icon_custom_emoji_id="5366421375605040850"),
        btn(text="Тех поддержка", url=SUPPORT_URL, icon_custom_emoji_id="5238025132177369293"),
    )
    return kb.as_markup()


def back_kb(*extra: InlineKeyboardButton) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for button in extra:
        kb.row(button)
    kb.row(back_btn("menu"))
    return kb.as_markup()


def back_kb_to(callback_data: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(back_btn(callback_data))
    return kb.as_markup()


async def safe_edit(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def ensure_user(call_or_msg) -> dict:
    u = call_or_msg.from_user
    await create_user(u.id, u.username, u.full_name, None)
    await update_profile(u.id, u.username, u.full_name)
    return await get_user(u.id)


# ══════════════════════════════════════════════════════════════
#  ХЕНДЛЕРЫ
# ══════════════════════════════════════════════════════════════
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, bot: Bot, state: FSMContext) -> None:
    await state.clear()
    u = message.from_user

    referrer_id = None
    if command.args and command.args.startswith("ref_"):
        raw = command.args[4:]
        if raw.isdigit() and int(raw) != u.id and await get_user(int(raw)):
            referrer_id = int(raw)

    is_new = await create_user(u.id, u.username, u.full_name, referrer_id)
    await update_profile(u.id, u.username, u.full_name)

    if is_new and referrer_id:
        try:
            await bot.send_message(
                referrer_id,
                f"🎉 <b>Новый реферал!</b>\nПо вашей ссылке зарегистрировался "
                f"<b>{html.escape(u.full_name)}</b>.\n"
                f"Вы будете получать <b>{REF_PERCENT:g}%</b> с его пополнений.",
            )
        except Exception:
            pass

    user = await get_user(u.id)
    await message.answer(await build_menu_text(user), reply_markup=main_menu_kb())


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user(call)
    await safe_edit(call, await build_menu_text(user), main_menu_kb())
    await call.answer()


PRODUCT_MARKS = {"fresh": EMOJI_FRESH, "warm": EMOJI_WARM}
PRODUCT_ICONS = {"fresh": "5850317551090800862", "warm": "5881806211195605908"}


@router.callback_query(F.data == "buy")
async def cb_buy(call: CallbackQuery) -> None:
    user = await ensure_user(call)
    blocks = []
    for key, p in PRODUCTS.items():
        blocks.append(
            f"{PRODUCT_MARKS[key]} <b>{p['title']}</b>\n"
            f"└ {EMOJI_PAY_AMOUNT} Цена: <b>{money(p['price'])}</b>"
        )
    text = (
        f"{EMOJI_SHOP} <b>Каталог</b>\n{SEP}\n\n"
        "<blockquote><i>Выберите нужную категорию. Сумма списывается с вашего баланса, "
        "а товар выдаётся автоматически.</i></blockquote>\n\n"
        + "\n\n".join(blocks)
        + f"\n\n{EMOJI_BALANCE} <b>Ваш баланс:</b> {money(user['balance'])}\n\n"
        "<i>Выберите категорию ниже 👇</i>"
    )
    kb = InlineKeyboardBuilder()
    for key, p in PRODUCTS.items():
        kb.row(
            btn(
                text=f"{p['title']} • {money(p['price'])}",
                style="success",
                callback_data=f"item:{key}",
                icon_custom_emoji_id=PRODUCT_ICONS[key],
            )
        )
    kb.row(back_btn("menu"))
    await safe_edit(call, text, kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("item:"))
async def cb_item(call: CallbackQuery) -> None:
    key = call.data.split(":", 1)[1]
    product = PRODUCTS.get(key)
    if not product:
        await call.answer("Товар не найден", show_alert=True)
        return
    user = await ensure_user(call)
    if user["balance"] < product["price"]:
        await call.answer(
            f"Недостаточно средств. Нужно {money(product['price'])}, на балансе {money(user['balance'])}.",
            show_alert=True,
        )
        return
    # Баланса хватает, но товара нет на складе — деньги не списываются
    await call.answer("😔 Товара нет на складе. Загляните позже.", show_alert=True)


@router.callback_query(F.data == "refs")
async def cb_refs(call: CallbackQuery, bot: Bot) -> None:
    user = await ensure_user(call)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{user['user_id']}"
    refs = await count_referrals(user["user_id"])

    text = (
        f"{EMOJI_REFS} <b>Реферальная программа</b>\n{SEP}\n\n"
        f"<blockquote><i>Приглашайте друзей в {html.escape(SHOP_NAME)} и получайте "
        f"{REF_PERCENT:g}% с каждого их пополнения — без ограничений по времени "
        f"и количеству приглашённых.\n\n"
        f"Бонус зачисляется на ваш баланс автоматически сразу после того, как друг "
        f"пополнит счёт. Просто отправьте ему свою ссылку и наблюдайте, как растёт ваш доход.</i></blockquote>\n\n"
        f"{EMOJI_STATS} <b>Ваша статистика</b>\n"
        f"├ Приглашено: <b>{refs}</b>\n"
        f"└ Заработано: <b>{money(user['ref_earned'])}</b>\n\n"
        f"{EMOJI_LINK} <b>Ваша ссылка</b>\n"
        f"<code>{link}</code>"
    )
    share_text = quote(f"Заходи в {SHOP_NAME}!")
    share = btn(
        text="Поделиться ссылкой",
        style="success",
        icon_custom_emoji_id="5264759912025564026",
        url=f"https://t.me/share/url?url={quote(link, safe='')}&text={share_text}",
    )
    await safe_edit(call, text, back_kb(share))
    await call.answer()


@router.callback_query(F.data == "finance")
async def cb_finance(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user(call)
    text = (
        f"{EMOJI_FINANCE} <b>Финансы</b>\n{SEP}\n\n"
        "<blockquote><i>Пополняйте баланс в криптовалюте — быстро и без комиссии с нашей стороны. "
        "Деньги зачисляются автоматически сразу после оплаты.</i></blockquote>\n\n"
        f"{EMOJI_BALANCE} <b>Ваш баланс:</b> <b>{money(user['balance'])}</b>\n\n"
        f"{EMOJI_STATS} <b>Статистика</b>\n"
        f"├ Всего пополнено: <code>{money(user['deposited'])}</code>\n"
        f"├ Всего потрачено: <code>{money(user['spent'])}</code>\n"
        f"└ Реферальный доход: <code>{money(user['ref_earned'])}</code>\n\n"
        "<i>Выберите действие ниже 👇</i>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(btn(text="Пополнить баланс", style="success", callback_data="topup",
               icon_custom_emoji_id="5417924076503062111"))
    kb.row(btn(text="История пополнений", callback_data="history", icon_custom_emoji_id="5444856076954520455"))
    kb.row(back_btn("menu"))
    await safe_edit(call, text, kb.as_markup())
    await call.answer()


PAY_STATUS = {
    "paid": "✅ Оплачен",
    "pending": "⏳ Ожидает",
    "expired": "⌛ Истёк",
    "cancelled": "✖️ Отменён",
}


@router.callback_query(F.data == "history")
async def cb_history(call: CallbackQuery) -> None:
    await ensure_user(call)
    pays = await user_payments(call.from_user.id)
    if pays:
        lines = []
        for i, p in enumerate(pays):
            prov, _ = split_id(p["invoice_id"])
            when = datetime.fromtimestamp(p["created_at"]).strftime("%d.%m %H:%M")
            branch = "└" if i == len(pays) - 1 else "├"
            lines.append(
                f"{branch} <code>{when}</code> • <b>{money(p['amount'])}</b> • "
                f"{PROVIDERS.get(prov, prov)} • {PAY_STATUS.get(p['status'], p['status'])}"
            )
        body = "\n".join(lines)
    else:
        body = "<i>Пополнений пока не было.</i>"
    text = (
        f"🧾 <b>История пополнений</b>\n{SEP}\n\n"
        f"<blockquote><i>Последние {len(pays) or 8} операций по вашему аккаунту.</i></blockquote>\n\n"
        f"{body}"
    )
    await safe_edit(call, text, back_kb_to("finance"))
    await call.answer()


# ══════════════════════════════════════════════════════════════
#  XROCKET: API
# ══════════════════════════════════════════════════════════════
class XRocketError(Exception):
    pass


async def xr_request(method: str, path: str, payload: dict | None = None) -> dict:
    headers = {"Rocket-Pay-Key": XROCKET_API_KEY, "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(
            method, XROCKET_URL.rstrip("/") + path, json=payload, headers=headers
        ) as resp:
            data = await resp.json(content_type=None)
    if resp.status >= 400 or not isinstance(data, dict) or data.get("success") is False:
        raise XRocketError(f"HTTP {resp.status}: {str(data)[:300]}")
    return data.get("data", data)


async def xr_create_invoice(user_id: int, amount: float) -> dict:
    return await xr_request(
        "POST",
        "/tg-invoices",
        {
            "amount": amount,
            "numPayments": 1,
            "currency": PAY_CURRENCY,
            "description": f"Пополнение баланса {SHOP_NAME}",
            "hiddenMessage": "Спасибо! Баланс пополнится автоматически.",
            "commentsEnabled": False,
            "payload": str(user_id),
            "expiredIn": INVOICE_TTL,
        },
    )


async def xr_get_invoice(invoice_id: str) -> dict:
    return await xr_request("GET", f"/tg-invoices/{invoice_id}")


# ══════════════════════════════════════════════════════════════
#  CRYPTOBOT / @SEND: API (Crypto Pay)
# ══════════════════════════════════════════════════════════════
async def cp_request(method: str, params: dict | None = None) -> dict | list:
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_API_KEY, "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            CRYPTOBOT_URL.rstrip("/") + "/" + method, json=params or {}, headers=headers
        ) as resp:
            data = await resp.json(content_type=None)
    if not isinstance(data, dict) or not data.get("ok"):
        raise XRocketError(f"CryptoBot HTTP {resp.status}: {str(data)[:300]}")
    return data["result"]


async def cp_create_invoice(user_id: int, amount: float) -> dict:
    return await cp_request(
        "createInvoice",
        {
            "currency_type": "fiat",
            "fiat": "USD",
            "amount": f"{amount:.2f}",
            "description": f"Пополнение баланса {SHOP_NAME}",
            "payload": str(user_id),
            "allow_comments": False,
            "allow_anonymous": False,
            "expires_in": INVOICE_TTL,
        },
    )


async def cp_get_invoice(raw_id: str) -> dict:
    res = await cp_request("getInvoices", {"invoice_ids": raw_id})
    items = res.get("items", []) if isinstance(res, dict) else res
    if not items:
        raise XRocketError("CryptoBot: счёт не найден")
    return items[0]


# ══════════════════════════════════════════════════════════════
#  ЕДИНЫЙ ИНТЕРФЕЙС ПЛАТЁЖНЫХ СИСТЕМ
#  id счёта в БД хранится как «<провайдер>-<id>», например cb-123456
#  (старые счета без префикса считаются xRocket)
# ══════════════════════════════════════════════════════════════
def split_id(invoice_id: str) -> tuple[str, str]:
    prefix, sep, raw = invoice_id.partition("-")
    if sep and prefix in PROVIDERS:
        return prefix, raw
    return "xr", invoice_id


async def create_invoice(provider: str, user_id: int, amount: float) -> tuple[str, str]:
    """Возвращает (id счёта для БД, ссылка на оплату)."""
    if provider == "cb":
        inv = await cp_create_invoice(user_id, amount)
        link = inv.get("bot_invoice_url") or inv.get("pay_url")
        return f"cb-{inv['invoice_id']}", link
    inv = await xr_create_invoice(user_id, amount)
    return f"xr-{inv['id']}", inv["link"]


async def invoice_status(invoice_id: str) -> str:
    """Нормализованный статус: paid / expired / pending."""
    provider, raw = split_id(invoice_id)
    if provider == "cb":
        status = str((await cp_get_invoice(raw)).get("status", "")).lower()
    else:
        status = str((await xr_get_invoice(raw)).get("status", "")).lower()
    return status if status in ("paid", "expired") else "pending"


async def delete_invoice(invoice_id: str) -> None:
    provider, raw = split_id(invoice_id)
    if provider == "cb":
        await cp_request("deleteInvoice", {"invoice_id": int(raw)})
    else:
        await xr_request("DELETE", f"/tg-invoices/{raw}")


# ══════════════════════════════════════════════════════════════
#  ПОПОЛНЕНИЕ: ЭКРАНЫ
# ══════════════════════════════════════════════════════════════
class TopUp(StatesGroup):
    amount = State()


async def edit_or_send(bot: Bot, chat_id: int, message_id: int, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, reply_markup=kb, disable_web_page_preview=True
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        try:
            await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            logging.exception("Не удалось отправить сообщение о платеже")


def topup_methods_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"{EMOJI_FINANCE} <b>Пополнение баланса</b>\n{SEP}\n\n"
        "<blockquote><i>Выберите удобный способ оплаты. "
        "Баланс пополнится автоматически сразу после платежа.</i></blockquote>\n\n"
        f"{EMOJI_MIN} <b>Минимум:</b> {CURRENCY}{MIN_TOPUP:g}\n"
        f"{EMOJI_MAX} <b>Максимум:</b> {CURRENCY}{MAX_TOPUP:g}\n\n"
        "<i>Выберите способ оплаты 👇</i>"
    )
    kb = InlineKeyboardBuilder()
    for code, name in PROVIDERS.items():
        kb.row(btn(text=name, callback_data=f"topup:{code}", icon_custom_emoji_id=PROVIDER_ICONS[code]))
    kb.row(back_btn("finance"))
    return text, kb.as_markup()


async def topup_screen(user_id: int, provider: str = "xr") -> tuple[str, InlineKeyboardMarkup]:
    user = await get_user(user_id)
    balance = user["balance"] if user else 0.0
    text = (
        f"{EMOJI_FINANCE} <b>Пополнение через {PROVIDERS[provider]}</b>\n{SEP}\n\n"
        "<blockquote><i>Выберите сумму или введите свою — "
        "мы выставим счёт, а вы оплатите его в любой удобной криптовалюте.</i></blockquote>\n\n"
        f"{EMOJI_MIN} <b>Минимум:</b> {CURRENCY}{MIN_TOPUP:g}\n"
        f"{EMOJI_MAX} <b>Максимум:</b> {CURRENCY}{MAX_TOPUP:g}\n"
        f"{EMOJI_BALANCE} <b>Ваш баланс:</b> {money(balance)}\n\n"
        "<i>Выберите сумму ниже 👇</i>"
    )
    kb = InlineKeyboardBuilder()
    amounts = [a for a in TOPUP_AMOUNTS if a >= MIN_TOPUP]
    if amounts:
        kb.row(*[btn(text=f"{CURRENCY}{a:g}", callback_data=f"pay:{provider}:{a:g}") for a in amounts])
    kb.row(btn(text="Другая сумма", callback_data=f"pay_custom:{provider}", icon_custom_emoji_id="5197269100878907942"))
    kb.row(back_btn("topup"))
    return text, kb.as_markup()


def invoice_screen(invoice_id: str, amount: float, link: str) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"{EMOJI_STATUS} <b>Счёт на оплату</b>\n{SEP}\n\n"
        f"<blockquote><i>Нажмите «Оплатить» и завершите платёж в {PROVIDERS[split_id(invoice_id)[0]]}. "
        "Баланс пополнится автоматически — бот сам проверяет оплату.</i></blockquote>\n\n"
        f"├ {EMOJI_PAY_AMOUNT} <b>К оплате:</b> {money(amount)}\n"
        f"├ {EMOJI_INVOICE} <b>Счёт:</b> <code>#{html.escape(split_id(invoice_id)[1])}</code>\n"
        f"├ {EMOJI_TTL} <b>Действует:</b> {INVOICE_TTL // 60} мин.\n"
        f"└ {EMOJI_STATUS} <b>Статус:</b> ожидает оплаты"
    )
    kb = InlineKeyboardBuilder()
    kb.row(btn(text="Оплатить", style="success", url=link, icon_custom_emoji_id="5445353829304387411"))
    kb.row(btn(text="Отменить счёт", style="danger", callback_data=f"cancel:{invoice_id}",
               icon_custom_emoji_id="5210952531676504517"))
    return text, kb.as_markup()


async def show_invoice(
    bot: Bot, user_id: int, chat_id: int, message_id: int, amount: float, provider: str
) -> None:
    try:
        invoice_id, link = await create_invoice(provider, user_id, amount)
    except Exception:
        logging.exception("%s: не удалось создать счёт", provider)
        text = (
            f"⚠️ <b>Не удалось создать счёт</b>\n{SEP}\n\n"
            "<blockquote><i>Платёжная система временно недоступна. "
            "Попробуйте ещё раз через минуту или напишите в тех поддержку.</i></blockquote>"
        )
        kb = InlineKeyboardBuilder()
        kb.row(back_btn(f"topup:{provider}"))
        await edit_or_send(bot, chat_id, message_id, text, kb.as_markup())
        return

    await create_payment(invoice_id, user_id, amount, link, chat_id, message_id)
    text, kb = invoice_screen(invoice_id, amount, link)
    await edit_or_send(bot, chat_id, message_id, text, kb)
    start_watcher(bot, invoice_id)


# ══════════════════════════════════════════════════════════════
#  ПОПОЛНЕНИЕ: ФОНОВАЯ ПРОВЕРКА ОПЛАТЫ
# ══════════════════════════════════════════════════════════════
watchers: dict[str, asyncio.Task] = {}


def start_watcher(bot: Bot, invoice_id: str) -> None:
    task = watchers.get(invoice_id)
    if task and not task.done():
        return
    watchers[invoice_id] = asyncio.create_task(watch_invoice(bot, invoice_id))


async def finalize_paid(bot: Bot, invoice_id: str) -> None:
    if not await credit_payment(invoice_id):
        return  # уже зачислено ранее
    pay = await get_payment(invoice_id)
    user = await get_user(pay["user_id"])
    text = (
        f"✅ <b>Оплата прошла успешно</b>\n{SEP}\n\n"
        f"<blockquote><i>Баланс пополнен автоматически. "
        f"Спасибо, что выбираете {html.escape(SHOP_NAME)}!</i></blockquote>\n\n"
        f"💵 <b>Зачислено:</b> {money(pay['amount'])}\n"
        f"{EMOJI_BALANCE} <b>Ваш баланс:</b> {money(user['balance'])}\n"
        f"{EMOJI_INVOICE} <b>Счёт:</b> <code>#{html.escape(split_id(invoice_id)[1])}</code>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(back_btn("menu"))
    await edit_or_send(bot, pay["chat_id"], pay["message_id"], text, kb.as_markup())


async def finalize_expired(bot: Bot, invoice_id: str) -> None:
    if not await set_payment_status(invoice_id, "expired"):
        return
    pay = await get_payment(invoice_id)
    text = (
        f"⌛ <b>Счёт истёк</b>\n{SEP}\n\n"
        "<blockquote><i>Время на оплату вышло, деньги не списаны. "
        "Создайте новый счёт, чтобы пополнить баланс.</i></blockquote>\n\n"
        f"{EMOJI_INVOICE} <b>Счёт:</b> <code>#{html.escape(split_id(invoice_id)[1])}</code>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(back_btn(f"topup:{split_id(invoice_id)[0]}"))
    await edit_or_send(bot, pay["chat_id"], pay["message_id"], text, kb.as_markup())


async def watch_invoice(bot: Bot, invoice_id: str) -> None:
    try:
        while True:
            pay = await get_payment(invoice_id)
            if not pay or pay["status"] != "pending":
                return
            try:
                status = await invoice_status(invoice_id)
            except Exception:
                logging.warning("Ошибка проверки счёта %s", invoice_id)
                status = ""
            if status == "paid":
                await finalize_paid(bot, invoice_id)
                return
            if status == "expired" or time.time() > pay["expires_at"] + 120:
                await finalize_expired(bot, invoice_id)
                return
            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("Ошибка в watch_invoice(%s)", invoice_id)
    finally:
        watchers.pop(invoice_id, None)


# ══════════════════════════════════════════════════════════════
#  ПОПОЛНЕНИЕ: ХЕНДЛЕРЫ
# ══════════════════════════════════════════════════════════════
@router.callback_query(F.data == "topup")
async def cb_topup(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await ensure_user(call)
    text, kb = topup_methods_screen()
    await safe_edit(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("topup:"))
async def cb_topup_method(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await ensure_user(call)
    provider = call.data.split(":", 1)[1]
    if provider not in PROVIDERS:
        await call.answer("Неизвестный способ оплаты", show_alert=True)
        return
    text, kb = await topup_screen(call.from_user.id, provider)
    await safe_edit(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay_amount(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await state.clear()
    await ensure_user(call)
    try:
        _, provider, raw_amount = call.data.split(":")
        amount = round(float(raw_amount), 2)
    except ValueError:
        await call.answer("Неверная сумма", show_alert=True)
        return
    if provider not in PROVIDERS:
        await call.answer("Неизвестный способ оплаты", show_alert=True)
        return
    if not MIN_TOPUP <= amount <= MAX_TOPUP:
        await call.answer("Сумма вне допустимого диапазона", show_alert=True)
        return
    await call.answer("Создаю счёт…")
    await show_invoice(bot, call.from_user.id, call.message.chat.id, call.message.message_id, amount, provider)


@router.callback_query(F.data.startswith("pay_custom:"))
async def cb_pay_custom(call: CallbackQuery, state: FSMContext) -> None:
    provider = call.data.split(":", 1)[1]
    if provider not in PROVIDERS:
        await call.answer("Неизвестный способ оплаты", show_alert=True)
        return
    await state.set_state(TopUp.amount)
    await state.update_data(msg_id=call.message.message_id, provider=provider)
    text = (
        f"{EMOJI_CUSTOM} <b>Своя сумма</b>\n{SEP}\n\n"
        "<blockquote><i>Отправьте сообщением сумму пополнения в долларах. "
        "Можно использовать дробные значения, например 7.5</i></blockquote>\n\n"
        f"{EMOJI_MIN} <b>Минимум:</b> {CURRENCY}{MIN_TOPUP:g}\n"
        f"{EMOJI_MAX} <b>Максимум:</b> {CURRENCY}{MAX_TOPUP:g}"
    )
    kb = InlineKeyboardBuilder()
    kb.row(back_btn(f"topup:{provider}"))
    await safe_edit(call, text, kb.as_markup())
    await call.answer()


@router.message(TopUp.amount, F.text)
async def msg_custom_amount(message: Message, bot: Bot, state: FSMContext) -> None:
    raw = message.text.replace("$", "").replace(",", ".").strip()
    try:
        amount = round(float(raw), 2)
    except ValueError:
        amount = None

    if amount is None or not MIN_TOPUP <= amount <= MAX_TOPUP:
        await message.answer(
            f"⚠️ <b>Неверная сумма.</b>\n"
            f"<i>Введите число от {CURRENCY}{MIN_TOPUP:g} до {CURRENCY}{MAX_TOPUP:g}.</i>"
        )
        return

    data = await state.get_data()
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    provider = data.get("provider", "xr")
    msg_id = data.get("msg_id")
    if msg_id is None:
        sent = await message.answer("⏳ Создаю счёт…")
        msg_id = sent.message_id
    await show_invoice(bot, message.from_user.id, message.chat.id, msg_id, amount, provider)


async def _own_payment(call: CallbackQuery, invoice_id: str) -> dict | None:
    pay = await get_payment(invoice_id)
    if not pay or pay["user_id"] != call.from_user.id:
        await call.answer("Счёт не найден", show_alert=True)
        return None
    return pay


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(call: CallbackQuery, bot: Bot) -> None:
    invoice_id = call.data.split(":", 1)[1]
    pay = await _own_payment(call, invoice_id)
    if not pay:
        return

    if pay["status"] == "pending":
        # если деньги уже пришли — не отменяем, а зачисляем
        try:
            status = await invoice_status(invoice_id)
        except Exception:
            status = ""
        if status == "paid":
            await call.answer("✅ Оплата получена!")
            await finalize_paid(bot, invoice_id)
            return
        try:
            await delete_invoice(invoice_id)
        except Exception:
            logging.warning("Не удалось удалить счёт %s", invoice_id)
        await set_payment_status(invoice_id, "cancelled")

    text, kb = await topup_screen(call.from_user.id, split_id(invoice_id)[0])
    await safe_edit(call, text, kb)
    await call.answer("Счёт отменён")


@router.callback_query(F.data == "guide")
async def cb_guide(call: CallbackQuery) -> None:
    text = (
        f"<b>🔖 Инструкция</b>\n{SEP}\n\n"
        f"{NUM_1} <b>Возьмите номер</b>\n"
        "<i>С баланса замораживается сумма. Вы получаете номер и пароль, если он есть.</i>\n\n"
        f"{NUM_2} <b>Подтвердите отправку</b>\n"
        "<i>Введите номер в MAX и нажмите «✅ Код отправлен». На это даётся 5 минут. "
        "Не успели — заявка сгорит, деньги вернутся.</i>\n\n"
        f"{NUM_3} <b>Ждите SMS</b>\n"
        "<i>Бот пришлёт код и пароль в течение 1 минуты.</i>\n\n"
        f"{NUM_4} <b>Введите код</b>\n"
        "<i>Как только код получен — средства списываются. Услуга оказана.</i>"
    )
    await safe_edit(call, text, back_kb())
    await call.answer()


# ══════════════════════════════════════════════════════════════
#  АДМИН-ПАНЕЛЬ (/admin)
# ══════════════════════════════════════════════════════════════
class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        return bool(event.from_user) and event.from_user.id in ADMIN_IDS


admin_router = Router()
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())


class Admin(StatesGroup):
    give_user = State()
    give_amount = State()
    price = State()
    min_topup = State()


MAX_PRICE = 10000.0
MAX_GIVE = 100000.0


def parse_number(raw: str) -> float | None:
    try:
        value = round(float(raw.replace("$", "").replace(",", ".").strip()), 2)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def admin_panel(note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"├ {p['title']}: <b>{money(p['price'])}</b>" for p in PRODUCTS.values()]
    lines.append(f"└ Мин. пополнение: <b>{money(MIN_TOPUP)}</b>")
    text = (
        (f"{note}\n\n" if note else "")
        + f"{EMOJI_SHOP} <b>Админ-панель</b>\n{SEP}\n\n"
        "<blockquote><i>Управление магазином: баланс пользователей, цены и лимит пополнения.</i></blockquote>\n\n"
        f"{EMOJI_STATS} <b>Текущие настройки</b>\n" + "\n".join(lines) + "\n\n"
        "<i>Выберите действие ниже 👇</i>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(btn(text="Выдать баланс", style="success", callback_data="adm_give",
               icon_custom_emoji_id="5224257782013769471"))
    kb.row(btn(text="Изменить цены", callback_data="adm_prices", icon_custom_emoji_id="5409048419211682843"))
    kb.row(btn(text="Мин. пополнение", callback_data="adm_min", icon_custom_emoji_id="5447183459602669338"))
    kb.row(back_btn("menu"))
    return text, kb.as_markup()


def prompt_screen(title: str, hint: str, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"{title}\n{SEP}\n\n"
        f"<blockquote><i>{hint}</i></blockquote>"
        + (f"\n\n⚠️ <b>{note}</b>" if note else "")
    )
    kb = InlineKeyboardBuilder()
    kb.row(back_btn("admin"))
    return text, kb.as_markup()


GIVE_TITLE = f"{EMOJI_BALANCE} <b>Выдача баланса</b>"


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, kb = admin_panel()
    await message.answer(text, reply_markup=kb)


@admin_router.callback_query(F.data == "admin")
async def cb_admin(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = admin_panel()
    await safe_edit(call, text, kb)
    await call.answer()


# ── выдача баланса ──
@admin_router.callback_query(F.data == "adm_give")
async def cb_adm_give(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Admin.give_user)
    await state.update_data(msg_id=call.message.message_id)
    text, kb = prompt_screen(GIVE_TITLE, "Отправьте ID или @username пользователя.")
    await safe_edit(call, text, kb)
    await call.answer()


@admin_router.message(Admin.give_user, F.text)
async def msg_give_user(message: Message, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    target = await find_user(message.text)
    try:
        await message.delete()
    except Exception:
        pass
    if not target:
        text, kb = prompt_screen(
            GIVE_TITLE, "Отправьте ID или @username пользователя.", "Пользователь не найден. Он должен хотя бы раз запустить бота."
        )
        await edit_or_send(bot, message.chat.id, data["msg_id"], text, kb)
        return
    await state.set_state(Admin.give_amount)
    await state.update_data(target=target["user_id"])
    text, kb = prompt_screen(
        GIVE_TITLE,
        f"Пользователь: {display_name(target)} (ID {target['user_id']})\n"
        f"Баланс сейчас: {money(target['balance'])}\n\n"
        "Отправьте сумму в долларах, например 5 или 7.5",
    )
    await edit_or_send(bot, message.chat.id, data["msg_id"], text, kb)


@admin_router.message(Admin.give_amount, F.text)
async def msg_give_amount(message: Message, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    amount = parse_number(message.text)
    try:
        await message.delete()
    except Exception:
        pass
    target = await get_user(data["target"])
    if amount is None or not 0 < amount <= MAX_GIVE:
        text, kb = prompt_screen(
            GIVE_TITLE,
            f"Пользователь: {display_name(target)} (ID {target['user_id']})\nОтправьте сумму в долларах.",
            f"Введите число от 0.01 до {MAX_GIVE:g}.",
        )
        await edit_or_send(bot, message.chat.id, data["msg_id"], text, kb)
        return
    new_balance = await admin_give(target["user_id"], amount)
    await state.clear()
    try:
        await bot.send_message(
            target["user_id"],
            f"{EMOJI_BALANCE} <b>Вам начислено {money(amount)}</b>\nВаш баланс: <b>{money(new_balance)}</b>",
        )
    except Exception:
        pass
    text, kb = admin_panel(
        f"✅ <b>Выдано {money(amount)}</b> пользователю {display_name(target)}. Новый баланс: <b>{money(new_balance)}</b>"
    )
    await edit_or_send(bot, message.chat.id, data["msg_id"], text, kb)


# ── цены ──
@admin_router.callback_query(F.data == "adm_prices")
async def cb_adm_prices(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text = (
        f"{EMOJI_PAY_AMOUNT} <b>Цены</b>\n{SEP}\n\n"
        "<blockquote><i>Выберите товар, цену которого нужно изменить.</i></blockquote>"
    )
    kb = InlineKeyboardBuilder()
    for key, p in PRODUCTS.items():
        kb.row(btn(text=f"{p['title']} • {money(p['price'])}", callback_data=f"adm_price:{key}",
                   icon_custom_emoji_id=PRODUCT_ICONS.get(key)))
    kb.row(back_btn("admin"))
    await safe_edit(call, text, kb.as_markup())
    await call.answer()


@admin_router.callback_query(F.data.startswith("adm_price:"))
async def cb_adm_price(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":", 1)[1]
    if key not in PRODUCTS:
        await call.answer("Товар не найден", show_alert=True)
        return
    await state.set_state(Admin.price)
    await state.update_data(msg_id=call.message.message_id, key=key)
    p = PRODUCTS[key]
    text, kb = prompt_screen(
        f"{EMOJI_PAY_AMOUNT} <b>Цена: {p['title']}</b>",
        f"Сейчас: {money(p['price'])}\nОтправьте новую цену в долларах, например 0.8",
    )
    await safe_edit(call, text, kb)
    await call.answer()


@admin_router.message(Admin.price, F.text)
async def msg_price(message: Message, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    key = data["key"]
    p = PRODUCTS[key]
    value = parse_number(message.text)
    try:
        await message.delete()
    except Exception:
        pass
    if value is None or not 0 < value <= MAX_PRICE:
        text, kb = prompt_screen(
            f"{EMOJI_PAY_AMOUNT} <b>Цена: {p['title']}</b>",
            f"Сейчас: {money(p['price'])}\nОтправьте новую цену в долларах.",
            f"Введите число от 0.01 до {MAX_PRICE:g}.",
        )
        await edit_or_send(bot, message.chat.id, data["msg_id"], text, kb)
        return
    p["price"] = value
    await set_setting(f"price:{key}", str(value))
    await state.clear()
    text, kb = admin_panel(f"✅ Цена <b>{p['title']}</b> теперь <b>{money(value)}</b>")
    await edit_or_send(bot, message.chat.id, data["msg_id"], text, kb)


# ── минимальное пополнение ──
MIN_TITLE = f"{EMOJI_MIN} <b>Минимальное пополнение</b>"


@admin_router.callback_query(F.data == "adm_min")
async def cb_adm_min(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Admin.min_topup)
    await state.update_data(msg_id=call.message.message_id)
    text, kb = prompt_screen(
        MIN_TITLE, f"Сейчас: {money(MIN_TOPUP)}\nОтправьте новый минимум в долларах, например 2"
    )
    await safe_edit(call, text, kb)
    await call.answer()


@admin_router.message(Admin.min_topup, F.text)
async def msg_min_topup(message: Message, bot: Bot, state: FSMContext) -> None:
    global MIN_TOPUP
    data = await state.get_data()
    value = parse_number(message.text)
    try:
        await message.delete()
    except Exception:
        pass
    if value is None or not 0 < value <= MAX_TOPUP:
        text, kb = prompt_screen(
            MIN_TITLE,
            f"Сейчас: {money(MIN_TOPUP)}\nОтправьте новый минимум в долларах.",
            f"Введите число от 0.01 до {MAX_TOPUP:g}.",
        )
        await edit_or_send(bot, message.chat.id, data["msg_id"], text, kb)
        return
    MIN_TOPUP = value
    await set_setting("min_topup", str(value))
    await state.clear()
    text, kb = admin_panel(f"✅ Минимальное пополнение теперь <b>{money(value)}</b>")
    await edit_or_send(bot, message.chat.id, data["msg_id"], text, kb)


# ══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════
async def ensure_no_webhook(bot: Bot, attempts: int = 5) -> None:
    """Гарантированно удаляет webhook (иначе getUpdates даёт Conflict)."""
    for attempt in range(1, attempts + 1):
        try:
            info = await bot.get_webhook_info()
            if info.url:
                logging.warning("Найден активный webhook: %s — удаляю", info.url)
            await bot.delete_webhook(drop_pending_updates=True)
            info = await bot.get_webhook_info()
            if not info.url:
                logging.info("Webhook удалён, работаем через polling")
                return
        except Exception as e:
            logging.error("Не удалось удалить webhook (попытка %s/%s): %s", attempt, attempts, e)
        await asyncio.sleep(2)
    logging.error("Webhook не удалился — возможно, другой процесс с этим же токеном снова его ставит")


async def webhook_watchdog(bot: Bot, interval: int = 10) -> None:
    """Фоновая проверка: если кто-то снова поставил webhook — сразу удаляем."""
    while True:
        try:
            info = await bot.get_webhook_info()
            if info.url:
                logging.warning("Watchdog: обнаружен webhook %s — удаляю", info.url)
                await bot.delete_webhook(drop_pending_updates=False)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error("Watchdog: ошибка проверки webhook: %s", e)
        await asyncio.sleep(interval)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    await init_db()
    await load_settings()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(admin_router)
    dp.include_router(router)

    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS не задан — админ-панель (/admin) недоступна")
    if XROCKET_API_KEY.startswith("PASTE_"):
        logging.warning("XROCKET_API_KEY не задан — пополнение через xRocket работать не будет")
    if CRYPTOBOT_API_KEY.startswith("PASTE_"):
        logging.warning("CRYPTOBOT_API_KEY не задан — пополнение через CryptoBot работать не будет")
    for invoice_id in await pending_payments():
        start_watcher(bot, invoice_id)  # продолжаем следить за счетами после перезапуска

    if WEBHOOK_BASE_URL:
        await run_webhook(bot, dp)
    else:
        await run_polling(bot, dp)


async def run_webhook(bot: Bot, dp: Dispatcher) -> None:
    """Режим для Render: aiohttp-сервер принимает обновления от Telegram."""
    webhook_url = f"{WEBHOOK_BASE_URL}{WEBHOOK_PATH}"

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=False,
    )
    logging.info("Бот запущен через webhook: %s (порт %s)", f"{WEBHOOK_BASE_URL}/webhook/***", PORT)
    try:
        await asyncio.Event().wait()
    finally:
        # webhook при остановке не удаляем: при деплое на Render новый инстанс уже мог выставить свой
        await runner.cleanup()
        await bot.session.close()


async def run_polling(bot: Bot, dp: Dispatcher) -> None:
    """Локальный режим без публичного URL."""
    await ensure_no_webhook(bot)
    watchdog_task = asyncio.create_task(webhook_watchdog(bot))
    logging.info("Бот запущен через polling")
    try:
        while True:
            try:
                await dp.start_polling(bot, handle_signals=False)
                break
            except TelegramConflictError as e:
                logging.error("Conflict: %s — снова удаляю webhook и перезапускаю polling", e)
                await ensure_no_webhook(bot)
                await asyncio.sleep(3)
    finally:
        watchdog_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
