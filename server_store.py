"""VPN-серверы в БД (управляются админкой) с фолбэком на .env."""

import logging

import config
import db

log = logging.getLogger(__name__)


def _row_to_server(r) -> dict:
    return {
        "id":        r["id"],
        "name":      r["name"],
        "scheme":    r["scheme"],
        "host":      r["host"],
        "port":      int(r["port"]),
        "base_path": r["base_path"] or "",
        "login":     r["login"],
        "password":  r["password"],
        "api_token": r.get("api_token", ""),
        "is_active": r["is_active"],
    }


async def get_active_servers() -> list[dict]:
    """Только включённые серверы — для xray_api. Безопасно при любой ошибке."""
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM servers WHERE is_active = TRUE ORDER BY sort_order, id"
            )
        if rows:
            return [_row_to_server(r) for r in rows]
        return list(config.SERVERS)
    except Exception as e:
        log.warning("Не смог прочитать серверы из БД, использую .env: %s", e)
        return list(config.SERVERS)


async def list_servers(conn) -> list[dict]:
    rows = await conn.fetch("SELECT * FROM servers ORDER BY sort_order, id")
    return [_row_to_server(r) for r in rows]


async def get_server(conn, server_id) -> dict | None:
    r = await conn.fetchrow("SELECT * FROM servers WHERE id = $1", int(server_id))
    return _row_to_server(r) if r else None


async def add_server(conn, data: dict) -> int:
    next_order = await conn.fetchval("SELECT COALESCE(MAX(sort_order)+1, 0) FROM servers")
    return await conn.fetchval(
        """INSERT INTO servers (name, scheme, host, port, base_path, login, password, api_token, is_active, sort_order)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id""",
        data["name"], data.get("scheme", "https"), data["host"], int(data.get("port", 2053)),
        data.get("base_path", ""), data.get("login", "admin"), data.get("password", "admin"),
        data.get("api_token", ""), bool(data.get("is_active", True)), next_order,
    )


async def update_server(conn, server_id, data: dict) -> None:
    await conn.execute(
        """UPDATE servers SET name=$1, scheme=$2, host=$3, port=$4, base_path=$5, login=$6, password=$7,
               api_token=$8, is_active=$9 WHERE id = $10""",
        data["name"], data.get("scheme", "https"), data["host"], int(data.get("port", 2053)),
        data.get("base_path", ""), data.get("login", "admin"), data.get("password", "admin"),
        data.get("api_token", ""), bool(data.get("is_active", True)), int(server_id),
    )


async def delete_server(conn, server_id) -> None:
    await conn.execute("DELETE FROM servers WHERE id = $1", int(server_id))


async def toggle_server(conn, server_id) -> bool:
    await conn.execute(
        "UPDATE servers SET is_active = NOT is_active WHERE id = $1", int(server_id)
    )
    return await conn.fetchval("SELECT is_active FROM servers WHERE id = $1", int(server_id))
