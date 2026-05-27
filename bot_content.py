# bot_content.py — ТЕКСТЫ И КНОПКИ БОТА (редактируются из админ-панели)
#
# Тексты сообщений и раскладка кнопок хранятся в БД (таблицы bot_messages,
# bot_buttons) и редактируются в панели. Бот читает их при каждом сообщении.
# Если БД недоступна или запись отсутствует — берётся ДЕФОЛТ (бот не ломается).
#
# Кнопки маршрутизируются по полю `action` (стабильный идентификатор), а не по
# тексту. Поэтому кнопку можно переименовать/выключить/подвинуть, и обработчик
# всё равно сработает. kind='action' — встроенное поведение; kind='message' —
# кнопка просто показывает текст из bot_messages[msg_key].

import asyncpg
import config

# ─── Встроенные действия (для kind='action') ───
ACTIONS = {
    "cabinet":         "Личный кабинет",
    "bonus_subscribe": "Бонус за подписку",
    "bonus_phone":     "Бонус за телефон",
    "rules":           "Правила",
    "help":            "Помощь (меню)",
    "main_menu":       "Главное меню",
    "cabinet_broken":  "Кабинет не работает",
    "vpn_broken":      "VPN не работает",
    "about":           "Об этом боте",
    "not_found":       "Не нашёл ответ (меню)",
    "support":         "Чат поддержки",
}

MENUS = ["main", "help", "not_found", "about"]

# ─── Дефолтные тексты (равны текущим в bot.py) ───
DEFAULT_MESSAGES = {
    "welcome": {
        "title": "Приветствие (/start)",
        "placeholders": "{full_name}, {channel_url}",
        "text": (
            "Добро пожаловать в <b>SihaVPN</b>, {full_name}!\n\n"
            "🚀 Высокая скорость без ограничений\n"
            "🔓 Полный доступ ко всем сайтам\n\n"
            "🎁 <b>Получи бесплатный период:</b>\n"
            "▫️ <b>1 дней</b> — подпишись на наш канал\n"
            "📣 Канал: <a href='{channel_url}'>SihaVPN_news</a>\n\n"
            "👇 Выбери действие в меню ниже:"
        ),
    },
    "rules": {
        "title": "Правила",
        "placeholders": "{channel_url}",
        "text": (
            "<b>🎁 Как получить бесплатный период</b>\n\n"
            "<b>Бонус 7 дней (25₽)</b>\n"
            "Подпишитесь на канал <a href='{channel_url}'>SihaVPN_news</a> "
            "и нажмите «🎁 Бонус 7 дней».\n\n"
            "<b>🤝 Реферальная программа</b>\n\n"
            "Приглашайте друзей и получайте бонус!\n"
            "▫️ Менее 3 дней — <i>0 дней</i>\n"
            "▫️ Менее 1 недели — <b>1 день</b>\n"
            "▫️ Менее 1 месяца — <b>3 дня</b>\n"
            "▫️ Менее 1 года — <b>7 дней</b>\n"
            "▫️ Более 1 года — <b>15 дней</b>\n\n"
            "🔥 <b>СУПЕР-БОНУС:</b> Если друг пополнит баланс более чем на 50₽ — вы получите <b>15 дней</b>!\n"
            "💸 <b>ПАССИВНЫЙ ДОХОД:</b> <b>3%</b> от каждого пополнения ваших рефералов навсегда!\n\n"
            "<i>*1 день = 3.33₽</i>"
        ),
    },
    "help_intro":     {"title": "Помощь — заголовок", "placeholders": "", "text": "Раздел помощи. Выберите вашу проблему:"},
    "main_menu":      {"title": "Возврат в меню", "placeholders": "", "text": "Вы вернулись в главное меню 👇"},
    "cabinet_caption":{"title": "Подпись к ссылке кабинета", "placeholders": "", "text": "⬇️ <b>Ваша индивидуальная ссылка:</b> ⬇️"},
    "cabinet_return": {"title": "После выдачи кабинета", "placeholders": "", "text": "Возвращаю вас в главное меню."},
    "cabinet_broken": {"title": "Кабинет не работает", "placeholders": "", "text": "Если основной сайт заблокирован, используйте резервную ссылку."},
    "vpn_broken": {
        "title": "VPN не работает",
        "placeholders": "",
        "text": (
            "Чаще всего проблема решается <b>обновлением подписки</b>!\n\n"
            "1. Зайдите в приложение HAPP (или V2Box).\n"
            "2. Нажмите 'Обновить' на профиле SihaVPN.\n"
            "3. Переподключитесь.\n\n"
            "Если не помогло — обратитесь в поддержку."
        ),
    },
    "about": {"title": "Об этом боте", "placeholders": "",
              "text": "Это информационный бот сервиса <b>SihaVPN</b>.\nВсе услуги и ключи — в Личном кабинете."},
    "not_found": {"title": "Не нашёл ответ", "placeholders": "",
                  "text": "Ничего страшного! Наша поддержка готова помочь."},
    "support": {"title": "Чат поддержки", "placeholders": "{support_url}",
                "text": "Напишите нашему специалисту: {support_url}"},
}

# ─── Дефолтная раскладка кнопок (равна текущим клавиатурам bot.py) ───
# (menu, action, text, row, position)
DEFAULT_BUTTONS = [
    ("main", "cabinet",         "👤 Личный кабинет", 0, 0),
    ("main", "bonus_subscribe", "🎁 Бонус 7 дней",   1, 0),
    ("main", "rules",           "📋 Правила",         2, 0),
    ("main", "help",            "🆘 Помощь",          2, 1),

    ("help", "cabinet",        "👤 Личный кабинет",       0, 0),
    ("help", "main_menu",      "🏠 Главное меню",          0, 1),
    ("help", "vpn_broken",     "🛡 VPN не работает?",       1, 0),
    ("help", "cabinet_broken", "🌐 Кабинет не работает?",   1, 1),
    ("help", "about",          "🤖 Об этом боте",           2, 0),
    ("help", "not_found",      "❓ Не нашел ответ",         2, 1),

    ("not_found", "cabinet",   "👤 Личный кабинет", 0, 0),
    ("not_found", "main_menu", "🏠 Главное меню",    0, 1),
    ("not_found", "support",   "💬 Чат поддержки",   1, 0),

    ("about", "cabinet",   "👤 Личный кабинет", 0, 0),
    ("about", "main_menu", "🏠 Главное меню",    0, 1),
]


async def _connect():
    return await asyncpg.connect(
        user=config.DB_USER, password=config.DB_PASS,
        database=config.DB_NAME, host=config.DB_HOST,
    )


# ──────────── СООБЩЕНИЯ ────────────
def _default_message(key):
    d = DEFAULT_MESSAGES.get(key)
    return d["text"] if d else ""


async def get_message(key, conn=None):
    """Текст сообщения по ключу (с фолбэком на дефолт). Никогда не падает."""
    own = False
    try:
        if conn is None:
            conn = await _connect(); own = True
        v = await conn.fetchval("SELECT text FROM bot_messages WHERE key = $1", key)
        return v if v is not None else _default_message(key)
    except Exception as e:
        print(f"⚠️ [bot_content] get_message('{key}'): {e}")
        return _default_message(key)
    finally:
        if own and conn is not None:
            try: await conn.close()
            except Exception: pass


async def list_messages(conn):
    rows = await conn.fetch("SELECT key, title, text, placeholders FROM bot_messages ORDER BY key")
    by_key = {r["key"]: dict(r) for r in rows}
    # дополняем недостающие дефолтами (если миграция не заполнила)
    out = []
    for key, d in DEFAULT_MESSAGES.items():
        if key in by_key:
            out.append(by_key[key])
        else:
            out.append({"key": key, "title": d["title"], "text": d["text"], "placeholders": d["placeholders"]})
    return out


async def update_message(conn, key, text):
    title = DEFAULT_MESSAGES.get(key, {}).get("title", key)
    ph = DEFAULT_MESSAGES.get(key, {}).get("placeholders", "")
    await conn.execute(
        '''INSERT INTO bot_messages (key, title, text, placeholders) VALUES ($1,$2,$3,$4)
           ON CONFLICT (key) DO UPDATE SET text = EXCLUDED.text''',
        key, title, text, ph,
    )


# ──────────── КНОПКИ ────────────
def _default_menu_rows(menu):
    btns = [b for b in DEFAULT_BUTTONS if b[0] == menu]
    return _group_rows([
        {"action": a, "text": t, "row": r, "position": p, "kind": "action", "msg_key": None}
        for (_m, a, t, r, p) in btns
    ])


def _group_rows(buttons):
    """Сортирует и группирует плоский список кнопок в строки [[btn,btn],[btn],...]."""
    buttons = sorted(buttons, key=lambda b: (b["row"], b["position"]))
    rows, cur, cur_row = [], [], None
    for b in buttons:
        if cur_row is None:
            cur_row = b["row"]
        if b["row"] != cur_row:
            rows.append(cur); cur = []; cur_row = b["row"]
        cur.append(b)
    if cur:
        rows.append(cur)
    return rows


async def get_menu_rows(menu, conn=None):
    """Строки кнопок для меню (только включённые). Фолбэк на дефолт."""
    own = False
    try:
        if conn is None:
            conn = await _connect(); own = True
        rows = await conn.fetch(
            "SELECT action, text, kind, msg_key, row, position FROM bot_buttons "
            "WHERE menu = $1 AND enabled = TRUE ORDER BY row, position",
            menu,
        )
        if rows:
            return _group_rows([dict(r) for r in rows])
        return _default_menu_rows(menu)
    except Exception as e:
        print(f"⚠️ [bot_content] get_menu_rows('{menu}'): {e}")
        return _default_menu_rows(menu)
    finally:
        if own and conn is not None:
            try: await conn.close()
            except Exception: pass


async def resolve_text(text, conn=None):
    """По тексту кнопки вернуть {action, kind, msg_key} или None. Фолбэк на дефолт."""
    own = False
    try:
        if conn is None:
            conn = await _connect(); own = True
        r = await conn.fetchrow(
            "SELECT action, kind, msg_key FROM bot_buttons WHERE text = $1 AND enabled = TRUE LIMIT 1",
            text,
        )
        if r:
            return dict(r)
        # фолбэк: ищем в дефолтах
        for (_m, a, t, _r, _p) in DEFAULT_BUTTONS:
            if t == text:
                return {"action": a, "kind": "action", "msg_key": None}
        return None
    except Exception as e:
        print(f"⚠️ [bot_content] resolve_text: {e}")
        for (_m, a, t, _r, _p) in DEFAULT_BUTTONS:
            if t == text:
                return {"action": a, "kind": "action", "msg_key": None}
        return None
    finally:
        if own and conn is not None:
            try: await conn.close()
            except Exception: pass


async def list_buttons(conn):
    rows = await conn.fetch(
        "SELECT id, menu, action, text, kind, msg_key, row, position, enabled "
        "FROM bot_buttons ORDER BY menu, row, position, id"
    )
    return [dict(r) for r in rows]


async def save_button(conn, data):
    if data.get("id"):
        await conn.execute(
            '''UPDATE bot_buttons SET menu=$1, action=$2, text=$3, kind=$4, msg_key=$5,
                                      row=$6, position=$7, enabled=$8 WHERE id=$9''',
            data["menu"], data["action"], data["text"], data.get("kind", "action"),
            data.get("msg_key") or None, int(data.get("row", 0)), int(data.get("position", 0)),
            bool(data.get("enabled", True)), int(data["id"]),
        )
    else:
        await conn.execute(
            '''INSERT INTO bot_buttons (menu, action, text, kind, msg_key, row, position, enabled)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)''',
            data["menu"], data["action"], data["text"], data.get("kind", "action"),
            data.get("msg_key") or None, int(data.get("row", 0)), int(data.get("position", 0)),
            bool(data.get("enabled", True)),
        )


async def delete_button(conn, bid):
    await conn.execute("DELETE FROM bot_buttons WHERE id = $1", int(bid))


async def toggle_button(conn, bid):
    await conn.execute("UPDATE bot_buttons SET enabled = NOT enabled WHERE id = $1", int(bid))
    return await conn.fetchval("SELECT enabled FROM bot_buttons WHERE id = $1", int(bid))
