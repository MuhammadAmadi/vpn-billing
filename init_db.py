# init_db.py — ЕДИНСТВЕННЫЙ файл для создания таблиц
#
# Что изменено:
#   1. Убраны дублирующие db.py — теперь один источник правды
#   2. Добавлена колонка short_id в таблицу devices (она использовалась в web.py, но не была в схеме!)
#   3. Скрипт НЕ удаляет существующие таблицы (убран DROP TABLE)
#      Если запустить повторно — просто ничего не сломается (IF NOT EXISTS)
#   4. Добавлены индексы — ускоряют поиск по magic_token и user_id
#
# КАК ЗАПУСКАТЬ: python init_db.py
# Можно запускать несколько раз — безопасно.

import asyncio
import asyncpg
import config  # Читает настройки из .env


async def create_tables():
    print("🔄 Подключение к базе данных...")
    conn = await asyncpg.connect(
        user=config.DB_USER,
        password=config.DB_PASS,
        database=config.DB_NAME,
        host=config.DB_HOST
    )

    print("🏗  Создание таблицы 'users' (Пользователи)...")
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id           BIGINT PRIMARY KEY,
            username          VARCHAR(255),
            balance           DECIMAL(10, 2) DEFAULT 0.00,
            days_left         INT DEFAULT 0,
            invited_by        BIGINT,
            magic_token       VARCHAR(100) UNIQUE,
            phone             VARCHAR(50),
            bonus_given       BOOLEAN DEFAULT FALSE,
            phone_bonus_given BOOLEAN DEFAULT FALSE,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    print("🏗  Создание таблицы 'devices' (Устройства)...")
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            id               VARCHAR(36)  PRIMARY KEY,     -- Длинный UUID для Xray (36 символов)
            short_id         VARCHAR(10)  NOT NULL DEFAULT '', -- Короткий ID для отображения (например "A3F1")
            user_id          BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            name             VARCHAR(255),
            os               VARCHAR(50),
            key_string       TEXT,
            links_updated_at TIMESTAMP,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    # Добавляем short_id если таблица уже существует без этой колонки
    # (на случай если база уже была создана старым скриптом)
    try:
        await conn.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS short_id VARCHAR(10) NOT NULL DEFAULT '';")
        print("   ✅ Колонка short_id проверена/добавлена в devices")
    except Exception as e:
        print(f"   ℹ️  short_id: {e}")
    
    # Накатываем недостающие поля, если таблицы уже существовали
    try:
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(50);")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_given BOOLEAN DEFAULT FALSE;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_bonus_given BOOLEAN DEFAULT FALSE;")
        await conn.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS links_updated_at TIMESTAMP;")
        print("   ✅ Колонки для бонусов и обновлений линков успешно проверены/добавлены!")
    except Exception as e:
        print(f"   ⚠️ Ошибка при добавлении колонок миграции: {e}")

    print("🏗  Создание таблицы 'transactions' (История операций)...")
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            type        VARCHAR(50),    -- 'income' (пополнение), 'expense' (списание), 'action' (действие)
            title       VARCHAR(255),
            description TEXT,
            amount      VARCHAR(50),    -- Текст: "+50₽" или "-3.3₽" (для гибкости отображения)
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    print("📇 Создание индексов для ускорения поиска...")
    # Индекс на magic_token — каждый запрос к кабинету ищет по этому полю
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_magic_token ON users(magic_token);"
    )
    # Индекс на user_id в devices — часто запрашиваем все устройства пользователя
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_devices_user_id ON devices(user_id);"
    )
    # Индекс на user_id в transactions
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id);"
    )

    print("\n✅ Все таблицы и индексы успешно созданы! База данных готова к работе.")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(create_tables())
