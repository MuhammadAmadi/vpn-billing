# system_control.py — ПЕРЕЗАПУСК СЕРВИСОВ ИЗ ПАНЕЛИ
#
# Разрешён только белый список юнитов (ALLOWED). Команда: systemctl restart <unit>.
#
# ВАЖНО про права:
#   Веб-процессу нужно право выполнять перезапуск. Если веб запущен от root —
#   работает сразу. Если от обычного пользователя — добавь в sudoers (см. README):
#     <user> ALL=(root) NOPASSWD: /usr/bin/systemctl restart sihabot, /usr/bin/systemctl restart sihaweb
#
# Перезапуск самого веба (sihaweb) убил бы текущий запрос, поэтому он делается
# отложенно (через ~1.5 c), чтобы панель успела отдать ответ.

import shlex
import subprocess

# имя в панели -> имя systemd-юнита
ALLOWED = {
    "sihaweb": "sihaweb",
    "sihabot": "sihabot",
}
SELF_SERVICE = "sihaweb"  # этот юнит — мы сами


def _systemctl_restart_cmd(unit: str) -> str:
    # sudo не помешает: под root он просто проходит насквозь
    return f"sudo systemctl restart {shlex.quote(unit)}"


def restart_service(name: str):
    unit = ALLOWED.get(name)
    if not unit:
        return False, "Неизвестный сервис"
    try:
        if name == SELF_SERVICE:
            # Отложенный перезапуск в отдельной сессии, чтобы ответ успел уйти
            subprocess.Popen(
                ["bash", "-c", f"sleep 1.5; {_systemctl_restart_cmd(unit)}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True, "Перезапуск запланирован (через ~1.5 c). Панель ненадолго станет недоступна."
        result = subprocess.run(
            ["bash", "-c", _systemctl_restart_cmd(unit)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True, "Сервис перезапущен"
        return False, (result.stderr or result.stdout or "Ошибка перезапуска").strip()[:300]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def service_status(name: str) -> str:
    """active / inactive / failed / unknown. is-active обычно не требует sudo."""
    unit = ALLOWED.get(name)
    if not unit:
        return "unknown"
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=10,
        )
        return (result.stdout or result.stderr or "unknown").strip() or "unknown"
    except Exception:
        return "unknown"
