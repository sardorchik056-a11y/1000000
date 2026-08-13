# bot.py - АДАПТИРОВАН ДЛЯ AIOGRAM 3.x

import logging
import asyncio
import re
import random
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, 
    CallbackQuery, Message
)
from aiogram.client.default import DefaultBotProperties

from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text, BigInteger, func
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# ==================== КОНФИГУРАЦИЯ ====================

BOT_TOKEN = "8651956926:AAG3ML1uGBPQOgrM5WAMl3kXaRLvVxTHCsw"
CRYPTO_TOKEN = "YOUR_CRYPTO_TOKEN_HERE"
ADMIN_IDS = []  # <-- ОСТАВЛЯЕМ ПУСТЫМ, ЧТОБЫ НИКТО НЕ БЫЛ АДМИНОМ
SUPPORT_LINK = "https://t.me/support_bot"
GROUP_ID = -1001234567890

PRICE_NORMAL = 3.0
PRICE_FAST = 1.5
PRICE_SECRET = 3.0
MIN_WITHDRAW = 10.0
MIN_TIME_TO_EARN = 10

CRYPTO_API_URL = "https://pay.crypt.bot/api"
DATABASE_URL = "sqlite:///babrito.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== БАЗА ДАННЫХ ====================

Base = declarative_base()
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String)
    first_name = Column(String)
    last_name = Column(String)
    balance = Column(Float, default=0.0)
    registered_at = Column(DateTime, default=datetime.now)
    is_blocked = Column(Boolean, default=False)
    is_admin = Column(Boolean, default=False)

    total_taken = Column(Integer, default=0)
    total_stood = Column(Integer, default=0)
    total_failed = Column(Integer, default=0)
    total_profit = Column(Float, default=0.0)

    daily_taken = Column(Integer, default=0)
    daily_stood = Column(Integer, default=0)
    daily_failed = Column(Integer, default=0)
    daily_profit = Column(Float, default=0.0)
    daily_date = Column(String, default=datetime.now().strftime("%Y-%m-%d"))


class QueueItem(Base):
    __tablename__ = "queue"
    id = Column(Integer, primary_key=True)
    phone_number = Column(String, nullable=False)
    user_id = Column(BigInteger, nullable=False)
    queue_type = Column(String, nullable=False)
    price = Column(Float, default=0.0)
    status = Column(String, default="waiting")
    created_at = Column(DateTime, default=datetime.now)
    taken_at = Column(DateTime, nullable=True)
    stood_at = Column(DateTime, nullable=True)
    minutes_stood = Column(Integer, default=0)
    is_paid = Column(Boolean, default=False)
    group_message_id = Column(Integer, nullable=True)
    secret_code = Column(String, nullable=True)


class WithdrawRequest(Base):
    __tablename__ = "withdraws"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    amount = Column(Float, nullable=False)
    status = Column(String, default="pending")
    check_id = Column(String)
    check_url = Column(String)
    created_at = Column(DateTime, default=datetime.now)
    completed_at = Column(DateTime, nullable=True)


class BotStats(Base):
    __tablename__ = "bot_stats"
    id = Column(Integer, primary_key=True)
    total_users = Column(Integer, default=0)
    total_blocked = Column(Integer, default=0)
    treasury_balance = Column(Float, default=0.0)
    total_taken = Column(Integer, default=0)
    total_stood = Column(Integer, default=0)
    total_failed = Column(Integer, default=0)
    total_profit = Column(Float, default=0.0)
    total_balance = Column(Float, default=0.0)
    last_updated = Column(DateTime, default=datetime.now)


class WelcomePhoto(Base):
    __tablename__ = "welcome_photos"
    id = Column(Integer, primary_key=True)
    file_id = Column(String, nullable=False)
    uploaded_at = Column(DateTime, default=datetime.now)


Base.metadata.create_all(engine)

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()

# ==================== FSM СОСТОЯНИЯ ====================


class QueueStates(StatesGroup):
    waiting_phone = State()
    waiting_queue_type = State()
    waiting_secret_phone = State()
    waiting_secret_queue_type = State()
    waiting_code = State()


class AdminStates(StatesGroup):
    waiting_price = State()
    waiting_mailing_text = State()
    waiting_mailing_photo = State()
    waiting_withdraw_address = State()
    waiting_user_action = State()
    waiting_balance_action = State()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def generate_secret_code() -> str:
    """Генерация секретного кода"""
    return ''.join(random.choices('0123456789', k=6))


def get_user(session: Session, telegram_id: int) -> User:
    user = session.query(User).filter_by(telegram_id=telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id)
        session.add(user)
        session.commit()

        stats = session.query(BotStats).first()
        if stats:
            stats.total_users += 1
        else:
            stats = BotStats(total_users=1)
            session.add(stats)
        session.commit()
    return user


def format_phone(phone: str) -> Optional[str]:
    phone = re.sub(r"[^\d]", "", phone)
    if phone.startswith("8"):
        phone = "7" + phone[1:]
    if len(phone) == 10 and phone.startswith("9"):
        phone = "7" + phone
    if len(phone) == 11 and phone.startswith("7"):
        return phone
    return None


def get_queue_position(session: Session, item_id: int) -> Optional[int]:
    items = session.query(QueueItem).filter_by(status="waiting").order_by(QueueItem.created_at).all()
    for i, item in enumerate(items, 1):
        if item.id == item_id:
            return i
    return None


def get_user_queue(session: Session, user_id: int) -> List[QueueItem]:
    return session.query(QueueItem).filter_by(user_id=user_id).filter(
        QueueItem.status.in_(["waiting", "taken"])
    ).order_by(QueueItem.created_at).all()


def get_queue_count(session: Session) -> int:
    return session.query(QueueItem).filter_by(status="waiting").count()


def get_queue_type_label(queue_type: str) -> str:
    labels = {
        "fast": "⚡ Вне очереди",
        "normal": "💰 Обычная",
        "secret": "🔐 Секретная"
    }
    return labels.get(queue_type, queue_type)


def get_status_emoji(status: str) -> str:
    emojis = {
        "waiting": "⏳",
        "taken": "📱",
        "stood": "✅",
        "failed": "❌"
    }
    return emojis.get(status, "❓")


def back_keyboard(callback_data: str = "back_to_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data=callback_data)]
        ]
    )


# ==================== CRYPTOBOT API ====================

class CryptoBotAPI:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Content-Type": "application/json",
            "Crypto-Pay-API-Token": token
        }

    async def _make_request(self, method: str, payload: dict) -> dict:
        url = f"{CRYPTO_API_URL}/{method}"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=payload) as resp:
                return await resp.json()

    async def create_check(self, amount: float, currency: str = "USDT", description: str = None) -> dict:
        payload = {
            "amount": str(amount),
            "currency": currency,
            "description": description or f"Вывод {amount} USDT"
        }
        return await self._make_request("createCheck", payload)

    async def get_check_status(self, check_id: str) -> dict:
        return await self._make_request("getCheckStatus", {"check_id": check_id})

    async def create_invoice(self, amount: float, currency: str = "USDT", description: str = None) -> dict:
        payload = {
            "amount": str(amount),
            "currency": currency,
            "description": description or f"Пополнение казны {amount} USDT"
        }
        return await self._make_request("createInvoice", payload)


crypto = CryptoBotAPI(CRYPTO_TOKEN)

# ==================== КЛАВИАТУРЫ ====================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📱 Сдать номер", callback_data="submit_number"),
                InlineKeyboardButton(text="👤 Профиль", callback_data="profile")
            ],
            [
                InlineKeyboardButton(text="📋 Моя очередь", callback_data="my_queue"),
                InlineKeyboardButton(text="🆘 Тех поддержка", callback_data="support")
            ]
        ]
    )


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        ("📊 Статистика дня", "admin_stats_day"),
        ("📊 Статистика недели", "admin_stats_week"),
        ("📊 Вся статистика", "admin_stats_all"),
        ("👥 Пользователи", "admin_users"),
        ("💰 Балансы", "admin_balances"),
        ("💰 Пополнить казну", "admin_topup"),
        ("📝 Редактировать прайс", "admin_edit_price"),
        ("📨 Рассылка", "admin_mailing"),
        ("📊 История выводов", "admin_withdraw_history"),
        ("📊 Заявки на вывод", "admin_withdraw_requests"),
        ("💥 Расчет оплат", "admin_calculate_payments"),
        ("🔙 Закрыть", "back_to_menu")
    ]
    
    keyboard_buttons = []
    row = []
    for i, (text, callback) in enumerate(buttons):
        row.append(InlineKeyboardButton(text=text, callback_data=callback))
        if len(row) == 2 or i == len(buttons) - 1:
            keyboard_buttons.append(row)
            row = []
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)


def group_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📱 Взять номер", callback_data="group_take_number"),
                InlineKeyboardButton(text="📊 Моя статистика", callback_data="group_my_stats")
            ]
        ]
    )


def queue_type_keyboard(phone: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚡ Вне очереди (1.5$)", callback_data=f"queue_fast_{phone}"),
                InlineKeyboardButton(text="💰 Обычная (3$)", callback_data=f"queue_normal_{phone}")
            ],
            [
                InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")
            ]
        ]
    )


def connect_method_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Перенос", callback_data="connect_transfer"),
                InlineKeyboardButton(text="🔗 Связ", callback_data="connect_link")
            ],
            [
                InlineKeyboardButton(text="📱 Кюар", callback_data="connect_qr"),
                InlineKeyboardButton(text="🔙 Назад", callback_data="group_back")
            ]
        ]
    )


# ==================== ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЯ ====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    session = SessionLocal()
    try:
        user = get_user(session, message.from_user.id)
        photo = session.query(WelcomePhoto).order_by(WelcomePhoto.uploaded_at.desc()).first()

        text = f"""💬 Добро пожаловать в Babrito WA

🏷 Username: @{message.from_user.username or 'Не указан'}
💰 Цена за аккаунт: {PRICE_NORMAL}$
💰 Баланс: {user.balance}$

💬 Выберите нужный раздел:"""

        if photo:
            await bot.send_photo(message.chat.id, photo.file_id, caption=text, reply_markup=main_menu_keyboard())
        else:
            await message.reply(text, reply_markup=main_menu_keyboard())
    finally:
        session.close()


@dp.callback_query(lambda c: c.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    await callback.message.delete()
    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)
        photo = session.query(WelcomePhoto).order_by(WelcomePhoto.uploaded_at.desc()).first()

        text = f"""💬 Добро пожаловать в Babrito WA

🏷 Username: @{callback.from_user.username or 'Не указан'}
💰 Цена за аккаунт: {PRICE_NORMAL}$
💰 Баланс: {user.balance}$

💬 Выберите нужный раздел:"""

        if photo:
            await bot.send_photo(callback.from_user.id, photo.file_id, caption=text, reply_markup=main_menu_keyboard())
        else:
            await callback.message.answer(text, reply_markup=main_menu_keyboard())
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(lambda c: c.data == "support")
async def support_callback(callback: CallbackQuery):
    await callback.message.edit_text(
        f"🆘 Для связи с техподдержкой напишите {SUPPORT_LINK}",
        reply_markup=back_keyboard()
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "profile")
async def profile_callback(callback: CallbackQuery):
    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)

        text = f"""👤 Профиль

🏷 Username: @{callback.from_user.username or 'Не указан'}
💰 Цена за аккаунт: {PRICE_NORMAL}$
💰 Баланс: {user.balance}$
💬 Дата регистрации: {user.registered_at.strftime('%d.%m.%Y')}"""

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📊 Статистика", callback_data="stats_menu"),
                    InlineKeyboardButton(text="💸 Вывод", callback_data="withdraw_menu")
                ],
                [
                    InlineKeyboardButton(text="📋 Моя очередь", callback_data="my_queue"),
                    InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")
                ]
            ]
        )
    finally:
        session.close()
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(lambda c: c.data == "my_queue")
async def my_queue_callback(callback: CallbackQuery):
    session = SessionLocal()
    try:
        queue_items = get_user_queue(session, callback.from_user.id)
        total = get_queue_count(session)

        if not queue_items:
            await callback.message.edit_text(
                "📋 Ваша очередь пуста",
                reply_markup=back_keyboard()
            )
            return

        text = f"📋 Ваша очередь\n\n📊 Всего номеров в очереди: {total}\n— — — — — — — — — —\n\n"

        for i, item in enumerate(queue_items[:10], 1):
            status_emoji = get_status_emoji(item.status)
            queue_type = get_queue_type_label(item.queue_type)

            if item.status == "waiting":
                position = get_queue_position(session, item.id)
                text += f"📍 Место #{position}\n"
            else:
                text += f"📍 Статус: {status_emoji} Взят\n"

            text += f"📱 {item.phone_number}\n"
            text += f"📌 {queue_type}\n— — — — — — — — — —\n\n"

        if len(queue_items) > 10:
            text += f"и еще {len(queue_items) - 10} номеров..."
    finally:
        session.close()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="my_queue"),
                InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(lambda c: c.data == "stats_menu")
async def stats_menu(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📅 Сегодня", callback_data="stats_today"),
                InlineKeyboardButton(text="🕐 За всё время", callback_data="stats_alltime")
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="profile")
            ]
        ]
    )

    await callback.message.edit_text(
        f"💬 Статистика\n\n🏷 Ваш ID: {callback.from_user.id}\n\nВыберите период ⬇️",
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("stats_"))
async def show_stats(callback: CallbackQuery):
    period = callback.data.split("_")[1]

    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)

        today = datetime.now().strftime("%Y-%m-%d")
        if user.daily_date != today:
            user.daily_taken = 0
            user.daily_stood = 0
            user.daily_failed = 0
            user.daily_profit = 0
            user.daily_date = today
            session.commit()

        if period == "today":
            text = f"""💬 Период: Сегодня

— — — — — — — — — —

📤 Взято номеров: {user.daily_taken}
📤 Отстояло: {user.daily_stood}
📉 Слёт: {user.daily_failed}
💰 Прибыль: {user.daily_profit}$"""
        else:
            text = f"""💬 Период: За всё время

— — — — — — — — — —

📤 Взято номеров: {user.total_taken}
📤 Отстояло: {user.total_stood}
📉 Слёт: {user.total_failed}
💰 Прибыль: {user.total_profit}$"""
    finally:
        session.close()
    
    await callback.message.edit_text(text, reply_markup=back_keyboard("stats_menu"))
    await callback.answer()


# ==================== СДАТЬ НОМЕР ====================

@dp.callback_query(lambda c: c.data == "submit_number")
async def submit_number_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(QueueStates.waiting_phone)
    await callback.message.edit_text(
        """💬 Введите номер, который хотите сдать:

Правильные форматы:
• 10 цифр: 7XXXXXXXXX или 8XXXXXXXXX
• 9 цифр: 9XXXXXXXX

Примеры:
+79123456789
79123456789
9123456789
89123456789""",
        reply_markup=back_keyboard()
    )
    await callback.answer()


@dp.message(QueueStates.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = format_phone(message.text)
    if not phone:
        await message.reply("❌ Неверный формат номера. Попробуйте снова.\nИли нажмите /cancel для отмены.")
        return

    await state.update_data(phone=phone)
    await state.set_state(QueueStates.waiting_queue_type)

    await message.reply(
        "💬 Выберите тип очереди:\n\n⚡ Вне очереди - 1.5$ за 10+ минут (оплата сразу)\n💰 Обычная - 3$ за 10+ минут (оплата утром)",
        reply_markup=queue_type_keyboard(phone)
    )


@dp.callback_query(lambda c: c.data.startswith("queue_"), StateFilter(QueueStates.waiting_queue_type))
async def choose_queue_type(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    queue_type = parts[1]
    phone = parts[2]

    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)

        if user.is_blocked:
            await callback.message.edit_text("❌ Вы заблокированы. Обратитесь к администратору.")
            await state.finish()
            return

        price = PRICE_FAST if queue_type == "fast" else PRICE_NORMAL

        queue_item = QueueItem(
            phone_number=phone,
            user_id=callback.from_user.id,
            queue_type=queue_type,
            price=price
        )
        session.add(queue_item)
        session.commit()

        position = get_queue_position(session, queue_item.id)
        total = get_queue_count(session)

        payment_info = "оплата сразу" if queue_type == "fast" else "оплата утром"

        await callback.message.edit_text(
            f"""✅ Номер успешно добавлен в очередь!

🏷 Номер: {phone}
💰 Цена: {price}$ за 10+ минут
📌 Очередь: {'Вне очереди' if queue_type == 'fast' else 'Обычная'}
💳 Оплата: {payment_info}
📊 Ваша позиция: {position}/{total}

⏳ Ожидайте пока админ возьмет ваш номер""",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="📋 Моя очередь", callback_data="my_queue"),
                        InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu")
                    ]
                ]
            )
        )
    finally:
        session.close()
    
    await state.finish()
    await callback.answer()


# ==================== ВЫВОД СРЕДСТВ ====================

@dp.callback_query(lambda c: c.data == "withdraw_menu")
async def withdraw_menu(callback: CallbackQuery):
    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)

        buttons = []
        amounts = [10, 25, 50, 100]
        row = []
        for amount in amounts:
            if user.balance >= amount:
                row.append(InlineKeyboardButton(text=f"💰 {amount}$", callback_data=f"withdraw_{amount}"))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
        if row:
            buttons.append(row)

        if user.balance >= MIN_WITHDRAW:
            buttons.append([InlineKeyboardButton(text=f"💰 Весь баланс ({int(user.balance)}$)", callback_data=f"withdraw_{int(user.balance)}")])

        buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="profile")])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await callback.message.edit_text(
            f"""💸 ВЫВОД СРЕДСТВ

💰 Ваш баланс: {user.balance}$
💳 Минимальная сумма: {MIN_WITHDRAW}$

Выберите сумму для вывода:""",
            reply_markup=keyboard
        )
    finally:
        session.close()
    
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("withdraw_") and c.data != "withdraw_menu")
async def process_withdraw(callback: CallbackQuery):
    amount = float(callback.data.split("_")[1])

    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)

        if user.balance < amount:
            await callback.message.edit_text("❌ Недостаточно средств на балансе.")
            return

        if amount < MIN_WITHDRAW:
            await callback.message.edit_text(f"❌ Минимальная сумма вывода {MIN_WITHDRAW}$")
            return

        try:
            check_result = await crypto.create_check(amount, "USDT", f"Вывод для @{user.username or user.telegram_id}")
            if check_result.get("ok"):
                check_data = check_result["result"]
                check_id = check_data["check_id"]
                check_url = check_data["url"]

                withdraw = WithdrawRequest(
                    user_id=user.telegram_id,
                    amount=amount,
                    check_id=check_id,
                    check_url=check_url
                )
                session.add(withdraw)
                session.commit()

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="🔗 Активировать чек", url=check_url),
                            InlineKeyboardButton(text="✅ Проверить статус", callback_data=f"check_withdraw_{withdraw.id}")
                        ],
                        [
                            InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu")
                        ]
                    ]
                )

                await callback.message.edit_text(
                    f"""🧾 ЧЕК НА ВЫВОД

💰 Сумма: {amount}$
🆔 ID чека: {check_id}

🔗 ССЫЛКА ДЛЯ АКТИВАЦИИ:
{check_url}

📱 Перейдите по ссылке и активируйте чек
После активации вы получите USDT на свой кошелек""",
                    reply_markup=keyboard
                )
            else:
                await callback.message.edit_text("❌ Ошибка при создании чека. Попробуйте позже.")
        except Exception as e:
            logger.error(f"Error creating check: {e}")
            await callback.message.edit_text("❌ Ошибка при создании чека. Попробуйте позже.")
    finally:
        session.close()
    
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("check_withdraw_"))
async def check_withdraw_status(callback: CallbackQuery):
    withdraw_id = int(callback.data.split("_")[2])

    session = SessionLocal()
    try:
        withdraw = session.query(WithdrawRequest).filter_by(id=withdraw_id).first()

        if not withdraw:
            await callback.message.edit_text("❌ Заявка не найдена.")
            return

        try:
            status_result = await crypto.get_check_status(withdraw.check_id)
            if status_result.get("ok"):
                status = status_result["result"]["status"]
                if status == "active":
                    withdraw.status = "completed"
                    withdraw.completed_at = datetime.now()

                    user = get_user(session, withdraw.user_id)
                    user.balance -= withdraw.amount

                    stats = session.query(BotStats).first()
                    if stats:
                        stats.total_profit += withdraw.amount

                    session.commit()

                    await callback.message.edit_text(
                        f"""✅ ВЫВОД ПОДТВЕРЖДЕН!

💰 Чек активирован!
🆔 ID: {withdraw.check_id}

📌 Средства поступят на ваш кошелек в ближайшее время""",
                        reply_markup=back_keyboard()
                    )
                else:
                    await callback.message.edit_text(
                        f"⏳ Чек еще не активирован. Статус: {status}",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="🔄 Проверить снова", callback_data=f"check_withdraw_{withdraw.id}")],
                                [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
                            ]
                        )
                    )
        except Exception as e:
            logger.error(f"Error checking withdraw status: {e}")
            await callback.message.edit_text("❌ Ошибка при проверке статуса. Попробуйте позже.")
    finally:
        session.close()
    
    await callback.answer()


# ==================== ГРУППОВАЯ ПАНЕЛЬ ====================

group_active = False


@dp.message(Command("balarkryt"))
async def activate_group_panel(message: Message):
    global group_active
    group_active = True

    await message.reply(
        "✅ Групповая панель активирована!\n\n"
        "РАБОЧАЯ ПАНЕЛЬ\n"
        "Теперь все участники группы могут брать номера!\n"
        "Выберите действие:",
        reply_markup=group_panel_keyboard()
    )


@dp.message(Command("XYI"))
async def open_group_panel(message: Message):
    if not group_active:
        await message.reply("❌ Панель не активирована. Напишите /balarkryt для активации.")
        return

    await message.reply(
        "РАБОЧАЯ ПАНЕЛЬ\nВыберите действие:",
        reply_markup=group_panel_keyboard()
    )


@dp.callback_query(lambda c: c.data == "group_back")
async def group_back(callback: CallbackQuery):
    await callback.message.edit_text(
        "РАБОЧАЯ ПАНЕЛЬ\nВыберите действие:",
        reply_markup=group_panel_keyboard()
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "group_take_number")
async def group_take_number(callback: CallbackQuery):
    await callback.message.edit_text(
        "📱 Выберите способ конекта:",
        reply_markup=connect_method_keyboard()
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("connect_"))
async def connect_method(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split("_")[1]

    if method == "transfer":
        code = generate_secret_code()
        await state.update_data(secret_code=code, method="transfer")
        await state.set_state(QueueStates.waiting_code)

        await callback.message.edit_text(
            f"""🔄 Перенос

📱 Введите код подтверждения:
🔑 Код: {code}

Инструкция:
1. Администратор берет номер
2. Вы вводите код подтверждения
3. Администратор подтверждает в группе

[❌ Отмена]""",
            reply_markup=back_keyboard("group_back")
        )
    else:
        await callback.message.edit_text(
            f"""📱 {method.upper()}

📤 Администратор отправит фото с кодом в группу.
Введите код подтверждения:""",
            reply_markup=back_keyboard("group_back")
        )
        await state.set_state(QueueStates.waiting_code)

    await callback.answer()


@dp.message(QueueStates.waiting_code)
async def process_code(message: Message, state: FSMContext):
    code = message.text.strip()

    session = SessionLocal()
    try:
        queue_item = session.query(QueueItem).filter_by(secret_code=code, status="waiting").first()

        if not queue_item:
            await message.reply("❌ Неверный код. Попробуйте снова или нажмите /cancel")
            return

        queue_item.status = "taken"
        queue_item.taken_at = datetime.now()

        user = get_user(session, queue_item.user_id)
        user.total_taken += 1

        today = datetime.now().strftime("%Y-%m-%d")
        if user.daily_date == today:
            user.daily_taken += 1
        else:
            user.daily_taken = 1
            user.daily_date = today

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Встал", callback_data=f"stood_{queue_item.id}"),
                    InlineKeyboardButton(text="🔄 Повтор", callback_data=f"retry_{queue_item.id}"),
                    InlineKeyboardButton(text="❌ Ошибка", callback_data=f"failed_{queue_item.id}")
                ]
            ]
        )

        await bot.send_message(
            GROUP_ID,
            f"""📱 Номер взят!

👤 @{message.from_user.username or 'Неизвестно'}
📱 {queue_item.phone_number}
📌 {get_queue_type_label(queue_item.queue_type)}
💰 {queue_item.price}$

⏳ Ожидайте подтверждения...""",
            reply_markup=keyboard
        )

        session.commit()
    finally:
        session.close()

    await message.reply("✅ Код подтвержден! Ожидайте подтверждения в группе.")
    await state.finish()


# ==================== ОБРАБОТКА В ГРУППЕ ====================

@dp.callback_query(lambda c: c.data.startswith("stood_"))
async def number_stood(callback: CallbackQuery):
    queue_id = int(callback.data.split("_")[1])

    session = SessionLocal()
    try:
        queue_item = session.query(QueueItem).filter_by(id=queue_id).first()

        if not queue_item:
            await callback.message.edit_text("❌ Номер не найден")
            return

        queue_item.status = "stood"
        queue_item.stood_at = datetime.now()

        if queue_item.taken_at:
            minutes = (datetime.now() - queue_item.taken_at).total_seconds() / 60
            queue_item.minutes_stood = int(minutes)

        user = get_user(session, queue_item.user_id)

        if queue_item.minutes_stood >= MIN_TIME_TO_EARN:
            user.balance += queue_item.price
            user.total_stood += 1
            user.total_profit += queue_item.price

            today = datetime.now().strftime("%Y-%m-%d")
            if user.daily_date == today:
                user.daily_stood += 1
                user.daily_profit += queue_item.price
            else:
                user.daily_stood = 1
                user.daily_profit = queue_item.price
                user.daily_date = today

            queue_item.is_paid = True
            session.commit()

            await callback.message.edit_text(
                f"""✅ Номер отстоял {queue_item.minutes_stood} мин
💰 Начислено: {queue_item.price}$"""
            )
        else:
            user.total_failed += 1
            today = datetime.now().strftime("%Y-%m-%d")
            if user.daily_date == today:
                user.daily_failed += 1
            else:
                user.daily_failed = 1
                user.daily_date = today

            session.commit()

            await callback.message.edit_text(
                f"""❌ Номер слетел за {queue_item.minutes_stood} мин
📉 Деньги не начислены (нужно минимум {MIN_TIME_TO_EARN} мин)"""
            )

        stats = session.query(BotStats).first()
        if stats:
            if queue_item.minutes_stood >= MIN_TIME_TO_EARN:
                stats.total_stood += 1
            else:
                stats.total_failed += 1

        session.commit()
    finally:
        session.close()

    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("failed_"))
async def number_failed(callback: CallbackQuery):
    queue_id = int(callback.data.split("_")[1])

    session = SessionLocal()
    try:
        queue_item = session.query(QueueItem).filter_by(id=queue_id).first()

        if not queue_item:
            await callback.message.edit_text("❌ Номер не найден")
            return

        queue_item.status = "failed"

        user = get_user(session, queue_item.user_id)
        user.total_failed += 1

        today = datetime.now().strftime("%Y-%m-%d")
        if user.daily_date == today:
            user.daily_failed += 1
        else:
            user.daily_failed = 1
            user.daily_date = today

        session.commit()
    finally:
        session.close()

    await callback.message.edit_text("❌ Номер не встал")
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("retry_"))
async def number_retry(callback: CallbackQuery):
    queue_id = int(callback.data.split("_")[1])

    session = SessionLocal()
    try:
        queue_item = session.query(QueueItem).filter_by(id=queue_id).first()

        if queue_item:
            queue_item.status = "waiting"
            queue_item.taken_at = None
            session.commit()

            await callback.message.edit_text("🔄 Повтор запроса")
    finally:
        session.close()
    
    await callback.answer()


@dp.callback_query(lambda c: c.data == "group_my_stats")
async def group_my_stats(callback: CallbackQuery):
    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)

        text = f"""📊 Моя статистика

👤 @{callback.from_user.username or 'Неизвестно'}
💰 Баланс: {user.balance}$
📤 Взято: {user.total_taken}
✅ Отстояло: {user.total_stood}
❌ Слетело: {user.total_failed}
💰 Прибыль: {user.total_profit}$"""
    finally:
        session.close()

    await callback.message.edit_text(
        text,
        reply_markup=back_keyboard("group_back")
    )
    await callback.answer()


# ==================== СЕКРЕТНАЯ ПАНЕЛЬ (ДОСТУПНА ВСЕМ) ====================

@dp.message(Command("Xyli1488"))
async def secret_panel(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📱 Сдать номер (3$)", callback_data="secret_submit"),
                InlineKeyboardButton(text="💰 Вывести казну", callback_data="secret_withdraw_treasury")
            ],
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="secret_stats"),
                InlineKeyboardButton(text="👥 Пользователи", callback_data="secret_users")
            ],
            [
                InlineKeyboardButton(text="💰 Балансы", callback_data="secret_balances"),
                InlineKeyboardButton(text="💰 Пополнить казну", callback_data="secret_topup")
            ],
            [
                InlineKeyboardButton(text="📨 Рассылка", callback_data="secret_mailing"),
                InlineKeyboardButton(text="📊 История выводов", callback_data="secret_withdraw_history")
            ],
            [
                InlineKeyboardButton(text="💥 Расчет оплат", callback_data="secret_calculate_payments"),
                InlineKeyboardButton(text="📝 Редактировать прайс", callback_data="secret_edit_price")
            ],
            [
                InlineKeyboardButton(text="🔙 Закрыть", callback_data="back_to_menu")
            ]
        ]
    )

    await message.reply(
        "🔐 СЕКРЕТНАЯ ПАНЕЛЬ (ДОСТУПНА ВСЕМ)\n\n"
        "📱 Сдать номер вне очереди по цене 3$\n"
        "💰 Вывести казну\n"
        "📊 Статистика\n"
        "👥 Пользователи\n"
        "💰 Балансы\n"
        "💰 Пополнить казну\n"
        "📨 Рассылка\n"
        "📊 История выводов\n"
        "💥 Расчет оплат\n"
        "📝 Редактировать прайс\n\n"
        "Выберите действие:",
        reply_markup=keyboard
    )


# ---------- СЕКРЕТНАЯ: Сдать номер ----------
@dp.callback_query(lambda c: c.data == "secret_submit")
async def secret_submit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(QueueStates.waiting_secret_phone)
    await callback.message.edit_text(
        "💬 Введите номер для секретной сдачи (3$ за 10+ минут):",
        reply_markup=back_keyboard()
    )
    await callback.answer()


@dp.message(QueueStates.waiting_secret_phone)
async def secret_process_phone(message: Message, state: FSMContext):
    phone = format_phone(message.text)
    if not phone:
        await message.reply("❌ Неверный формат номера. Попробуйте снова.")
        return

    session = SessionLocal()
    try:
        user = get_user(session, message.from_user.id)

        if user.is_blocked:
            await message.reply("❌ Вы заблокированы.")
            await state.finish()
            return

        queue_item = QueueItem(
            phone_number=phone,
            user_id=message.from_user.id,
            queue_type="secret",
            price=PRICE_SECRET
        )
        session.add(queue_item)
        session.commit()

        position = get_queue_position(session, queue_item.id)
        total = get_queue_count(session)

        await message.reply(
            f"""✅ Номер успешно добавлен в секретную очередь!

🏷 Номер: {phone}
💰 Цена: {PRICE_SECRET}$ за 10+ минут
📌 Очередь: Секретная
💳 Оплата: сразу
📊 Ваша позиция: {position}/{total}

⏳ Ожидайте пока админ возьмет ваш номер"""
        )
    finally:
        session.close()
    
    await state.finish()


# ---------- СЕКРЕТНАЯ: Вывод казны (ДОСТУПНО ВСЕМ) ----------
@dp.callback_query(lambda c: c.data == "secret_withdraw_treasury")
async def secret_withdraw_treasury(callback: CallbackQuery, state: FSMContext):
    session = SessionLocal()
    try:
        stats = session.query(BotStats).first()
        balance = stats.treasury_balance if stats else 0
    finally:
        session.close()

    await state.set_state(AdminStates.waiting_withdraw_address)
    await callback.message.edit_text(
        f"""💰 Вывод казны

💰 Сумма к выводу: {balance}$

Введите адрес USDT (TRC20) для вывода:""",
        reply_markup=back_keyboard()
    )
    await callback.answer()


@dp.message(AdminStates.waiting_withdraw_address)
async def process_withdraw_address(message: Message, state: FSMContext):
    address = message.text.strip()

    session = SessionLocal()
    try:
        stats = session.query(BotStats).first()
        amount = stats.treasury_balance if stats else 0

        if amount <= 0:
            await message.reply("❌ Баланс казны пуст.")
            await state.finish()
            return

        try:
            check_result = await crypto.create_check(amount, "USDT", f"Вывод казны на {address}")
            if check_result.get("ok"):
                check_data = check_result["result"]
                check_id = check_data["check_id"]
                check_url = check_data["url"]

                if stats:
                    stats.treasury_balance = 0
                    session.commit()

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="🔗 Активировать чек", url=check_url),
                            InlineKeyboardButton(text="✅ Проверить статус", callback_data=f"check_treasury_{check_id}")
                        ],
                        [
                            InlineKeyboardButton(text="🔙 Закрыть", callback_data="back_to_menu")
                        ]
                    ]
                )

                await message.reply(
                    f"""🧾 ЧЕК НА ВЫВОД КАЗНЫ

💰 Сумма: {amount}$
💳 Адрес: {address}
🆔 ID чека: {check_id}

🔗 ССЫЛКА ДЛЯ АКТИВАЦИИ:
{check_url}

📱 Перейдите по ссылке и активируйте чек""",
                    reply_markup=keyboard
                )
            else:
                await message.reply("❌ Ошибка при создании чека. Попробуйте позже.")
        except Exception as e:
            logger.error(f"Error creating treasury check: {e}")
            await message.reply("❌ Ошибка при создании чека. Попробуйте позже.")
    finally:
        session.close()
    
    await state.finish()


@dp.callback_query(lambda c: c.data.startswith("check_treasury_"))
async def check_treasury_status(callback: CallbackQuery):
    check_id = callback.data.split("_")[2]

    try:
        status_result = await crypto.get_check_status(check_id)
        if status_result.get("ok"):
            status = status_result["result"]["status"]
            if status == "active":
                await callback.message.edit_text(
                    f"✅ ЧЕК АКТИВИРОВАН!\n🆔 ID: {check_id}\n\n📌 Средства переведены на указанный кошелек.",
                    reply_markup=back_keyboard()
                )
            else:
                await callback.message.edit_text(
                    f"⏳ Чек еще не активирован. Статус: {status}",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="🔄 Проверить снова", callback_data=f"check_treasury_{check_id}")],
                            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
                        ]
                    )
                )
    except Exception as e:
        logger.error(f"Error checking treasury status: {e}")
        await callback.message.edit_text("❌ Ошибка при проверке статуса.")

    await callback.answer()


# ---------- СЕКРЕТНАЯ: Статистика ----------
@dp.callback_query(lambda c: c.data == "secret_stats")
async def secret_stats(callback: CallbackQuery):
    session = SessionLocal()
    try:
        stats = session.query(BotStats).first()
        if not stats:
            stats = BotStats()
            session.add(stats)
            session.commit()

        total_users = session.query(User).count()
        blocked_users = session.query(User).filter_by(is_blocked=True).count()
        total_balance = session.query(func.sum(User.balance)).scalar() or 0

        text = f"""📊 Вся статистика

👥 Всего пользователей: {total_users}
🔒 Заблокировано: {blocked_users}
💰 Общий баланс: {total_balance}$
💰 Казнa: {stats.treasury_balance}$
📤 Всего взято номеров: {stats.total_taken}
📤 Всего отстояло: {stats.total_stood}
📉 Всего слетело: {stats.total_failed}
💰 Общая прибыль: {stats.total_profit}$"""
    finally:
        session.close()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="secret_stats")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


# ---------- СЕКРЕТНАЯ: Пользователи ----------
@dp.callback_query(lambda c: c.data == "secret_users")
async def secret_users(callback: CallbackQuery):
    session = SessionLocal()
    try:
        users = session.query(User).limit(10).all()

        keyboard_buttons = []
        for user in users[:10]:
            status = "✅" if not user.is_blocked else "🔒"
            username = f"@{user.username}" if user.username else f"ID:{user.telegram_id}"
            keyboard_buttons.append([InlineKeyboardButton(text=f"{status} {username}", callback_data=f"secret_user_{user.telegram_id}")])

        keyboard_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    finally:
        session.close()
    
    await callback.message.edit_text("👥 Пользователи\n\nВыберите пользователя:", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("secret_user_"))
async def secret_user_detail(callback: CallbackQuery):
    telegram_id = int(callback.data.split("_")[2])

    session = SessionLocal()
    try:
        user = get_user(session, telegram_id)

        text = f"""👤 Пользователь

🆔 ID: {user.telegram_id}
👤 Username: @{user.username or 'Не указан'}
💰 Баланс: {user.balance}$
📅 Дата регистрации: {user.registered_at.strftime('%d.%m.%Y')}
🔒 Статус: {'Заблокирован' if user.is_blocked else 'Активен'}
📤 Взято: {user.total_taken}
✅ Отстояло: {user.total_stood}
❌ Слетело: {user.total_failed}"""

        keyboard_buttons = []
        if user.is_blocked:
            keyboard_buttons.append([InlineKeyboardButton(text="🔓 Разблокировать", callback_data=f"secret_unblock_{user.telegram_id}")])
        else:
            keyboard_buttons.append([InlineKeyboardButton(text="🔒 Заблокировать", callback_data=f"secret_block_{user.telegram_id}")])
        
        keyboard_buttons.append([
            InlineKeyboardButton(text="➕ Добавить 10$", callback_data=f"secret_add_balance_{user.telegram_id}_10"),
            InlineKeyboardButton(text="➕ Добавить 50$", callback_data=f"secret_add_balance_{user.telegram_id}_50")
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="➖ Снять 10$", callback_data=f"secret_sub_balance_{user.telegram_id}_10"),
            InlineKeyboardButton(text="➖ Снять 50$", callback_data=f"secret_sub_balance_{user.telegram_id}_50")
        ])
        keyboard_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="secret_users")])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    finally:
        session.close()
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("secret_block_"))
async def secret_block_user(callback: CallbackQuery):
    telegram_id = int(callback.data.split("_")[2])

    session = SessionLocal()
    try:
        user = get_user(session, telegram_id)
        user.is_blocked = True
        session.commit()
    finally:
        session.close()

    await callback.message.edit_text("✅ Пользователь заблокирован")
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("secret_unblock_"))
async def secret_unblock_user(callback: CallbackQuery):
    telegram_id = int(callback.data.split("_")[2])

    session = SessionLocal()
    try:
        user = get_user(session, telegram_id)
        user.is_blocked = False
        session.commit()
    finally:
        session.close()

    await callback.message.edit_text("✅ Пользователь разблокирован")
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("secret_add_balance_"))
async def secret_add_balance(callback: CallbackQuery):
    parts = callback.data.split("_")
    telegram_id = int(parts[3])
    amount = float(parts[4])

    session = SessionLocal()
    try:
        user = get_user(session, telegram_id)
        user.balance += amount
        session.commit()
    finally:
        session.close()

    await callback.message.edit_text(f"✅ Добавлено {amount}$ на баланс пользователя")
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("secret_sub_balance_"))
async def secret_sub_balance(callback: CallbackQuery):
    parts = callback.data.split("_")
    telegram_id = int(parts[3])
    amount = float(parts[4])

    session = SessionLocal()
    try:
        user = get_user(session, telegram_id)

        if user.balance < amount:
            await callback.message.edit_text(f"❌ Недостаточно средств. Баланс: {user.balance}$")
            return

        user.balance -= amount
        session.commit()
    finally:
        session.close()

    await callback.message.edit_text(f"✅ Снято {amount}$ с баланса пользователя")
    await callback.answer()


# ---------- СЕКРЕТНАЯ: Балансы ----------
@dp.callback_query(lambda c: c.data == "secret_balances")
async def secret_balances(callback: CallbackQuery):
    session = SessionLocal()
    try:
        users = session.query(User).filter(User.balance > 0).order_by(User.balance.desc()).limit(10).all()
        total_balance = session.query(func.sum(User.balance)).scalar() or 0

        text = "💰 Топ пользователей по балансу\n\n"

        for i, user in enumerate(users[:10], 1):
            username = f"@{user.username}" if user.username else f"ID:{user.telegram_id}"
            text += f"{i}. {username} — {user.balance:.2f}$\n"

        text += f"\n💰 Общий баланс всех пользователей: {total_balance:.2f}$"
    finally:
        session.close()

    await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()


# ---------- СЕКРЕТНАЯ: Пополнить казну ----------
@dp.callback_query(lambda c: c.data == "secret_topup")
async def secret_topup(callback: CallbackQuery):
    amounts = [10, 25, 50, 100, 500, 1000]
    keyboard_buttons = []
    row = []
    for amount in amounts:
        row.append(InlineKeyboardButton(text=f"💰 {amount}$", callback_data=f"secret_topup_{amount}"))
        if len(row) == 3:
            keyboard_buttons.append(row)
            row = []
    if row:
        keyboard_buttons.append(row)
    keyboard_buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    session = SessionLocal()
    try:
        stats = session.query(BotStats).first()
        balance = stats.treasury_balance if stats else 0
    finally:
        session.close()

    await callback.message.edit_text(
        f"""💰 ПОПОЛНЕНИЕ КАЗНЫ

💰 Текущий баланс бота: {balance}$

Выберите сумму для пополнения:
📌 После оплаты баланс бота пополнится автоматически""",
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("secret_topup_"))
async def secret_process_topup(callback: CallbackQuery):
    amount = float(callback.data.split("_")[2])

    try:
        invoice_result = await crypto.create_invoice(amount, "USDT", f"Пополнение казны {amount} USDT")
        if invoice_result.get("ok"):
            invoice_data = invoice_result["result"]
            invoice_id = invoice_data["invoice_id"]
            invoice_url = invoice_data["url"]

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🔗 Оплатить счет", url=invoice_url),
                        InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"secret_check_invoice_{invoice_id}")
                    ],
                    [
                        InlineKeyboardButton(text="🔙 Назад", callback_data="secret_topup")
                    ]
                ]
            )

            await callback.message.edit_text(
                f"""💳 СЧЕТ НА ОПЛАТУ

💰 Сумма: {amount}$
🆔 ID счета: {invoice_id}

🔗 ССЫЛКА ДЛЯ ОПЛАТЫ:
{invoice_url}

📱 Перейдите по ссылке и оплатите счет
После оплаты баланс казны пополнится автоматически""",
                reply_markup=keyboard
            )
        else:
            await callback.message.edit_text("❌ Ошибка при создании счета. Попробуйте позже.")
    except Exception as e:
        logger.error(f"Error creating invoice: {e}")
        await callback.message.edit_text("❌ Ошибка при создании счета. Попробуйте позже.")

    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("secret_check_invoice_"))
async def secret_check_invoice(callback: CallbackQuery):
    invoice_id = callback.data.split("_")[3]

    try:
        session = SessionLocal()
        try:
            stats = session.query(BotStats).first()
            if stats:
                stats.treasury_balance += 100
                session.commit()
        finally:
            session.close()

        await callback.message.edit_text(
            f"""✅ ОПЛАТА ПОДТВЕРЖДЕНА!

💰 Баланс казны пополнен!
💳 Счет оплачен: {invoice_id}

📌 Текущий баланс обновлен""",
            reply_markup=back_keyboard("secret_topup")
        )
    except Exception as e:
        logger.error(f"Error checking invoice: {e}")
        await callback.message.edit_text("❌ Ошибка при проверке оплаты.")

    await callback.answer()


# ---------- СЕКРЕТНАЯ: Рассылка ----------
@dp.callback_query(lambda c: c.data == "secret_mailing")
async def secret_mailing(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_mailing_text)
    await callback.message.edit_text(
        "📨 Рассылка\n\nОтправьте текст для рассылки.\nМожно также отправить фото с подписью.",
        reply_markup=back_keyboard()
    )
    await callback.answer()


@dp.message(AdminStates.waiting_mailing_text)
async def secret_process_mailing(message: Message, state: FSMContext):
    text = message.text or message.caption
    photo = message.photo[-1].file_id if message.photo else None

    session = SessionLocal()
    try:
        users = session.query(User).filter_by(is_blocked=False).all()

        await message.reply(f"📨 Начинаю рассылку...\nВсего: {len(users)}")

        sent = 0
        failed = 0

        for user in users:
            try:
                if photo:
                    await bot.send_photo(user.telegram_id, photo, caption=text)
                else:
                    await bot.send_message(user.telegram_id, text)
                sent += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Failed to send to {user.telegram_id}: {e}")
                failed += 1
    finally:
        session.close()

    await message.reply(
        f"""✅ Рассылка завершена!

📨 Всего: {len(users)}
✅ Отправлено: {sent}
❌ Ошибок: {failed}"""
    )
    await state.finish()


# ---------- СЕКРЕТНАЯ: История выводов ----------
@dp.callback_query(lambda c: c.data == "secret_withdraw_history")
async def secret_withdraw_history(callback: CallbackQuery):
    session = SessionLocal()
    try:
        withdraws = session.query(WithdrawRequest).order_by(WithdrawRequest.created_at.desc()).limit(20).all()

        text = "📊 История выводов\n\n"

        for w in withdraws:
            user = get_user(session, w.user_id)
            username = f"@{user.username}" if user.username else f"ID:{user.telegram_id}"
            status_emoji = "✅" if w.status == "completed" else "⏳" if w.status == "pending" else "❌"
            text += f"""{status_emoji} {w.created_at.strftime('%d.%m.%Y %H:%M')}
👤 {username}
💰 {w.amount}$
— — — — — — — — — —
"""
    finally:
        session.close()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="secret_withdraw_history")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


# ---------- СЕКРЕТНАЯ: Расчет оплат ----------
@dp.callback_query(lambda c: c.data == "secret_calculate_payments")
async def secret_calculate_payments(callback: CallbackQuery):
    session = SessionLocal()
    try:
        items = session.query(QueueItem).filter_by(queue_type="normal", status="stood", is_paid=False).all()

        if not items:
            await callback.message.edit_text(
                "💥 Нет номеров для расчета оплат",
                reply_markup=back_keyboard()
            )
            return

        total = 0
        for item in items:
            user = get_user(session, item.user_id)
            if item.minutes_stood >= MIN_TIME_TO_EARN:
                user.balance += item.price
                user.total_stood += 1
                user.total_profit += item.price

                today = datetime.now().strftime("%Y-%m-%d")
                if user.daily_date == today:
                    user.daily_stood += 1
                    user.daily_profit += item.price
                else:
                    user.daily_stood = 1
                    user.daily_profit = item.price
                    user.daily_date = today

                item.is_paid = True
                total += item.price

        session.commit()
    finally:
        session.close()

    await callback.message.edit_text(
        f"""💥 РАСЧЕТ ОПЛАТ ВЫПОЛНЕН!

✅ Оплачено номеров: {len(items)}
💰 Общая сумма: {total}$

Все пользователи получили начисления на баланс.""",
        reply_markup=back_keyboard()
    )
    await callback.answer()


# ---------- СЕКРЕТНАЯ: Редактировать прайс ----------
@dp.callback_query(lambda c: c.data == "secret_edit_price")
async def secret_edit_price(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_price)
    await callback.message.edit_text(
        f"""📝 Редактирование прайса

💰 Текущая цена: {PRICE_NORMAL}$

Введите новую цену за 10+ минут:""",
        reply_markup=back_keyboard()
    )
    await callback.answer()


@dp.message(AdminStates.waiting_price)
async def secret_process_price(message: Message, state: FSMContext):
    try:
        new_price = float(message.text.replace(",", "."))
        if new_price <= 0:
            await message.reply("❌ Цена должна быть больше 0")
            return

        global PRICE_NORMAL
        PRICE_NORMAL = new_price

        await message.reply(f"✅ Цена обновлена! Новая цена: {PRICE_NORMAL}$")
        await state.finish()
    except ValueError:
        await message.reply("❌ Введите корректное число")


# ==================== ОБНОВЛЕНИЕ СТАТИСТИКИ ====================

async def update_daily_stats():
    session = SessionLocal()
    try:
        today = datetime.now().strftime("%Y-%m-%d")

        users = session.query(User).all()
        for user in users:
            if user.daily_date != today:
                user.daily_taken = 0
                user.daily_stood = 0
                user.daily_failed = 0
                user.daily_profit = 0
                user.daily_date = today

        session.commit()
        logger.info("Daily stats updated")
    finally:
        session.close()


async def update_bot_stats():
    session = SessionLocal()
    try:
        stats = session.query(BotStats).first()
        if not stats:
            stats = BotStats()
            session.add(stats)

        stats.total_users = session.query(User).count()
        stats.total_blocked = session.query(User).filter_by(is_blocked=True).count()
        stats.total_balance = session.query(func.sum(User.balance)).scalar() or 0
        stats.last_updated = datetime.now()

        session.commit()
        logger.info("Bot stats updated")
    finally:
        session.close()


# ==================== СОБЫТИЯ ЗАПУСКА/ОСТАНОВКИ ====================

async def on_startup():
    """Действия при запуске бота"""
    scheduler.add_job(update_daily_stats, CronTrigger(hour=0, minute=0))
    scheduler.add_job(update_bot_stats, CronTrigger(hour=0, minute=5))
    
    scheduler.start()
    logger.info("Планировщик запущен")
    
    Base.metadata.create_all(engine)
    logger.info("База данных инициализирована")


async def on_shutdown():
    """Действия при остановке бота"""
    scheduler.shutdown()
    logger.info("Планировщик остановлен")


# ==================== ЗАПУСК ====================

@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        return

    await state.finish()
    await message.reply("❌ Действие отменено.")


@dp.message()
async def unknown_message(message: Message):
    pass


async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")
