# server_store.py — ИСТОЧНИК СПИСКА VPN-СЕРВЕРОВ
#
# Раньше серверы брались только из .env (config.SERVERS).
# Теперь они хранятся в таблице `servers` и управляются через админ-панель.
#
# get_active_servers() — возвращает активные серверы в ТОМ ЖЕ формате, что и
# раньше config.SERVERS (список словарей с ключами name/scheme/host/port/...),
# поэтому xray_api работает без изменений в логике.
#
# ВАЖНО: get_active_servers() никогда не падает. Если БД недоступна или таблицы
# ещё нет — он откатывается на config.SERVERS из .env, чтобы сайт продолжал жить.

import asyncpg
import config


async def _connect():
    return await asyncpg.connect(
        user=config.DB_USER, password=config.DB_PASS,
        database=config.DB_NAME, host=config.DB_HOST,
    )


def _row_to_server(r):
    return {
        "id":        r["id"],
        "name":      r["name"],
        "scheme":    r["scheme"],
        "host":      r["host"],
        "port":      int(r["port"]),
        "base_path": r["base_path"] or "",
        "login":     r["login"],
        "password":  r["password"],
        "is_active": r["is_active"],
    }


async def get_active_servers(conn=None):
    """Только ВКЛЮЧЁННЫЕ серверы — для xray_api. Безопасно при любой ошибке."""
    own = False
    try:
        if conn is None:
            conn = await _connect()
            own = True
        rows = await conn.fetch(
            "SELECT * FROM servers WHERE is_active = TRUE ORDER BY sort_order, id"
        )
        if rows:
            return [_row_to_server(r) for r in rows]
        # Таблица есть, но пустая — откат на .env
        return list(getattr(config, "SERVERS", []))
    except Exception as e:
        print(f"⚠️ [server_store] не смог прочитать серверы из БД, использую .env: {e}")
        return list(getattr(config, "SERVERS", []))
    finally:
        if own and conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def list_servers(conn):
    """ВСЕ серверы (включая выключенные) — для админ-панели."""
    rows = await conn.fetch("SELECT * FROM servers ORDER BY sort_order, id")
    return [_row_to_server(r) for r in rows]


async def get_server(conn, server_id):
    r = await conn.fetchrow("SELECT * FROM servers WHERE id = $1", int(server_id))
    return _row_to_server(r) if r else None


async def add_server(conn, data):
    next_order = await conn.fetchval("SELECT COALESCE(MAX(sort_order)+1, 0) FROM servers")
    sid = await conn.fetchval(
        '''INSERT INTO servers (name, scheme, host, port, base_path, login, password, is_active, sort_order)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id''',
        data["name"], data.get("scheme", "https"), data["host"], int(data.get("port", 2053)),
        data.get("base_path", ""), data.get("login", "admin"), data.get("password", "admin"),
        bool(data.get("is_active", True)), next_order,
    )
    return sid


async def update_server(conn, server_id, data):
    await conn.execute(
        '''UPDATE servers SET name=$1, scheme=$2, host=$3, port=$4, base_path=$5,
                              login=$6, password=$7, is_active=$8 WHERE id=$9''',
        data["name"], data.get("scheme", "https"), data["host"], int(data.get("port", 2053)),
        data.get("base_path", ""), data.get("login", "admin"), data.get("password", "admin"),
        bool(data.get("is_active", True)), int(server_id),
    )


async def delete_server(conn, server_id):
    await conn.execute("DELETE FROM servers WHERE id = $1", int(server_id))


async def toggle_server(conn, server_id):
    await conn.execute(
        "UPDATE servers SET is_active = NOT is_active WHERE id = $1", int(server_id)
    )
    return await conn.fetchval("SELECT is_active FROM servers WHERE id = $1", int(server_id))
