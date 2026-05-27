# bypass_store.py — СПИСОК BYPASS IP / ДОМЕНОВ
#
# Механизм «обхода»: если у inbound в 3x-ui в названии (remark) есть слово BYPASS,
# то в ссылку вместо хоста сервера подставляется адрес из ЭТОГО списка
# (по кругу — ОБХОД 1, ОБХОД 2, ...). Раньше список жил в .env (BYPASS_IPS),
# теперь — в БД и управляется через админ-панель.
#
# get_active_bypass_ips() никогда не падает: при любой ошибке/пустой таблице
# откатывается на config.BYPASS_IPS из .env.

import asyncpg
import config


async def _connect():
    return await asyncpg.connect(
        user=config.DB_USER, password=config.DB_PASS,
        database=config.DB_NAME, host=config.DB_HOST,
    )


async def get_active_bypass_ips(conn=None):
    """Список активных значений (IP/домены) для подстановки в BYPASS-ссылки."""
    own = False
    try:
        if conn is None:
            conn = await _connect()
            own = True
        rows = await conn.fetch(
            "SELECT value FROM bypass_ips WHERE is_active = TRUE ORDER BY sort_order, id"
        )
        if rows:
            return [r["value"] for r in rows]
        return list(getattr(config, "BYPASS_IPS", []))
    except Exception as e:
        print(f"⚠️ [bypass_store] не смог прочитать bypass из БД, использую .env: {e}")
        return list(getattr(config, "BYPASS_IPS", []))
    finally:
        if own and conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


async def list_bypass(conn):
    rows = await conn.fetch("SELECT * FROM bypass_ips ORDER BY sort_order, id")
    return [dict(r) for r in rows]


async def add_bypass(conn, value, label=""):
    next_order = await conn.fetchval("SELECT COALESCE(MAX(sort_order)+1,0) FROM bypass_ips")
    return await conn.fetchval(
        "INSERT INTO bypass_ips (value, label, is_active, sort_order) VALUES ($1,$2,TRUE,$3) RETURNING id",
        value.strip(), (label or "").strip(), next_order,
    )


async def update_bypass(conn, bid, value, label=""):
    await conn.execute(
        "UPDATE bypass_ips SET value=$1, label=$2 WHERE id=$3",
        value.strip(), (label or "").strip(), int(bid),
    )


async def delete_bypass(conn, bid):
    await conn.execute("DELETE FROM bypass_ips WHERE id=$1", int(bid))


async def toggle_bypass(conn, bid):
    await conn.execute("UPDATE bypass_ips SET is_active = NOT is_active WHERE id=$1", int(bid))
    return await conn.fetchval("SELECT is_active FROM bypass_ips WHERE id=$1", int(bid))
