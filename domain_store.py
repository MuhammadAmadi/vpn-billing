"""Пул доменов (host + опциональный port + label).

Из этого пула админ назначает host'ы конкретным inbound'ам конкретных серверов
через таблицу inbound_host_overrides. Заменяет старый bypass_store.
"""

import logging

import db

log = logging.getLogger(__name__)


def _row_to_dict(r) -> dict:
    d = dict(r)
    # port в БД INTEGER NULL — отдаём как int|None
    if d.get("port") is None:
        d["port"] = None
    else:
        d["port"] = int(d["port"])
    return d


async def list_all(conn) -> list[dict]:
    rows = await conn.fetch(
        "SELECT * FROM domain_pool ORDER BY sort_order, id"
    )
    return [_row_to_dict(r) for r in rows]


async def list_active(conn) -> list[dict]:
    """Только активные — для UI dropdown'ов и резолва хоста."""
    rows = await conn.fetch(
        "SELECT * FROM domain_pool WHERE is_active = TRUE ORDER BY sort_order, id"
    )
    return [_row_to_dict(r) for r in rows]


async def get_active_map() -> dict[int, dict]:
    """{domain_id: row} только для активных доменов. Кешер для xray_api."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await list_active(conn)
    return {r["id"]: r for r in rows}


async def add(conn, host: str, port: int | None, label: str = "") -> int:
    next_order = await conn.fetchval("SELECT COALESCE(MAX(sort_order)+1, 0) FROM domain_pool")
    return await conn.fetchval(
        """INSERT INTO domain_pool (host, port, label, is_active, sort_order)
           VALUES ($1, $2, $3, TRUE, $4) RETURNING id""",
        host.strip(), port if port else None, (label or "").strip(), next_order,
    )


async def update(conn, did, host: str, port: int | None, label: str = "") -> None:
    await conn.execute(
        "UPDATE domain_pool SET host=$1, port=$2, label=$3 WHERE id=$4",
        host.strip(), port if port else None, (label or "").strip(), int(did),
    )


async def delete(conn, did) -> None:
    await conn.execute("DELETE FROM domain_pool WHERE id=$1", int(did))


async def toggle(conn, did) -> bool:
    await conn.execute(
        "UPDATE domain_pool SET is_active = NOT is_active WHERE id=$1", int(did),
    )
    return await conn.fetchval("SELECT is_active FROM domain_pool WHERE id=$1", int(did))
