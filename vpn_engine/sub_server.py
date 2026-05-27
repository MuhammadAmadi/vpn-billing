import base64
import sqlite3
import json
import os
from fastapi import FastAPI, Response, Request
import uvicorn

app = FastAPI(title="SihaVPN Smart Subscription Server")

def init_db():
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
    conn.commit()
    conn.close()

init_db()

@app.get("/sub/{user_uuid}")
def get_subscription(request: Request, user_uuid: str):
    conn = sqlite3.connect("sihavpn.db")
    cursor = conn.cursor()
    cursor.execute("SELECT links, username FROM clients WHERE uuid = ?", (user_uuid,))
    result = cursor.fetchone()
    conn.close()

    if not result:
        return Response(content="User not found", status_code=404)

    links_text, username = result
    user_agent = request.headers.get('user-agent', '').lower()

    # 1. ТЕЛО ОТВЕТА (Стандартный Base64)
    encoded_links = base64.b64encode(links_text.encode('utf-8')).decode('utf-8')
    
    # 2. БАЗОВЫЕ ЗАГОЛОВКИ
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "profile-title": f"SihaVPN | {username}",
        "profile-update-interval": "24",
        "Content-Disposition": f"attachment; filename*=UTF-8''SihaVPN_{username}"
    }

    # 3. СЕКРЕТНЫЙ ЗАГОЛОВОК ДЛЯ HAPP (Читаем из файла!)
    if 'happ' in user_agent:
        try:
            with open('routing.json', 'r', encoding='utf-8') as f:
                routing_json = json.load(f)
            
            print(f"📡 Обнаружен HAPP! Прикрепляем правила из routing.json...")
            routing_b64 = base64.b64encode(json.dumps(routing_json).encode('utf-8')).decode('utf-8')
            headers["routing"] = f"happ://routing/onadd/{routing_b64}"
        except Exception as e:
            print(f"⚠️ Ошибка чтения routing.json: {e}")

    return Response(content=encoded_links, headers=headers)

if __name__ == "__main__":
    print("="*60)
    print("🚀 СЕРВЕР ПОДПИСОК SAYVPN ЗАПУЩЕН (Подключен routing.json)!")
    print("="*60)
    uvicorn.run(app, host="0.0.0.0", port=8000)
