#!/usr/bin/env python3
"""Удаление осиротевших клиентов на 3x-ui панелях.

Сравнивает email'ы клиентов на каждой панели с устройствами в БД
(таблица devices) и удаляет тех, кто соответствует нашему шаблону
email'а (user{user_id}_{short_id} или legacy user{user_id}_{short_id}_iN),
но не привязан к существующему устройству.

Клиенты с email-ом, не подходящим под наш шаблон (например, созданные
вручную через UI панели — `ddta0b3u`, `ych18knz` и т.п.), не трогаются.

Запуск:
    python cleanup_orphans.py             # dry-run, только печатает что было бы удалено
    python cleanup_orphans.py --apply     # реально удаляет
"""

import argparse
import asyncio
import logging
import re
from collections import defaultdict

import httpx

import db
import xray_api
from server_store import get_active_servers

logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("cleanup")

# user{digits}_{short_id}  ИЛИ  user{digits}_{short_id}_i{digits}
# short_id у нас 4-символьный uppercase hex (см. web.py: uuid.uuid4().hex[:4].upper())
EMAIL_RE = re.compile(r"^user(\d+)_([A-Za-z0-9]+?)(?:_i\d+)?$")


async def get_device_keys() -> set[str]:
    """Из БД собирает множество ключей 'user_id_short_id', соответствующих
    устройствам в таблице devices. Сетка валидных клиентов."""
    keys: set[str] = set()
    pool = await db.create_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, short_id FROM devices")
        for r in rows:
            keys.add(f"{r['user_id']}_{r['short_id']}")
    finally:
        await pool.close()
    return keys


def is_orphan(email: str, valid_device_keys: set[str]) -> bool:
    """True — email похож на наш шаблон, но соответствующего устройства в БД нет."""
    m = EMAIL_RE.match(email)
    if not m:
        return False
    key = f"{m.group(1)}_{m.group(2)}"
    return key not in valid_device_keys


async def panel_clients(client: httpx.AsyncClient, server: dict) -> dict[str, set[int]]:
    """Возвращает {email: {inbound_id, ...}} для всех клиентов на панели."""
    base_url = xray_api._base_url(server)
    auth = await xray_api.get_auth_kwargs(client, server)
    if not auth:
        log.warning("Нет авторизации на %s", server["name"])
        return {}

    inbounds = await xray_api._fetch_inbounds(client, base_url, auth, server["name"])
    email_to_inbounds: dict[str, set[int]] = defaultdict(set)
    for inb in inbounds:
        for stat in inb.get("clientStats") or []:
            email = stat.get("email")
            if email:
                email_to_inbounds[email].add(inb["id"])
        settings = xray_api._parse_json_field(inb.get("settings"))
        for c in settings.get("clients") or []:
            email = c.get("email")
            if email:
                email_to_inbounds[email].add(inb["id"])
    return email_to_inbounds


async def process_server(client: httpx.AsyncClient, server: dict,
                         valid_keys: set[str], apply: bool) -> tuple[int, int]:
    """Возвращает (orphan_count, deleted_count) для одного сервера."""
    base_url = xray_api._base_url(server)
    auth = await xray_api.get_auth_kwargs(client, server)
    if not auth:
        log.warning("Пропускаю %s — нет авторизации", server["name"])
        return 0, 0

    emails = await panel_clients(client, server)
    log.info("[%s] всего клиентов: %s", server["name"], len(emails))

    orphans = [(e, sorted(ids)) for e, ids in emails.items() if is_orphan(e, valid_keys)]
    if not orphans:
        log.info("[%s] осиротевших не найдено", server["name"])
        return 0, 0

    deleted = 0
    for email, ids in sorted(orphans):
        if apply:
            ok, err = await xray_api._delete_client(client, base_url, auth, email)
            if ok:
                log.info("[%s] DEL %s (inbounds=%s)", server["name"], email, ids)
                deleted += 1
            else:
                log.warning("[%s] ОШИБКА удаления %s: %s", server["name"], email, err)
        else:
            log.info("[%s] [dry-run] DEL %s (inbounds=%s)", server["name"], email, ids)
    return len(orphans), deleted


async def main(apply: bool) -> None:
    valid_keys = await get_device_keys()
    log.info("В БД найдено %s устройств", len(valid_keys))

    servers = await get_active_servers()
    log.info("Активных серверов: %s", len(servers))

    total_orphans = 0
    total_deleted = 0
    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            log.info("=== %s ===", server["name"])
            orphans, deleted = await process_server(client, server, valid_keys, apply)
            total_orphans += orphans
            total_deleted += deleted

    if apply:
        log.info("Итого: помечено %s осиротевших, удалено %s", total_orphans, total_deleted)
    else:
        log.info("Итого (dry-run): %s осиротевших. Перезапусти с --apply, чтобы удалить.",
                 total_orphans)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="Реально удалять. Без флага — только печать (dry-run).")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
