import httpx
import json
import warnings
import urllib.parse
import config
from server_store import get_active_servers
from error_log import log_error
from bypass_store import get_active_bypass_ips

warnings.filterwarnings("ignore", message="Unverified HTTPS request")


async def get_session_cookie(client: httpx.AsyncClient, server: dict):
    base_url = f"{server['scheme']}://{server['host']}:{server['port']}/{server['base_path'].strip('/')}"
    try:
        resp = await client.post(
            f"{base_url}/login",
            json={"username": server["login"], "password": server["password"]},
            timeout=10.0
        )
        if resp.status_code == 200 and resp.json().get("success"):
            print(f"   🔑 Авторизация на {server['name']} успешна")
            return dict(resp.cookies)
        else:
            print(f"   ❌ Авторизация не удалась: {resp.text[:100]}")
            await log_error(
                f"Авторизация не удалась (HTTP {resp.status_code})",
                source="xray_api", server_name=server.get("name"),
                level="error", details=resp.text[:500],
            )
            return None
    except Exception as e:
        print(f"   ❌ Ошибка авторизации на {server['name']}: {e}")
        await log_error(
            "Сервер недоступен (ошибка авторизации)",
            source="xray_api", server_name=server.get("name"),
            level="error", details=f"{type(e).__name__}: {e}",
        )
        return None


def build_vless_link(client_uuid: str, email: str, server_host: str, inbound: dict, custom_name: str = None) -> str:
    port = inbound.get("port", 443)
    remark = inbound.get("remark", "").strip()
    stream = json.loads(inbound.get("streamSettings", "{}"))

    # Проверяем External Proxy
    external_proxy = stream.get("externalProxy", [])
    if external_proxy:
        ep = external_proxy[0]
        server_host = ep.get("dest", server_host)
        port = ep.get("port", port)
        if port == 443:
            use_tls = True
        else:
            use_tls = False
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
        grpc = stream.get("grpcSettings", {})
        params["serviceName"] = grpc.get("serviceName", "")

    elif network == "tcp":
        tcp = stream.get("tcpSettings", {})
        header = tcp.get("header", {})
        if header.get("type") == "http":
            request = header.get("request", {})
            paths = request.get("path", ["/"])
            params["path"] = paths[0] if paths else "/"

    # Название: если есть remark — используем его, иначе название сервера
    display_name = custom_name if custom_name else (remark if remark else "Server")
    query = urllib.parse.urlencode(params)
    name = urllib.parse.quote(display_name)
    link = f"vless://{client_uuid}@{server_host}:{port}?{query}#{name}"
    return link


async def add_client_to_all_servers(client_uuid: str, email: str, tariff: str = "Standard"):
    gathered_links = []
    bypass_counter = 0
    bypass_ips = await get_active_bypass_ips()
    servers = await get_active_servers()
    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = f"{server['scheme']}://{server['host']}:{server['port']}/{server['base_path'].strip('/')}"
            cookies = await get_session_cookie(client, server)
            if not cookies:
                print(f"❌ Пропускаем {server['name']} — нет авторизации")
                continue
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            try:
                resp = await client.get(f"{base_url}/panel/api/inbounds/list", headers=headers, cookies=cookies, timeout=10.0)
                inbounds = resp.json().get("obj", [])
                print(f"📋 {server['name']}: найдено {len(inbounds)} inbound(ов)")
                for inbound in inbounds:
                    remark = inbound.get("remark", "")
                    # Глобальный игнор приватных подключений
                    if "| PRIVATE" in remark.upper():
                        continue

                    if tariff == "Standard" and ("| VIP" in remark.upper() or "| PRO" in remark.upper()):
                        continue
                    inbound_id = inbound.get("id")
                    unique_email = f"{email}_i{inbound_id}"
                    print(f"   ➕ Добавляем в '{remark}' (id={inbound_id}), email={unique_email}")
                    client_payload = {"clients": [{"id": client_uuid, "email": unique_email, "limitIp": 0, "totalGB": 0, "expiryTime": 0, "enable": True, "flow": "xtls-rprx-vision"}]}
                    add_payload = {"id": inbound_id, "settings": json.dumps(client_payload)}
                    add_resp = await client.post(f"{base_url}/panel/api/inbounds/addClient", headers=headers, cookies=cookies, json=add_payload)
                    add_result = add_resp.json()
                    print(f"   📨 addClient: {add_resp.status_code} — {add_resp.text[:150]}")
                    if add_resp.status_code == 200 and add_result.get("success"):
                        # Проверяем, это BYPASS или нет
                        if "BYPASS" in remark.upper():
                            current_ip = bypass_ips[bypass_counter % len(bypass_ips)] if bypass_ips else server["host"]
                            current_name = f"ОБХОД {bypass_counter + 1}"
                            bypass_counter += 1
                        else:
                            current_ip = server["host"]
                            current_name = None

                        link = build_vless_link(client_uuid, unique_email, current_ip, inbound, current_name)
                        print(f"   ✅ Ссылка собрана: {link[:80]}...")
                        gathered_links.append(link)
                    else:
                        await log_error(
                            f"addClient вернул ошибку (HTTP {add_resp.status_code})",
                            source="xray_api", server_name=server.get("name"),
                            level="warning", details=add_resp.text[:500],
                        )
            except Exception as e:
                print(f"❌ Ошибка Xray на {server['name']}: {e}")
                await log_error(
                    "Ошибка при добавлении клиента",
                    source="xray_api", server_name=server.get("name"),
                    level="error", details=f"{type(e).__name__}: {e}",
                )
    print(f"🏁 Итого собрано ссылок: {len(gathered_links)}")
    return gathered_links


async def remove_client_from_all_servers(client_uuid: str):
    servers = await get_active_servers()
    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = f"{server['scheme']}://{server['host']}:{server['port']}/{server['base_path'].strip('/')}"
            cookies = await get_session_cookie(client, server)
            if not cookies:
                continue
            headers = {"Accept": "application/json"}
            try:
                resp = await client.get(f"{base_url}/panel/api/inbounds/list", headers=headers, cookies=cookies, timeout=10.0)
                inbounds = resp.json().get("obj", [])
                for inbound in inbounds:
                    inbound_id = inbound["id"]
                    client_stats = inbound.get("clientStats", [])
                    real_uuid = None
                    for stat in client_stats:
                        if stat.get("uuid") == client_uuid:
                            real_uuid = stat.get("uuid")
                            break
                    if not real_uuid:
                        continue
                    del_resp = await client.post(f"{base_url}/panel/api/inbounds/{inbound_id}/delClient/{real_uuid}", headers=headers, cookies=cookies)
                    print(f"   🗑️ delClient {server['name']} inbound={inbound_id}: {del_resp.status_code}")
            except Exception as e:
                print(f"❌ Ошибка удаления Xray на {server['name']}: {e}")
                await log_error(
                    "Ошибка при удалении клиента",
                    source="xray_api", server_name=server.get("name"),
                    level="error", details=f"{type(e).__name__}: {e}",
                )


async def update_client_uuid_on_all_servers(old_uuid: str, new_uuid: str, email: str):
    servers = await get_active_servers()
    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = f"{server['scheme']}://{server['host']}:{server['port']}/{server['base_path'].strip('/')}"
            cookies = await get_session_cookie(client, server)
            if not cookies:
                continue
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            try:
                resp = await client.get(f"{base_url}/panel/api/inbounds/list", headers=headers, cookies=cookies, timeout=10.0)
                inbounds = resp.json().get("obj", [])
                for inbound in inbounds:
                    remark = inbound.get("remark", "")
                    # Глобальный игнор приватных подключений
                    if "| PRIVATE" in remark.upper():
                        continue

                    if "| VIP" in remark.upper() or "| PRO" in remark.upper():
                        continue
                    inbound_id = inbound["id"]
                    unique_email = f"{email}_i{inbound_id}"
                    client_stats = inbound.get("clientStats", [])
                    real_uuid = None
                    for stat in client_stats:
                        if stat.get("email") == unique_email:
                            real_uuid = stat.get("uuid")
                            break
                    if not real_uuid:
                        print(f"   ⚠️ {unique_email} не найден, добавляем заново...")
                        client_payload = {"clients": [{"id": new_uuid, "email": unique_email, "limitIp": 0, "totalGB": 0, "expiryTime": 0, "enable": True, "flow": "xtls-rprx-vision"}]}
                        add_payload = {"id": inbound_id, "settings": json.dumps(client_payload)}
                        add_resp = await client.post(f"{base_url}/panel/api/inbounds/addClient", headers=headers, cookies=cookies, json=add_payload)
                        print(f"   ➕ addClient: {add_resp.status_code} {add_resp.text[:100]}")
                        continue
                    print(f"   📤 Обновляем {server['name']} inbound={inbound_id}: {real_uuid} → {new_uuid}")
                    client_payload = {"clients": [{"id": new_uuid, "email": unique_email, "limitIp": 0, "totalGB": 0, "expiryTime": 0, "enable": True, "flow": "xtls-rprx-vision"}]}
                    upd_payload = {"id": inbound_id, "settings": json.dumps(client_payload)}
                    upd_resp = await client.post(f"{base_url}/panel/api/inbounds/updateClient/{real_uuid}", headers=headers, cookies=cookies, data=json.dumps(upd_payload), timeout=10.0)
                    print(f"   🔄 updateClient: {upd_resp.status_code} {upd_resp.text[:100]}")
            except Exception as e:
                print(f"❌ Ошибка updateClient на {server['name']}: {e}")
                await log_error(
                    "Ошибка при обновлении ключа клиента",
                    source="xray_api", server_name=server.get("name"),
                    level="error", details=f"{type(e).__name__}: {e}",
                )


async def get_client_links_from_all_servers(client_uuid: str, email: str, tariff: str = "Standard"):
    gathered_links = []
    bypass_counter = 0
    bypass_ips = await get_active_bypass_ips()
    servers = await get_active_servers()
    async with httpx.AsyncClient(verify=False) as client:
        for server in servers:
            base_url = f"{server['scheme']}://{server['host']}:{server['port']}/{server['base_path'].strip('/')}"
            cookies = await get_session_cookie(client, server)
            if not cookies:
                continue
            headers = {"Accept": "application/json"}
            try:
                resp = await client.get(f"{base_url}/panel/api/inbounds/list", headers=headers, cookies=cookies, timeout=10.0)
                inbounds = resp.json().get("obj", [])
                for inbound in inbounds:
                    remark = inbound.get("remark", "")
                    # Глобальный игнор приватных подключений
                    if "| PRIVATE" in remark.upper():
                        continue

                    if tariff == "Standard" and ("| VIP" in remark.upper() or "| PRO" in remark.upper()):
                        continue
                    inbound_id = inbound["id"]
                    unique_email = f"{email}_i{inbound_id}"
                    settings = json.loads(inbound.get("settings", "{}"))
                    clients = settings.get("clients", [])
                    client_uuid_real = next((c.get("id") for c in clients if c.get("email") == unique_email), None)

                    # Если клиента нет на сервере — добавляем его на лету
                    if not client_uuid_real:
                        print(f"   ⚠️ Клиент {unique_email} не найден на {server['name']}, добавляем...")
                        client_payload = {
                            "clients": [{
                                "id": client_uuid,
                                "email": unique_email,
                                "limitIp": 0,
                                "totalGB": 0,
                                "expiryTime": 0,
                                "enable": True,
                                "flow": "xtls-rprx-vision"
                            }]
                        }
                        add_payload = {"id": inbound_id, "settings": json.dumps(client_payload)}
                        try:
                            add_resp = await client.post(
                                f"{base_url}/panel/api/inbounds/addClient",
                                headers=headers,
                                cookies=cookies,
                                json=add_payload
                            )
                            if add_resp.status_code == 200 and add_resp.json().get("success"):
                                print(f"   ➕ Клиент успешно добавлен на лету!")
                                client_uuid_real = client_uuid
                            else:
                                print(f"   ❌ Ошибка авто-добавления: {add_resp.text[:100]}")
                                await log_error(
                                    f"Авто-добавление клиента не удалось (HTTP {add_resp.status_code})",
                                    source="xray_api", server_name=server.get("name"),
                                    level="warning", details=add_resp.text[:500],
                                )
                                continue
                        except Exception as e:
                            print(f"   ❌ Сетевая ошибка при авто-добавлении: {e}")
                            await log_error(
                                "Сетевая ошибка при авто-добавлении клиента",
                                source="xray_api", server_name=server.get("name"),
                                level="error", details=f"{type(e).__name__}: {e}",
                            )
                            continue

                        # Проверяем, это BYPASS или нет
                    if "BYPASS" in remark.upper():
                        current_ip = bypass_ips[bypass_counter % len(bypass_ips)] if bypass_ips else server["host"]
                        current_name = f"ОБХОД {bypass_counter + 1}"
                        bypass_counter += 1
                    else:
                        current_ip = server["host"]
                        current_name = None

                    link = build_vless_link(client_uuid, unique_email, current_ip, inbound, current_name)
                    print(f"   🔗 {server['name']} inbound={inbound_id}: ссылка собрана")
                    gathered_links.append(link)
            except Exception as e:
                print(f"❌ Ошибка getLinks на {server['name']}: {e}")
                await log_error(
                    "Ошибка при получении ссылок",
                    source="xray_api", server_name=server.get("name"),
                    level="error", details=f"{type(e).__name__}: {e}",
                )
    return gathered_links


# === Проверка одного сервера (используется кнопкой "Тест" в админ-панели) ===
async def test_server(server: dict):
    """Пробует залогиниться и получить список inbound'ов. Возвращает (ok, message)."""
    base_url = f"{server['scheme']}://{server['host']}:{server['port']}/{server['base_path'].strip('/')}"
    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.post(
                f"{base_url}/login",
                json={"username": server["login"], "password": server["password"]},
                timeout=10.0,
            )
            if resp.status_code != 200 or not resp.json().get("success"):
                return False, f"Авторизация не удалась (HTTP {resp.status_code})"
            cookies = dict(resp.cookies)
            lst = await client.get(
                f"{base_url}/panel/api/inbounds/list",
                headers={"Accept": "application/json"}, cookies=cookies, timeout=10.0,
            )
            inbounds = lst.json().get("obj", [])
            return True, f"OK — доступен, inbound'ов: {len(inbounds)}"
    except Exception as e:
        return False, f"Недоступен: {type(e).__name__}: {e}"
