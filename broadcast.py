"""Рассылка всем пользователям через Telegram Bot API.

• Шлёт сообщение всем активным (is_deleted=FALSE, is_inactive=FALSE) пользователям.
• Идёт в фоне (asyncio task), прогресс виден в админ-панели.
• Троттлинг ~20 сообщений/с (лимит Telegram ~30/с), обработка 429.
• Если задан PROXY_URL — запросы идут через прокси.
• При отказе Telegram 403 (бот заблокирован / аккаунт удалён) пользователь
  сразу помечается is_inactive=TRUE. На прочих ошибках инкрементится
  broadcast_failures; после 3-х неудач — тоже неактивный.
"""

import asyncio
import logging
import time

import httpx

import config
import db
import users_store

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
THROTTLE_DELAY = 0.05  # ~20 сообщений/с
FAILURE_THRESHOLD = 3   # после стольких подряд неудач — пометить неактивным

# Сигнатуры ошибок Telegram, которые означают, что пользователю слать бесполезно.
_FATAL_ERRORS = (
    "blocked",            # bot was blocked by the user
    "user is deactivated",
    "chat not found",
    "user is deleted",
)

_state = {
    "running": False, "total": 0, "sent": 0, "failed": 0, "skipped": 0,
    "marked_inactive": 0, "started_at": None, "finished_at": None, "last_error": None,
}


def get_progress() -> dict:
    return dict(_state)


def _make_client(proxy: str | None) -> httpx.AsyncClient:
    if not proxy:
        return httpx.AsyncClient(timeout=20)
    # httpx переименовал proxies -> proxy между версиями — поддерживаем оба варианта
    try:
        return httpx.AsyncClient(timeout=20, proxy=proxy)
    except TypeError:
        return httpx.AsyncClient(timeout=20, proxies=proxy)


async def _fetch_user_ids() -> list[int]:
    """Только активные (не удалены и не помечены неактивными) пользователи."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id FROM users WHERE is_deleted = FALSE AND is_inactive = FALSE"
        )
    return [r["user_id"] for r in rows]


def _build_markup(buttons: list[dict] | None) -> dict | None:
    if not buttons:
        return None
    kb = [
        [{"text": b.get("text", "Открыть"), "url": b["value"]}]
        for b in buttons
        if b.get("type") == "url" and b.get("value")
    ]
    return {"inline_keyboard": kb} if kb else None


def _is_fatal_telegram_error(description: str) -> bool:
    """True — пользователь больше недоступен, рассылку ему слать нет смысла."""
    if not description:
        return False
    desc = description.lower()
    return any(sig in desc for sig in _FATAL_ERRORS)


async def _send_one(client: httpx.AsyncClient, token: str, payload: dict) -> tuple[bool, str]:
    """Одна попытка с обработкой 429. Возвращает (ok, error_description)."""
    try:
        r = await client.post(f"{API}/bot{token}/sendMessage", json=payload)
        body = {}
        try:
            body = r.json()
        except Exception:
            pass
        if r.status_code == 200 and body.get("ok"):
            return True, ""
        if r.status_code == 429:
            retry = body.get("parameters", {}).get("retry_after", 1)
            await asyncio.sleep(retry + 1)
            r2 = await client.post(f"{API}/bot{token}/sendMessage", json=payload)
            try:
                body2 = r2.json()
            except Exception:
                body2 = {}
            if r2.status_code == 200 and body2.get("ok"):
                return True, ""
            return False, body2.get("description", f"HTTP {r2.status_code}")
        return False, body.get("description", f"HTTP {r.status_code}")
    except Exception as e:
        _state["last_error"] = str(e)[:200]
        return False, f"{type(e).__name__}: {e}"


async def _handle_failure(uid: int, err_desc: str) -> None:
    """Помечает пользователя при «фатальной» ошибке Telegram, либо инкрементит счётчик."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if _is_fatal_telegram_error(err_desc):
            await users_store.mark_inactive(conn, uid)
            _state["marked_inactive"] += 1
        else:
            became_inactive = await users_store.bump_broadcast_failure(
                conn, uid, threshold=FAILURE_THRESHOLD,
            )
            if became_inactive:
                _state["marked_inactive"] += 1


async def _run(text: str, buttons: list[dict] | None = None) -> None:
    token = config.BOT_TOKEN
    markup = _build_markup(buttons)

    try:
        user_ids = await _fetch_user_ids()
    except Exception as e:
        _state.update(running=False, last_error=f"Не удалось получить список: {e}")
        log.exception("Не смог получить список пользователей")
        return

    _state.update(running=True, total=len(user_ids), sent=0, failed=0,
                  skipped=0, marked_inactive=0,
                  started_at=time.time(), finished_at=None, last_error=None)

    try:
        async with _make_client(config.PROXY_URL) as client:
            for uid in user_ids:
                payload = {
                    "chat_id": uid, "text": text,
                    "parse_mode": "HTML", "disable_web_page_preview": True,
                }
                if markup:
                    payload["reply_markup"] = markup
                ok, err = await _send_one(client, token, payload)
                if ok:
                    _state["sent"] += 1
                else:
                    _state["failed"] += 1
                    _state["last_error"] = err[:200]
                    try:
                        await _handle_failure(uid, err)
                    except Exception as e:
                        log.warning("Не смог пометить uid=%s: %s", uid, e)
                await asyncio.sleep(THROTTLE_DELAY)
    finally:
        _state["running"] = False
        _state["finished_at"] = time.time()
        log.info("Рассылка завершена: sent=%s failed=%s marked_inactive=%s",
                 _state["sent"], _state["failed"], _state["marked_inactive"])


def start(text: str, buttons: list[dict] | None = None) -> tuple[bool, str]:
    """Запустить рассылку в фоне."""
    if _state["running"]:
        return False, "Рассылка уже идёт"
    if not (text or "").strip():
        return False, "Пустой текст"
    asyncio.create_task(_run(text, buttons))
    return True, "Рассылка запущена"
