"""Per-inbound override host'а: на каждый (server_id, inbound_id) можно
закрепить домен из domain_pool. Если override нет — берётся server.host.

xray_api при сборке vless-ссылок один раз тянет map (server_id, inbound_id)
→ {host, port, label} и резолвит локально без лишних JOIN'ов.
"""

import logging

import db

log = logging.getLogger(__name__)


async def list_for_server(conn, server_id) -> list[dict]:
    rows = await conn.fetch(
        """SELECT o.server_id, o.inbound_id, o.inbound_remark, o.inbound_port,
                  o.domain_id, d.host AS domain_host, d.port AS domain_port,
                  d.label AS domain_label
             FROM inbound_host_overrides o
             LEFT JOIN domain_pool d ON d.id = o.domain_id
            WHERE o.server_id = $1
            ORDER BY o.inbound_id""",
        int(server_id),
    )
    return [dict(r) for r in rows]


async def list_all(conn) -> list[dict]:
    rows = await conn.fetch(
        """SELECT o.server_id, o.inbound_id, o.inbound_remark, o.inbound_port,
                  o.domain_id, d.host AS domain_host, d.port AS domain_port,
                  d.label AS domain_label
             FROM inbound_host_overrides o
             LEFT JOIN domain_pool d ON d.id = o.domain_id"""
    )
    return [dict(r) for r in rows]


async def get_map() -> dict[tuple[int, int], dict]:
    """{(server_id, inbound_id): {host, port, label}} — для использования в xray_api.
    Возвращает только записи, у которых реально установлен domain_id (иначе override нет)."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT o.server_id, o.inbound_id, d.host, d.port, d.label
                 FROM inbound_host_overrides o
                 JOIN domain_pool d ON d.id = o.domain_id
                WHERE d.is_active = TRUE"""
        )
    return {
        (r["server_id"], r["inbound_id"]): {
            "host": r["host"],
            "port": r["port"],
            "label": r["label"] or None,
        }
        for r in rows
    }


async def upsert_meta(conn, server_id: int, inbound_id: int,
                      remark: str | None, port: int | None) -> None:
    """Кеш метаданных inbound'а (remark/port) — чтобы админка показывала их без
    обращения к панели. Вызывается при выборе override'а в UI."""
    await conn.execute(
        """INSERT INTO inbound_host_overrides (server_id, inbound_id, inbound_remark, inbound_port)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (server_id, inbound_id) DO UPDATE
              SET inbound_remark = EXCLUDED.inbound_remark,
                  inbound_port   = EXCLUDED.inbound_port,
                  updated_at     = NOW()""",
        int(server_id), int(inbound_id), remark, port,
    )


async def set_override(conn, server_id: int, inbound_id: int,
                       domain_id: int | None,
                       remark: str | None = None,
                       port: int | None = None) -> None:
    """Устанавливает (или сбрасывает) override для (server_id, inbound_id).
    domain_id=None → возвращаемся к server.host."""
    await conn.execute(
        """INSERT INTO inbound_host_overrides
              (server_id, inbound_id, inbound_remark, inbound_port, domain_id)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (server_id, inbound_id) DO UPDATE
              SET domain_id      = EXCLUDED.domain_id,
                  inbound_remark = COALESCE(EXCLUDED.inbound_remark, inbound_host_overrides.inbound_remark),
                  inbound_port   = COALESCE(EXCLUDED.inbound_port, inbound_host_overrides.inbound_port),
                  updated_at     = NOW()""",
        int(server_id), int(inbound_id), remark, port,
        int(domain_id) if domain_id else None,
    )


async def delete_override(conn, server_id: int, inbound_id: int) -> None:
    await conn.execute(
        "DELETE FROM inbound_host_overrides WHERE server_id=$1 AND inbound_id=$2",
        int(server_id), int(inbound_id),
    )


async def delete_for_server(conn, server_id) -> None:
    await conn.execute("DELETE FROM inbound_host_overrides WHERE server_id=$1", int(server_id))
