"""Работа с 3x-ui панелями: регистрация/обновление/удаление клиентов, сборка vless-ссылок.

Использует новый API под /panel/api/clients/* (3x-ui, версия с клиент-сущностями).
Bearer-токен указывается в админ-панели сервера. Cookie-авторизация (login/password)
работает как фолбэк, если токен пустой.
"""

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


def _is_bypass(remark: str) -> bool:
    return BYPASS_MARK in remark.upper()


def _skip_for_tariff(remark: str, tariff: str) -> bool:
    if _is_private(remark):
        return True
    return tariff == "Standard" and _is_premium(remark)


def _should_skip_inbound(remark: str, tariff: str, bypass_ips: list[str]) -> bool:
    """True — этот inbound нужно полностью пропустить (учитывает тариф и наличие bypass-IP)."""
    if _skip_for_tariff(remark, tariff):
        return True
    if _is_bypass(remark) and not bypass_ips:
        return True
    return False


def _parse_json_field(value, default=None):
    """settings/streamSettings может прийти JSON-строкой (старый формат) или dict-ом (новый)."""
    if default is None:
        default = {}
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return default
    return default


def _safe_json(resp) -> dict:
    """resp.json() с проглатыванием ошибок парсинга — всегда отдаёт dict."""
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")


async def get_auth_kwargs(client: httpx.AsyncClient, server: dict) -> dict | None:
    """Возвращает {'headers': {...}, 'cookies': {...}} или None при ошибке."""
    if server.get("api_token"):
        return {
            "headers": {
                "Accept": "application/json",
                "Authorization": f"Bearer {server['api_token']}",
            },
            "cookies": None,
        }

    base_url = _base_url(server)
    try:
        resp = await client.post(
            f"{base_url}/login",
            json={"username": server.get("login", ""), "password": server.get("password", "")},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200 and _safe_json(resp).get("success"):
            log.debug("Авторизация на %s успешна", server["name"])
            return {"headers": {"Accept": "application/json"}, "cookies": dict(resp.cookies)}
        log.warning("Авторизация на %s не удалась (HTTP %s)", server["name"], resp.status_code)
    except Exception as e:
        log.error("Ошибка авторизации на %s: %s", server["name"], e)
    return None


def build_vless_link(client_uuid: str, email: str, server_host: str,
                     inbound: dict, custom_name: str | None = None) -> str:
    port = inbound.get("port", 443)
    remark = inbound.get("remark", "").strip()
    stream = _parse_json_field(inbound.get("streamSettings"))

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
    """Универсальный payload клиента для /panel/api/clients/add и /update."""
    return {
        "id": uuid,
        "email": email,
        "totalGB": 0,
        "expiryTime": 0,
        "tgId": 0,
        "limitIp": 0,
        "enable": True,
        "flow": "xtls-rprx-vision",
    }


def _resolve_bypass_host(remark: str, server_host: str,
                         bypass_ips: list[str], counter: int) -> tuple[str, str | None, int]:
    """Если inbound помечен как BYPASS — возвращает (IP из списка, имя 'ОБХОД N', новый счётчик).
    Пустой bypass_ips сюда не должен попасть (отсеивается в _should_skip_inbound)."""
    if not _is_bypass(remark):
        return server_host, None, counter
    if not bypass_ips:
        return server_host, None, counter
    host = bypass_ips[counter % len(bypass_ips)]
    return host, f"ОБХОД {counter + 1}", counter + 1


async def _fetch_inbounds(client: httpx.AsyncClient, base_url: str,
                          auth_kwargs: dict, server_name: str) -> list[dict]:
    """Получает список inbound'ов с clientStats. Логирует не-200 ответы."""
    url = f"{base_url}/panel/api/inbounds/list"
    resp = await client.get(url, timeout=HTTP_TIMEOUT, **auth_kwargs)
    if resp.status_code != 200:
        log.warning("%s: inbounds/list вернул HTTP %s (url=%s)",
                    server_name, resp.status_code, url)
        return []
    inbounds = _safe_json(resp).get("obj") or []
    log.debug("%s: найдено %s inbound(ов)", server_name, len(inbounds))
    return inbounds


def _has_client_with_email(inbounds: list[dict], email: str) -> bool:
    """True — на одной из inbound'ов уже есть клиент с таким email."""
    for inb in inbounds:
        for stat in inb.get("clientStats") or []:
            if stat.get("email") == email:
                return True
        settings = _parse_json_field(inb.get("settings"))
        for c in settings.get("clients") or []:
            if c.get("email") == email:
                return True
    return False


def _emails_with_uuid(inbounds: list[dict], client_uuid: str) -> set[str]:
    """Возвращает все email'ы клиентов с указанным UUID (clientStats + settings.clients).
    Нужно, чтобы при удалении устройства убрать и legacy-клиентов со старым `_iN`-суффиксом."""
    emails: set[str] = set()
    for inb in inbounds:
        for stat in inb.get("clientStats") or []:
            if stat.get("uuid") == client_uuid:
                email = stat.get("email")
                if email:
                    emails.add(email)
        settings = _parse_json_field(inb.get("settings"))
        for c in settings.get("clients") or []:
            if c.get("id") == client_uuid:
                email = c.get("email")
                if email:
                    emails.add(email)
    return emails


async def _add_client(client: httpx.AsyncClient, base_url: str, auth_kwargs: dict,
                      uuid: str, email: str, inbound_ids: list[int]) -> tuple[bool, str]:
    """POST /panel/api/clients/add — регистрирует клиента и привязывает к inbound'ам.
    Возвращает (ok, error_message)."""
    if not inbound_ids:
        return False, "пустой список inboundIds"
    url = f"{base_url}/panel/api/clients/add"
    payload = {"client": _client_payload(uuid, email), "inboundIds": inbound_ids}
    try:
        resp = await client.post(url, json=payload, timeout=HTTP_TIMEOUT, **auth_kwargs)
    except Exception as e:
        return False, f"{type(e).__name__}: {e} (url={url})"

    data = _safe_json(resp)
    if resp.status_code == 200 and data.get("success", False):
        return True, ""
    panel_msg = data.get("msg") or data.get("message") or (resp.text or "")[:200]
    return False, f"HTTP {resp.status_code} {panel_msg!r}"


async def _update_client(client: httpx.AsyncClient, base_url: str, auth_kwargs: dict,
                         email: str, new_uuid: str) -> tuple[bool, str]:
    """POST /panel/api/clients/update/:email — заменяет конфиг клиента (в т.ч. UUID)."""
    url = f"{base_url}/panel/api/clients/update/{_quote(email)}"
    payload = _client_payload(new_uuid, email)
    try:
        resp = await client.post(url, json=payload, timeout=HTTP_TIMEOUT, **auth_kwargs)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    data = _safe_json(resp)
    if resp.status_code == 200 and data.get("success", False):
        return True, ""
    panel_msg = data.get("msg") or data.get("message") or (resp.text or "")[:200]
    return False, f"HTTP {resp.status_code} {panel_msg!r}"


async def _delete_client(client: httpx.AsyncClient, base_url: str, auth_kwargs: dict,
                         email: str) -> tuple[bool, str]:
    """POST /panel/api/clients/del/:email — удаляет клиента со всех привязанных inbound'ов."""
    url = f"{base_url}/panel/api/clients/del/{_quote(email)}"
    try:
        resp = await client.post(url, timeout=HTTP_TIMEOUT, **auth_kwargs)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    data = _safe_json(resp)
    if resp.status_code == 200 and data.get("success", False):
        return True, ""
    panel_msg = data.get("msg") or data.get("message") or (resp.text or "")[:200]
    return False, f"HTTP {resp.status_code} {panel_msg!r}"


def _eligible_inbounds(inbounds: list[dict], tariff: str, bypass_ips: list[str]) -> list[dict]:
    return [
        inb for inb in inbounds
        if not _should_skip_inbound(inb.get("remark", ""), tariff, bypass_ips)
    ]


def _build_links(client_uuid: str, email: str, server: dict, eligible: list[dict],
                 bypass_ips: list[str], bypass_counter: int) -> tuple[list[str], int]:
    """Собирает vless-ссылки для всех eligible inbound'ов, возвращает (links, обновлённый счётчик)."""
    links: list[str] = []
    target_host = server.get("client_host") or server["host"]
    for inbound in eligible:
        remark = inbound.get("remark", "")
        host, custom_name, bypass_counter = _resolve_bypass_host(
            remark, target_host, bypass_ips, bypass_counter,
        )
        links.append(build_vless_link(client_uuid, email, host, inbound, custom_name))
    return links, bypass_counter


async def add_client_to_all_servers(client_uuid: str, email: str,
                                    tariff: str = "Standard") -> list[str]:
    """Регистрирует клиента на всех серверах одним POST /clients/add на сервер
    (привязка ко всем подходящим inbound'ам сразу) и возвращает список ссылок."""
    gathered_links: list[str] = []
    bypass_counter = 0
    bypass_ips = await get_active_bypass_ips()
    servers = await get_active_servers()

    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = _base_url(server)
            auth_kwargs = await get_auth_kwargs(client, server)
            if not auth_kwargs:
                log.warning("Пропускаю %s — нет авторизации", server["name"])
                continue
            try:
                inbounds = await _fetch_inbounds(client, base_url, auth_kwargs, server["name"])
                eligible = _eligible_inbounds(inbounds, tariff, bypass_ips)
                if not eligible:
                    continue

                inbound_ids = [inb["id"] for inb in eligible]
                ok, err = await _add_client(client, base_url, auth_kwargs,
                                            client_uuid, email, inbound_ids)
                if not ok:
                    await log_error(
                        "Не удалось зарегистрировать клиента",
                        source="xray_api", server_name=server.get("name"),
                        level="warning", details=f"inbounds={inbound_ids}: {err}",
                    )
                    continue

                links, bypass_counter = _build_links(
                    client_uuid, email, server, eligible, bypass_ips, bypass_counter,
                )
                gathered_links.extend(links)
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
    """Удаляет все клиенты с указанным UUID со всех серверов.
    Ищет email'ы в clientStats/settings.clients — поддерживает и legacy-формат
    с `_iN`-суффиксами в email'е (несколько записей на одно устройство)."""
    servers = await get_active_servers()
    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = _base_url(server)
            auth_kwargs = await get_auth_kwargs(client, server)
            if not auth_kwargs:
                continue
            try:
                inbounds = await _fetch_inbounds(client, base_url, auth_kwargs, server["name"])
                emails = _emails_with_uuid(inbounds, client_uuid)
                for email in emails:
                    ok, err = await _delete_client(client, base_url, auth_kwargs, email)
                    if ok:
                        log.debug("delClient %s email=%s", server["name"], email)
                    else:
                        log.info("%s: delete %s: %s", server["name"], email, err)
            except Exception as e:
                log.error("Ошибка удаления Xray на %s: %s", server["name"], e)
                await log_error(
                    "Ошибка при удалении клиента",
                    source="xray_api", server_name=server.get("name"),
                    level="error", details=f"{type(e).__name__}: {e}",
                )


async def update_client_uuid_on_all_servers(old_uuid: str, new_uuid: str, email: str,
                                            tariff: str = "Standard") -> None:
    """Меняет UUID клиента на всех серверах. old_uuid сохранён в сигнатуре
    для совместимости с вызовами — фактически используется email для поиска.
    Если клиента ещё нет (новая панель / устройство переезжает) — фолбэк на add."""
    bypass_ips = await get_active_bypass_ips()
    servers = await get_active_servers()
    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = _base_url(server)
            auth_kwargs = await get_auth_kwargs(client, server)
            if not auth_kwargs:
                continue
            try:
                ok, err = await _update_client(client, base_url, auth_kwargs, email, new_uuid)
                if ok:
                    log.debug("updateClient %s email=%s → uuid=%s",
                              server["name"], email, new_uuid)
                    continue

                log.info("%s: update %s не удался (%s) — пробую add", server["name"], email, err)
                inbounds = await _fetch_inbounds(client, base_url, auth_kwargs, server["name"])
                eligible = _eligible_inbounds(inbounds, tariff, bypass_ips)
                if not eligible:
                    continue
                inbound_ids = [inb["id"] for inb in eligible]
                ok2, err2 = await _add_client(
                    client, base_url, auth_kwargs, new_uuid, email, inbound_ids,
                )
                if not ok2:
                    await log_error(
                        "Не удалось обновить ключ клиента",
                        source="xray_api", server_name=server.get("name"),
                        level="warning", details=f"update_err={err}; add_err={err2}",
                    )
            except Exception as e:
                log.error("Ошибка updateClient на %s: %s", server["name"], e)
                await log_error(
                    "Ошибка при обновлении ключа клиента",
                    source="xray_api", server_name=server.get("name"),
                    level="error", details=f"{type(e).__name__}: {e}",
                )


async def get_client_links_from_all_servers(client_uuid: str, email: str,
                                            tariff: str = "Standard") -> list[str]:
    """Собирает текущие ссылки для клиента. Если клиента ещё нет на сервере —
    регистрирует одним вызовом /clients/add."""
    gathered_links: list[str] = []
    bypass_counter = 0
    bypass_ips = await get_active_bypass_ips()
    servers = await get_active_servers()

    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = _base_url(server)
            auth_kwargs = await get_auth_kwargs(client, server)
            if not auth_kwargs:
                continue
            try:
                inbounds = await _fetch_inbounds(client, base_url, auth_kwargs, server["name"])
                eligible = _eligible_inbounds(inbounds, tariff, bypass_ips)
                if not eligible:
                    continue

                if not _has_client_with_email(inbounds, email):
                    log.info("%s: %s не найден, добавляю", server["name"], email)
                    inbound_ids = [inb["id"] for inb in eligible]
                    ok, err = await _add_client(
                        client, base_url, auth_kwargs, client_uuid, email, inbound_ids,
                    )
                    if not ok:
                        await log_error(
                            "Авто-добавление клиента не удалось",
                            source="xray_api", server_name=server.get("name"),
                            level="warning", details=f"inbounds={inbound_ids}: {err}",
                        )
                        continue

                links, bypass_counter = _build_links(
                    client_uuid, email, server, eligible, bypass_ips, bypass_counter,
                )
                gathered_links.extend(links)
            except Exception as e:
                log.error("Ошибка getLinks на %s: %s", server["name"], e)
                await log_error(
                    "Ошибка при получении ссылок",
                    source="xray_api", server_name=server.get("name"),
                    level="error", details=f"{type(e).__name__}: {e}",
                )

    return gathered_links


async def test_server(server: dict) -> tuple[bool, str]:
    base_url = _base_url(server)
    try:
        async with httpx.AsyncClient(verify=False) as client:
            auth_kwargs = await get_auth_kwargs(client, server)
            if not auth_kwargs:
                return False, "Авторизация не удалась (проверьте логин/пароль или токен)"

            lst = await client.get(
                f"{base_url}/panel/api/inbounds/list",
                timeout=HTTP_TIMEOUT,
                **auth_kwargs,
            )
            data = _safe_json(lst)
            if lst.status_code == 200 and data.get("success"):
                inbounds = data.get("obj") or []
                return True, f"OK — доступен, inbound'ов: {len(inbounds)}"
            return False, f"Ошибка API (HTTP {lst.status_code})"
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
