# bot_content_migrate.py — ТАБЛИЦЫ КОНТЕНТА БОТА
#
# Создаёт bot_messages и bot_buttons и заполняет их текущими текстами/кнопками
# (из bot_content.DEFAULT_*). Запускать один раз:  python bot_content_migrate.py
# Повторный запуск безопасен (если таблицы уже заполнены — не трогает).

import asyncio
import asyncpg
import config
import bot_content as bc


async def migrate():
    print("🔄 Подключение к БД...")
    conn = await asyncpg.connect(
        user=config.DB_USER, password=config.DB_PASS,
        database=config.DB_NAME, host=config.DB_HOST,
    )

    print("🏗  Таблица 'bot_messages'...")
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS bot_messages (
            key          VARCHAR(50) PRIMARY KEY,
            title        VARCHAR(255),
            text         TEXT,
            placeholders VARCHAR(255)
        );
    ''')

    print("🏗  Таблица 'bot_buttons'...")
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS bot_buttons (
            id        SERIAL PRIMARY KEY,
            menu      VARCHAR(30)  NOT NULL,
            action    VARCHAR(50)  NOT NULL,
            text      VARCHAR(255) NOT NULL,
            kind      VARCHAR(20)  NOT NULL DEFAULT 'action',
            msg_key   VARCHAR(50),
            row       INTEGER      NOT NULL DEFAULT 0,
            position  INTEGER      NOT NULL DEFAULT 0,
            enabled   BOOLEAN      NOT NULL DEFAULT TRUE
        );
    ''')

    # Заполняем сообщения (только отсутствующие ключи)
    for key, d in bc.DEFAULT_MESSAGES.items():
        await conn.execute(
            '''INSERT INTO bot_messages (key, title, text, placeholders)
               VALUES ($1,$2,$3,$4) ON CONFLICT (key) DO NOTHING''',
            key, d["title"], d["text"], d["placeholders"],
        )
    print(f"   ✅ Сообщений в наборе: {len(bc.DEFAULT_MESSAGES)}")

    # Заполняем кнопки, только если таблица пустая
    cnt = await conn.fetchval("SELECT COUNT(*) FROM bot_buttons")
    if cnt == 0:
        for (menu, action, text, row, pos) in bc.DEFAULT_BUTTONS:
            await conn.execute(
                '''INSERT INTO bot_buttons (menu, action, text, kind, row, position, enabled)
                   VALUES ($1,$2,$3,'action',$4,$5,TRUE)''',
                menu, action, text, row, pos,
            )
        print(f"   ✅ Кнопок добавлено: {len(bc.DEFAULT_BUTTONS)}")
    else:
        print(f"ℹ️  В bot_buttons уже есть {cnt} запис(ей) — пропуск.")

    print("\n✅ Готово.")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
