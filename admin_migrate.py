# admin_migrate.py — СОЗДАЁТ ТАБЛИЦЫ ДЛЯ АДМИН-ПАНЕЛИ
#
# Что делает:
#   1. Создаёт таблицу `servers`    — VPN-серверы (теперь в БД, а не в .env)
#   2. Создаёт таблицу `bypass_ips` — адреса обхода для inbound'ов с "| BYPASS"
#   3. Один раз переносит серверы и bypass-адреса из .env, если таблицы пустые.
#   (Журнал ошибок хранится в файле logs/errors.jsonl, таблица в БД не нужна.)
#
# ЗАПУСК:  python admin_migrate.py
# Можно запускать несколько раз — повторный запуск ничего не сломает.

import asyncio
import asyncpg
import config


async def migrate():
    print("🔄 Подключение к базе данных...")
    conn = await asyncpg.connect(
        user=config.DB_USER,
        password=config.DB_PASS,
        database=config.DB_NAME,
        host=config.DB_HOST,
    )

    print("🏗  Таблица 'servers' (VPN-серверы)...")
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS servers (
            id          SERIAL PRIMARY KEY,
            name        VARCHAR(255) NOT NULL,
            scheme      VARCHAR(10)  NOT NULL DEFAULT 'https',
            host        VARCHAR(255) NOT NULL,
            port        INTEGER      NOT NULL DEFAULT 2053,
            base_path   VARCHAR(255) NOT NULL DEFAULT '',
            login       VARCHAR(255) NOT NULL DEFAULT 'admin',
            password    VARCHAR(255) NOT NULL DEFAULT 'admin',
            is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
            sort_order  INTEGER      NOT NULL DEFAULT 0,
            created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    # Журнал ошибок теперь в файле (logs/errors.jsonl), таблица в БД не нужна.
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_servers_sort ON servers(sort_order, id);"
    )

    print("🏗  Таблица 'bypass_ips' (адреса обхода)...")
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS bypass_ips (
            id         SERIAL PRIMARY KEY,
            value      VARCHAR(255) NOT NULL,   -- IP или домен
            label      VARCHAR(255),
            is_active  BOOLEAN      NOT NULL DEFAULT TRUE,
            sort_order INTEGER      NOT NULL DEFAULT 0,
            created_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    # --- Разовый перенос серверов из .env в БД ---
    count = await conn.fetchval("SELECT COUNT(*) FROM servers")
    if count == 0 and getattr(config, "SERVERS", None):
        print(f"📦 Таблица серверов пустая — переношу {len(config.SERVERS)} сервер(ов) из .env...")
        for i, s in enumerate(config.SERVERS):
            await conn.execute(
                '''INSERT INTO servers (name, scheme, host, port, base_path, login, password, is_active, sort_order)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,TRUE,$8)''',
                s.get("name", f"Server{i+1}"),
                s.get("scheme", "https"),
                s.get("host", ""),
                int(s.get("port", 2053)),
                s.get("base_path", ""),
                s.get("login", "admin"),
                s.get("password", "admin"),
                i,
            )
            print(f"   ➕ {s.get('name')}")
        print("   ✅ Серверы перенесены. Теперь управляй ими через админ-панель.")
    else:
        print(f"ℹ️  В таблице servers уже есть {count} запис(ей) — перенос пропущен.")

    # --- Разовый перенос bypass-адресов из .env ---
    bcount = await conn.fetchval("SELECT COUNT(*) FROM bypass_ips")
    if bcount == 0 and getattr(config, "BYPASS_IPS", None):
        print(f"📦 Переношу {len(config.BYPASS_IPS)} bypass-адрес(ов) из .env...")
        for i, val in enumerate(config.BYPASS_IPS):
            if val and val.strip():
                await conn.execute(
                    "INSERT INTO bypass_ips (value, label, is_active, sort_order) VALUES ($1,'',TRUE,$2)",
                    val.strip(), i,
                )
                print(f"   ➕ {val.strip()}")
    else:
        print(f"ℹ️  В таблице bypass_ips уже есть {bcount} запис(ей) — перенос пропущен.")

    print("\n✅ Миграция завершена.")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
