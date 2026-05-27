# broadcast.py — РАССЫЛКА ВСЕМ ПОЛЬЗОВАТЕЛЯМ ЧЕРЕЗ БОТА
#
# Шлёт сообщение всем, кто есть в таблице users (т.е. кто запускал бота),
# напрямую через Telegram Bot API — сам процесс бота для этого не нужен.
#
# • Работает в фоне (asyncio task), прогресс виден в панели.
# • Троттлинг ~20 сообщений/с (лимит Telegram ~30/с), обработка 429 (retry_after).
# • Если задан PROXY_URL в .env — запросы к Telegram идут через прокси
#   (актуально для серверов в РФ, где api.telegram.org заблокирован).
# • Можно приложить inline-кнопки-ссылки (url-кнопки из вкладки «Кнопки»).

import os
import time
import asyncio

import httpx
import asyncpg
import config

API = "https://api.telegram.org"

_state = {
    "running": False, "total": 0, "sent": 0, "failed": 0,
    "started_at": None, "finished_at": None, "last_error": None,
}


def get_progress():
    return dict(_state)


def _make_client(proxy):
    # httpx менял имя параметра (proxies -> proxy) между версиями — поддержим оба
    if not proxy:
        return httpx.AsyncClient(timeout=20)
    try:
        return httpx.AsyncClient(timeout=20, proxy=proxy)
    except TypeError:
        return httpx.AsyncClient(timeout=20, proxies=proxy)


async def _fetch_user_ids():
    conn = await asyncpg.connect(
        user=config.DB_USER, password=config.DB_PASS,
        database=config.DB_NAME, host=config.DB_HOST,
    )
    try:
        rows = await conn.fetch("SELECT user_id FROM users")
        return [r["user_id"] for r in rows]
    finally:
        await conn.close()


def _build_markup(buttons):
    if not buttons:
        return None
    kb = []
    for b in buttons:
        if b.get("type") == "url" and b.get("value"):
            kb.append([{"text": b.get("text", "Открыть"), "url": b["value"]}])
    return {"inline_keyboard": kb} if kb else None


async def _run(text, buttons=None):
    token = config.BOT_TOKEN
    proxy = os.getenv("PROXY_URL") or None
    markup = _build_markup(buttons)

    try:
        user_ids = await _fetch_user_ids()
    except Exception as e:
        _state.update(running=False, last_error=f"Не удалось получить список: {e}")
        return

    _state.update(running=True, total=len(user_ids), sent=0, failed=0,
                  started_at=time.time(), finished_at=None, last_error=None)

    try:
        async with _make_client(proxy) as client:
            for uid in user_ids:
                payload = {
                    "chat_id": uid, "text": text,
                    "parse_mode": "HTML", "disable_web_page_preview": True,
                }
                if markup:
                    payload["reply_markup"] = markup
                try:
                    r = await client.post(f"{API}/bot{token}/sendMessage", json=payload)
                    if r.status_code == 200 and r.json().get("ok"):
                        _state["sent"] += 1
                    elif r.status_code == 429:
                        retry = r.json().get("parameters", {}).get("retry_after", 1)
                        await asyncio.sleep(retry + 1)
                        # одна повторная попытка
                        r2 = await client.post(f"{API}/bot{token}/sendMessage", json=payload)
                        if r2.status_code == 200 and r2.json().get("ok"):
                            _state["sent"] += 1
                        else:
                            _state["failed"] += 1
                    else:
                        # 403 = пользователь заблокировал бота и т.п.
                        _state["failed"] += 1
                except Exception as e:
                    _state["failed"] += 1
                    _state["last_error"] = str(e)[:200]
                await asyncio.sleep(0.05)  # ~20 сообщений/с
    finally:
        _state["running"] = False
        _state["finished_at"] = time.time()


def start(text, buttons=None):
    """Запустить рассылку в фоне. Возвращает (ok, msg)."""
    if _state["running"]:
        return False, "Рассылка уже идёт"
    if not (text or "").strip():
        return False, "Пустой текст"
    asyncio.create_task(_run(text, buttons))
    return True, "Рассылка запущена"
