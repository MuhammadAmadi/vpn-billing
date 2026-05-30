"""Единая миграция БД.

Запуск:  python migrate.py

Объединяет всё что раньше было в init_db.py + admin_migrate.py + bot_content_migrate.py:
  • Базовые таблицы (users, devices, transactions)
  • Таблицы админ-панели (servers, bypass_ips)
  • Контент бота (bot_messages, bot_buttons)
  • Индексы
  • Однократный перенос серверов и bypass-адресов из .env

Скрипт идемпотентен: можно запускать многократно, ничего не сломается.
"""

import asyncio
import logging

import db
import config
import bot_content as bc

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("migrate")


SCHEMA = [
    # ── Пользователи ──
    """
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
    """,
    # ── Устройства ──
    """
    CREATE TABLE IF NOT EXISTS devices (
        id               VARCHAR(36) PRIMARY KEY,
        short_id         VARCHAR(10) NOT NULL DEFAULT '',
        user_id          BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
        name             VARCHAR(255),
        os               VARCHAR(50),
        key_string       TEXT,
        links_updated_at TIMESTAMP,
        created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── История операций ──
    """
    CREATE TABLE IF NOT EXISTS transactions (
        id          SERIAL PRIMARY KEY,
        user_id     BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
        type        VARCHAR(50),
        title       VARCHAR(255),
        description TEXT,
        amount      VARCHAR(50),
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── VPN-серверы (управляются админкой) ──
    """
    CREATE TABLE IF NOT EXISTS servers (
        id          SERIAL PRIMARY KEY,
        name        VARCHAR(255) NOT NULL,
        scheme      VARCHAR(10)  NOT NULL DEFAULT 'https',
        host        VARCHAR(255) NOT NULL,
        port        INTEGER      NOT NULL DEFAULT 2053,
        base_path   VARCHAR(255) NOT NULL DEFAULT '',
        login       VARCHAR(255) NOT NULL DEFAULT 'admin',
        password    VARCHAR(255) NOT NULL DEFAULT 'admin',
        api_token   VARCHAR(255) NOT NULL DEFAULT '',
        client_host VARCHAR(255) NOT NULL DEFAULT '',
        is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
        sort_order  INTEGER      NOT NULL DEFAULT 0,
        created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── Bypass IP / домены (deprecated — заменено на domain_pool, но таблица оставлена для бэкап-чтения) ──
    """
    CREATE TABLE IF NOT EXISTS bypass_ips (
        id         SERIAL PRIMARY KEY,
        value      VARCHAR(255) NOT NULL,
        label      VARCHAR(255),
        is_active  BOOLEAN      NOT NULL DEFAULT TRUE,
        sort_order INTEGER      NOT NULL DEFAULT 0,
        created_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── Пул доменов (host + опциональный port + label) ──
    # Из этого пула админ назначает host'ы конкретным inbound'ам сервера.
    """
    CREATE TABLE IF NOT EXISTS domain_pool (
        id         SERIAL PRIMARY KEY,
        host       VARCHAR(255) NOT NULL,
        port       INTEGER,
        label      VARCHAR(255),
        is_active  BOOLEAN      NOT NULL DEFAULT TRUE,
        sort_order INTEGER      NOT NULL DEFAULT 0,
        created_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── Per-inbound override host'а ──
    # На каждый inbound каждого сервера можно назначить домен из domain_pool.
    # Без override используется server.client_host или server.host (как сейчас).
    """
    CREATE TABLE IF NOT EXISTS inbound_host_overrides (
        server_id      INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
        inbound_id     INTEGER NOT NULL,
        inbound_remark VARCHAR(255),
        inbound_port   INTEGER,
        domain_id      INTEGER REFERENCES domain_pool(id) ON DELETE SET NULL,
        updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (server_id, inbound_id)
    );
    """,
    # ── Контент бота: сообщения ──
    """
    CREATE TABLE IF NOT EXISTS bot_messages (
        key          VARCHAR(50) PRIMARY KEY,
        title        VARCHAR(255),
        text         TEXT,
        placeholders VARCHAR(255)
    );
    """,
    # ── Контент бота: кнопки ──
    """
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
    """,
]

# ALTER-ы для случая, когда таблицы уже были созданы старыми версиями.
ALTERS = [
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS short_id VARCHAR(10) NOT NULL DEFAULT '';",
    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS links_updated_at TIMESTAMP;",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(50);",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_given BOOLEAN DEFAULT FALSE;",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_bonus_given BOOLEAN DEFAULT FALSE;",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE;",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_inactive BOOLEAN NOT NULL DEFAULT FALSE;",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS inactive_since TIMESTAMP;",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS broadcast_failures INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE servers ADD COLUMN IF NOT EXISTS api_token VARCHAR(255) NOT NULL DEFAULT '';",
    "ALTER TABLE servers ADD COLUMN IF NOT EXISTS client_host VARCHAR(255) NOT NULL DEFAULT '';",
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_users_magic_token ON users(magic_token);",
    "CREATE INDEX IF NOT EXISTS idx_users_is_deleted ON users(is_deleted) WHERE is_deleted = FALSE;",
    "CREATE INDEX IF NOT EXISTS idx_users_is_inactive ON users(is_inactive) WHERE is_inactive = TRUE;",
    "CREATE INDEX IF NOT EXISTS idx_devices_user_id ON devices(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_transactions_type_created ON transactions(type, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_servers_sort ON servers(sort_order, id);",
    "CREATE INDEX IF NOT EXISTS idx_domain_pool_sort ON domain_pool(sort_order, id);",
    "CREATE INDEX IF NOT EXISTS idx_overrides_server ON inbound_host_overrides(server_id);",
    "CREATE INDEX IF NOT EXISTS idx_bot_buttons_menu ON bot_buttons(menu, row, position);",
]


async def _create_schema(conn) -> None:
    for stmt in SCHEMA:
        await conn.execute(stmt)
    for stmt in ALTERS:
        await conn.execute(stmt)
    for stmt in INDEXES:
        await conn.execute(stmt)
    log.info("Схема и индексы созданы")


async def _seed_servers(conn) -> None:
    count = await conn.fetchval("SELECT COUNT(*) FROM servers")
    if count or not config.SERVERS:
        log.info("Серверы: пропуск (в БД %s, в .env %s)", count, len(config.SERVERS))
        return
    log.info("Переношу %s сервер(ов) из .env в БД", len(config.SERVERS))
    for i, s in enumerate(config.SERVERS):
        await conn.execute(
            """INSERT INTO servers (name, scheme, host, port, base_path, login, password, is_active, sort_order)
               VALUES ($1,$2,$3,$4,$5,$6,$7,TRUE,$8)""",
            s["name"], s["scheme"], s["host"], s["port"],
            s["base_path"], s["login"], s["password"], i,
        )


async def _seed_domain_pool(conn) -> None:
    """Однократная миграция: переносим записи из старой bypass_ips в новую
    domain_pool, если последняя ещё пустая. Чтобы админ не терял свои домены."""
    count = await conn.fetchval("SELECT COUNT(*) FROM domain_pool")
    if count:
        log.info("domain_pool: пропуск (в БД %s записей)", count)
        return
    legacy = await conn.fetch(
        "SELECT value, label, is_active, sort_order FROM bypass_ips ORDER BY sort_order, id"
    )
    if not legacy:
        log.info("domain_pool: исходных bypass_ips нет, начинаем с пустой таблицы")
        return
    log.info("Переношу %s запис(ей) из bypass_ips в domain_pool", len(legacy))
    for r in legacy:
        await conn.execute(
            """INSERT INTO domain_pool (host, port, label, is_active, sort_order)
               VALUES ($1, NULL, $2, $3, $4)""",
            r["value"], r["label"] or "", r["is_active"], r["sort_order"],
        )


async def _seed_bot_content(conn) -> None:
    for key, d in bc.DEFAULT_MESSAGES.items():
        await conn.execute(
            """INSERT INTO bot_messages (key, title, text, placeholders) VALUES ($1,$2,$3,$4)
               ON CONFLICT (key) DO NOTHING""",
            key, d["title"], d["text"], d["placeholders"],
        )
    log.info("Сообщения бота: загружено %s ключ(ей)", len(bc.DEFAULT_MESSAGES))

    cnt = await conn.fetchval("SELECT COUNT(*) FROM bot_buttons")
    if cnt:
        log.info("Кнопки бота: пропуск (в БД уже %s)", cnt)
        return
    for menu, action, text, row, pos in bc.DEFAULT_BUTTONS:
        await conn.execute(
            """INSERT INTO bot_buttons (menu, action, text, kind, row, position, enabled)
               VALUES ($1,$2,$3,'action',$4,$5,TRUE)""",
            menu, action, text, row, pos,
        )
    log.info("Кнопки бота: загружено %s", len(bc.DEFAULT_BUTTONS))


async def main() -> None:
    log.info("Подключение к %s@%s/%s", config.DB_USER, config.DB_HOST, config.DB_NAME)
    conn = await db.connect()
    try:
        await _create_schema(conn)
        await _seed_servers(conn)
        await _seed_domain_pool(conn)
        await _seed_bot_content(conn)
        log.info("✅ Миграция завершена успешно")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
