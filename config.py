"""Единая точка конфигурации. Все значения читаются из .env."""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name, default)
    return val.strip() if isinstance(val, str) else val


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise EnvironmentError(f"{name} должен быть числом, получено: {raw!r}") from e


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise EnvironmentError(f"{name} должен быть числом, получено: {raw!r}") from e


# ──────────── Логирование ────────────
LOG_LEVEL = (_env("LOG_LEVEL", "INFO") or "INFO").upper()

# ──────────── Админ-панель ────────────
ADMIN_PASSWORD = _env("ADMIN_PASSWORD")
ADMIN_SECRET = _env("ADMIN_SECRET")

# ──────────── Телеграм ────────────
BOT_TOKEN = _env("BOT_TOKEN")
BOT_USERNAME = _env("BOT_USERNAME", "SihaVPN_bot")
CHANNEL_ID = _env_int("CHANNEL_ID", 0)
PROXY_URL = _env("PROXY_URL") or None

# ──────────── База данных ────────────
DB_USER = _env("DB_USER", "siha_user")
DB_PASS = _env("DB_PASS")
DB_NAME = _env("DB_NAME", "sihavpn")
DB_HOST = _env("DB_HOST", "127.0.0.1")
DB_PORT = _env_int("DB_PORT", 5432)
DB_POOL_MIN = _env_int("DB_POOL_MIN", 2)
DB_POOL_MAX = _env_int("DB_POOL_MAX", 10)

# ──────────── Веб-сервер ────────────
WEB_HOST = _env("WEB_HOST", "0.0.0.0")
WEB_PORT = _env_int("WEB_PORT", 8000)
CABINET_BASE_URL = (_env("CABINET_BASE_URL") or f"http://127.0.0.1:{WEB_PORT}").rstrip("/")
FALLBACK_CABINET_URL = _env("FALLBACK_CABINET_URL", "https://backup.sihavpn.ru/cabinet")

# ──────────── Ссылки в боте ────────────
CHANNEL_URL = _env("CHANNEL_URL", "https://t.me/SihaVPN_news")
SUPPORT_URL = _env("SUPPORT_URL", "https://t.me/SihaSupport")
REFERRAL_URL_TEMPLATE = f"https://t.me/{BOT_USERNAME}?start={{user_id}}"

# ──────────── Тарифы и бонусы ────────────
PRICE_PER_DEVICE = _env_float("PRICE_PER_DEVICE", 3.33)
BONUS_SUBSCRIBE = _env_float("BONUS_SUBSCRIBE", 25.0)
BONUS_PHONE = _env_float("BONUS_PHONE", 95.0)
BONUS_PHONE_SUB = _env_float("BONUS_PHONE_SUB", 5.0)

# ──────────── VPN-серверы из .env (используются только при первой миграции) ────────────
SERVERS: list[dict] = []
_server_count = _env_int("SERVER_COUNT", 0)
for _i in range(1, _server_count + 1):
    _host = _env(f"SERVER{_i}_HOST")
    if _host:
        SERVERS.append({
            "name":      _env(f"SERVER{_i}_NAME", f"Server{_i}"),
            "scheme":    _env(f"SERVER{_i}_SCHEME", "https"),
            "host":      _host,
            "port":      _env_int(f"SERVER{_i}_PORT", 2053),
            "base_path": _env(f"SERVER{_i}_BASE_PATH", ""),
            "login":     _env(f"SERVER{_i}_LOGIN", "admin"),
            "password":  _env(f"SERVER{_i}_PASSWORD", "admin"),
        })

# ──────────── Bypass-адреса из .env (только для первой миграции) ────────────
_bypass_raw = _env("BYPASS_IPS", "") or ""
BYPASS_IPS = [ip.strip() for ip in _bypass_raw.split(",") if ip.strip()]


# ──────────── Проверка обязательных переменных ────────────
_REQUIRED = {"BOT_TOKEN": BOT_TOKEN, "DB_PASS": DB_PASS}
_missing = [name for name, val in _REQUIRED.items() if not val]
if _missing:
    sys.stderr.write(
        f"\n❌ Не найдены обязательные переменные в .env: {', '.join(_missing)}\n"
        f"Скопируйте .env.example в .env и заполните значения.\n\n"
    )
    raise EnvironmentError(f"Missing required env vars: {', '.join(_missing)}")
