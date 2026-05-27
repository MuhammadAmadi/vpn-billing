# config_files.py — РЕДАКТИРОВАНИЕ КОНФИГ-ФАЙЛОВ ИЗ ПАНЕЛИ
#
# Разрешён только белый список файлов (EDITABLE). Перед сохранением:
#   • для .json — проверяем, что это валидный JSON (иначе не сохраняем);
#   • делаем резервную копию <файл>.bak;
#   • защита от выхода за пределы папки бэкенда.
#
# ВАЖНО: .env применяется только ПОСЛЕ перезапуска веб-сервера (load_dotenv
# читается один раз при старте). routing.json подхватывается сразу — web.py
# читает его при каждом запросе подписки.

import os
import json
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Белый список редактируемых файлов
EDITABLE = {
    ".env": {
        "label": "Переменные окружения (.env)",
        "kind": "env",
        "restart": True,
        "hint": "Применится после перезапуска веб-сервера.",
    },
    "routing.json": {
        "label": "Маршрутизация (routing.json)",
        "kind": "json",
        "restart": False,
        "hint": "Подхватывается сразу. Должен быть валидным JSON.",
    },
}


def _safe_path(name):
    if name not in EDITABLE:
        raise ValueError("Файл не разрешён для редактирования")
    path = os.path.realpath(os.path.join(BASE_DIR, name))
    if os.path.dirname(path) != os.path.realpath(BASE_DIR):
        raise ValueError("Недопустимый путь")
    return path


def list_files():
    out = []
    for name, meta in EDITABLE.items():
        path = os.path.join(BASE_DIR, name)
        out.append({
            "name": name,
            "label": meta["label"],
            "kind": meta["kind"],
            "restart": meta["restart"],
            "hint": meta["hint"],
            "exists": os.path.exists(path),
        })
    return out


def read_file(name):
    path = _safe_path(name)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def save_file(name, content):
    path = _safe_path(name)
    meta = EDITABLE[name]

    # Валидация JSON перед записью — чтобы не сломать routing.json
    if meta["kind"] == "json":
        try:
            json.loads(content)
        except Exception as e:
            raise ValueError(f"Невалидный JSON: {e}")

    # Резервная копия
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except Exception:
            pass

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    return {"saved": True, "restart": meta["restart"]}
