"""Рассылка всем пользователям через Telegram Bot API.

• Шлёт сообщение всем, кто есть в users (т.е. кто запускал бота).
• Идёт в фоне (asyncio task), прогресс виден в админ-панели.
• Троттлинг ~20 сообщений/с (лимит Telegram ~30/с), обработка 429.
• Если задан PROXY_URL — запросы идут через прокси.
"""

import asyncio
import logging
import time

import httpx

import config
import db

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
THROTTLE_DELAY = 0.05  # ~20 сообщений/с

_state = {
    "running": False, "total": 0, "sent": 0, "failed": 0,
    "started_at": None, "finished_at": None, "last_error": None,
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
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
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


async def _send_one(client: httpx.AsyncClient, token: str, payload: dict) -> bool:
    """Одна попытка с обработкой 429. True = успех."""
    try:
        r = await client.post(f"{API}/bot{token}/sendMessage", json=payload)
        if r.status_code == 200 and r.json().get("ok"):
            return True
        if r.status_code == 429:
            retry = r.json().get("parameters", {}).get("retry_after", 1)
            await asyncio.sleep(retry + 1)
            r2 = await client.post(f"{API}/bot{token}/sendMessage", json=payload)
            return r2.status_code == 200 and r2.json().get("ok")
        return False
    except Exception as e:
        _state["last_error"] = str(e)[:200]
        return False


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
                if await _send_one(client, token, payload):
                    _state["sent"] += 1
                else:
                    _state["failed"] += 1
                await asyncio.sleep(THROTTLE_DELAY)
    finally:
        _state["running"] = False
        _state["finished_at"] = time.time()
        log.info("Рассылка завершена: sent=%s failed=%s", _state["sent"], _state["failed"])


def start(text: str, buttons: list[dict] | None = None) -> tuple[bool, str]:
    """Запустить рассылку в фоне."""
    if _state["running"]:
        return False, "Рассылка уже идёт"
    if not (text or "").strip():
        return False, "Пустой текст"
    asyncio.create_task(_run(text, buttons))
    return True, "Рассылка запущена"
