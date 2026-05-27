# config.py — БЕЗОПАСНАЯ ВЕРСИЯ
# Все секреты читаются из файла .env, НЕ хранятся в коде
#
# Как это работает:
#   1. Библиотека python-dotenv читает файл .env
#   2. Значения попадают в переменные окружения (os.environ)
#   3. os.getenv("ИМЯ") берёт значение оттуда
#   4. Если значение не найдено — используется значение по умолчанию (второй аргумент)

import os
from dotenv import load_dotenv

# Загружаем файл .env (он должен лежать рядом с config.py)
load_dotenv()

# ──────────────── Телеграм ────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ──────────────── База данных ────────────────
DB_USER = os.getenv("DB_USER", "siha_user")
DB_PASS = os.getenv("DB_PASS")
DB_NAME = os.getenv("DB_NAME", "sihavpn")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")

# ──────────────── Сеть ────────────────
MOSCOW_IP = os.getenv("MOSCOW_IP", "138.16.179.223")
WEB_PORT   = int(os.getenv("WEB_PORT", "8000"))

# Собираем полный URL кабинета автоматически
CABINET_URL = f"http://{MOSCOW_IP}:{WEB_PORT}/cabinet"
CABINET_BASE_URL = os.getenv("CABINET_BASE_URL", f"http://{MOSCOW_IP}:{WEB_PORT}")
FALLBACK_CABINET_URL = os.getenv("FALLBACK_CABINET_URL", "https://backup.sihavpn.ru/cabinet")
CHANNEL_URL         = os.getenv("CHANNEL_URL", "https://t.me/SihaVPN_news")
SUPPORT_URL         = os.getenv("SUPPORT_URL", "https://t.me/SihaSupport")

# ──────────────── VPN-серверы ────────────────
# Читаем настройки каждого сервера из .env
# Если появится сервер3 — просто добавь SERVER3_* в .env и SERVER_COUNT=3
SERVERS = []
server_count = int(os.getenv("SERVER_COUNT", "2"))
for i in range(1, server_count + 1):
    host = os.getenv(f"SERVER{i}_HOST")
    if host:
        SERVERS.append({
            "name":      os.getenv(f"SERVER{i}_NAME",    f"Server{i}"),
            "scheme":    os.getenv(f"SERVER{i}_SCHEME",   "https"),
            "host":      host,
            "port":      int(os.getenv(f"SERVER{i}_PORT", "2053")),
            "base_path": os.getenv(f"SERVER{i}_BASE_PATH", ""),
            "login":     os.getenv(f"SERVER{i}_LOGIN",    "admin"),
            "password":  os.getenv(f"SERVER{i}_PASSWORD", "admin"),
        })

# ──────────────── Обход блокировок (Bypass) ────────────────
_bypass_raw = os.getenv("BYPASS_IPS", "")
BYPASS_IPS = [ip.strip() for ip in _bypass_raw.split(",")] if _bypass_raw else []

# ──────────────── Проверка при запуске ────────────────
# Если каких-то критичных переменных нет — сразу говорим об этом,
# чтобы не получить непонятную ошибку в середине работы
_REQUIRED = {
    "BOT_TOKEN": BOT_TOKEN,
    "DB_PASS":   DB_PASS,
}
_missing = [name for name, val in _REQUIRED.items() if not val]
if _missing:
    raise EnvironmentError(
        f"❌ Не найдены обязательные переменные в .env: {', '.join(_missing)}\n"
        f"Скопируй .env.example в .env и заполни значения."
    )
