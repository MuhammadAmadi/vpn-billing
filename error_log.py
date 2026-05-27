# error_log.py — ЖУРНАЛ ОШИБОК В ФАЙЛ (JSON Lines + ротация)
#
# Почему файл, а не БД:
#   • главные ошибки — это «БД/сервер недоступны»; в БД их записать как раз и не выйдет;
#   • никакой нагрузки на PostgreSQL и лишних соединений;
#   • стандартный формат, легко смотреть через `tail -f logs/errors.jsonl`.
#
# Формат: один JSON-объект на строку. При превышении размера файл ротируется
# (errors.jsonl -> errors.jsonl.1, хранится 1 архив).
#
# log_error() НИКОГДА не выбрасывает исключение — запись лога не может уронить сайт.

import os
import json
import threading
from datetime import datetime, timedelta

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "errors.jsonl")
BACKUP_FILE = LOG_FILE + ".1"
MAX_BYTES = 5 * 1024 * 1024  # 5 МБ, потом ротация
_TS_FMT = "%d.%m.%Y %H:%M:%S"
_lock = threading.Lock()


def _rotate_if_needed():
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_BYTES:
            if os.path.exists(BACKUP_FILE):
                os.remove(BACKUP_FILE)
            os.rename(LOG_FILE, BACKUP_FILE)
    except Exception:
        pass


async def log_error(message, *, source=None, server_name=None,
                    level="error", details=None, conn=None):
    """Записать событие в журнал-файл. conn оставлен для совместимости, не используется."""
    try:
        record = {
            "ts": datetime.now().strftime(_TS_FMT),
            "level": (level or "error"),
            "source": source,
            "server_name": server_name,
            "message": str(message)[:1000],
            "details": (str(details)[:3000] if details is not None else None),
        }
        line = json.dumps(record, ensure_ascii=False)
        with _lock:
            os.makedirs(LOG_DIR, exist_ok=True)
            _rotate_if_needed()
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        print(f"⚠️ [error_log] не смог записать лог: {e}")


def _read_file(path):
    out = []
    try:
        if not os.path.exists(path):
            return out
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception as e:
        print(f"⚠️ [error_log] не смог прочитать {path}: {e}")
    return out


async def get_recent_errors(*, level=None, limit=300):
    """Последние записи (новые сверху). level=None/'all' — все уровни."""
    records = _read_file(BACKUP_FILE) + _read_file(LOG_FILE)  # старые -> новые
    records.reverse()  # новые сверху
    if level and level != "all":
        records = [r for r in records if r.get("level") == level]
    return records[:limit]


async def count_errors_since(hours=24):
    """Сколько ошибок уровня 'error' за последние N часов (для бейджа)."""
    threshold = datetime.now() - timedelta(hours=hours)
    n = 0
    for r in _read_file(BACKUP_FILE) + _read_file(LOG_FILE):
        if r.get("level") != "error":
            continue
        try:
            if datetime.strptime(r.get("ts", ""), _TS_FMT) >= threshold:
                n += 1
        except Exception:
            pass
    return n


async def clear_errors():
    """Удалить файлы журнала."""
    with _lock:
        for p in (LOG_FILE, BACKUP_FILE):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception as e:
                print(f"⚠️ [error_log] не смог удалить {p}: {e}")
