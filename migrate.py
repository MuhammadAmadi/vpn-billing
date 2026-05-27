import sqlite3
import re
import os

old_db = 'sayvpn.db'
new_db = 'sihavpn.db'
new_ip = '138.16.179.223'

print("🔄 Начинаем миграцию на SihaVPN...")

# 1. Переименовываем базу
if os.path.exists(old_db):
    os.rename(old_db, new_db)
    print("✅ База данных переименована в sihavpn.db")

# 2. Обновляем ссылки в базе
if os.path.exists(new_db):
    conn = sqlite3.connect(new_db)
    cur = conn.cursor()
    cur.execute("SELECT uuid, links FROM clients")
    rows = cur.fetchall()

    for row in rows:
        uuid, links = row
        # Меняем имя
        links = links.replace('SayVPN', 'SihaVPN')
        # Меняем IP в ссылках vless (@IP:PORT) и http (http://IP:PORT)
        links = re.sub(r'@[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:', f'@{new_ip}:', links)
        links = re.sub(r'http://[0-9]+.[0-9]+.[0-9]+.[0-9]+:', f'http://{new_ip}:', links)
        
        cur.execute("UPDATE clients SET links = ? WHERE uuid = ?", (links, uuid))

    conn.commit()
    conn.close()
    print("✅ IP-адреса и названия в базе клиентов успешно обновлены!")

# 3. Обновляем код файлов
for filename in ['sub_server.py', 'master_creator.py', 'routing.json']:
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            content = f.read()
        
        content = content.replace('SayVPN', 'SihaVPN')
        content = content.replace('sayvpn.db', 'sihavpn.db')
        
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"✅ Файл {filename} обновлен!")

print("🎉 Миграция завершена! Добро пожаловать в SihaVPN!")
