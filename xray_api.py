"""Работа с 3x-ui панелями.

Поддерживает два API-режима:
  • v3.2+  — клиенты как самостоятельная сущность под /panel/api/clients/*,
             один клиент на устройство, привязан к нескольким inbound'ам.
  • legacy — старые панели до 3.2, клиент на каждый inbound отдельно,
             email с суффиксом _iN, эндпоинты /panel/api/inbounds/{addClient,…}.

Режим определяется автоматически при первом обращении к каждому серверу
(проба `GET /panel/api/clients/list`) и кешируется на жизнь процесса.

Хост для vless-ссылки резолвится в порядке: override → externalProxy → server.host.
BYPASS-логика (старая, на bypass_ips) убрана — её заменяет per-inbound override.
"""

import asyncio
import json
import logging
import urllib.parse
import warnings

import httpx

from error_log import log_error
from inbound_overrides_store import get_map as get_overrides_map
from server_store import get_active_servers

warnings.filterwarnings("ignore", message="Unverified HTTPS request")
log = logging.getLogger(__name__)

HTTP_TIMEOUT = 10.0
PRIVATE_MARK = "| PRIVATE"
PREMIUM_MARKS = ("| VIP", "| PRO")

# ────────── REST API ──────────
URL_LOGIN = "/login"
URL_INBOUNDS_LIST = "/panel/api/inbounds/list"

# v3.2+: клиенты — самостоятельная сущность
URL_CLIENT_ADD = "/panel/api/clients/add"
URL_CLIENT_UPDATE = "/panel/api/clients/update/{email}"
URL_CLIENT_DELETE = "/panel/api/clients/del/{email}"

# legacy: per-inbound клиенты
URL_LEGACY_ADD = "/panel/api/inbounds/addClient"
URL_LEGACY_UPDATE = "/panel/api/inbounds/updateClient/{uuid}"
URL_LEGACY_DELETE = "/panel/api/inbounds/{inbound_id}/delClient/{uuid}"

MODE_V32 = "v3.2"
MODE_LEGACY = "legacy"

# server_id → mode. Лениво заполняется при первом обращении.
_MODE_CACHE: dict[int, str] = {}


# ────────────── Утилиты ──────────────
def _base_url(server: dict) -> str:
    return f"{server['scheme']}://{server['host']}:{server['port']}/{server['base_path'].strip('/')}"


def _is_private(remark: str) -> bool:
    return PRIVATE_MARK in remark.upper()


def _is_premium(remark: str) -> bool:
    upper = remark.upper()
    return any(m in upper for m in PREMIUM_MARKS)


def _should_skip_inbound(remark: str, tariff: str) -> bool:
    """PRIVATE отсеивается всегда. Premium-маркированные inbound'ы — на Standard-тарифе."""
    if _is_private(remark):
        return True
    return tariff == "Standard" and _is_premium(remark)


def _parse_json_field(value, default=None):
    """settings/streamSettings может прийти JSON-строкой (старый формат) или dict-ом."""
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
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _quote(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


# ────────────── Авторизация ──────────────
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
            f"{base_url}{URL_LOGIN}",
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


# ────────────── Детект режима API ──────────────
async def _detect_mode(client: httpx.AsyncClient, base_url: str,
                       auth_kwargs: dict, server: dict) -> str:
    """v3.2 если /panel/api/clients/list отвечает 200 (даже с пустым списком),
    иначе legacy. Кешируется на жизнь процесса по server.id."""
    sid = server.get("id")
    if sid and sid in _MODE_CACHE:
        return _MODE_CACHE[sid]
    try:
        resp = await client.get(
            f"{base_url}/panel/api/clients/list",
            timeout=HTTP_TIMEOUT, **auth_kwargs,
        )
        mode = MODE_V32 if resp.status_code == 200 else MODE_LEGACY
    except Exception as e:
        log.warning("Detect режима для %s: %s — считаю legacy", server.get("name"), e)
        mode = MODE_LEGACY
    if sid:
        _MODE_CACHE[sid] = mode
    log.info("Панель %s: режим API = %s", server.get("name"), mode)
    return mode


def reset_mode_cache(server_id: int | None = None) -> None:
    """Сбросить кеш режима (после изменения сервера в админке или ручного тригера)."""
    if server_id is None:
        _MODE_CACHE.clear()
    else:
        _MODE_CACHE.pop(int(server_id), None)


# ────────────── Inbounds ──────────────
async def _fetch_inbounds(client: httpx.AsyncClient, base_url: str,
                          auth_kwargs: dict, server_name: str) -> list[dict]:
    url = f"{base_url}{URL_INBOUNDS_LIST}"
    resp = await client.get(url, timeout=HTTP_TIMEOUT, **auth_kwargs)
    if resp.status_code != 200:
        log.warning("%s: inbounds/list вернул HTTP %s", server_name, resp.status_code)
        return []
    inbounds = _safe_json(resp).get("obj") or []
    log.debug("%s: найдено %s inbound(ов)", server_name, len(inbounds))
    return inbounds


def _eligible_inbounds(inbounds: list[dict], tariff: str) -> list[dict]:
    return [
        inb for inb in inbounds
        if not _should_skip_inbound(inb.get("remark", ""), tariff)
    ]


def _has_email(inbounds: list[dict], email: str) -> bool:
    """True — на одной из inbound'ов уже есть клиент с этим email (любой режим)."""
    for inb in inbounds:
        for stat in inb.get("clientStats") or []:
            if stat.get("email") == email:
                return True
        settings = _parse_json_field(inb.get("settings"))
        for c in settings.get("clients") or []:
            if c.get("email") == email:
                return True
    return False


def _has_email_in_inbound(inb: dict, email: str) -> bool:
    for stat in inb.get("clientStats") or []:
        if stat.get("email") == email:
            return True
    settings = _parse_json_field(inb.get("settings"))
    for c in settings.get("clients") or []:
        if c.get("email") == email:
            return True
    return False


def _emails_with_uuid(inbounds: list[dict], client_uuid: str) -> set[str]:
    """Все email'ы клиентов с указанным UUID — для удаления (v3.2 и legacy)."""
    emails: set[str] = set()
    for inb in inbounds:
        for stat in inb.get("clientStats") or []:
            if stat.get("uuid") == client_uuid:
                e = stat.get("email")
                if e:
                    emails.add(e)
        settings = _parse_json_field(inb.get("settings"))
        for c in settings.get("clients") or []:
            if c.get("id") == client_uuid:
                e = c.get("email")
                if e:
                    emails.add(e)
    return emails


def _uuids_with_email(inb: dict, email: str) -> set[str]:
    """UUID'ы клиентов в указанном inbound с этим email — для legacy delClient/updateClient."""
    uuids: set[str] = set()
    for stat in inb.get("clientStats") or []:
        if stat.get("email") == email and stat.get("uuid"):
            uuids.add(stat["uuid"])
    settings = _parse_json_field(inb.get("settings"))
    for c in settings.get("clients") or []:
        if c.get("email") == email and c.get("id"):
            uuids.add(c["id"])
    return uuids


# ────────────── Payload клиента ──────────────
def _client_payload(uuid: str, email: str) -> dict:
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


# ────────────── v3.2 path ──────────────
async def _v32_add(client: httpx.AsyncClient, base_url: str, auth_kwargs: dict,
                   uuid: str, email: str, inbound_ids: list[int]) -> tuple[bool, str]:
    if not inbound_ids:
        return False, "пустой список inboundIds"
    payload = {"client": _client_payload(uuid, email), "inboundIds": inbound_ids}
    url = f"{base_url}{URL_CLIENT_ADD}"
    try:
        resp = await client.post(url, json=payload, timeout=HTTP_TIMEOUT, **auth_kwargs)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    data = _safe_json(resp)
    if resp.status_code == 200 and data.get("success", False):
        return True, ""
    msg = data.get("msg") or data.get("message") or (resp.text or "")[:200]
    return False, f"HTTP {resp.status_code} {msg!r}"


async def _v32_update(client: httpx.AsyncClient, base_url: str, auth_kwargs: dict,
                      email: str, new_uuid: str) -> tuple[bool, str]:
    url = f"{base_url}{URL_CLIENT_UPDATE.format(email=_quote(email))}"
    try:
        resp = await client.post(url, json=_client_payload(new_uuid, email),
                                 timeout=HTTP_TIMEOUT, **auth_kwargs)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    data = _safe_json(resp)
    if resp.status_code == 200 and data.get("success", False):
        return True, ""
    msg = data.get("msg") or data.get("message") or (resp.text or "")[:200]
    return False, f"HTTP {resp.status_code} {msg!r}"


async def _v32_delete(client: httpx.AsyncClient, base_url: str, auth_kwargs: dict,
                      email: str) -> tuple[bool, str]:
    url = f"{base_url}{URL_CLIENT_DELETE.format(email=_quote(email))}"
    try:
        resp = await client.post(url, timeout=HTTP_TIMEOUT, **auth_kwargs)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    data = _safe_json(resp)
    if resp.status_code == 200 and data.get("success", False):
        return True, ""
    msg = data.get("msg") or data.get("message") or (resp.text or "")[:200]
    return False, f"HTTP {resp.status_code} {msg!r}"


# ────────────── legacy path (3x-ui до 3.2) ──────────────
async def _legacy_add(client: httpx.AsyncClient, base_url: str, auth_kwargs: dict,
                      inbound_id: int, uuid: str, email: str) -> tuple[bool, str]:
    payload = {
        "id": inbound_id,
        "settings": json.dumps({"clients": [_client_payload(uuid, email)]}),
    }
    url = f"{base_url}{URL_LEGACY_ADD}"
    try:
        resp = await client.post(url, json=payload, timeout=HTTP_TIMEOUT, **auth_kwargs)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    data = _safe_json(resp)
    if resp.status_code == 200 and data.get("success", False):
        return True, ""
    msg = data.get("msg") or data.get("message") or (resp.text or "")[:200]
    return False, f"HTTP {resp.status_code} {msg!r}"


async def _legacy_update(client: httpx.AsyncClient, base_url: str, auth_kwargs: dict,
                         inbound_id: int, old_uuid: str, new_uuid: str,
                         email: str) -> tuple[bool, str]:
    payload = {
        "id": inbound_id,
        "settings": json.dumps({"clients": [_client_payload(new_uuid, email)]}),
    }
    url = f"{base_url}{URL_LEGACY_UPDATE.format(uuid=_quote(old_uuid))}"
    try:
        resp = await client.post(url, json=payload, timeout=HTTP_TIMEOUT, **auth_kwargs)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    data = _safe_json(resp)
    if resp.status_code == 200 and data.get("success", False):
        return True, ""
    msg = data.get("msg") or data.get("message") or (resp.text or "")[:200]
    return False, f"HTTP {resp.status_code} {msg!r}"


async def _legacy_delete(client: httpx.AsyncClient, base_url: str, auth_kwargs: dict,
                         inbound_id: int, uuid: str) -> tuple[bool, str]:
    url = f"{base_url}{URL_LEGACY_DELETE.format(inbound_id=inbound_id, uuid=_quote(uuid))}"
    try:
        resp = await client.post(url, timeout=HTTP_TIMEOUT, **auth_kwargs)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    data = _safe_json(resp)
    if resp.status_code == 200 and data.get("success", False):
        return True, ""
    msg = data.get("msg") or data.get("message") or (resp.text or "")[:200]
    return False, f"HTTP {resp.status_code} {msg!r}"


def _legacy_email(base_email: str, inbound_id: int) -> str:
    return f"{base_email}_i{inbound_id}"


# ────────────── Резолв host'а + сборка ссылки ──────────────
def _resolve_host_port(server: dict, inbound: dict,
                       overrides_map: dict[tuple[int, int], dict]) -> tuple[str, int, str | None]:
    """Возвращает (host, port, label).

    Приоритеты:
      1. inbound_host_overrides (если задан и домен активен) — host + опц. port + label.
      2. externalProxy из streamSettings (3x-ui сам подставляет внешний прокси).
      3. server.client_host или server.host (дефолт).
    """
    inbound_port = inbound.get("port", 443)
    sid = server.get("id")
    iid = inbound.get("id")
    if sid is not None and iid is not None:
        ov = overrides_map.get((sid, iid))
        if ov:
            return ov["host"], (ov.get("port") or inbound_port), ov.get("label")

    stream = _parse_json_field(inbound.get("streamSettings"))
    ext = stream.get("externalProxy") or []
    if ext:
        ep = ext[0]
        return ep.get("dest", server["host"]), ep.get("port", inbound_port), None

    return (server.get("client_host") or server["host"]), inbound_port, None


def build_vless_link(client_uuid: str, email: str, host: str, port: int,
                     inbound: dict, custom_name: str | None = None) -> str:
    """Собирает vless:// URL для одного inbound. host/port уже резолвены."""
    remark = inbound.get("remark", "").strip()
    stream = _parse_json_field(inbound.get("streamSettings"))
    network = stream.get("network", "tcp")
    security = stream.get("security", "none")

    params: dict[str, str] = {
        "type": network,
        "security": security,
        "flow": "xtls-rprx-vision",
    }

    if security == "reality":
        reality = stream.get("realitySettings", {})
        rs = reality.get("settings", {})
        params["pbk"] = rs.get("publicKey", "")
        params["fp"] = rs.get("fingerprint", "chrome")
        sns = reality.get("serverNames", [])
        params["sni"] = sns[0] if sns else ""
        sids = reality.get("shortIds", [""])
        params["sid"] = sids[0] if sids else ""
    elif security == "tls":
        tls = stream.get("tlsSettings", {})
        params["sni"] = tls.get("serverName", "") or host
        params["fp"] = "chrome"

    if network == "xhttp":
        xhttp = stream.get("xhttpSettings", {})
        params["path"] = xhttp.get("path", "/")
        h = (xhttp.get("host") or "").strip()
        params["host"] = h or host
    elif network == "ws":
        ws = stream.get("wsSettings", {})
        params["path"] = ws.get("path", "/")
        params["host"] = ws.get("headers", {}).get("Host", host)
    elif network == "grpc":
        params["serviceName"] = stream.get("grpcSettings", {}).get("serviceName", "")
    elif network == "tcp":
        header = stream.get("tcpSettings", {}).get("header", {})
        if header.get("type") == "http":
            paths = header.get("request", {}).get("path", ["/"])
            params["path"] = paths[0] if paths else "/"

    display_name = custom_name or (remark if remark else "Server")
    return (
        f"vless://{client_uuid}@{host}:{port}"
        f"?{urllib.parse.urlencode(params)}#{urllib.parse.quote(display_name)}"
    )


def _build_links(client_uuid: str, email: str, server: dict,
                 eligible: list[dict], overrides: dict) -> list[str]:
    out: list[str] = []
    for inb in eligible:
        host, port, label = _resolve_host_port(server, inb, overrides)
        out.append(build_vless_link(client_uuid, email, host, port, inb, label))
    return out


# ────────────── Публичный API (оркестрация по серверам) ──────────────
async def add_client_to_all_servers(client_uuid: str, email: str,
                                    tariff: str = "Standard") -> list[str]:
    """Регистрирует клиента на каждом активном сервере и возвращает список vless-ссылок."""
    gathered: list[str] = []
    overrides = await get_overrides_map()
    servers = await get_active_servers()

    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = _base_url(server)
            auth = await get_auth_kwargs(client, server)
            if not auth:
                log.warning("Пропускаю %s — нет авторизации", server["name"])
                continue
            try:
                mode = await _detect_mode(client, base_url, auth, server)
                inbounds = await _fetch_inbounds(client, base_url, auth, server["name"])
                eligible = _eligible_inbounds(inbounds, tariff)
                if not eligible:
                    continue

                if mode == MODE_V32:
                    ok, err = await _v32_add(
                        client, base_url, auth, client_uuid, email,
                        [inb["id"] for inb in eligible],
                    )
                    if not ok:
                        await log_error(
                            "Не удалось зарегистрировать клиента (v3.2)",
                            source="xray_api", server_name=server.get("name"),
                            level="warning", details=err,
                        )
                        continue
                else:
                    # legacy: по одному на каждый inbound, email с суффиксом
                    fails: list[str] = []
                    for inb in eligible:
                        e = _legacy_email(email, inb["id"])
                        ok, err = await _legacy_add(
                            client, base_url, auth, inb["id"], client_uuid, e,
                        )
                        if not ok:
                            fails.append(f"inbound={inb['id']}: {err}")
                    if fails:
                        await log_error(
                            "Часть addClient'ов не удалась (legacy)",
                            source="xray_api", server_name=server.get("name"),
                            level="warning", details="; ".join(fails[:5]),
                        )

                gathered.extend(_build_links(client_uuid, email, server, eligible, overrides))
            except Exception as e:
                log.error("Ошибка Xray на %s: %s", server["name"], e)
                await log_error(
                    "Ошибка при добавлении клиента",
                    source="xray_api", server_name=server.get("name"),
                    level="error", details=f"{type(e).__name__}: {e}",
                )

    log.info("Собрано ссылок: %s", len(gathered))
    return gathered


async def remove_client_from_all_servers(client_uuid: str) -> None:
    """Удаляет все клиенты с этим UUID со всех серверов (v3.2 + legacy)."""
    servers = await get_active_servers()
    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = _base_url(server)
            auth = await get_auth_kwargs(client, server)
            if not auth:
                continue
            try:
                mode = await _detect_mode(client, base_url, auth, server)
                inbounds = await _fetch_inbounds(client, base_url, auth, server["name"])

                if mode == MODE_V32:
                    # Один клиент на устройство — собираем все email'ы с этим UUID и удаляем
                    for e in _emails_with_uuid(inbounds, client_uuid):
                        ok, err = await _v32_delete(client, base_url, auth, e)
                        if not ok:
                            log.info("%s: delete %s: %s", server["name"], e, err)
                else:
                    # legacy: на каждый inbound где этот UUID — отдельный delClient
                    for inb in inbounds:
                        if any(
                            (s.get("uuid") == client_uuid) for s in (inb.get("clientStats") or [])
                        ) or any(
                            (c.get("id") == client_uuid)
                            for c in _parse_json_field(inb.get("settings")).get("clients", [])
                        ):
                            await _legacy_delete(client, base_url, auth, inb["id"], client_uuid)
            except Exception as e:
                log.error("Ошибка удаления на %s: %s", server["name"], e)
                await log_error(
                    "Ошибка при удалении клиента",
                    source="xray_api", server_name=server.get("name"),
                    level="error", details=f"{type(e).__name__}: {e}",
                )


async def update_client_uuid_on_all_servers(old_uuid: str, new_uuid: str, email: str,
                                            tariff: str = "Standard") -> None:
    """Меняет UUID клиента на всех серверах. Если клиента нет — добавляет с новым UUID."""
    overrides = await get_overrides_map()  # пока не нужен здесь, но единая сигнатура
    _ = overrides  # noqa: F841 - на будущее
    servers = await get_active_servers()
    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = _base_url(server)
            auth = await get_auth_kwargs(client, server)
            if not auth:
                continue
            try:
                mode = await _detect_mode(client, base_url, auth, server)
                inbounds = await _fetch_inbounds(client, base_url, auth, server["name"])
                eligible = _eligible_inbounds(inbounds, tariff)
                if not eligible:
                    continue

                if mode == MODE_V32:
                    ok, err = await _v32_update(client, base_url, auth, email, new_uuid)
                    if not ok:
                        log.info("%s: update %s не удался (%s) — пробую add",
                                 server["name"], email, err)
                        ok2, err2 = await _v32_add(
                            client, base_url, auth, new_uuid, email,
                            [inb["id"] for inb in eligible],
                        )
                        if not ok2:
                            await log_error(
                                "Не удалось обновить ключ (v3.2)",
                                source="xray_api", server_name=server.get("name"),
                                level="warning",
                                details=f"update_err={err}; add_err={err2}",
                            )
                else:
                    # legacy: на каждый inbound — попытка update, фолбэк add
                    for inb in eligible:
                        leg_email = _legacy_email(email, inb["id"])
                        # Если клиент с таким email есть — берём его UUID для update
                        uuids = _uuids_with_email(inb, leg_email)
                        if uuids:
                            for u in uuids:
                                await _legacy_update(
                                    client, base_url, auth, inb["id"], u, new_uuid, leg_email,
                                )
                        else:
                            await _legacy_add(
                                client, base_url, auth, inb["id"], new_uuid, leg_email,
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
    """Текущие ссылки для клиента. Если на сервере его ещё нет — регистрирует."""
    gathered: list[str] = []
    overrides = await get_overrides_map()
    servers = await get_active_servers()

    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = _base_url(server)
            auth = await get_auth_kwargs(client, server)
            if not auth:
                continue
            try:
                mode = await _detect_mode(client, base_url, auth, server)
                inbounds = await _fetch_inbounds(client, base_url, auth, server["name"])
                eligible = _eligible_inbounds(inbounds, tariff)
                if not eligible:
                    continue

                if mode == MODE_V32:
                    if not _has_email(inbounds, email):
                        ok, err = await _v32_add(
                            client, base_url, auth, client_uuid, email,
                            [inb["id"] for inb in eligible],
                        )
                        if not ok:
                            await log_error(
                                "Авто-добавление клиента не удалось (v3.2)",
                                source="xray_api", server_name=server.get("name"),
                                level="warning", details=err,
                            )
                            continue
                else:
                    # legacy: проверяем наличие per-inbound, добавляем недостающее
                    for inb in eligible:
                        e = _legacy_email(email, inb["id"])
                        if not _has_email_in_inbound(inb, e):
                            ok, err = await _legacy_add(
                                client, base_url, auth, inb["id"], client_uuid, e,
                            )
                            if not ok:
                                log.info(
                                    "%s: addClient(legacy) inbound=%s: %s",
                                    server["name"], inb["id"], err,
                                )

                gathered.extend(_build_links(client_uuid, email, server, eligible, overrides))
            except Exception as e:
                log.error("Ошибка getLinks на %s: %s", server["name"], e)
                await log_error(
                    "Ошибка при получении ссылок",
                    source="xray_api", server_name=server.get("name"),
                    level="error", details=f"{type(e).__name__}: {e}",
                )
    return gathered


# ────────────── Диагностика ──────────────
async def fetch_inbounds_summary(server: dict) -> list[dict]:
    """Лёгкий список inbound'ов сервера для вкладки «Inbounds» админки.
    [{id, port, remark, protocol, enable}]."""
    base_url = _base_url(server)
    try:
        async with httpx.AsyncClient(verify=False) as client:
            auth = await get_auth_kwargs(client, server)
            if not auth:
                return []
            inbounds = await _fetch_inbounds(client, base_url, auth, server["name"])
            out = []
            for inb in inbounds:
                out.append({
                    "id": inb.get("id"),
                    "port": inb.get("port"),
                    "remark": inb.get("remark") or "",
                    "protocol": inb.get("protocol") or "",
                    "enable": bool(inb.get("enable", True)),
                })
            return out
    except Exception as e:
        log.warning("fetch_inbounds_summary %s: %s", server.get("name"), e)
        return []


async def fetch_all_inbounds(servers: list[dict]) -> list[dict]:
    """Параллельно собирает inbound'ы со всех переданных серверов.
    Возвращает [{server_id, server_name, server_host, inbound: {...}}, ...]."""
    if not servers:
        return []
    results = await asyncio.gather(
        *(fetch_inbounds_summary(s) for s in servers), return_exceptions=True,
    )
    out: list[dict] = []
    for srv, res in zip(servers, results):
        if isinstance(res, Exception):
            log.warning("fetch_all_inbounds %s: %s", srv.get("name"), res)
            continue
        for inb in res:
            out.append({
                "server_id": srv.get("id"),
                "server_name": srv.get("name"),
                "server_host": srv.get("host"),
                "server_client_host": srv.get("client_host") or "",
                "inbound": inb,
            })
    return out


async def test_server(server: dict) -> tuple[bool, str]:
    base_url = _base_url(server)
    try:
        async with httpx.AsyncClient(verify=False) as client:
            auth = await get_auth_kwargs(client, server)
            if not auth:
                return False, "Авторизация не удалась (логин/пароль или токен)"
            lst = await client.get(
                f"{base_url}{URL_INBOUNDS_LIST}",
                timeout=HTTP_TIMEOUT, **auth,
            )
            data = _safe_json(lst)
            if lst.status_code == 200 and data.get("success"):
                inbounds = data.get("obj") or []
                mode = await _detect_mode(client, base_url, auth, server)
                return True, f"OK — {mode}, inbound'ов: {len(inbounds)}"
            return False, f"Ошибка API (HTTP {lst.status_code})"
    except Exception as e:
        return False, f"Недоступен: {type(e).__name__}: {e}"


async def test_servers_parallel(servers: list[dict]) -> list[dict]:
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
