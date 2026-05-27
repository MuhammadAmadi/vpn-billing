# users_store.py — ПОЛЬЗОВАТЕЛИ ДЛЯ АДМИН-ПАНЕЛИ
#
# Считает по каждому пользователю:
#   • topup_total  — суммарная сумма пополнений (сумма income-транзакций)
#   • topup_count  — сколько раз пополнял (кол-во income-транзакций)
#   • topup_streak — самая длинная СЕРИЯ пополнений подряд (по календарным дням)
#   • device_count — количество устройств
#   • balance      — текущий баланс
#
# Поддерживает пагинацию, сортировку (whitelist — без SQL-инъекций) и фильтры.
# Изменение баланса пишется в transactions как 'action' (чтобы не портить
# статистику реальных пополнений).

from decimal import Decimal
import config  # noqa: F401  (тянет .env, на случай отдельного использования)

# Разрешённые сортировки. Ключ приходит из панели, значение — безопасный SQL.
SORT_MAP = {
    "topup_total":  "q.topup_total DESC NULLS LAST, q.user_id DESC",   # по сумме пополнений
    "topup_count":  "q.topup_count DESC, q.user_id DESC",              # по кол-ву пополнений
    "topup_streak": "q.topup_streak DESC, q.topup_count DESC",         # по серии подряд
    "balance":      "q.balance DESC",                                  # по балансу
    "devices":      "q.device_count DESC, q.user_id DESC",             # по устройствам
    "newest":       "q.created_ts DESC",                               # новые
    "oldest":       "q.created_ts ASC",                                # старые
}

# Фильтры по статусу. daily_cost = устройства × 3.33₽/день.
STATUS_MAP = {
    "all":          "TRUE",
    "active":       "q.device_count > 0 AND q.balance >= q.device_count * 3.33",
    "stopped":      "q.device_count > 0 AND q.balance <  q.device_count * 3.33",
    "with_devices": "q.device_count > 0",
    "no_devices":   "q.device_count = 0",
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
       COALESCE(inc.topup_count, 0)                        AS topup_count,
       COALESCE(inc.topup_total, 0)::float8               AS topup_total,
       COALESCE(streak.topup_streak, 0)                    AS topup_streak,
       COALESCE(dev.device_count, 0)                       AS device_count
FROM users u
LEFT JOIN inc    ON inc.user_id    = u.user_id
LEFT JOIN streak ON streak.user_id = u.user_id
LEFT JOIN dev    ON dev.user_id    = u.user_id
"""


def _build_filtered(search, status):
    """Возвращает (base_sql, params) с применёнными фильтрами (без сортировки/лимита)."""
    status_sql = STATUS_MAP.get(status, "TRUE")
    where = [status_sql]
    params = []
    if search:
        params.append(f"%{search.strip()}%")
        p = f"${len(params)}"
        where.append(
            f"(CAST(q.user_id AS TEXT) LIKE {p} OR q.username ILIKE {p} OR COALESCE(q.phone,'') ILIKE {p})"
        )
    where_sql = " AND ".join(where)
    base = f"SELECT q.* FROM ({_INNER}) q WHERE {where_sql}"
    return base, params


async def get_users(conn, *, search="", status="all", sort="topup_total", page=1, page_size=25):
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


async def set_balance(conn, user_id, new_balance, note="Изменение баланса (админ)"):
    """Устанавливает точный баланс и записывает корректировку в историю."""
    row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", int(user_id))
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
        '''INSERT INTO transactions (user_id, type, title, description, amount)
           VALUES ($1, 'action', $2, $3, $4)''',
        int(user_id), note, f"Было {old:.2f}₽ → стало {nb:.2f}₽", f"{sign}{abs(delta):.2f}₽",
    )
    return nb


async def get_user_card(conn, user_id):
    """Краткая карточка пользователя для модалки изменения баланса."""
    r = await conn.fetchrow(
        '''SELECT u.user_id, u.username, u.phone, u.balance::float8 AS balance,
                  (SELECT COUNT(*) FROM devices WHERE user_id = u.user_id) AS device_count
           FROM users u WHERE u.user_id = $1''',
        int(user_id),
    )
    return dict(r) if r else None


async def export_users(conn, *, search="", status="all", sort="topup_total", limit=100000):
    """Все пользователи под текущими фильтрами (без пагинации) — для CSV."""
    sort_sql = SORT_MAP.get(sort, SORT_MAP["topup_total"])
    base, params = _build_filtered(search, status)
    rows = await conn.fetch(
        f"{base} ORDER BY {sort_sql} LIMIT ${len(params) + 1}",
        *params, int(limit),
    )
    return [dict(r) for r in rows]


async def get_user_detail(conn, user_id):
    """Полная карточка: данные + устройства + история транзакций."""
    user = await conn.fetchrow(
        '''SELECT user_id, username, phone, balance::float8 AS balance, magic_token,
                  TO_CHAR(created_at, 'DD.MM.YYYY') AS created
           FROM users WHERE user_id = $1''',
        int(user_id),
    )
    if not user:
        return None

    devices = await conn.fetch(
        '''SELECT id, short_id, name, os, TO_CHAR(created_at, 'DD.MM.YY') AS created
           FROM devices WHERE user_id = $1 ORDER BY created_at DESC''',
        int(user_id),
    )

    history = await conn.fetch(
        '''SELECT type, title, description AS descr, amount,
                  TO_CHAR(created_at, 'DD.MM.YY HH24:MI') AS ts
           FROM transactions WHERE user_id = $1
           ORDER BY created_at DESC LIMIT 100''',
        int(user_id),
    )

    base_url = getattr(config, "CABINET_BASE_URL", "")
    dev_list = []
    for d in devices:
        dd = dict(d)
        dd["sub_url"] = f"{base_url}/sub/{dd['id']}" if base_url else f"/sub/{dd['id']}"
        dev_list.append(dd)

    u = dict(user)
    u["cabinet_url"] = f"{base_url}/cabinet/{u['magic_token']}" if (base_url and u.get("magic_token")) else None
    u.pop("magic_token", None)  # токен наружу не отдаём
    return {"user": u, "devices": dev_list, "history": [dict(h) for h in history]}
