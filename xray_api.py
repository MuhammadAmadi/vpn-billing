"""Работа с 3x-ui панелями: добавление/обновление/удаление клиентов, сборка vless-ссылок."""

import asyncio
import json
import logging
import urllib.parse
import warnings

import httpx

from bypass_store import get_active_bypass_ips
from error_log import log_error
from server_store import get_active_servers

warnings.filterwarnings("ignore", message="Unverified HTTPS request")
log = logging.getLogger(__name__)

HTTP_TIMEOUT = 10.0
PRIVATE_MARK = "| PRIVATE"
BYPASS_MARK = "BYPASS"
PREMIUM_MARKS = ("| VIP", "| PRO")


def _base_url(server: dict) -> str:
    return f"{server['scheme']}://{server['host']}:{server['port']}/{server['base_path'].strip('/')}"


def _is_private(remark: str) -> bool:
    return PRIVATE_MARK in remark.upper()


def _is_premium(remark: str) -> bool:
    upper = remark.upper()
    return any(m in upper for m in PREMIUM_MARKS)


def _skip_for_tariff(remark: str, tariff: str) -> bool:
    if _is_private(remark):
        return True
    return tariff == "Standard" and _is_premium(remark)


async def get_session_cookie(client: httpx.AsyncClient, server: dict) -> dict | None:
    base_url = _base_url(server)
    try:
        resp = await client.post(
            f"{base_url}/login",
            json={"username": server["login"], "password": server["password"]},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200 and resp.json().get("success"):
            log.debug("Авторизация на %s успешна", server["name"])
            return dict(resp.cookies)
        log.warning("Авторизация на %s не удалась (HTTP %s)", server["name"], resp.status_code)
        await log_error(
            f"Авторизация не удалась (HTTP {resp.status_code})",
            source="xray_api", server_name=server.get("name"),
            level="error", details=resp.text[:500],
        )
    except Exception as e:
        log.error("Ошибка авторизации на %s: %s", server["name"], e)
        await log_error(
            "Сервер недоступен (ошибка авторизации)",
            source="xray_api", server_name=server.get("name"),
            level="error", details=f"{type(e).__name__}: {e}",
        )
    return None


def build_vless_link(client_uuid: str, email: str, server_host: str,
                     inbound: dict, custom_name: str | None = None) -> str:
    port = inbound.get("port", 443)
    remark = inbound.get("remark", "").strip()
    stream = json.loads(inbound.get("streamSettings", "{}"))

    external_proxy = stream.get("externalProxy", [])
    if external_proxy:
        ep = external_proxy[0]
        server_host = ep.get("dest", server_host)
        port = ep.get("port", port)
        use_tls = port == 443
    else:
        use_tls = False

    network = stream.get("network", "tcp")
    security = stream.get("security", "none")

    params = {
        "type": network,
        "security": "tls" if use_tls else security,
        "flow": "xtls-rprx-vision",
    }
    if use_tls:
        params["sni"] = server_host

    if security == "reality":
        reality = stream.get("realitySettings", {})
        reality_settings = reality.get("settings", {})
        params["pbk"] = reality_settings.get("publicKey", "")
        params["fp"] = reality_settings.get("fingerprint", "chrome")
        server_names = reality.get("serverNames", [])
        params["sni"] = server_names[0] if server_names else ""
        short_ids = reality.get("shortIds", [""])
        params["sid"] = short_ids[0] if short_ids else ""
    elif security == "tls":
        tls = stream.get("tlsSettings", {})
        params["sni"] = tls.get("serverName", "")
        params["fp"] = "chrome"

    if network == "xhttp":
        xhttp = stream.get("xhttpSettings", {})
        params["path"] = xhttp.get("path", "/")
        host_val = xhttp.get("host", "").strip()
        params["host"] = host_val if host_val else server_host
    elif network == "ws":
        ws = stream.get("wsSettings", {})
        params["path"] = ws.get("path", "/")
        params["host"] = ws.get("headers", {}).get("Host", server_host)
    elif network == "grpc":
        params["serviceName"] = stream.get("grpcSettings", {}).get("serviceName", "")
    elif network == "tcp":
        header = stream.get("tcpSettings", {}).get("header", {})
        if header.get("type") == "http":
            paths = header.get("request", {}).get("path", ["/"])
            params["path"] = paths[0] if paths else "/"

    display_name = custom_name or (remark if remark else "Server")
    return (
        f"vless://{client_uuid}@{server_host}:{port}"
        f"?{urllib.parse.urlencode(params)}#{urllib.parse.quote(display_name)}"
    )


def _client_payload(uuid: str, email: str) -> dict:
    return {
        "clients": [{
            "id": uuid, "email": email,
            "limitIp": 0, "totalGB": 0, "expiryTime": 0,
            "enable": True, "flow": "xtls-rprx-vision",
        }]
    }


def _resolve_bypass_host(remark: str, server_host: str,
                         bypass_ips: list[str], counter: int) -> tuple[str, str | None, int]:
    """Если inbound помечен как BYPASS — возвращает (адрес из списка, имя 'ОБХОД N', новый счётчик)."""
    if BYPASS_MARK not in remark.upper():
        return server_host, None, counter
    host = bypass_ips[counter % len(bypass_ips)] if bypass_ips else server_host
    return host, f"ОБХОД {counter + 1}", counter + 1


async def _fetch_inbounds(client: httpx.AsyncClient, base_url: str,
                          cookies: dict, server_name: str) -> list[dict]:
    headers = {"Accept": "application/json"}
    resp = await client.get(
        f"{base_url}/panel/api/inbounds/list",
        headers=headers, cookies=cookies, timeout=HTTP_TIMEOUT,
    )
    inbounds = resp.json().get("obj", [])
    log.debug("%s: найдено %s inbound(ов)", server_name, len(inbounds))
    return inbounds


async def _add_client(client: httpx.AsyncClient, base_url: str, cookies: dict,
                      inbound_id: int, uuid: str, email: str) -> bool:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    payload = {"id": inbound_id, "settings": json.dumps(_client_payload(uuid, email))}
    resp = await client.post(
        f"{base_url}/panel/api/inbounds/addClient",
        headers=headers, cookies=cookies, json=payload,
    )
    return resp.status_code == 200 and resp.json().get("success")


async def add_client_to_all_servers(client_uuid: str, email: str,
                                    tariff: str = "Standard") -> list[str]:
    gathered_links: list[str] = []
    bypass_counter = 0
    bypass_ips = await get_active_bypass_ips()
    servers = await get_active_servers()

    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = _base_url(server)
            cookies = await get_session_cookie(client, server)
            if not cookies:
                log.warning("Пропускаю %s — нет авторизации", server["name"])
                continue
            try:
                inbounds = await _fetch_inbounds(client, base_url, cookies, server["name"])
                for inbound in inbounds:
                    remark = inbound.get("remark", "")
                    if _skip_for_tariff(remark, tariff):
                        continue
                    inbound_id = inbound.get("id")
                    unique_email = f"{email}_i{inbound_id}"
                    if not await _add_client(client, base_url, cookies, inbound_id,
                                             client_uuid, unique_email):
                        await log_error(
                            "addClient вернул ошибку",
                            source="xray_api", server_name=server.get("name"),
                            level="warning", details=f"inbound={inbound_id}",
                        )
                        continue
                    host, custom_name, bypass_counter = _resolve_bypass_host(
                        remark, server["host"], bypass_ips, bypass_counter,
                    )
                    link = build_vless_link(client_uuid, unique_email, host, inbound, custom_name)
                    gathered_links.append(link)
            except Exception as e:
                log.error("Ошибка Xray на %s: %s", server["name"], e)
                await log_error(
                    "Ошибка при добавлении клиента",
                    source="xray_api", server_name=server.get("name"),
                    level="error", details=f"{type(e).__name__}: {e}",
                )

    log.info("Собрано ссылок: %s", len(gathered_links))
    return gathered_links


async def remove_client_from_all_servers(client_uuid: str) -> None:
    servers = await get_active_servers()
    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = _base_url(server)
            cookies = await get_session_cookie(client, server)
            if not cookies:
                continue
            try:
                inbounds = await _fetch_inbounds(client, base_url, cookies, server["name"])
                for inbound in inbounds:
                    inbound_id = inbound["id"]
                    real_uuid = next(
                        (s.get("uuid") for s in inbound.get("clientStats", [])
                         if s.get("uuid") == client_uuid),
                        None,
                    )
                    if not real_uuid:
                        continue
                    await client.post(
                        f"{base_url}/panel/api/inbounds/{inbound_id}/delClient/{real_uuid}",
                        headers={"Accept": "application/json"}, cookies=cookies,
                    )
                    log.debug("delClient %s inbound=%s", server["name"], inbound_id)
            except Exception as e:
                log.error("Ошибка удаления Xray на %s: %s", server["name"], e)
                await log_error(
                    "Ошибка при удалении клиента",
                    source="xray_api", server_name=server.get("name"),
                    level="error", details=f"{type(e).__name__}: {e}",
                )


async def update_client_uuid_on_all_servers(old_uuid: str, new_uuid: str, email: str) -> None:
    servers = await get_active_servers()
    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = _base_url(server)
            cookies = await get_session_cookie(client, server)
            if not cookies:
                continue
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            try:
                inbounds = await _fetch_inbounds(client, base_url, cookies, server["name"])
                for inbound in inbounds:
                    remark = inbound.get("remark", "")
                    if _is_private(remark) or _is_premium(remark):
                        continue
                    inbound_id = inbound["id"]
                    unique_email = f"{email}_i{inbound_id}"
                    real_uuid = next(
                        (s.get("uuid") for s in inbound.get("clientStats", [])
                         if s.get("email") == unique_email),
                        None,
                    )
                    payload = {"id": inbound_id, "settings": json.dumps(_client_payload(new_uuid, unique_email))}
                    if not real_uuid:
                        log.info("%s: %s не найден, добавляю заново", server["name"], unique_email)
                        await client.post(
                            f"{base_url}/panel/api/inbounds/addClient",
                            headers=headers, cookies=cookies, json=payload,
                        )
                        continue
                    await client.post(
                        f"{base_url}/panel/api/inbounds/updateClient/{real_uuid}",
                        headers=headers, cookies=cookies,
                        data=json.dumps(payload), timeout=HTTP_TIMEOUT,
                    )
                    log.debug("updateClient %s inbound=%s: %s → %s",
                              server["name"], inbound_id, real_uuid, new_uuid)
            except Exception as e:
                log.error("Ошибка updateClient на %s: %s", server["name"], e)
                await log_error(
                    "Ошибка при обновлении ключа клиента",
                    source="xray_api", server_name=server.get("name"),
                    level="error", details=f"{type(e).__name__}: {e}",
                )


async def get_client_links_from_all_servers(client_uuid: str, email: str,
                                            tariff: str = "Standard") -> list[str]:
    gathered_links: list[str] = []
    bypass_counter = 0
    bypass_ips = await get_active_bypass_ips()
    servers = await get_active_servers()

    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = _base_url(server)
            cookies = await get_session_cookie(client, server)
            if not cookies:
                continue
            try:
                inbounds = await _fetch_inbounds(client, base_url, cookies, server["name"])
                for inbound in inbounds:
                    remark = inbound.get("remark", "")
                    if _skip_for_tariff(remark, tariff):
                        continue
                    inbound_id = inbound["id"]
                    unique_email = f"{email}_i{inbound_id}"
                    settings = json.loads(inbound.get("settings", "{}"))
                    clients = settings.get("clients", [])
                    real_uuid = next(
                        (c.get("id") for c in clients if c.get("email") == unique_email),
                        None,
                    )

                    if not real_uuid:
                        log.info("%s: %s не найден на сервере, добавляю", server["name"], unique_email)
                        if not await _add_client(client, base_url, cookies, inbound_id,
                                                 client_uuid, unique_email):
                            await log_error(
                                "Авто-добавление клиента не удалось",
                                source="xray_api", server_name=server.get("name"),
                                level="warning", details=f"inbound={inbound_id}",
                            )
                            continue
                        real_uuid = client_uuid

                    host, custom_name, bypass_counter = _resolve_bypass_host(
                        remark, server["host"], bypass_ips, bypass_counter,
                    )
                    link = build_vless_link(client_uuid, unique_email, host, inbound, custom_name)
                    gathered_links.append(link)
            except Exception as e:
                log.error("Ошибка getLinks на %s: %s", server["name"], e)
                await log_error(
                    "Ошибка при получении ссылок",
                    source="xray_api", server_name=server.get("name"),
                    level="error", details=f"{type(e).__name__}: {e}",
                )

    return gathered_links


async def test_server(server: dict) -> tuple[bool, str]:
    """Залогиниться и получить inbound'ы — для кнопки 'Тест' в админ-панели."""
    base_url = _base_url(server)
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.post(
                f"{base_url}/login",
                json={"username": server["login"], "password": server["password"]},
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code != 200 or not resp.json().get("success"):
                return False, f"Авторизация не удалась (HTTP {resp.status_code})"
            cookies = dict(resp.cookies)
            lst = await client.get(
                f"{base_url}/panel/api/inbounds/list",
                headers={"Accept": "application/json"}, cookies=cookies, timeout=HTTP_TIMEOUT,
            )
            inbounds = lst.json().get("obj", [])
            return True, f"OK — доступен, inbound'ов: {len(inbounds)}"
    except Exception as e:
        return False, f"Недоступен: {type(e).__name__}: {e}"


async def test_servers_parallel(servers: list[dict]) -> list[dict]:
    """Параллельно проверяет список серверов. Возвращает [{id, ok, msg}, ...]."""
    if not servers:
        return []
    results = await asyncio.gather(
        *(test_server(s) for s in servers), return_exceptions=True,
    )
    out = []
    for s, r in zip(servers, results):
        if isinstance(r, Exception):
            ok, msg = False, f"{type(r).__name__}: {r}"
        else:
            ok, msg = r
        out.append({"id": s.get("id"), "ok": ok, "msg": msg})
    return out
