import asyncio
import httpx
import uuid
import json
import sqlite3
import os

# === ЗАГРУЖАЕМ НАСТРОЙКИ ИЗ config.json ===
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

SERVERS = config["servers"]
MOSCOW_IP = config["moscow_ip"]
SUB_PORT = config["sub_port"]
# ==========================================

def save_to_db(client_uuid, username, links_list, tariff):
    conn = sqlite3.connect("sihavpn.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            uuid TEXT PRIMARY KEY,
            username TEXT,
            links TEXT,
            tariff TEXT
        )
    ''')
    links_text = "\n".join(links_list)
    cursor.execute("INSERT OR REPLACE INTO clients (uuid, username, links, tariff) VALUES (?, ?, ?, ?)", 
                   (client_uuid, username, links_text, tariff))
    conn.commit()
    conn.close()

async def process_server(client, server, client_uuid, base_email, tariff):
    server_name = server["name"]
    base_url = f"{server['scheme']}://{server['host']}:{server['port']}"
    if server["base_path"]:
        base_url += f"/{server['base_path'].strip('/')}"
        
    headers = {
        "Authorization": f"Bearer {server['api_token']}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    gathered_links = []
    print(f"\n🌍 Подключаемся к серверу: {server_name}")
    
    try:
        inbounds_resp = await client.get(f"{base_url}/panel/api/inbounds/list", headers=headers, timeout=10.0)
        inbounds_data = inbounds_resp.json()
        if not inbounds_data.get("success"):
            print(f"❌ Ошибка получения Inbounds с {server_name}: {inbounds_data.get('msg')}")
            return []
            
        all_inbounds = inbounds_data.get("obj", [])
    except Exception as e:
        print(f"❌ Сетевая ошибка при опросе {server_name}: {e}")
        return []

    target_inbounds = []
    for inbound in all_inbounds:
        remark = inbound.get("remark", "")
        # Если тариф Standard, отсекаем всё, где есть VIP или PRO
        if tariff == "Standard" and ("| VIP" in remark.upper() or "| PRO" in remark.upper()):
            print(f"   ⏭️ Пропускаем '{remark}' (Доступно только для VIP/PRO)")
            continue
        target_inbounds.append(inbound)

    if not target_inbounds:
        print(f"   ⚠️ Не найдено подходящих подключений для тарифа {tariff}.")
        return []

    for inbound in target_inbounds:
        inbound_id = inbound.get("id")
        remark = inbound.get("remark")
        print(f"   ⚙️ Добавляем в '{remark}' (ID: {inbound_id})...")
        
        # ВОТ ОНО: Делаем email уникальным для каждого Inbound
        unique_email = f"{base_email}_i{inbound_id}"
        
        client_settings = {
            "clients": [{
                "id": client_uuid,
                "email": unique_email,
                "limitIp": 0, "totalGB": 0, "expiryTime": 0, "enable": True, "flow": ""
            }]
        }
        
        payload = {"id": inbound_id, "settings": json.dumps(client_settings)}
        
        add_resp = await client.post(f"{base_url}/panel/api/inbounds/addClient", headers=headers, json=payload)
        
        if add_resp.status_code == 200 and add_resp.json().get("success"):
            links_url = f"{base_url}/panel/api/inbounds/getClientLinks/{inbound_id}/{unique_email}"
            links_resp = await client.get(links_url, headers=headers)
            if links_resp.status_code == 200 and links_resp.json().get("success"):
                links = links_resp.json().get("obj", [])
                if links:
                    gathered_links.append(links[0])
                    print(f"      ✅ Успех! Ссылка получена.")
                else:
                    print(f"      ⚠️ Клиент добавлен, но ссылка не сгенерировалась.")
        else:
            msg = add_resp.json().get("msg") if add_resp.status_code == 200 else add_resp.text
            if "already exists" not in str(msg).lower():
                print(f"      ❌ Ошибка: {msg}")

    return gathered_links

async def create_master_client(username: str, tariff: str):
    print("="*60)
    print(f"🚀 НАЧАЛО СОЗДАНИЯ: Пользователь '{username}' | Тариф '{tariff}'")
    print("="*60)
    
    client_uuid = str(uuid.uuid4())
    # Базовый email, к которому мы будем приклеивать ID инбаунда
    base_email = f"{username}_{client_uuid[:6]}"
    print(f"🔑 Сгенерирован единый UUID: {client_uuid}\n")

    all_links = []
    
    async with httpx.AsyncClient(verify=False) as client:
        for server in SERVERS:
            links = await process_server(client, server, client_uuid, base_email, tariff)
            all_links.extend(links)
            
    if all_links:
        print("\n💾 Сохраняем ссылки в базу данных Москвы...")
        save_to_db(client_uuid, username, all_links, tariff)
        
        print("\n🎉 ГОТОВО! Процесс завершен.")
        print("="*60)
        print(f"🔗 ССЫЛКА-ПОДПИСКА ДЛЯ КЛИЕНТА:")
        print(f"http://{MOSCOW_IP}:{SUB_PORT}/sub/{client_uuid}")
        print("="*60)
    else:
        print("\n❌ Не удалось получить ссылки.")

if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    
    test_username = "tg_user_alex"
    test_tariff = "Standard" 
    
    asyncio.run(create_master_client(test_username, test_tariff))
