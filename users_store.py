"""Пользователи для админ-панели.

По каждому пользователю считает:
  • topup_total  — сумма пополнений (income-транзакции)
  • topup_count  — сколько раз пополнял
  • topup_streak — самая длинная серия пополнений подряд (по календарным дням)
  • device_count — количество устройств
  • balance      — текущий баланс

Изменение баланса записывается в transactions как 'action' — чтобы не портить
статистику реальных пополнений.
"""

from decimal import Decimal

import config


# Разрешённые сортировки. Ключ приходит из панели, значение — безопасный SQL.
SORT_MAP = {
    "topup_total":  "q.topup_total DESC NULLS LAST, q.user_id DESC",
    "topup_count":  "q.topup_count DESC, q.user_id DESC",
    "topup_streak": "q.topup_streak DESC, q.topup_count DESC",
    "balance":      "q.balance DESC",
    "devices":      "q.device_count DESC, q.user_id DESC",
    "newest":       "q.created_ts DESC",
    "oldest":       "q.created_ts ASC",
    "inactive":     "q.is_inactive DESC, q.broadcast_failures DESC, q.user_id DESC",
    "failures":     "q.broadcast_failures DESC, q.user_id DESC",
}

# Фильтры по статусу. daily_cost = устройства × PRICE_PER_DEVICE.
STATUS_MAP = {
    "all":          "TRUE",
    "active":       "q.device_count > 0 AND q.balance >= q.device_count * $price",
    "stopped":      "q.device_count > 0 AND q.balance <  q.device_count * $price",
    "with_devices": "q.device_count > 0",
    "no_devices":   "q.device_count = 0",
    "inactive":     "q.is_inactive = TRUE",
}

# Внутренний запрос: считает всю статистику по каждому пользователю.
# Серия пополнений = «острова» подряд идущих дат (классический gaps-and-islands).
_INNER = """
WITH inc AS (
    SELECT user_id,
           COUNT(*) AS topup_count,
           COALESCE(SUM(NULLIF(regexp_replace(amount, '[^0-9.]', '', 'g'), '')::numeric), 0) AS topup_total
    FROM transactions WHERE type = 'income' GROUP BY user_id
),
days AS (
    SELECT DISTINCT user_id, created_at::date AS d FROM transactions WHERE type = 'income'
),
islands AS (
    SELECT user_id, d - (ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY d))::int AS grp
    FROM days
),
streak AS (
    SELECT user_id, MAX(c) AS topup_streak
    FROM (SELECT user_id, grp, COUNT(*) AS c FROM islands GROUP BY user_id, grp) s
    GROUP BY user_id
),
dev AS (
    SELECT user_id, COUNT(*) AS device_count FROM devices GROUP BY user_id
)
SELECT u.user_id,
       u.username,
       u.phone,
       u.balance::float8                                   AS balance,
       TO_CHAR(u.created_at, 'DD.MM.YYYY')                 AS created,
       CAST(EXTRACT(EPOCH FROM u.created_at) AS BIGINT)    AS created_ts,
       u.is_inactive,
       TO_CHAR(u.inactive_since, 'DD.MM.YYYY HH24:MI')      AS inactive_since,
       u.broadcast_failures,
       COALESCE(inc.topup_count, 0)                        AS topup_count,
       COALESCE(inc.topup_total, 0)::float8               AS topup_total,
       COALESCE(streak.topup_streak, 0)                    AS topup_streak,
       COALESCE(dev.device_count, 0)                       AS device_count
FROM users u
LEFT JOIN inc    ON inc.user_id    = u.user_id
LEFT JOIN streak ON streak.user_id = u.user_id
LEFT JOIN dev    ON dev.user_id    = u.user_id
WHERE u.is_deleted = FALSE
"""


def _build_filtered(search: str, status: str):
    """Возвращает (sql, params) с применёнными фильтрами (без сортировки/лимита)."""
    status_tpl = STATUS_MAP.get(status, "TRUE")
    params: list = []
    # Подставляем PRICE_PER_DEVICE как параметр, если статус его использует
    if "$price" in status_tpl:
        params.append(config.PRICE_PER_DEVICE)
        status_sql = status_tpl.replace("$price", f"${len(params)}")
    else:
        status_sql = status_tpl
    where = [status_sql]
    if search:
        params.append(f"%{search.strip()}%")
        p = f"${len(params)}"
        where.append(
            f"(CAST(q.user_id AS TEXT) LIKE {p} OR q.username ILIKE {p} OR COALESCE(q.phone,'') ILIKE {p})"
        )
    base = f"SELECT q.* FROM ({_INNER}) q WHERE {' AND '.join(where)}"
    return base, params


async def get_users(conn, *, search: str = "", status: str = "all",
                    sort: str = "topup_total", page: int = 1, page_size: int = 25) -> dict:
    sort_sql = SORT_MAP.get(sort, SORT_MAP["topup_total"])

    page = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))
    offset = (page - 1) * page_size

    base, params = _build_filtered(search, status)
    total = await conn.fetchval(f"SELECT COUNT(*) FROM ({base}) cnt", *params)

    limit_p = f"${len(params) + 1}"
    offset_p = f"${len(params) + 2}"
    rows = await conn.fetch(
        f"{base} ORDER BY {sort_sql} LIMIT {limit_p} OFFSET {offset_p}",
        *params, page_size, offset,
    )

    return {
        "users": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


async def set_balance(conn, user_id, new_balance, note: str = "Изменение баланса (админ)"):
    """Устанавливает точный баланс и записывает корректировку в одной транзакции."""
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT balance FROM users WHERE user_id = $1 FOR UPDATE", int(user_id)
        )
        if not row:
            return None
        old = float(row["balance"])
        nb = round(float(new_balance), 2)
        await conn.execute(
            "UPDATE users SET balance = $1 WHERE user_id = $2",
            Decimal(str(nb)), int(user_id),
        )
        delta = round(nb - old, 2)
        sign = "+" if delta >= 0 else "-"
        await conn.execute(
            """INSERT INTO transactions (user_id, type, title, description, amount)
               VALUES ($1, 'action', $2, $3, $4)""",
            int(user_id), note, f"Было {old:.2f}₽ → стало {nb:.2f}₽", f"{sign}{abs(delta):.2f}₽",
        )
    return nb


async def get_user_card(conn, user_id) -> dict | None:
    """Краткая карточка пользователя для модалки изменения баланса."""
    r = await conn.fetchrow(
        """SELECT u.user_id, u.username, u.phone, u.balance::float8 AS balance,
                  (SELECT COUNT(*) FROM devices WHERE user_id = u.user_id) AS device_count
           FROM users u WHERE u.user_id = $1""",
        int(user_id),
    )
    return dict(r) if r else None


async def export_users(conn, *, search: str = "", status: str = "all",
                       sort: str = "topup_total", limit: int = 100000) -> list[dict]:
    """Все пользователи под фильтрами (без пагинации) — для CSV."""
    sort_sql = SORT_MAP.get(sort, SORT_MAP["topup_total"])
    base, params = _build_filtered(search, status)
    rows = await conn.fetch(
        f"{base} ORDER BY {sort_sql} LIMIT ${len(params) + 1}",
        *params, int(limit),
    )
    return [dict(r) for r in rows]


async def delete_device(conn, user_id, device_id) -> dict | None:
    """Удаляет устройство из БД. VPN-панели чистятся в admin_panel перед вызовом.
    Возвращает строку устройства, если оно найдено и удалено."""
    row = await conn.fetchrow(
        "SELECT id, short_id, name, os FROM devices WHERE id = $1 AND user_id = $2",
        str(device_id), int(user_id),
    )
    if not row:
        return None
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM devices WHERE id = $1 AND user_id = $2",
            str(device_id), int(user_id),
        )
        await conn.execute(
            """INSERT INTO transactions (user_id, type, title, description)
               VALUES ($1, 'action', 'Устройство удалено (админ)', $2)""",
            int(user_id), f"ID {row['short_id']} {row['name'] or ''} ({row['os'] or ''})".strip(),
        )
    return dict(row)


async def soft_delete_user(conn, user_id) -> list[str]:
    """Помечает пользователя удалённым (is_deleted=TRUE) и удаляет его устройства из БД.
    bonus_given / phone_bonus_given сохраняются — при возврате клиент бонус не получит.
    Возвращает список device_id, которые нужно убрать с VPN-панелей (вызвать снаружи)."""
    device_ids = [
        r["id"] for r in await conn.fetch(
            "SELECT id FROM devices WHERE user_id = $1", int(user_id),
        )
    ]
    async with conn.transaction():
        await conn.execute("DELETE FROM devices WHERE user_id = $1", int(user_id))
        # Анонимизируем PII, bonus_given/phone_bonus_given оставляем как есть
        await conn.execute(
            """UPDATE users
                  SET is_deleted = TRUE,
                      phone = NULL,
                      magic_token = NULL,
                      balance = 0,
                      is_inactive = FALSE,
                      inactive_since = NULL
                WHERE user_id = $1""",
            int(user_id),
        )
        await conn.execute(
            """INSERT INTO transactions (user_id, type, title, description)
               VALUES ($1, 'action', 'Аккаунт удалён (админ)',
                       'soft-delete: TG ID сохранён, бонусы повторно не выдаются')""",
            int(user_id),
        )
    return device_ids


async def mark_inactive(conn, user_id: int) -> None:
    """Помечает пользователя как недоступного для рассылки (бот заблокирован/аккаунт удалён)."""
    await conn.execute(
        """UPDATE users
              SET is_inactive = TRUE,
                  inactive_since = COALESCE(inactive_since, NOW()),
                  broadcast_failures = broadcast_failures + 1
            WHERE user_id = $1""",
        int(user_id),
    )


async def bump_broadcast_failure(conn, user_id: int, threshold: int = 3) -> bool:
    """Инкрементит счётчик неудач рассылки. Возвращает True, если после инкремента
    пользователь стал неактивным (порог достигнут)."""
    new_count = await conn.fetchval(
        """UPDATE users
              SET broadcast_failures = broadcast_failures + 1
            WHERE user_id = $1
        RETURNING broadcast_failures""",
        int(user_id),
    )
    if new_count is not None and new_count >= threshold:
        await conn.execute(
            """UPDATE users SET is_inactive = TRUE,
                                inactive_since = COALESCE(inactive_since, NOW())
                WHERE user_id = $1 AND is_inactive = FALSE""",
            int(user_id),
        )
        return True
    return False


async def reset_inactive(conn, user_id: int) -> None:
    """Сбрасывает флаг неактивности (если пользователь вернулся и пишет боту)."""
    await conn.execute(
        """UPDATE users SET is_inactive = FALSE,
                            inactive_since = NULL,
                            broadcast_failures = 0
            WHERE user_id = $1""",
        int(user_id),
    )


async def get_user_detail(conn, user_id) -> dict | None:
    """Полная карточка: данные + устройства + история транзакций."""
    user = await conn.fetchrow(
        """SELECT user_id, username, phone, balance::float8 AS balance, magic_token,
                  is_inactive, broadcast_failures,
                  TO_CHAR(inactive_since, 'DD.MM.YYYY HH24:MI') AS inactive_since,
                  TO_CHAR(created_at, 'DD.MM.YYYY') AS created
           FROM users WHERE user_id = $1 AND is_deleted = FALSE""",
        int(user_id),
    )
    if not user:
        return None

    devices = await conn.fetch(
        """SELECT id, short_id, name, os, TO_CHAR(created_at, 'DD.MM.YY') AS created
           FROM devices WHERE user_id = $1 ORDER BY created_at DESC""",
        int(user_id),
    )

    history = await conn.fetch(
        """SELECT type, title, description AS descr, amount,
                  TO_CHAR(created_at, 'DD.MM.YY HH24:MI') AS ts
           FROM transactions WHERE user_id = $1
           ORDER BY created_at DESC LIMIT 100""",
        int(user_id),
    )

    base_url = config.CABINET_BASE_URL
    dev_list = []
    for d in devices:
        dd = dict(d)
        dd["sub_url"] = f"{base_url}/sub/{dd['id']}"
        dev_list.append(dd)

    u = dict(user)
    u["cabinet_url"] = f"{base_url}/cabinet/{u['magic_token']}" if u.get("magic_token") else None
    u.pop("magic_token", None)
    return {"user": u, "devices": dev_list, "history": [dict(h) for h in history]}
