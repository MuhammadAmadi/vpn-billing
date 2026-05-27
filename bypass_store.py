"""Список Bypass IP/доменов: хранится в БД, редактируется через админ-панель.

При первой миграции значения переносятся из .env. После этого .env-список
используется только как аварийный фолбэк, если БД временно недоступна.
"""

import logging

import config
import db

log = logging.getLogger(__name__)


async def get_active_bypass_ips() -> list[str]:
    """Активные адреса для подстановки в BYPASS-ссылки. Не падает при сбое БД.

    Фолбэк на .env только если в таблице ВООБЩЕ нет записей (значит админ
    её ещё не настраивал). Если записи есть, но все is_active=FALSE — это
    осознанное отключение, возвращаем пустой список, чтобы BYPASS-inbound'ы
    не создавались.
    """
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT value, is_active FROM bypass_ips ORDER BY sort_order, id"
            )
    except Exception as e:
        log.warning("Не смог прочитать bypass из БД, использую .env: %s", e)
        return list(config.BYPASS_IPS)

    if not rows:
        return list(config.BYPASS_IPS)
    return [r["value"] for r in rows if r["is_active"]]


async def list_bypass(conn):
    rows = await conn.fetch("SELECT * FROM bypass_ips ORDER BY sort_order, id")
    return [dict(r) for r in rows]


async def add_bypass(conn, value: str, label: str = ""):
    next_order = await conn.fetchval("SELECT COALESCE(MAX(sort_order)+1, 0) FROM bypass_ips")
    return await conn.fetchval(
        "INSERT INTO bypass_ips (value, label, is_active, sort_order) VALUES ($1,$2,TRUE,$3) RETURNING id",
        value.strip(), (label or "").strip(), next_order,
    )


async def update_bypass(conn, bid, value: str, label: str = ""):
    await conn.execute(
        "UPDATE bypass_ips SET value=$1, label=$2 WHERE id=$3",
        value.strip(), (label or "").strip(), int(bid),
    )


async def delete_bypass(conn, bid):
    await conn.execute("DELETE FROM bypass_ips WHERE id=$1", int(bid))


async def toggle_bypass(conn, bid):
    await conn.execute("UPDATE bypass_ips SET is_active = NOT is_active WHERE id=$1", int(bid))
    return await conn.fetchval("SELECT is_active FROM bypass_ips WHERE id=$1", int(bid))
