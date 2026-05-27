# admin_panel.py — АДМИН-ПАНЕЛЬ (подключается к web.py одной строкой)
#
# Вкладки:
#   • Серверы       — добавить / изменить / вкл-выкл / удалить / тест
#   • Пользователи  — список+баланс, пагинация, сортировки/фильтры, CSV-экспорт,
#                     клик по строке = карточка (устройства + история), смена баланса
#   • Bypass        — список IP/доменов обхода (подставляются в BYPASS-ссылки)
#   • Файлы         — редактирование .env и routing.json (с бэкапом и проверкой)
#   • Журнал ошибок — читается из файла logs/errors.jsonl
#
# Подключение к web.py:
#   from admin_panel import router as admin_router
#   app.include_router(admin_router)
#
# Адрес: https://ТВОЙ_ДОМЕН/admin
# В .env:  ADMIN_PASSWORD=...   (ADMIN_SECRET=... — ключ подписи сессии, необязательно)

import csv
import hashlib
import hmac
import io

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import bot_content
import broadcast
import bypass_store
import config
import config_files
import error_log
import server_store
import system_control
import users_store
import xray_api

router = APIRouter(prefix="/admin", tags=["admin"])

ADMIN_PASSWORD = config.ADMIN_PASSWORD
_SECRET = (config.ADMIN_SECRET or config.BOT_TOKEN or "siha-default-secret").encode()
COOKIE_NAME = "siha_admin"


def _session_token() -> str:
    return hmac.new(_SECRET, b"siha-admin-session-v1", hashlib.sha256).hexdigest()


def _is_authed(request: Request) -> bool:
    if not ADMIN_PASSWORD:
        return False
    token = request.cookies.get(COOKIE_NAME, "")
    return bool(token) and hmac.compare_digest(token, _session_token())


def _need_login_json():
    return JSONResponse({"status": "error", "msg": "Не авторизован"}, status_code=401)


# ──────────── ВХОД / ВЫХОД ────────────
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_home(request: Request):
    if not ADMIN_PASSWORD:
        return HTMLResponse(NO_PASSWORD_HTML, status_code=500)
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=302)
    return HTMLResponse(DASHBOARD_HTML)


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if _is_authed(request):
        return RedirectResponse(url="/admin", status_code=302)
    return HTMLResponse(LOGIN_HTML)


@router.post("/login")
async def admin_login(request: Request):
    if not ADMIN_PASSWORD:
        return JSONResponse({"status": "error", "msg": "ADMIN_PASSWORD не задан в .env"}, status_code=500)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not hmac.compare_digest(str(data.get("password", "")), str(ADMIN_PASSWORD)):
        return JSONResponse({"status": "error", "msg": "Неверный пароль"}, status_code=403)
    resp = JSONResponse({"status": "ok"})
    resp.set_cookie(COOKIE_NAME, _session_token(),
                    httponly=True, samesite="lax", path="/admin", max_age=60 * 60 * 24 * 14)
    return resp


@router.get("/logout")
async def admin_logout():
    resp = RedirectResponse(url="/admin/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/admin")
    return resp


# ──────────── СЕРВЕРЫ ────────────
@router.get("/api/servers")
async def api_servers(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    async with request.app.state.db.acquire() as conn:
        servers = await server_store.list_servers(conn)
    return {"status": "ok", "servers": servers}


@router.post("/api/server/save")
async def api_server_save(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    if not data.get("name") or not data.get("host"):
        return {"status": "error", "msg": "Укажите как минимум имя и host"}
    try:
        async with request.app.state.db.acquire() as conn:
            if data.get("id"):
                await server_store.update_server(conn, data["id"], data)
                msg = "Сервер обновлён"
            else:
                await server_store.add_server(conn, data)
                msg = "Сервер добавлен"
        return {"status": "ok", "msg": msg}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@router.post("/api/server/delete")
async def api_server_delete(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    try:
        async with request.app.state.db.acquire() as conn:
            await server_store.delete_server(conn, data["id"])
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@router.post("/api/server/toggle")
async def api_server_toggle(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    try:
        async with request.app.state.db.acquire() as conn:
            new_state = await server_store.toggle_server(conn, data["id"])
        return {"status": "ok", "is_active": new_state}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@router.post("/api/server/test")
async def api_server_test(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    try:
        if data.get("id"):
            async with request.app.state.db.acquire() as conn:
                server = await server_store.get_server(conn, data["id"])
        else:
            server = {
                "name": data.get("name", "test"), "scheme": data.get("scheme", "https"),
                "host": data.get("host", ""), "port": int(data.get("port", 2053)),
                "base_path": data.get("base_path", ""), "login": data.get("login", "admin"),
                "password": data.get("password", "admin"),
                "api_token": data.get("api_token", ""),
            }
        if not server:
            return {"status": "error", "msg": "Сервер не найден"}
        ok, message = await xray_api.test_server(server)
        return {"status": "ok" if ok else "fail", "msg": message}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@router.get("/api/servers/health")
async def api_servers_health(request: Request):
    """Параллельно проверяет все включённые серверы. Для авто-индикации в UI."""
    if not _is_authed(request):
        return _need_login_json()
    async with request.app.state.db.acquire() as conn:
        servers = await server_store.list_servers(conn)
    enabled = [s for s in servers if s.get("is_active")]
    results = await xray_api.test_servers_parallel(enabled)
    return {"status": "ok", "results": results}


# ──────────── ПОЛЬЗОВАТЕЛИ ────────────
@router.get("/api/users")
async def api_users(request: Request, search: str = "", status: str = "all",
                    sort: str = "topup_total", page: int = 1, page_size: int = 25):
    if not _is_authed(request):
        return _need_login_json()
    async with request.app.state.db.acquire() as conn:
        data = await users_store.get_users(conn, search=search, status=status,
                                           sort=sort, page=page, page_size=page_size)
    return {"status": "ok", **data}


@router.get("/api/users/export")
async def api_users_export(request: Request, search: str = "", status: str = "all", sort: str = "topup_total"):
    if not _is_authed(request):
        return _need_login_json()
    async with request.app.state.db.acquire() as conn:
        rows = await users_store.export_users(conn, search=search, status=status, sort=sort)
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM, чтобы Excel корректно открыл UTF-8
    w = csv.writer(buf, delimiter=";")
    w.writerow(["user_id", "username", "phone", "balance", "topup_total",
                "topup_count", "topup_streak", "device_count", "created"])
    for u in rows:
        w.writerow([u["user_id"], u.get("username") or "", u.get("phone") or "",
                    f"{u['balance']:.2f}", f"{u['topup_total']:.2f}", u["topup_count"],
                    u["topup_streak"], u["device_count"], u["created"]])
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=siha_users.csv"})


@router.get("/api/user/{user_id}")
async def api_user_detail(request: Request, user_id: int):
    if not _is_authed(request):
        return _need_login_json()
    async with request.app.state.db.acquire() as conn:
        data = await users_store.get_user_detail(conn, user_id)
    if not data:
        return {"status": "error", "msg": "Пользователь не найден"}
    return {"status": "ok", **data}


@router.post("/api/user/balance")
async def api_user_balance(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    uid, nb = data.get("user_id"), data.get("balance")
    if uid is None or nb is None:
        return {"status": "error", "msg": "Нужны user_id и balance"}
    try:
        async with request.app.state.db.acquire() as conn:
            new_balance = await users_store.set_balance(conn, uid, nb)
        if new_balance is None:
            return {"status": "error", "msg": "Пользователь не найден"}
        return {"status": "ok", "balance": new_balance}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# ──────────── BYPASS ────────────
@router.get("/api/bypass")
async def api_bypass_list(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    async with request.app.state.db.acquire() as conn:
        items = await bypass_store.list_bypass(conn)
    return {"status": "ok", "items": items}


@router.post("/api/bypass/save")
async def api_bypass_save(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    if not data.get("value"):
        return {"status": "error", "msg": "Укажите IP или домен"}
    try:
        async with request.app.state.db.acquire() as conn:
            if data.get("id"):
                await bypass_store.update_bypass(conn, data["id"], data["value"], data.get("label", ""))
            else:
                await bypass_store.add_bypass(conn, data["value"], data.get("label", ""))
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@router.post("/api/bypass/delete")
async def api_bypass_delete(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    try:
        async with request.app.state.db.acquire() as conn:
            await bypass_store.delete_bypass(conn, data["id"])
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@router.post("/api/bypass/toggle")
async def api_bypass_toggle(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    try:
        async with request.app.state.db.acquire() as conn:
            st = await bypass_store.toggle_bypass(conn, data["id"])
        return {"status": "ok", "is_active": st}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# ──────────── КОНФИГ-ФАЙЛЫ ────────────
@router.get("/api/config/files")
async def api_config_files(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    return {"status": "ok", "files": config_files.list_files()}


@router.get("/api/config/file")
async def api_config_file_read(request: Request, name: str):
    if not _is_authed(request):
        return _need_login_json()
    try:
        return {"status": "ok", "content": config_files.read_file(name)}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@router.post("/api/config/file/save")
async def api_config_file_save(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    try:
        res = config_files.save_file(data["name"], data.get("content", ""))
        return {"status": "ok", **res}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# ──────────── ЖУРНАЛ ОШИБОК (файл) ────────────
@router.get("/api/errors")
async def api_errors(request: Request, level: str = "all"):
    if not _is_authed(request):
        return _need_login_json()
    errors = await error_log.get_recent_errors(level=level, limit=300)
    last24 = await error_log.count_errors_since(24)
    return {"status": "ok", "errors": errors, "last24": last24}


@router.post("/api/errors/clear")
async def api_errors_clear(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    await error_log.clear_errors()
    return {"status": "ok"}


# ──────────── БОТ: СООБЩЕНИЯ ────────────
@router.get("/api/bot/messages")
async def api_bot_messages(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    async with request.app.state.db.acquire() as conn:
        items = await bot_content.list_messages(conn)
    return {"status": "ok", "items": items}


@router.post("/api/bot/message/save")
async def api_bot_message_save(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    if not data.get("key"):
        return {"status": "error", "msg": "Не указан ключ"}
    try:
        async with request.app.state.db.acquire() as conn:
            await bot_content.update_message(conn, data["key"], data.get("text", ""))
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# ──────────── БОТ: КНОПКИ ────────────
@router.get("/api/bot/buttons")
async def api_bot_buttons(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    async with request.app.state.db.acquire() as conn:
        items = await bot_content.list_buttons(conn)
    return {"status": "ok", "items": items, "actions": bot_content.ACTIONS, "menus": bot_content.MENUS}


@router.post("/api/bot/button/save")
async def api_bot_button_save(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    if not data.get("menu") or not data.get("text") or not data.get("action"):
        return {"status": "error", "msg": "Нужны меню, действие и текст"}
    try:
        async with request.app.state.db.acquire() as conn:
            await bot_content.save_button(conn, data)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@router.post("/api/bot/button/delete")
async def api_bot_button_delete(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    try:
        async with request.app.state.db.acquire() as conn:
            await bot_content.delete_button(conn, data["id"])
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@router.post("/api/bot/button/toggle")
async def api_bot_button_toggle(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    try:
        async with request.app.state.db.acquire() as conn:
            st = await bot_content.toggle_button(conn, data["id"])
        return {"status": "ok", "enabled": st}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# ──────────── РАССЫЛКА ────────────
@router.post("/api/broadcast/start")
async def api_broadcast_start(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    ok, m = broadcast.start(data.get("text", ""), data.get("buttons"))
    return {"status": "ok" if ok else "error", "msg": m}


@router.get("/api/broadcast/status")
async def api_broadcast_status(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    return {"status": "ok", "progress": broadcast.get_progress()}


# ──────────── СИСТЕМА (перезапуск) ────────────
@router.get("/api/system/status")
async def api_system_status(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    return {"status": "ok",
            "sihaweb": system_control.service_status("sihaweb"),
            "sihabot": system_control.service_status("sihabot")}


@router.post("/api/system/restart")
async def api_system_restart(request: Request):
    if not _is_authed(request):
        return _need_login_json()
    data = await request.json()
    ok, m = system_control.restart_service(data.get("service", ""))
    return {"status": "ok" if ok else "error", "msg": m}


# ════════════ HTML ════════════
NO_PASSWORD_HTML = """<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<title>SihaVPN Admin</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gray-900 text-white flex items-center justify-center min-h-screen font-sans">
<div class="max-w-md p-8 bg-gray-800 rounded-2xl border border-yellow-500/40 text-center">
<h1 class="text-2xl font-bold text-yellow-400 mb-3">Нужна настройка</h1>
<p class="text-gray-300">Добавь в файл <code class="text-green-400">.env</code> строку:</p>
<pre class="bg-black/60 text-green-400 p-3 rounded-lg mt-3 text-sm text-left">ADMIN_PASSWORD=твой_пароль</pre>
<p class="text-gray-400 text-sm mt-3">и перезапусти веб-сервер.</p></div></body></html>"""

LOGIN_HTML = """<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SihaVPN | Вход в админку</title>
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet"></head>
<body class="bg-gray-900 text-white flex items-center justify-center min-h-screen font-sans bg-gradient-to-b from-gray-900 to-black">
<div class="w-full max-w-sm p-6">
  <h1 class="text-3xl font-bold text-center mb-8 text-transparent bg-clip-text bg-gradient-to-r from-green-400 to-blue-500">SihaVPN Admin</h1>
  <div class="bg-gray-800 rounded-3xl p-6 border border-gray-700 shadow-lg">
    <p class="text-gray-400 text-sm mb-2">Пароль администратора</p>
    <input id="pass" type="password" autofocus
      class="w-full bg-gray-900 border border-gray-600 rounded-xl px-4 py-3 mb-4 focus:outline-none focus:border-green-500 transition"
      onkeydown="if(event.key==='Enter')doLogin()">
    <button onclick="doLogin()" class="w-full bg-gradient-to-r from-green-500 to-green-600 hover:from-green-400 hover:to-green-500 font-bold py-3 rounded-xl transition">
      <i class="fa-solid fa-right-to-bracket mr-2"></i> Войти</button>
    <p id="err" class="text-red-400 text-sm mt-3 text-center hidden"></p>
  </div></div>
<script>
async function doLogin(){
  const err=document.getElementById('err'); err.classList.add('hidden');
  const r=await fetch('/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('pass').value})});
  const j=await r.json();
  if(j.status==='ok'){location.href='/admin';} else {err.innerText=j.msg||'Ошибка'; err.classList.remove('hidden');}
}
</script></body></html>"""

DASHBOARD_HTML = """<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SihaVPN | Админ-панель</title>
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
<style>
::-webkit-scrollbar{width:8px;height:8px}::-webkit-scrollbar-thumb{background:#374151;border-radius:8px}
.modal-active{display:flex!important}
.inp{width:100%;background:#111827;border:1px solid #374151;border-radius:.75rem;padding:.6rem .8rem;margin-top:.25rem;font-size:.875rem;outline:none}
.inp:focus{border-color:#22c55e}
.tabbtn{padding:.5rem .75rem;border-radius:.5rem;transition:all .15s;white-space:nowrap}
</style></head>
<body class="bg-gray-900 text-white font-sans min-h-screen bg-gradient-to-b from-gray-900 to-black pb-20">
<div class="max-w-5xl mx-auto p-5">

  <div class="flex justify-between items-center mb-6 mt-2">
    <h1 class="text-2xl font-bold text-transparent bg-clip-text bg-gradient-to-r from-green-400 to-blue-500">SihaVPN Admin</h1>
    <a href="/admin/logout" class="text-sm text-gray-400 hover:text-white"><i class="fa-solid fa-right-from-bracket mr-1"></i> Выйти</a>
  </div>

  <div class="flex gap-1 bg-gray-800 rounded-xl p-1 mb-6 border border-gray-700 overflow-x-auto">
    <button id="tab-servers" onclick="switchTab('servers')" class="tabbtn bg-gray-700 font-semibold"><i class="fa-solid fa-server mr-1"></i> Серверы</button>
    <button id="tab-users" onclick="switchTab('users')" class="tabbtn text-gray-400"><i class="fa-solid fa-users mr-1"></i> Пользователи</button>
    <button id="tab-bypass" onclick="switchTab('bypass')" class="tabbtn text-gray-400"><i class="fa-solid fa-shuffle mr-1"></i> Bypass</button>
    <button id="tab-files" onclick="switchTab('files')" class="tabbtn text-gray-400"><i class="fa-solid fa-file-code mr-1"></i> Файлы</button>
    <button id="tab-bot" onclick="switchTab('bot')" class="tabbtn text-gray-400"><i class="fa-solid fa-robot mr-1"></i> Бот</button>
    <button id="tab-system" onclick="switchTab('system')" class="tabbtn text-gray-400"><i class="fa-solid fa-power-off mr-1"></i> Система</button>
    <button id="tab-errors" onclick="switchTab('errors')" class="tabbtn text-gray-400"><i class="fa-solid fa-triangle-exclamation mr-1"></i> Журнал <span id="err-badge" class="hidden ml-1 text-xs bg-red-500 text-white px-1.5 rounded-full"></span></button>
  </div>

  <!-- СЕРВЕРЫ -->
  <div id="view-servers">
    <div class="flex justify-between items-center mb-4">
      <h2 class="text-lg font-bold">VPN-серверы</h2>
      <button onclick="openServerModal()" class="text-sm bg-blue-500/20 text-blue-400 hover:bg-blue-500/30 px-3 py-1.5 rounded-lg font-semibold transition"><i class="fa-solid fa-plus mr-1"></i> Добавить сервер</button>
    </div>
    <div id="servers-list" class="space-y-3"><div class="text-gray-500 text-center py-8">Загрузка...</div></div>
  </div>

  <!-- ПОЛЬЗОВАТЕЛИ -->
  <div id="view-users" class="hidden">
    <div class="flex flex-wrap items-center gap-2 mb-4">
      <div class="relative flex-1 min-w-[170px]">
        <input id="u-search" placeholder="Поиск: ID, username, телефон" onkeydown="if(event.key==='Enter')applyUsers()"
          class="w-full bg-gray-800 border border-gray-700 rounded-lg pl-9 pr-3 py-2 text-sm focus:outline-none focus:border-green-500">
        <i class="fa-solid fa-magnifying-glass absolute left-3 top-1/2 -translate-y-1/2 text-gray-500 text-sm"></i>
      </div>
      <select id="u-status" onchange="applyUsers()" class="bg-gray-800 border border-gray-700 rounded-lg px-2 py-2 text-sm">
        <option value="all">Все статусы</option><option value="active">Активные</option>
        <option value="stopped">Остановленные</option><option value="with_devices">С устройствами</option>
        <option value="no_devices">Без устройств</option>
      </select>
      <select id="u-sort" onchange="applyUsers()" class="bg-gray-800 border border-gray-700 rounded-lg px-2 py-2 text-sm">
        <option value="topup_total">Сумма пополнений ↓</option><option value="topup_count">Кол-во пополнений ↓</option>
        <option value="topup_streak">Серия пополнений ↓</option><option value="balance">Баланс ↓</option>
        <option value="devices">Устройства ↓</option><option value="newest">Новые</option><option value="oldest">Старые</option>
      </select>
      <select id="u-size" onchange="applyUsers()" class="bg-gray-800 border border-gray-700 rounded-lg px-2 py-2 text-sm">
        <option value="25">25</option><option value="50">50</option><option value="100">100</option>
      </select>
      <button onclick="exportUsers()" class="bg-green-600/20 text-green-400 hover:bg-green-600/30 px-3 py-2 rounded-lg text-sm font-semibold"><i class="fa-solid fa-file-csv mr-1"></i> CSV</button>
    </div>
    <div class="bg-gray-800 rounded-2xl border border-gray-700 overflow-hidden">
      <div class="overflow-x-auto"><table class="w-full text-sm">
        <thead class="text-gray-400 text-xs uppercase tracking-wider bg-gray-900/50"><tr>
          <th class="text-left px-4 py-3">Пользователь</th>
          <th class="text-right px-3 py-3">Баланс</th>
          <th class="text-right px-3 py-3" title="Суммарно пополнено">Пополнено</th>
          <th class="text-right px-3 py-3" title="Сколько раз пополнял">Раз</th>
          <th class="text-right px-3 py-3" title="Лучшая серия дней подряд">Серия</th>
          <th class="text-center px-3 py-3">Устр.</th>
          <th class="text-right px-3 py-3">Рег.</th>
          <th class="px-3 py-3"></th>
        </tr></thead>
        <tbody id="users-rows"><tr><td colspan="8" class="text-gray-500 text-center py-8">Загрузка...</td></tr></tbody>
      </table></div>
    </div>
    <div class="flex items-center justify-between mt-4 text-sm">
      <div id="users-total" class="text-gray-400"></div>
      <div class="flex items-center gap-2">
        <button onclick="changePage(-1)" class="bg-gray-800 border border-gray-700 hover:bg-gray-700 px-3 py-1.5 rounded-lg"><i class="fa-solid fa-chevron-left"></i></button>
        <span id="users-page" class="text-gray-300"></span>
        <button onclick="changePage(1)" class="bg-gray-800 border border-gray-700 hover:bg-gray-700 px-3 py-1.5 rounded-lg"><i class="fa-solid fa-chevron-right"></i></button>
      </div>
    </div>
  </div>

  <!-- BYPASS -->
  <div id="view-bypass" class="hidden">
    <div class="flex justify-between items-center mb-3">
      <h2 class="text-lg font-bold">Адреса обхода (Bypass)</h2>
      <button onclick="openBypassModal()" class="text-sm bg-blue-500/20 text-blue-400 hover:bg-blue-500/30 px-3 py-1.5 rounded-lg font-semibold"><i class="fa-solid fa-plus mr-1"></i> Добавить</button>
    </div>
    <p class="text-xs text-gray-500 mb-4">Если у inbound в 3x-ui в названии есть слово <b class="text-gray-300">BYPASS</b>, в ссылку вместо адреса сервера подставляется один из этих адресов (по очереди: ОБХОД 1, ОБХОД 2…). Можно держать 0, 1 или несколько — выключенные не используются.</p>
    <div id="bypass-list" class="space-y-3"><div class="text-gray-500 text-center py-8">Загрузка...</div></div>
  </div>

  <!-- ФАЙЛЫ -->
  <div id="view-files" class="hidden">
    <div class="flex flex-wrap items-center gap-2 mb-3">
      <h2 class="text-lg font-bold mr-2">Конфиг-файлы</h2>
      <select id="file-select" onchange="loadFileContent()" class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm"></select>
      <button onclick="saveFile()" class="bg-green-600 hover:bg-green-500 px-4 py-2 rounded-lg text-sm font-semibold"><i class="fa-solid fa-floppy-disk mr-1"></i> Сохранить</button>
    </div>
    <p id="file-hint" class="text-xs text-yellow-400/80 mb-2"></p>
    <textarea id="file-content" spellcheck="false"
      class="w-full h-[60vh] bg-black/60 border border-gray-700 rounded-xl p-4 font-mono text-xs text-green-300 focus:outline-none focus:border-green-500 resize-none" placeholder="Выберите файл..."></textarea>
    <p id="file-result" class="text-sm mt-2 hidden"></p>
  </div>

  <!-- ВКЛАДКА: БОТ -->
  <div id="view-bot" class="hidden">
    <div class="flex bg-gray-800 rounded-xl p-1 mb-5 border border-gray-700 max-w-md">
      <button id="bsub-messages" onclick="switchBot('messages')" class="tabbtn bg-gray-700 font-semibold text-sm">Сообщения</button>
      <button id="bsub-buttons" onclick="switchBot('buttons')" class="tabbtn text-gray-400 text-sm">Кнопки</button>
      <button id="bsub-broadcast" onclick="switchBot('broadcast')" class="tabbtn text-gray-400 text-sm">Рассылка</button>
    </div>

    <div id="bot-messages">
      <p class="text-xs text-gray-500 mb-3">Тексты сообщений бота. Поддерживается HTML. Плейсхолдеры вида <code>{channel_url}</code> подставляются автоматически.</p>
      <div id="messages-list" class="space-y-3"><div class="text-gray-500 text-center py-8">Загрузка...</div></div>
    </div>

    <div id="bot-buttons" class="hidden">
      <div class="flex justify-between items-center mb-3">
        <p class="text-xs text-gray-500 max-w-lg">Кнопки меню. Можно переименовать, выключить, подвинуть (ряд/позиция) и добавить. «Действие» — встроенное поведение; «Сообщение» — кнопка показывает выбранный текст.</p>
        <button onclick="openBtnModal()" class="text-sm bg-blue-500/20 text-blue-400 hover:bg-blue-500/30 px-3 py-1.5 rounded-lg font-semibold whitespace-nowrap"><i class="fa-solid fa-plus mr-1"></i> Добавить</button>
      </div>
      <div id="buttons-list" class="space-y-4"><div class="text-gray-500 text-center py-8">Загрузка...</div></div>
    </div>

    <div id="bot-broadcast" class="hidden">
      <p class="text-xs text-gray-500 mb-3">Сообщение уйдёт всем, кто запускал бота. HTML поддерживается. Идёт в фоне (~20 сообщений/с).</p>
      <textarea id="bc-text" placeholder="Текст рассылки..." class="w-full h-40 bg-black/60 border border-gray-700 rounded-xl p-4 text-sm focus:outline-none focus:border-green-500 resize-none mb-3"></textarea>
      <div class="flex items-center gap-3 mb-4">
        <button onclick="startBroadcast()" id="bc-start" class="bg-green-600 hover:bg-green-500 px-5 py-2.5 rounded-xl font-bold"><i class="fa-solid fa-paper-plane mr-1"></i> Отправить всем</button>
        <div id="bc-progress" class="text-sm text-gray-400"></div>
      </div>
    </div>
  </div>

  <!-- ВКЛАДКА: СИСТЕМА -->
  <div id="view-system" class="hidden">
    <h2 class="text-lg font-bold mb-1">Управление сервисами</h2>
    <p class="text-xs text-gray-500 mb-5">Перезапуск через <code>systemctl</code>. Перезапуск веба ненадолго прервёт работу панели.</p>
    <div class="grid sm:grid-cols-2 gap-4 max-w-2xl">
      <div class="bg-gray-800 rounded-2xl p-5 border border-gray-700">
        <div class="flex items-center justify-between mb-3">
          <div class="font-bold"><i class="fa-solid fa-robot mr-2 text-blue-400"></i> Бот <span class="text-xs text-gray-500">sihabot</span></div>
          <span id="st-sihabot" class="text-xs px-2 py-0.5 rounded-full bg-gray-700 text-gray-400">…</span>
        </div>
        <button onclick="restartService('sihabot')" class="w-full bg-blue-600/20 text-blue-300 hover:bg-blue-600/30 py-2.5 rounded-xl font-semibold"><i class="fa-solid fa-arrows-rotate mr-1"></i> Перезапустить бота</button>
      </div>
      <div class="bg-gray-800 rounded-2xl p-5 border border-gray-700">
        <div class="flex items-center justify-between mb-3">
          <div class="font-bold"><i class="fa-solid fa-globe mr-2 text-green-400"></i> Веб <span class="text-xs text-gray-500">sihaweb</span></div>
          <span id="st-sihaweb" class="text-xs px-2 py-0.5 rounded-full bg-gray-700 text-gray-400">…</span>
        </div>
        <button onclick="restartService('sihaweb')" class="w-full bg-yellow-600/20 text-yellow-300 hover:bg-yellow-600/30 py-2.5 rounded-xl font-semibold"><i class="fa-solid fa-arrows-rotate mr-1"></i> Перезапустить веб</button>
      </div>
    </div>
    <p id="sys-result" class="text-sm mt-4 hidden"></p>
  </div>

  <!-- ОШИБКИ -->
  <div id="view-errors" class="hidden">
    <div class="flex flex-wrap justify-between items-center mb-4 gap-2">
      <h2 class="text-lg font-bold">Журнал ошибок <span class="text-xs text-gray-500">(logs/errors.jsonl)</span></h2>
      <div class="flex items-center gap-2">
        <select id="err-filter" onchange="loadErrors()" class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm">
          <option value="all">Все</option><option value="error">Только ошибки</option>
          <option value="warning">Предупреждения</option><option value="info">Инфо</option>
        </select>
        <button onclick="loadErrors()" class="bg-gray-800 hover:bg-gray-700 border border-gray-700 px-3 py-1.5 rounded-lg text-sm"><i class="fa-solid fa-rotate"></i></button>
        <button onclick="clearErrors()" class="bg-red-600/20 text-red-400 hover:bg-red-600/30 px-3 py-1.5 rounded-lg text-sm"><i class="fa-solid fa-trash-can mr-1"></i> Очистить</button>
      </div>
    </div>
    <div id="errors-list" class="space-y-2"><div class="text-gray-500 text-center py-8">Загрузка...</div></div>
  </div>
</div>

<div id="backdrop" class="hidden fixed inset-0 bg-black/80 backdrop-blur-sm z-40" onclick="closeModal()"></div>

<!-- МОДАЛКА: сообщение бота -->
<div id="botmsg-modal" class="hidden fixed inset-0 z-50 p-4 items-center justify-center overflow-y-auto">
  <div class="bg-gray-800 w-full max-w-2xl rounded-3xl p-6 border border-gray-700 shadow-2xl my-auto">
    <div class="flex justify-between items-center mb-3">
      <h3 id="bm-title" class="text-xl font-bold">Сообщение</h3>
      <button onclick="closeModal()" class="text-gray-400 hover:text-white"><i class="fa-solid fa-xmark text-xl"></i></button>
    </div>
    <p id="bm-ph" class="text-xs text-gray-500 mb-2"></p>
    <input type="hidden" id="bm-key">
    <textarea id="bm-text" class="w-full h-72 bg-black/60 border border-gray-700 rounded-xl p-4 text-sm font-mono focus:outline-none focus:border-green-500 resize-none"></textarea>
    <button onclick="saveBotMessage()" class="w-full mt-4 bg-green-600 hover:bg-green-500 py-3 rounded-xl font-bold"><i class="fa-solid fa-floppy-disk mr-1"></i> Сохранить</button>
  </div>
</div>

<!-- МОДАЛКА: кнопка бота -->
<div id="botbtn-modal" class="hidden fixed inset-0 z-50 p-4 items-center justify-center overflow-y-auto">
  <div class="bg-gray-800 w-full max-w-lg rounded-3xl p-6 border border-gray-700 shadow-2xl my-auto">
    <div class="flex justify-between items-center mb-4">
      <h3 id="bb-title" class="text-xl font-bold">Кнопка</h3>
      <button onclick="closeModal()" class="text-gray-400 hover:text-white"><i class="fa-solid fa-xmark text-xl"></i></button>
    </div>
    <input type="hidden" id="bb-id">
    <div class="grid grid-cols-2 gap-3">
      <div class="col-span-2"><label class="text-xs text-gray-400">Текст кнопки</label><input id="bb-text" class="inp"></div>
      <div><label class="text-xs text-gray-400">Меню</label><select id="bb-menu" class="inp"></select></div>
      <div><label class="text-xs text-gray-400">Тип</label><select id="bb-kind" class="inp" onchange="bbKindChange()"><option value="action">Действие</option><option value="message">Сообщение</option></select></div>
      <div id="bb-action-wrap" class="col-span-2"><label class="text-xs text-gray-400">Действие</label><select id="bb-action" class="inp"></select></div>
      <div id="bb-msg-wrap" class="col-span-2 hidden"><label class="text-xs text-gray-400">Какое сообщение показать</label><select id="bb-msg" class="inp"></select></div>
      <div><label class="text-xs text-gray-400">Ряд</label><input id="bb-row" type="number" class="inp" value="0"></div>
      <div><label class="text-xs text-gray-400">Позиция в ряду</label><input id="bb-pos" type="number" class="inp" value="0"></div>
    </div>
    <label class="flex items-center gap-2 mt-4 text-sm"><input id="bb-enabled" type="checkbox" checked class="w-4 h-4"> Включена</label>
    <button onclick="saveBotButton()" class="w-full mt-4 bg-green-600 hover:bg-green-500 py-3 rounded-xl font-bold"><i class="fa-solid fa-floppy-disk mr-1"></i> Сохранить</button>
  </div>
</div>

<!-- МОДАЛКА: сервер -->
<div id="server-modal" class="hidden fixed inset-0 z-50 p-4 items-center justify-center overflow-y-auto">
  <div class="bg-gray-800 w-full max-w-lg rounded-3xl p-6 border border-gray-700 shadow-2xl my-auto">
    <div class="flex justify-between items-center mb-5"><h3 id="modal-title" class="text-xl font-bold">Новый сервер</h3>
      <button onclick="closeModal()" class="text-gray-400 hover:text-white"><i class="fa-solid fa-xmark text-xl"></i></button></div>
    <input type="hidden" id="f-id">
    <div class="grid grid-cols-2 gap-3">
      <div class="col-span-2"><label class="text-xs text-gray-400">Имя сервера</label><input id="f-name" class="inp" placeholder="Amsterdam-VDSina"></div>
      <div><label class="text-xs text-gray-400">Схема</label><select id="f-scheme" class="inp"><option>https</option><option>http</option></select></div>
      <div><label class="text-xs text-gray-400">Порт</label><input id="f-port" class="inp" value="2053"></div>
      <div class="col-span-2"><label class="text-xs text-gray-400">Host</label><input id="f-host" class="inp" placeholder="appaz.xyz"></div>
      <div class="col-span-2"><label class="text-xs text-gray-400">Base path (без слэшей)</label><input id="f-base_path" class="inp" placeholder="ac0b8e07..."></div>
      
      <div class="col-span-2">
        <label class="text-xs text-gray-400">Тип авторизации</label>
        <select id="f-auth-type" class="inp" onchange="toggleAuthFields()">
          <option value="login">Логин и пароль</option>
          <option value="token">API Токен</option>
        </select>
      </div>
      
      <div id="wrap-login" class="col-span-2 grid grid-cols-2 gap-3">
        <div><label class="text-xs text-gray-400">Логин панели</label><input id="f-login" class="inp" value="admin"></div>
        <div><label class="text-xs text-gray-400">Пароль панели</label><input id="f-password" class="inp" type="text"></div>
      </div>

      <div id="wrap-token" class="col-span-2 hidden">
        <label class="text-xs text-gray-400">API Токен (Bearer)</label>
        <input id="f-api_token" class="inp" placeholder="Токен из настроек 3x-ui">
      </div>
      </div>
    <label class="flex items-center gap-2 mt-4 text-sm"><input id="f-active" type="checkbox" checked class="w-4 h-4"> Сервер включён</label>
    <p id="test-result" class="text-sm mt-3 hidden px-3 py-2 rounded-lg"></p>
    <div class="grid grid-cols-3 gap-3 mt-5">
      <button onclick="testServer()" class="bg-gray-700 hover:bg-gray-600 py-3 rounded-xl font-semibold"><i class="fa-solid fa-plug-circle-check mr-1"></i> Тест</button>
      <button onclick="saveServer()" class="col-span-2 bg-green-600 hover:bg-green-500 py-3 rounded-xl font-bold"><i class="fa-solid fa-floppy-disk mr-1"></i> Сохранить</button>
    </div>
  </div>
</div>

<!-- МОДАЛКА: bypass -->
<div id="bypass-modal" class="hidden fixed inset-0 z-50 p-4 items-center justify-center overflow-y-auto">
  <div class="bg-gray-800 w-full max-w-sm rounded-3xl p-6 border border-gray-700 shadow-2xl my-auto">
    <div class="flex justify-between items-center mb-4"><h3 class="text-xl font-bold">Bypass-адрес</h3>
      <button onclick="closeModal()" class="text-gray-400 hover:text-white"><i class="fa-solid fa-xmark text-xl"></i></button></div>
    <input type="hidden" id="b-id">
    <label class="text-xs text-gray-400">IP или домен</label>
    <input id="b-value" class="inp mb-3" placeholder="158.160.223.89 или proxy.example.com">
    <label class="text-xs text-gray-400">Метка (необязательно)</label>
    <input id="b-label" class="inp mb-4" placeholder="например: Yandex Cloud">
    <button onclick="saveBypass()" class="w-full bg-green-600 hover:bg-green-500 font-bold py-3 rounded-xl"><i class="fa-solid fa-check mr-1"></i> Сохранить</button>
  </div>
</div>

<!-- МОДАЛКА: баланс -->
<div id="balance-modal" class="hidden fixed inset-0 z-50 p-4 items-center justify-center overflow-y-auto">
  <div class="bg-gray-800 w-full max-w-sm rounded-3xl p-6 border border-gray-700 shadow-2xl my-auto">
    <div class="flex justify-between items-center mb-4"><h3 class="text-xl font-bold">Баланс пользователя</h3>
      <button onclick="closeModal()" class="text-gray-400 hover:text-white"><i class="fa-solid fa-xmark text-xl"></i></button></div>
    <div id="bal-user" class="text-sm text-gray-400 mb-1"></div>
    <div class="text-3xl font-bold mb-4">Сейчас: <span id="bal-current">0</span> <span class="text-lg text-gray-400">₽</span></div>
    <input type="hidden" id="bal-uid">
    <label class="text-xs text-gray-400">Новый баланс, ₽</label>
    <input id="bal-input" type="number" step="0.01" class="inp mb-3">
    <div class="grid grid-cols-4 gap-2 mb-4">
      <button onclick="balQuick(100)" class="bg-gray-700 hover:bg-gray-600 py-2 rounded-lg text-sm">+100</button>
      <button onclick="balQuick(300)" class="bg-gray-700 hover:bg-gray-600 py-2 rounded-lg text-sm">+300</button>
      <button onclick="balQuick(500)" class="bg-gray-700 hover:bg-gray-600 py-2 rounded-lg text-sm">+500</button>
      <button onclick="balZero()" class="bg-red-600/20 text-red-400 hover:bg-red-600/30 py-2 rounded-lg text-sm">0</button>
    </div>
    <button onclick="saveBalance()" class="w-full bg-green-600 hover:bg-green-500 font-bold py-3 rounded-xl"><i class="fa-solid fa-check mr-1"></i> Сохранить</button>
  </div>
</div>

<!-- МОДАЛКА: карточка пользователя -->
<div id="detail-modal" class="hidden fixed inset-0 z-50 p-4 items-center justify-center overflow-y-auto">
  <div class="bg-gray-800 w-full max-w-lg rounded-3xl p-6 border border-gray-700 shadow-2xl my-auto max-h-[88vh] flex flex-col">
    <div class="flex justify-between items-center mb-4">
      <h3 class="text-xl font-bold">Карточка пользователя</h3>
      <button onclick="closeModal()" class="text-gray-400 hover:text-white"><i class="fa-solid fa-xmark text-xl"></i></button>
    </div>
    <div id="detail-body" class="overflow-y-auto pr-1"><div class="text-gray-500 text-center py-8">Загрузка...</div></div>
  </div>
</div>

<script>
let servers=[], users=[], bypassItems=[];
let uState={search:'',status:'all',sort:'topup_total',page:1,page_size:25,pages:1};

function switchTab(t){
  ['servers','users','bypass','files','bot','system','errors'].forEach(n=>{
    document.getElementById('view-'+n).classList.toggle('hidden', n!==t);
    document.getElementById('tab-'+n).className='tabbtn '+(n===t?'bg-gray-700 font-semibold':'text-gray-400');
  });
  if(t==='users') loadUsers();
  if(t==='bypass') loadBypass();
  if(t==='files') loadFilesList();
  if(t==='bot') switchBot(botSub);
  if(t==='system') loadSystem();
  if(t==='errors') loadErrors();
}

/* ---- СЕРВЕРЫ ---- */
function toggleAuthFields() {
  const isToken = document.getElementById('f-auth-type').value === 'token';
  document.getElementById('wrap-login').classList.toggle('hidden', isToken);
  document.getElementById('wrap-token').classList.toggle('hidden', !isToken);
}

async function loadServers(){
  const j=await(await fetch('/admin/api/servers')).json(); servers=j.servers||[];
  const box=document.getElementById('servers-list');
  if(!servers.length){box.innerHTML='<div class="text-gray-500 text-center py-8">Серверов нет. Добавьте первый.</div>';return;}
  box.innerHTML=servers.map(s=>{
    const dot=s.is_active?'bg-yellow-500 animate-pulse':'bg-gray-600';
    const st=s.is_active?'Проверяю…':'Выключен';
    return `<div class="bg-gray-800 rounded-2xl p-4 border border-gray-700 flex items-center justify-between gap-3" data-server-id="${s.id}">
      <div class="min-w-0"><div class="font-bold flex items-center gap-2"><span class="w-2.5 h-2.5 rounded-full ${dot}" data-dot></span> ${esc(s.name)} <span class="text-xs text-gray-500" data-status>${st}</span></div>
      <div class="text-xs text-gray-400 mt-1 truncate">${esc(s.scheme)}://${esc(s.host)}:${s.port}/${esc(s.base_path)}</div>
      <div class="text-xs mt-1 hidden" data-health></div></div>
      <div class="flex items-center gap-2 flex-shrink-0">
        <button onclick="testServerById(${s.id})" title="Проверить" class="bg-gray-700 hover:bg-gray-600 w-9 h-9 rounded-lg"><i class="fa-solid fa-plug-circle-check"></i></button>
        <button onclick="toggleServer(${s.id})" title="Вкл/выкл" class="bg-gray-700 hover:bg-gray-600 w-9 h-9 rounded-lg"><i class="fa-solid fa-power-off ${s.is_active?'text-green-400':'text-gray-500'}"></i></button>
        <button onclick="openServerModal(${s.id})" title="Изменить" class="bg-gray-700 hover:bg-gray-600 w-9 h-9 rounded-lg"><i class="fa-solid fa-pen"></i></button>
        <button onclick="deleteServer(${s.id})" title="Удалить" class="bg-red-600/20 text-red-400 hover:bg-red-600/30 w-9 h-9 rounded-lg"><i class="fa-solid fa-trash-can"></i></button>
      </div></div>`;
  }).join('');
  refreshServersHealth();
}
async function refreshServersHealth(){
  try{
    const j=await(await fetch('/admin/api/servers/health')).json();
    (j.results||[]).forEach(r=>{
      const row=document.querySelector(`[data-server-id="${r.id}"]`); if(!row)return;
      const dot=row.querySelector('[data-dot]'), status=row.querySelector('[data-status]'), health=row.querySelector('[data-health]');
      dot.classList.remove('bg-yellow-500','animate-pulse','bg-gray-600','bg-green-500','bg-red-500');
      if(r.ok){
        dot.classList.add('bg-green-500');
        status.innerText='Включён';
        status.className='text-xs text-green-400';
        health.classList.add('hidden');
      }else{
        dot.classList.add('bg-red-500');
        status.innerText='ОШИБКА';
        status.className='text-xs text-red-400 font-semibold';
        health.classList.remove('hidden');
        health.className='text-xs text-red-400 mt-1 break-all';
        health.innerText=r.msg||'недоступен';
      }
    });
  }catch(e){/* пусть остаётся жёлтый — пользователь увидит что чек не прошёл */}
}
function openServerModal(id){
  const s=id?servers.find(x=>x.id===id):null;
  document.getElementById('modal-title').innerText=s?'Изменить сервер':'Новый сервер';
  document.getElementById('f-id').value=s?s.id:'';
  document.getElementById('f-name').value=s?s.name:'';
  document.getElementById('f-scheme').value=s?s.scheme:'https';
  document.getElementById('f-port').value=s?s.port:'2053';
  document.getElementById('f-host').value=s?s.host:'';
  document.getElementById('f-base_path').value=s?s.base_path:'';
  document.getElementById('f-login').value=s?s.login:'admin';
  document.getElementById('f-password').value=s?s.password:'';
  
  // Добавляем загрузку токена
  document.getElementById('f-api_token').value=s?(s.api_token||''):'';
  
  // Переключаем интерфейс в зависимости от того, есть ли токен
  document.getElementById('f-auth-type').value=(s && s.api_token) ? 'token' : 'login';
  toggleAuthFields();
  
  document.getElementById('f-active').checked=s?s.is_active:true;
  document.getElementById('test-result').classList.add('hidden');
  showModal('server-modal');
}
function serverForm() {
  const authType = document.getElementById('f-auth-type').value;
  return {
    id: document.getElementById('f-id').value || null,
    name: document.getElementById('f-name').value.trim(),
    scheme: document.getElementById('f-scheme').value,
    port: parseInt(document.getElementById('f-port').value) || 2053,
    host: document.getElementById('f-host').value.trim(),
    base_path: document.getElementById('f-base_path').value.trim().replace(/^\/+|\/+$/g, ''),
    login: authType === 'login' ? document.getElementById('f-login').value.trim() : "",
    password: authType === 'login' ? document.getElementById('f-password').value : "",
    api_token: authType === 'token' ? document.getElementById('f-api_token').value.trim() : "",
    is_active: document.getElementById('f-active').checked
  };
}
async function saveServer(){const j=await(await fetch('/admin/api/server/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(serverForm())})).json();
  if(j.status==='ok'){closeModal();loadServers();}else alert(j.msg||'Ошибка');}
async function deleteServer(id){if(!confirm('Удалить сервер? Клиенты на нём перестанут получать ссылки.'))return;
  await fetch('/admin/api/server/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});loadServers();}
async function toggleServer(id){await fetch('/admin/api/server/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});loadServers();}
function showTest(el,j){el.classList.remove('hidden','bg-green-500/20','text-green-400','bg-red-500/20','text-red-400','bg-gray-700','text-gray-300');
  const ok=j.status==='ok'; el.classList.add(ok?'bg-green-500/20':'bg-red-500/20',ok?'text-green-400':'text-red-400'); el.innerText=(ok?'✅ ':'❌ ')+(j.msg||'');}
async function testServer(){const el=document.getElementById('test-result');el.classList.remove('hidden');el.className='text-sm mt-3 px-3 py-2 rounded-lg bg-gray-700 text-gray-300';el.innerText='Проверяю...';
  showTest(el, await(await fetch('/admin/api/server/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(serverForm())})).json());}
async function testServerById(id){const j=await(await fetch('/admin/api/server/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})})).json();alert((j.status==='ok'?'✅ ':'❌ ')+(j.msg||''));}

/* ---- ПОЛЬЗОВАТЕЛИ ---- */
function applyUsers(){uState.page=1;loadUsers();}
function changePage(d){const np=uState.page+d;if(np<1||np>uState.pages)return;uState.page=np;loadUsers();}
function userQuery(){return new URLSearchParams({search:document.getElementById('u-search').value.trim(),status:document.getElementById('u-status').value,sort:document.getElementById('u-sort').value});}
async function loadUsers(){
  uState.page_size=parseInt(document.getElementById('u-size').value);
  const qs=userQuery(); qs.set('page',uState.page); qs.set('page_size',uState.page_size);
  const j=await(await fetch('/admin/api/users?'+qs.toString())).json();
  users=j.users||[]; uState.pages=j.pages||1;
  const tb=document.getElementById('users-rows');
  if(!users.length){tb.innerHTML='<tr><td colspan="8" class="text-gray-500 text-center py-8">Ничего не найдено</td></tr>';}
  else tb.innerHTML=users.map(u=>{
    const active=u.device_count>0&&u.balance>=u.device_count*3.33;
    const dot=u.device_count===0?'bg-gray-600':(active?'bg-green-500':'bg-red-500');
    const name=u.username?'@'+esc(u.username):'—';
    return `<tr onclick="openUserDetail(${u.user_id})" class="border-t border-gray-700/60 hover:bg-gray-700/30 cursor-pointer">
      <td class="px-4 py-3"><div class="flex items-center gap-2"><span class="w-2 h-2 rounded-full ${dot}"></span>
        <div><div class="font-semibold">${name}</div><div class="text-xs text-gray-500 font-mono">${u.user_id}${u.phone?' · '+esc(u.phone):''}</div></div></div></td>
      <td class="px-3 py-3 text-right font-bold ${u.balance<=0?'text-red-400':''}">${u.balance.toFixed(2)}</td>
      <td class="px-3 py-3 text-right text-green-400">${u.topup_total.toFixed(0)}₽</td>
      <td class="px-3 py-3 text-right text-gray-300">${u.topup_count}</td>
      <td class="px-3 py-3 text-right text-gray-300">${u.topup_streak}</td>
      <td class="px-3 py-3 text-center text-gray-300">${u.device_count}</td>
      <td class="px-3 py-3 text-right text-xs text-gray-500">${esc(u.created)}</td>
      <td class="px-3 py-3 text-right"><button onclick="event.stopPropagation();openBalance(${u.user_id})" class="bg-blue-500/20 text-blue-400 hover:bg-blue-500/30 px-3 py-1.5 rounded-lg text-xs font-semibold"><i class="fa-solid fa-wallet mr-1"></i> Баланс</button></td></tr>`;
  }).join('');
  document.getElementById('users-total').innerText='Всего: '+(j.total||0);
  document.getElementById('users-page').innerText='Стр. '+uState.page+' / '+uState.pages;
}
function exportUsers(){window.open('/admin/api/users/export?'+userQuery().toString(),'_blank');}
function openBalance(uid){const u=users.find(x=>x.user_id===uid);if(!u)return;
  document.getElementById('bal-uid').value=uid;
  document.getElementById('bal-user').innerText=(u.username?'@'+u.username+' · ':'')+'ID '+uid;
  document.getElementById('bal-current').innerText=u.balance.toFixed(2);
  document.getElementById('bal-input').value=u.balance.toFixed(2);
  showModal('balance-modal');}
function balQuick(a){const c=parseFloat(document.getElementById('bal-current').innerText)||0;document.getElementById('bal-input').value=(c+a).toFixed(2);}
function balZero(){document.getElementById('bal-input').value='0.00';}
async function saveBalance(){const uid=parseInt(document.getElementById('bal-uid').value);const nb=parseFloat(document.getElementById('bal-input').value);
  if(isNaN(nb)){alert('Введите число');return;}
  const j=await(await fetch('/admin/api/user/balance',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:uid,balance:nb})})).json();
  if(j.status==='ok'){closeModal();loadUsers();}else alert(j.msg||'Ошибка');}

async function openUserDetail(uid){
  showModal('detail-modal');
  document.getElementById('detail-body').innerHTML='<div class="text-gray-500 text-center py-8">Загрузка...</div>';
  const j=await(await fetch('/admin/api/user/'+uid)).json();
  if(j.status!=='ok'){document.getElementById('detail-body').innerHTML='<div class="text-red-400 text-center py-8">'+esc(j.msg||'Ошибка')+'</div>';return;}
  const u=j.user;
  const devHtml=(j.devices||[]).length?j.devices.map(d=>{
    const ic=d.os==='Android'?'fa-android text-green-400':d.os==='iOS'?'fa-apple text-gray-200':'fa-windows text-blue-400';
    return `<div class="bg-gray-900/60 rounded-xl p-3 border border-gray-700 mb-2">
      <div class="flex items-center justify-between gap-2"><div class="flex items-center gap-2 min-w-0">
        <i class="fa-brands ${ic}"></i><div class="min-w-0"><div class="font-semibold text-sm truncate">${esc(d.short_id)} ${esc(d.name||'')}</div>
        <div class="text-xs text-gray-500">${esc(d.os||'')} · ${esc(d.created)}</div></div></div>
        <button onclick="copyText('${encodeURIComponent(d.sub_url)}',this)" class="text-xs bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded-lg flex-shrink-0"><i class="fa-regular fa-copy"></i> ссылка</button></div></div>`;
  }).join(''):'<div class="text-gray-500 text-sm py-2">Устройств нет</div>';
  const histHtml=(j.history||[]).length?j.history.map(h=>{
    const ic=h.type==='income'?'text-green-400 fa-arrow-down':h.type==='expense'?'text-red-400 fa-arrow-up':'text-blue-400 fa-microchip';
    return `<div class="flex items-center justify-between bg-gray-900/60 p-2.5 rounded-xl border border-gray-700 mb-1.5">
      <div class="flex items-center gap-2 min-w-0"><i class="fa-solid ${ic}"></i><div class="min-w-0"><div class="text-sm font-medium truncate">${esc(h.title||'')}</div>
      ${h.descr?`<div class="text-xs text-gray-500 truncate">${esc(h.descr)}</div>`:''}<div class="text-[10px] text-gray-600">${esc(h.ts)}</div></div></div>
      <div class="text-sm font-bold flex-shrink-0">${esc(h.amount||'')}</div></div>`;
  }).join(''):'<div class="text-gray-500 text-sm py-2">Истории нет</div>';
  const cab=u.cabinet_url?`<a href="${esc(u.cabinet_url)}" target="_blank" class="text-blue-400 text-xs hover:underline"><i class="fa-solid fa-up-right-from-square mr-1"></i>кабинет</a>`:'';
  document.getElementById('detail-body').innerHTML=`
    <div class="bg-gray-900/60 rounded-2xl p-4 border border-gray-700 mb-4">
      <div class="flex justify-between items-start">
        <div><div class="font-bold text-lg">${u.username?'@'+esc(u.username):'—'}</div>
        <div class="text-xs text-gray-500 font-mono">ID ${u.user_id}${u.phone?' · '+esc(u.phone):''}</div>
        <div class="text-xs text-gray-500 mt-0.5">Регистрация: ${esc(u.created)} ${cab}</div></div>
        <div class="text-right"><div class="text-2xl font-bold ${u.balance<=0?'text-red-400':'text-green-400'}">${u.balance.toFixed(2)}₽</div>
        <button onclick="openBalance(${u.user_id})" class="mt-1 text-xs bg-blue-500/20 text-blue-400 hover:bg-blue-500/30 px-3 py-1 rounded-lg font-semibold"><i class="fa-solid fa-wallet mr-1"></i>изменить</button></div>
      </div></div>
    <h4 class="text-sm font-bold text-gray-400 uppercase tracking-wider mb-2">Устройства (${(j.devices||[]).length})</h4>${devHtml}
    <h4 class="text-sm font-bold text-gray-400 uppercase tracking-wider mb-2 mt-4">История</h4>${histHtml}`;
}

/* ---- BYPASS ---- */
async function loadBypass(){
  const j=await(await fetch('/admin/api/bypass')).json(); bypassItems=j.items||[];
  const box=document.getElementById('bypass-list');
  if(!bypassItems.length){box.innerHTML='<div class="text-gray-500 text-center py-8">Список пуст. BYPASS-ссылки будут использовать адрес самого сервера.</div>';return;}
  box.innerHTML=bypassItems.map((b,i)=>{
    const dot=b.is_active?'bg-green-500':'bg-gray-600';
    return `<div class="bg-gray-800 rounded-2xl p-4 border border-gray-700 flex items-center justify-between gap-3">
      <div class="min-w-0"><div class="font-bold flex items-center gap-2"><span class="w-2.5 h-2.5 rounded-full ${dot}"></span> ${esc(b.value)}</div>
      <div class="text-xs text-gray-500 mt-1">${b.is_active?('ОБХОД '+(i+1)):'выключен'}${b.label?' · '+esc(b.label):''}</div></div>
      <div class="flex items-center gap-2 flex-shrink-0">
        <button onclick="toggleBypass(${b.id})" class="bg-gray-700 hover:bg-gray-600 w-9 h-9 rounded-lg"><i class="fa-solid fa-power-off ${b.is_active?'text-green-400':'text-gray-500'}"></i></button>
        <button onclick="openBypassModal(${b.id})" class="bg-gray-700 hover:bg-gray-600 w-9 h-9 rounded-lg"><i class="fa-solid fa-pen"></i></button>
        <button onclick="deleteBypass(${b.id})" class="bg-red-600/20 text-red-400 hover:bg-red-600/30 w-9 h-9 rounded-lg"><i class="fa-solid fa-trash-can"></i></button>
      </div></div>`;
  }).join('');
}
function openBypassModal(id){const b=id?bypassItems.find(x=>x.id===id):null;
  document.getElementById('b-id').value=b?b.id:'';
  document.getElementById('b-value').value=b?b.value:'';
  document.getElementById('b-label').value=b?(b.label||''):'';
  showModal('bypass-modal');}
async function saveBypass(){const body={id:document.getElementById('b-id').value||null,value:document.getElementById('b-value').value.trim(),label:document.getElementById('b-label').value.trim()};
  if(!body.value){alert('Укажите IP или домен');return;}
  const j=await(await fetch('/admin/api/bypass/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(j.status==='ok'){closeModal();loadBypass();}else alert(j.msg||'Ошибка');}
async function deleteBypass(id){if(!confirm('Удалить адрес обхода?'))return;
  await fetch('/admin/api/bypass/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});loadBypass();}
async function toggleBypass(id){await fetch('/admin/api/bypass/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});loadBypass();}

/* ---- ФАЙЛЫ ---- */
let filesMeta=[];
async function loadFilesList(){
  const j=await(await fetch('/admin/api/config/files')).json(); filesMeta=j.files||[];
  const sel=document.getElementById('file-select');
  sel.innerHTML=filesMeta.map(f=>`<option value="${esc(f.name)}">${esc(f.label)}${f.exists?'':' (нет файла)'}</option>`).join('');
  loadFileContent();
}
async function loadFileContent(){
  const name=document.getElementById('file-select').value; if(!name)return;
  const meta=filesMeta.find(f=>f.name===name)||{};
  document.getElementById('file-hint').innerText=(meta.restart?'⚠️ Применится только после перезапуска веб-сервера. ':'')+(meta.hint||'');
  document.getElementById('file-result').classList.add('hidden');
  const j=await(await fetch('/admin/api/config/file?name='+encodeURIComponent(name))).json();
  document.getElementById('file-content').value=j.status==='ok'?j.content:('# ошибка: '+(j.msg||''));
}
async function saveFile(){
  const name=document.getElementById('file-select').value;
  const content=document.getElementById('file-content').value;
  const el=document.getElementById('file-result');
  const j=await(await fetch('/admin/api/config/file/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,content})})).json();
  el.classList.remove('hidden','text-green-400','text-red-400');
  if(j.status==='ok'){el.classList.add('text-green-400');el.innerText='✅ Сохранено.'+(j.restart?' Не забудьте перезапустить веб-сервер.':' Изменения уже активны.');}
  else{el.classList.add('text-red-400');el.innerText='❌ '+(j.msg||'Ошибка');}
}

/* ---- ОШИБКИ ---- */
async function loadErrors(){
  const level=document.getElementById('err-filter').value;
  const j=await(await fetch('/admin/api/errors?level='+level)).json();
  const badge=document.getElementById('err-badge');
  if(j.last24>0){badge.innerText=j.last24;badge.classList.remove('hidden');}else badge.classList.add('hidden');
  const box=document.getElementById('errors-list'); const list=j.errors||[];
  if(!list.length){box.innerHTML='<div class="text-gray-500 text-center py-8">Записей нет</div>';return;}
  box.innerHTML=list.map(e=>{
    const c=e.level==='error'?'text-red-400 border-red-500/30':e.level==='warning'?'text-yellow-400 border-yellow-500/30':'text-blue-400 border-blue-500/30';
    const ic=e.level==='error'?'fa-circle-xmark':e.level==='warning'?'fa-triangle-exclamation':'fa-circle-info';
    return `<div class="bg-gray-800 rounded-xl p-3 border ${c}"><div class="flex items-start gap-3"><i class="fa-solid ${ic} mt-1"></i>
      <div class="min-w-0 flex-1"><div class="font-semibold text-sm">${esc(e.message)}</div>
      <div class="text-xs text-gray-400 mt-0.5"><span class="font-mono">${esc(e.ts)}</span>${e.source?' · <span class="text-gray-500">'+esc(e.source)+'</span>':''}${e.server_name?' · <span class="text-gray-300">'+esc(e.server_name)+'</span>':''}</div>
      ${e.details?`<pre class="text-xs text-gray-500 mt-2 whitespace-pre-wrap break-all bg-black/40 p-2 rounded-lg">${esc(e.details)}</pre>`:''}</div></div></div>`;
  }).join('');
}
async function clearErrors(){if(!confirm('Очистить весь журнал ошибок?'))return;await fetch('/admin/api/errors/clear',{method:'POST'});loadErrors();}

/* ---- БОТ ---- */
let botSub='messages', botMessages=[], botButtons=[], botActions={}, botMenus=[], bcTimer=null;
function switchBot(s){
  botSub=s;
  ['messages','buttons','broadcast'].forEach(n=>{
    document.getElementById('bot-'+n).classList.toggle('hidden', n!==s);
    document.getElementById('bsub-'+n).className='tabbtn text-sm '+(n===s?'bg-gray-700 font-semibold':'text-gray-400');
  });
  if(s==='messages') loadBotMessages();
  if(s==='buttons') loadBotButtons();
  if(s==='broadcast') pollBroadcast();
}
async function loadBotMessages(){
  const j=await(await fetch('/admin/api/bot/messages')).json(); botMessages=j.items||[];
  document.getElementById('messages-list').innerHTML=botMessages.map(m=>`
    <div class="bg-gray-800 rounded-2xl p-4 border border-gray-700 flex items-center justify-between gap-3">
      <div class="min-w-0"><div class="font-semibold">${esc(m.title||m.key)}</div>
      <div class="text-xs text-gray-500 truncate mt-1">${esc((m.text||'').slice(0,90))}…</div></div>
      <button onclick="openBotMessage('${esc(m.key)}')" class="bg-gray-700 hover:bg-gray-600 px-3 py-1.5 rounded-lg text-sm flex-shrink-0"><i class="fa-solid fa-pen mr-1"></i> Изменить</button>
    </div>`).join('');
}
function openBotMessage(key){
  const m=botMessages.find(x=>x.key===key); if(!m)return;
  document.getElementById('bm-title').innerText=m.title||key;
  document.getElementById('bm-ph').innerText=m.placeholders?('Плейсхолдеры: '+m.placeholders):'';
  document.getElementById('bm-key').value=key;
  document.getElementById('bm-text').value=m.text||'';
  showModal('botmsg-modal');
}
async function saveBotMessage(){
  const key=document.getElementById('bm-key').value, text=document.getElementById('bm-text').value;
  const j=await(await fetch('/admin/api/bot/message/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key,text})})).json();
  if(j.status==='ok'){closeModal();loadBotMessages();}else alert(j.msg||'Ошибка');
}
async function loadBotButtons(){
  const j=await(await fetch('/admin/api/bot/buttons')).json();
  botButtons=j.items||[]; botActions=j.actions||{}; botMenus=j.menus||[];
  if(!botMessages.length){const jm=await(await fetch('/admin/api/bot/messages')).json(); botMessages=jm.items||[];}
  const byMenu={}; botButtons.forEach(b=>{(byMenu[b.menu]=byMenu[b.menu]||[]).push(b);});
  const menus=Object.keys(byMenu);
  const box=document.getElementById('buttons-list');
  if(!menus.length){box.innerHTML='<div class="text-gray-500 text-center py-8">Кнопок нет</div>';return;}
  box.innerHTML=menus.map(menu=>`
    <div><div class="text-xs font-bold text-gray-500 uppercase tracking-wider mb-2">Меню: ${esc(menu)}</div>
    ${byMenu[menu].map(b=>{
      const dot=b.enabled?'bg-green-500':'bg-gray-600';
      const kindLabel=b.kind==='message'?('сообщение: '+esc(b.msg_key||'')):('действие: '+esc(botActions[b.action]||b.action));
      return `<div class="bg-gray-800 rounded-xl p-3 border border-gray-700 flex items-center justify-between gap-2 mb-2">
        <div class="min-w-0"><div class="font-semibold flex items-center gap-2"><span class="w-2 h-2 rounded-full ${dot}"></span> ${esc(b.text)}</div>
        <div class="text-xs text-gray-500 mt-0.5">ряд ${b.row}·поз ${b.position} · ${kindLabel}</div></div>
        <div class="flex items-center gap-1.5 flex-shrink-0">
          <button onclick="toggleBotButton(${b.id})" class="bg-gray-700 hover:bg-gray-600 w-9 h-9 rounded-lg"><i class="fa-solid fa-power-off ${b.enabled?'text-green-400':'text-gray-500'}"></i></button>
          <button onclick="openBtnModal(${b.id})" class="bg-gray-700 hover:bg-gray-600 w-9 h-9 rounded-lg"><i class="fa-solid fa-pen"></i></button>
          <button onclick="deleteBotButton(${b.id})" class="bg-red-600/20 text-red-400 hover:bg-red-600/30 w-9 h-9 rounded-lg"><i class="fa-solid fa-trash-can"></i></button>
        </div></div>`;
    }).join('')}</div>`).join('');
}
function bbKindChange(){
  const k=document.getElementById('bb-kind').value;
  document.getElementById('bb-action-wrap').classList.toggle('hidden', k!=='action');
  document.getElementById('bb-msg-wrap').classList.toggle('hidden', k!=='message');
}
function openBtnModal(id){
  const b=id?botButtons.find(x=>x.id===id):null;
  document.getElementById('bb-title').innerText=b?'Изменить кнопку':'Новая кнопка';
  document.getElementById('bb-id').value=b?b.id:'';
  document.getElementById('bb-menu').innerHTML=botMenus.map(m=>`<option value="${esc(m)}">${esc(m)}</option>`).join('');
  document.getElementById('bb-action').innerHTML=Object.entries(botActions).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('');
  document.getElementById('bb-msg').innerHTML=botMessages.length?botMessages.map(m=>`<option value="${esc(m.key)}">${esc(m.title||m.key)}</option>`).join(''):'<option value="">— нет сообщений —</option>';
  document.getElementById('bb-text').value=b?b.text:'';
  document.getElementById('bb-menu').value=b?b.menu:(botMenus[0]||'main');
  document.getElementById('bb-kind').value=b?b.kind:'action';
  document.getElementById('bb-action').value=(b&&b.kind==='action')?b.action:'cabinet';
  document.getElementById('bb-msg').value=(b&&b.msg_key)?b.msg_key:'';
  document.getElementById('bb-row').value=b?b.row:0;
  document.getElementById('bb-pos').value=b?b.position:0;
  document.getElementById('bb-enabled').checked=b?b.enabled:true;
  bbKindChange();
  showModal('botbtn-modal');
}
async function saveBotButton(){
  const kind=document.getElementById('bb-kind').value;
  const body={id:document.getElementById('bb-id').value||null,
    menu:document.getElementById('bb-menu').value,
    text:document.getElementById('bb-text').value.trim(), kind,
    action: kind==='message'?'message':document.getElementById('bb-action').value,
    msg_key: kind==='message'?document.getElementById('bb-msg').value:null,
    row:parseInt(document.getElementById('bb-row').value)||0,
    position:parseInt(document.getElementById('bb-pos').value)||0,
    enabled:document.getElementById('bb-enabled').checked};
  if(!body.text){alert('Укажите текст кнопки');return;}
  if(kind==='message' && !body.msg_key){alert('Выберите сообщение');return;}
  const j=await(await fetch('/admin/api/bot/button/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(j.status==='ok'){closeModal();loadBotButtons();}else alert(j.msg||'Ошибка');
}
async function toggleBotButton(id){await fetch('/admin/api/bot/button/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});loadBotButtons();}
async function deleteBotButton(id){if(!confirm('Удалить кнопку?'))return;await fetch('/admin/api/bot/button/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});loadBotButtons();}

/* ---- РАССЫЛКА ---- */
async function startBroadcast(){
  const text=document.getElementById('bc-text').value.trim();
  if(!text){alert('Введите текст');return;}
  if(!confirm('Отправить сообщение ВСЕМ пользователям бота?'))return;
  const j=await(await fetch('/admin/api/broadcast/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})})).json();
  if(j.status!=='ok'){alert(j.msg||'Ошибка');return;}
  pollBroadcast();
}
async function pollBroadcast(){
  const j=await(await fetch('/admin/api/broadcast/status')).json(); const p=j.progress||{};
  const el=document.getElementById('bc-progress'), btn=document.getElementById('bc-start');
  if(p.running){
    btn.disabled=true; btn.classList.add('opacity-50','cursor-not-allowed');
    el.innerHTML=`⏳ Отправлено ${p.sent}/${p.total}, ошибок ${p.failed}`;
    clearTimeout(bcTimer); bcTimer=setTimeout(pollBroadcast,1500);
  } else {
    btn.disabled=false; btn.classList.remove('opacity-50','cursor-not-allowed');
    el.innerHTML=p.total?(`✅ Готово: отправлено ${p.sent}/${p.total}, ошибок ${p.failed}`+(p.last_error?` · ${esc(p.last_error)}`:'')):'';
  }
}

/* ---- СИСТЕМА ---- */
async function loadSystem(){
  const j=await(await fetch('/admin/api/system/status')).json();
  setSt('sihabot', j.sihabot); setSt('sihaweb', j.sihaweb);
}
function setSt(name,val){
  const el=document.getElementById('st-'+name); if(!el)return;
  el.innerText=val||'?';
  el.className='text-xs px-2 py-0.5 rounded-full '+(val==='active'?'bg-green-500/20 text-green-400':'bg-red-500/20 text-red-400');
}
async function restartService(name){
  if(!confirm('Перезапустить '+name+'?'))return;
  const el=document.getElementById('sys-result'); el.classList.remove('hidden'); el.className='text-sm mt-4 text-gray-300'; el.innerText='Выполняю...';
  const j=await(await fetch('/admin/api/system/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({service:name})})).json();
  el.className='text-sm mt-4 '+(j.status==='ok'?'text-green-400':'text-red-400');
  el.innerText=(j.status==='ok'?'✅ ':'❌ ')+(j.msg||'');
  setTimeout(loadSystem,2500);
}

/* ---- ОБЩЕЕ ---- */
function showModal(id){document.getElementById('backdrop').classList.remove('hidden');document.getElementById(id).classList.add('modal-active');}
function closeModal(){document.getElementById('backdrop').classList.add('hidden');document.querySelectorAll('.modal-active').forEach(el=>el.classList.remove('modal-active'));}
function copyText(enc,btn){try{navigator.clipboard.writeText(decodeURIComponent(enc));if(btn){const o=btn.innerHTML;btn.innerHTML='<i class="fa-solid fa-check"></i> ок';setTimeout(()=>btn.innerHTML=o,1200);}}catch(e){}}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

loadServers();
</script>
</body></html>"""
