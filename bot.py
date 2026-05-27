# bot.py — ВЕРСИЯ С РЕДАКТИРУЕМЫМ КОНТЕНТОМ
#
# Что изменилось по сравнению со старой версией:
#   • Тексты сообщений и кнопки берутся из БД (bot_content) и редактируются в
#     админ-панели. Если БД недоступна — используются дефолты (бот не падает).
#   • Кнопки маршрутизируются по «действию» (action), а не по тексту, поэтому их
#     можно переименовывать/выключать/двигать, и обработчик всё равно сработает.
#   • Вся логика бонусов, рефералов, телефона СОХРАНЕНА без изменений.
#
# Перед заменой сделай бэкап старого файла:  cp bot.py bot.py.bak
# И один раз накати контент:  python bot_content_migrate.py

import asyncio
import logging
import asyncpg
import uuid
import config
import os

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

import bot_content as bc

# ================= КОНСТАНТЫ =================
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

BONUS_SUBSCRIBE  = 25.00
BONUS_PHONE      = 95.00
BONUS_PHONE_SUB  = 5.00


# ================= ПУЛ БД =================
db_pool: asyncpg.Pool = None

async def get_pool() -> asyncpg.Pool:
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(
            user=config.DB_USER, password=config.DB_PASS,
            database=config.DB_NAME, host=config.DB_HOST,
            min_size=2, max_size=10,
        )
    return db_pool


# ================= КОНТЕНТ (тексты/кнопки из БД) =================
def render(text: str, **ctx) -> str:
    text = (text or "").replace("{channel_url}", config.CHANNEL_URL) \
                       .replace("{support_url}", config.SUPPORT_URL)
    for k, v in ctx.items():
        text = text.replace("{" + k + "}", str(v))
    return text

async def msg(key: str, **ctx) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        text = await bc.get_message(key, conn)
    return render(text, **ctx)

def build_reply_keyboard(rows):
    kb = [[KeyboardButton(text=b["text"]) for b in row] for row in rows]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

async def menu_kb(menu: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await bc.get_menu_rows(menu, conn)
    return build_reply_keyboard(rows)


# ── Спец-клавиатуры (не редактируются — транзакционные/inline) ──
def inline_cabinet_kb(url: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖥 Перейти в Личный кабинет", url=url)]
    ])

def subscribe_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=config.CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Я подписался!", callback_data="check_sub_bonus")]
    ])

def phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
            [KeyboardButton(text="🏠 Главное меню")]
        ],
        resize_keyboard=True, one_time_keyboard=True
    )


# ================= ВСПОМОГАТЕЛЬНЫЕ =================
async def check_channel_subscription(bot: Bot, user_id: int) -> bool:
    if CHANNEL_ID == 0:
        print("⚠️ CHANNEL_ID не настроен в .env!")
        return False
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status not in ("left", "kicked", "restricted")
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        print(f"⚠️ Не могу проверить подписку: {e}")
        return False

async def ensure_user(conn, user_id: int, username: str):
    await conn.execute(
        '''INSERT INTO users (user_id, username) VALUES ($1, $2)
           ON CONFLICT (user_id) DO NOTHING''',
        user_id, username
    )

async def add_balance(conn, user_id: int, amount: float, title: str, description: str):
    await conn.execute(
        'UPDATE users SET balance = balance + $1 WHERE user_id = $2', amount, user_id
    )
    await conn.execute(
        '''INSERT INTO transactions (user_id, type, title, description, amount)
           VALUES ($1, 'income', $2, $3, $4)''',
        user_id, title, description, f"+{amount:.0f}₽"
    )


# ================= ДИСПЕТЧЕР =================
dp = Dispatcher()


# ---- /start ----
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    pool = await get_pool()
    async with pool.acquire() as conn:
        args = message.text.split()
        invited_by = None
        if len(args) > 1:
            try:
                invited_by = int(args[1])
                if invited_by == message.from_user.id:
                    invited_by = None
            except ValueError:
                pass
        await conn.execute(
            '''INSERT INTO users (user_id, username, invited_by) VALUES ($1, $2, $3)
               ON CONFLICT (user_id) DO NOTHING''',
            message.from_user.id,
            message.from_user.username or message.from_user.first_name,
            invited_by
        )
    text = await msg("welcome", full_name=message.from_user.full_name)
    await message.answer(text, reply_markup=await menu_kb("main"),
                         parse_mode="HTML", disable_web_page_preview=True)


# ---- callback: проверка подписки ----
@dp.callback_query(F.data == "check_sub_bonus")
async def callback_check_sub_bonus(callback: types.CallbackQuery, bot: Bot):
    is_subscribed = await check_channel_subscription(bot, callback.from_user.id)
    if not is_subscribed:
        await callback.answer("❌ Вы ещё не подписались на канал!", show_alert=True)
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        already = await conn.fetchval(
            'SELECT bonus_given FROM users WHERE user_id = $1', callback.from_user.id)
        if already:
            await callback.message.edit_text("❌ Вы уже получили бонус за подписку.\n\n")
            return
        await add_balance(conn, callback.from_user.id, BONUS_SUBSCRIBE,
                          "Бонус за подписку", "7 дней за подписку на канал")
        await conn.execute('UPDATE users SET bonus_given = TRUE WHERE user_id = $1',
                           callback.from_user.id)
    await callback.message.edit_text(
        f"🎉 <b>Бонус начислен!</b>\n\n✅ Подписка подтверждена\n"
        f"💰 Начислено: <b>{BONUS_SUBSCRIBE:.0f}₽ (7 дней)</b>\n\nПриятного использования!",
        parse_mode="HTML")


# ---- контакт (бонус за телефон) ----
@dp.message(F.contact)
async def handle_contact(message: types.Message, bot: Bot):
    contact = message.contact
    if contact.user_id != message.from_user.id:
        await message.answer("❌ Пожалуйста, поделитесь своим номером, а не чужим.",
                             reply_markup=phone_kb())
        return
    phone = contact.phone_number.replace("+", "").replace(" ", "").strip()
    pool = await get_pool()
    sub_bonus_already = False
    async with pool.acquire() as conn:
        await ensure_user(conn, message.from_user.id,
                          message.from_user.username or message.from_user.first_name)
        already_user = await conn.fetchval(
            'SELECT phone_bonus_given FROM users WHERE user_id = $1', message.from_user.id)
        if already_user:
            await message.answer("❌ Вы уже получили бонус за номер телефона.",
                                 reply_markup=await menu_kb("main"))
            return
        phone_exists = await conn.fetchval('SELECT user_id FROM users WHERE phone = $1', phone)
        if phone_exists and phone_exists != message.from_user.id:
            await message.answer(
                "❌ Этот номер телефона уже использовался для получения бонуса.\n\n"
                "Каждый номер можно использовать только один раз.",
                reply_markup=await menu_kb("main"))
            return
        await conn.execute('UPDATE users SET phone = $1 WHERE user_id = $2',
                           phone, message.from_user.id)
        total = BONUS_PHONE
        await add_balance(conn, message.from_user.id, BONUS_PHONE,
                          "Бонус за телефон", "30 дней за номер телефона")
        sub_bonus_already = await conn.fetchval(
            'SELECT bonus_given FROM users WHERE user_id = $1', message.from_user.id)
        is_subscribed = await check_channel_subscription(bot, message.from_user.id)
        if is_subscribed and not sub_bonus_already:
            total += BONUS_PHONE_SUB
            await add_balance(conn, message.from_user.id, BONUS_PHONE_SUB,
                              "Бонус за подписку", "+1 день за подписку на канал")
            await conn.execute('UPDATE users SET bonus_given = TRUE WHERE user_id = $1',
                               message.from_user.id)
        await conn.execute('UPDATE users SET phone_bonus_given = TRUE WHERE user_id = $1',
                           message.from_user.id)
    if is_subscribed and not sub_bonus_already:
        detail = (f"📱 95₽ — за номер телефона\n📢 +5₽ — за подписку на канал\n"
                  f"💰 <b>Итого: {total:.0f}₽ (30 дней)!</b>")
    else:
        detail = (f"📱 95₽ — за номер телефона\n💰 <b>Итого: {total:.0f}₽</b>\n\n"
                  f"💡 Подпишитесь на канал и нажмите <b>«🎁 Бонус 1 день»</b> — получите ещё +5₽!")
    await message.answer(f"🎉 <b>Бонус начислен!</b>\n\n{detail}",
                         parse_mode="HTML", reply_markup=await menu_kb("main"))


# ================= ДЕЙСТВИЯ КНОПОК =================
async def act_cabinet(message: types.Message, bot: Bot):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_user(conn, message.from_user.id,
                          message.from_user.username or message.from_user.first_name)
        token = await conn.fetchval('SELECT magic_token FROM users WHERE user_id = $1',
                                    message.from_user.id)
        if not token:
            token = uuid.uuid4().hex
            await conn.execute('UPDATE users SET magic_token = $1 WHERE user_id = $2',
                               token, message.from_user.id)
    personal_url = f"{config.CABINET_BASE_URL}/cabinet/{token}"
    await message.answer(await msg("cabinet_caption"),
                         reply_markup=inline_cabinet_kb(personal_url), parse_mode="HTML")
    await message.answer(await msg("cabinet_return"), reply_markup=await menu_kb("main"))


async def act_bonus_subscribe(message: types.Message, bot: Bot):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_user(conn, message.from_user.id,
                          message.from_user.username or message.from_user.first_name)
        already = await conn.fetchval('SELECT bonus_given FROM users WHERE user_id = $1',
                                      message.from_user.id)
        if already:
            await message.answer("❌ Вы уже получили бонус за подписку на канал.\n\n",
                                 parse_mode="HTML", reply_markup=await menu_kb("main"))
            return
    is_subscribed = await check_channel_subscription(bot, message.from_user.id)
    if not is_subscribed:
        await message.answer(
            "📢 Для получения бонуса нужно подписаться на канал!\n\n"
            "После подписки нажми <b>«Я подписался!»</b>",
            reply_markup=subscribe_kb(), parse_mode="HTML")
        return
    async with pool.acquire() as conn:
        await add_balance(conn, message.from_user.id, BONUS_SUBSCRIBE,
                          "Бонус за подписку", "1 день за подписку на канал")
        await conn.execute('UPDATE users SET bonus_given = TRUE WHERE user_id = $1',
                           message.from_user.id)
    await message.answer(
        f"🎉 <b>Бонус начислен!</b>\n\n✅ Подписка подтверждена\n"
        f"💰 Начислено: <b>{BONUS_SUBSCRIBE:.0f}₽ (7 дней)</b>\n\nПриятного использования!",
        parse_mode="HTML", reply_markup=await menu_kb("main"))


async def act_bonus_phone(message: types.Message, bot: Bot):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_user(conn, message.from_user.id,
                          message.from_user.username or message.from_user.first_name)
        already = await conn.fetchval('SELECT phone_bonus_given FROM users WHERE user_id = $1',
                                      message.from_user.id)
        if already:
            await message.answer(
                "❌ Вы уже получили бонус за номер телефона.\n\n"
                "Если не получили бонус за подписку — нажмите <b>«🎁 Бонус 1 день»</b>!",
                parse_mode="HTML", reply_markup=await menu_kb("main"))
            return
    await message.answer(
        f"🏆 <b>Бонус за номер телефона</b>\n\n"
        f"Поделитесь номером и получите:\n📱 <b>95₽</b> — за номер телефона\n"
        f"📢 <b>+5₽</b> — если подписаны на канал\n💰 <b>Итого до 100₽ (30 дней)!</b>\n\n"
        f"Нажмите кнопку ниже — Telegram сам отправит ваш номер.\n"
        f"<i>Номер используется только для защиты от повторных регистраций.</i>",
        parse_mode="HTML", reply_markup=phone_kb())


async def act_rules(message: types.Message, bot: Bot):
    await message.answer(await msg("rules"), parse_mode="HTML", disable_web_page_preview=True,
                         reply_markup=await menu_kb("main"))

async def act_help(message: types.Message, bot: Bot):
    await message.answer(await msg("help_intro"), reply_markup=await menu_kb("help"))

async def act_main_menu(message: types.Message, bot: Bot):
    await message.answer(await msg("main_menu"), reply_markup=await menu_kb("main"))

async def act_cabinet_broken(message: types.Message, bot: Bot):
    await message.answer(await msg("cabinet_broken"),
                         reply_markup=inline_cabinet_kb(config.FALLBACK_CABINET_URL))

async def act_vpn_broken(message: types.Message, bot: Bot):
    await message.answer(await msg("vpn_broken"), parse_mode="HTML",
                         reply_markup=await menu_kb("help"))

async def act_about(message: types.Message, bot: Bot):
    await message.answer(await msg("about"), reply_markup=await menu_kb("about"),
                         parse_mode="HTML", disable_web_page_preview=True)

async def act_not_found(message: types.Message, bot: Bot):
    await message.answer(await msg("not_found"), reply_markup=await menu_kb("not_found"))

async def act_support(message: types.Message, bot: Bot):
    await message.answer(await msg("support"), disable_web_page_preview=True,
                         reply_markup=await menu_kb("help"))


ACTION_HANDLERS = {
    "cabinet": act_cabinet,
    "bonus_subscribe": act_bonus_subscribe,
    "bonus_phone": act_bonus_phone,
    "rules": act_rules,
    "help": act_help,
    "main_menu": act_main_menu,
    "cabinet_broken": act_cabinet_broken,
    "vpn_broken": act_vpn_broken,
    "about": act_about,
    "not_found": act_not_found,
    "support": act_support,
}


# ---- УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК ТЕКСТА (по кнопкам из БД) ----
# Должен идти ПОСЛЕДНИМ, чтобы не перехватывать /start, контакты и т.п.
@dp.message(F.text)
async def on_text(message: types.Message, bot: Bot):
    pool = await get_pool()
    async with pool.acquire() as conn:
        res = await bc.resolve_text(message.text, conn)
    if not res:
        return  # неизвестный текст — игнорируем (как и раньше)
    if res.get("kind") == "message" and res.get("msg_key"):
        text = await msg(res["msg_key"], full_name=message.from_user.full_name)
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True,
                             reply_markup=await menu_kb("main"))
        return
    handler = ACTION_HANDLERS.get(res.get("action"))
    if handler:
        await handler(message, bot)


# ================= ЗАПУСК =================
async def main():
    await get_pool()
    proxy = os.getenv("PROXY_URL")
    if proxy:
        from aiogram.client.session.aiohttp import AiohttpSession
        session = AiohttpSession(proxy=proxy)
        bot = Bot(token=config.BOT_TOKEN, session=session)
        print(f"🔀 Используем прокси: {proxy}")
    else:
        bot = Bot(token=config.BOT_TOKEN)
        print("🔗 Прямое подключение к Telegram")
    print("=" * 40)
    print("🚀 БОТ SihaVPN ЗАПУЩЕН")
    print("=" * 40)
    try:
        await dp.start_polling(bot)
    finally:
        if db_pool:
            await db_pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен")
