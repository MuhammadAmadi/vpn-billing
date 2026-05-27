"""Веб-сервер личного кабинета + API устройств + сервер подписок (/sub/{id})."""

import base64
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import config
import db
import xray_api
from admin_panel import router as admin_router

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("web")

ROUTING_PATH = Path(__file__).resolve().parent / "routing.json"
SUB_REFRESH_SECONDS = 3600  # как часто обновлять ссылки в кэше /sub


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Веб-сервер подключается к БД...")
    app.state.db = await db.create_pool()
    try:
        yield
    finally:
        await app.state.db.close()


app = FastAPI(title="SihaVPN Cabinet", lifespan=lifespan)
app.include_router(admin_router)


class DeviceCreate(BaseModel):
    token: str
    os: str
    name: str


class DeviceManage(BaseModel):
    token: str
    device_id: str
    new_name: str = ""


# ─── /sub/{id} — сервер подписок (заменяет старый sub_server.py) ───
@app.get("/sub/{device_id}")
async def get_subscription(request: Request, device_id: str):
    async with app.state.db.acquire() as conn:
        device = await conn.fetchrow("""
            SELECT d.id, d.short_id, d.name, d.user_id, d.key_string,
                   d.links_updated_at, u.balance,
                   (SELECT COUNT(*) FROM devices WHERE user_id = d.user_id) AS device_count
            FROM devices d JOIN users u ON u.user_id = d.user_id
            WHERE d.id = $1
        """, device_id)

    if not device:
        return Response(content="User not found", status_code=404)

    balance = float(device["balance"])
    device_count = max(int(device["device_count"]), 1)
    if balance < device_count * config.PRICE_PER_DEVICE:
        return Response(content="Subscription inactive", status_code=403)

    user_id = device["user_id"]
    short_id = device["short_id"]
    email = f"user{user_id}_{short_id}"
    links_text = device["key_string"]

    last_update = device["links_updated_at"]
    now = datetime.now(timezone.utc)
    need_refresh = (
        not links_text or
        not last_update or
        (now - last_update.replace(tzinfo=timezone.utc)).total_seconds() > SUB_REFRESH_SECONDS
    )

    if need_refresh:
        raw_links = await xray_api.get_client_links_from_all_servers(device_id, email)
        if raw_links:
            links_text = "\n".join(raw_links)
            async with app.state.db.acquire() as conn:
                await conn.execute(
                    "UPDATE devices SET key_string = $1, links_updated_at = NOW() WHERE id = $2",
                    links_text, device_id,
                )
            log.info("Ссылки обновлены для %s", short_id)
        else:
            log.warning("Не удалось обновить ссылки для %s, использую кэш", short_id)

    if not links_text:
        return Response(content="No links available", status_code=404)

    encoded_links = base64.b64encode(links_text.encode("utf-8")).decode("utf-8")
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "profile-title": "SihaVPN",
        "profile-update-interval": "24",
        "Content-Disposition": "attachment; filename=sub.txt",
    }

    user_agent = request.headers.get("user-agent", "").lower()
    if "happ" in user_agent:
        try:
            with open(ROUTING_PATH, "r", encoding="utf-8") as f:
                routing_json = json.load(f)
            routing_b64 = base64.b64encode(json.dumps(routing_json).encode("utf-8")).decode("utf-8")
            headers["routing"] = f"happ://routing/onadd/{routing_b64}"
        except Exception as e:
            log.warning("Ошибка чтения routing.json: %s", e)

    return Response(content=encoded_links, headers=headers)


# ─── API кабинета ───
@app.post("/api/device/add")
async def add_device(data: DeviceCreate):
    try:
        async with app.state.db.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT user_id, balance FROM users WHERE magic_token = $1", data.token,
            )
            if not user:
                return {"status": "error", "msg": "Пользователь не найден"}
            user_id = user["user_id"]
            devices_count = await conn.fetchval(
                "SELECT COUNT(*) FROM devices WHERE user_id = $1", user_id,
            )
            required_balance = (devices_count + 1) * config.PRICE_PER_DEVICE
            if float(user["balance"]) < required_balance:
                return {"status": "error",
                        "msg": f"Для {devices_count + 1} устройств нужен баланс минимум {required_balance:.2f}₽"}

            device_id = str(uuid.uuid4())
            short_id = uuid.uuid4().hex[:4].upper()
            device_email = f"user{user_id}_{short_id}"

            raw_links = await xray_api.add_client_to_all_servers(device_id, device_email)
            if not raw_links:
                return {"status": "error", "msg": "Ошибка связи с VPN серверами. Попробуйте позже."}

            links_text = "\n".join(raw_links)
            sub_url = f"{config.CABINET_BASE_URL}/sub/{device_id}"

            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO devices (id, short_id, user_id, name, os, key_string)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    device_id, short_id, user_id, data.name, data.os, links_text,
                )
                await conn.execute(
                    """INSERT INTO transactions (user_id, type, title, description)
                       VALUES ($1, 'action', 'Добавлено устройство', $2)""",
                    user_id, f"ID {short_id} {data.name} ({data.os})",
                )
        return {"status": "ok", "key": sub_url}
    except Exception as e:
        log.exception("add_device failed")
        return {"status": "error", "msg": str(e)}


@app.post("/api/device/delete")
async def delete_device(data: DeviceManage):
    try:
        async with app.state.db.acquire() as conn:
            user_id = await conn.fetchval(
                "SELECT user_id FROM users WHERE magic_token = $1", data.token,
            )
            if not user_id:
                return {"status": "ok"}
            await xray_api.remove_client_from_all_servers(data.device_id)
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM devices WHERE id = $1 AND user_id = $2",
                    data.device_id, user_id,
                )
                await conn.execute(
                    """INSERT INTO transactions (user_id, type, title, description)
                       VALUES ($1, 'action', 'Удалено устройство', $2)""",
                    user_id, "Устройство удалено",
                )
        return {"status": "ok"}
    except Exception as e:
        log.exception("delete_device failed")
        return {"status": "error", "msg": str(e)}


@app.post("/api/device/update_key")
async def update_device_key(data: DeviceManage):
    try:
        log.info("update_key вызван: device_id=%s", data.device_id)
        async with app.state.db.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT user_id FROM users WHERE magic_token = $1", data.token,
            )
            if not user:
                return {"status": "error", "msg": "Пользователь не найден"}
            user_id = user["user_id"]

            device_row = await conn.fetchrow(
                "SELECT short_id FROM devices WHERE id = $1 AND user_id = $2",
                data.device_id, user_id,
            )
            if not device_row:
                return {"status": "error", "msg": "Устройство не найдено"}
            short_id = device_row["short_id"]

            new_device_id = str(uuid.uuid4())
            new_email = f"user{user_id}_{short_id}"
            await xray_api.update_client_uuid_on_all_servers(data.device_id, new_device_id, new_email)
            raw_links = await xray_api.get_client_links_from_all_servers(new_device_id, new_email)

            if not raw_links:
                return {"status": "error", "msg": "Ошибка связи с VPN серверами."}

            links_text = "\n".join(raw_links)
            sub_url = f"{config.CABINET_BASE_URL}/sub/{new_device_id}"

            async with conn.transaction():
                await conn.execute(
                    "UPDATE devices SET id = $1, key_string = $2 WHERE id = $3 AND user_id = $4",
                    new_device_id, links_text, data.device_id, user_id,
                )
                await conn.execute(
                    """INSERT INTO transactions (user_id, type, title, description)
                       VALUES ($1, 'action', 'Обновлен ключ', $2)""",
                    user_id, f"ID {short_id}",
                )
        return {"status": "ok", "new_key": sub_url}
    except Exception as e:
        log.exception("update_device_key failed")
        return {"status": "error", "msg": str(e)}


@app.post("/api/device/rename")
async def rename_device(data: DeviceManage):
    try:
        async with app.state.db.acquire() as conn:
            user_id = await conn.fetchval(
                "SELECT user_id FROM users WHERE magic_token = $1", data.token,
            )
            if user_id:
                await conn.execute(
                    "UPDATE devices SET name = $1 WHERE id = $2 AND user_id = $3",
                    data.new_name, data.device_id, user_id,
                )
        return {"status": "ok"}
    except Exception as e:
        log.exception("rename_device failed")
        return {"status": "error", "msg": str(e)}


@app.get("/cabinet/{token}", response_class=HTMLResponse)
async def open_cabinet(token: str):
    async with app.state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE magic_token = $1", token)
        if not user:
            return HTMLResponse(
                "<h1 style='color:white; text-align:center; margin-top:50px; font-family:sans-serif;'>❌ Ссылка устарела.</h1>",
                status_code=404,
            )

        user_id = user["user_id"]
        balance = float(user["balance"])

        db_devices = await conn.fetch("""
            SELECT id, short_id, name, os,
                   TO_CHAR(created_at, 'DD.MM.YY') AS date,
                   CAST(extract(epoch from created_at)*1000 AS BIGINT) AS timestamp
            FROM devices WHERE user_id = $1 ORDER BY created_at DESC
        """, user_id)
        devices_list = [dict(d) for d in db_devices]

        devices_count = len(devices_list) if devices_list else 1
        daily_cost = devices_count * config.PRICE_PER_DEVICE
        days_left = int(balance // daily_cost) if balance >= daily_cost else 0

        is_active = balance >= daily_cost
        header_status = _render_status_badge(is_active)

        db_history = await conn.fetch("""
            SELECT type, title, description AS desc, amount,
                   CASE WHEN created_at::date = CURRENT_DATE THEN 'Сегодня'
                        ELSE TO_CHAR(created_at, 'DD.MM.YY') END AS date,
                   TO_CHAR(created_at, 'HH24:MI') AS time
            FROM transactions WHERE user_id = $1 ORDER BY created_at DESC
        """, user_id)
        history_list = [dict(h) for h in db_history]

    return HTMLResponse(_render_cabinet(
        token=token, user_id=user_id, balance=balance, days_left=days_left,
        header_status=header_status, devices_list=devices_list, history_list=history_list,
        ref_url=config.REFERRAL_URL_TEMPLATE.format(user_id=user_id),
        price=config.PRICE_PER_DEVICE,
    ))


def _render_status_badge(is_active: bool) -> str:
    if is_active:
        return ('<div class="bg-gray-800 px-3 py-1 rounded-full text-sm text-green-400 border border-green-500/30">'
                '<i class="fa-solid fa-circle-check mr-1"></i> Активен</div>')
    return ('<div class="bg-gray-800 px-3 py-1 rounded-full text-sm text-red-400 border border-red-500/30">'
            '<i class="fa-solid fa-circle-xmark mr-1"></i> Остановлен</div>')


def _render_cabinet(*, token, user_id, balance, days_left, header_status,
                    devices_list, history_list, ref_url, price) -> str:
    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>SihaVPN | Личный Кабинет</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
        <style>::-webkit-scrollbar {{ width: 0px; background: transparent; }} .modal-active {{ display: flex !important; }}</style>
    </head>
    <body class="bg-gray-900 text-white font-sans antialiased flex justify-center min-h-screen bg-gradient-to-b from-gray-900 to-black relative pb-10">

        <div class="w-full max-w-md p-6">
            <div class="flex justify-between items-center mb-8 mt-4">
                <h1 class="text-3xl font-bold text-transparent bg-clip-text bg-gradient-to-r from-green-400 to-blue-500">SihaVPN 🛡</h1>
                {header_status}
            </div>

            <div class="bg-gray-800 rounded-3xl p-6 mb-6 shadow-lg border border-gray-700 relative overflow-hidden">
                <div class="absolute top-0 right-0 w-32 h-32 bg-green-500 rounded-full mix-blend-multiply filter blur-3xl opacity-20"></div>
                <p class="text-gray-400 text-sm mb-1">Ваш баланс</p>
                <div class="text-4xl font-bold mb-4">{balance:.2f} <span class="text-xl text-gray-400">₽</span></div>
                <p class="text-gray-400 text-sm mb-1">Остаток подписки</p>
                <div id="days-left" class="text-2xl font-semibold text-green-400">{days_left} дней</div>
                <div class="grid grid-cols-2 gap-3 mt-6">
                    <button class="bg-gradient-to-r from-green-500 to-green-600 hover:from-green-400 hover:to-green-500 text-white font-bold py-3 px-2 rounded-xl transition shadow-[0_0_15px_rgba(34,197,94,0.3)] flex justify-center items-center"><i class="fa-solid fa-wallet mr-2"></i> Пополнить</button>
                    <button onclick="openModal('history-modal')" class="bg-gray-700 hover:bg-gray-600 text-white font-bold py-3 px-2 rounded-xl transition flex justify-center items-center border border-gray-600"><i class="fa-solid fa-clock-rotate-left mr-2 text-gray-400"></i> История</button>
                </div>
            </div>

            <div class="flex justify-between items-center mb-4 mt-8">
                <h2 class="text-xl font-bold flex items-center gap-2">Активные устройства <span id="device-count" class="text-sm bg-gray-800 text-gray-300 px-2 py-0.5 rounded-full border border-gray-700">0</span></h2>
                <button onclick="openModal('add-step1-modal')" class="text-sm bg-blue-500/20 text-blue-400 hover:bg-blue-500/30 px-3 py-1.5 rounded-lg transition font-semibold"><i class="fa-solid fa-plus mr-1"></i> Добавить</button>
            </div>

            <div id="device-list" class="space-y-3"></div>
            <div id="empty-state" class="hidden text-center py-8 bg-gray-800/50 rounded-2xl border border-gray-700/50 border-dashed"><i class="fa-solid fa-mobile-screen text-4xl text-gray-600 mb-3"></i><p class="text-gray-400">Устройств пока нет</p></div>

            <div class="bg-gray-800 rounded-2xl p-4 mt-8 shadow-lg border border-gray-700">
                <p class="text-xs text-gray-400 mb-2">Ваша реферальная ссылка (3% с пополнений):</p>
                <div class="flex items-center gap-2 bg-gray-900 p-2 rounded-xl border border-gray-600">
                    <input type="text" readonly value="{ref_url}" class="w-full bg-transparent text-purple-400 text-xs outline-none select-all" id="ref-link-input">
                    <button onclick="copyRefFromInput()" class="bg-gray-700 hover:bg-gray-600 text-white px-3 py-1.5 rounded-lg text-xs transition"><i class="fa-regular fa-copy"></i></button>
                </div>
            </div>

        </div>

        <div id="backdrop" class="hidden fixed inset-0 bg-black/80 backdrop-blur-sm z-40 transition-opacity"></div>

        <div id="history-modal" class="hidden fixed inset-x-0 bottom-0 z-50 p-4 pb-8 flex-col items-center">
            <div class="bg-gray-800 w-full max-w-md rounded-3xl p-6 border border-gray-700 shadow-2xl h-[85vh] flex flex-col">
                <div class="flex justify-between items-center mb-5">
                    <h3 class="text-xl font-bold">История операций</h3>
                    <button onclick="closeModals()" class="text-gray-400 hover:text-white"><i class="fa-solid fa-xmark text-xl"></i></button>
                </div>
                <div class="flex bg-gray-900 rounded-xl p-1 mb-5 border border-gray-700">
                    <button onclick="setHistoryFilter('transactions')" id="filter-transactions" class="flex-1 py-2 rounded-lg bg-gray-700 text-white font-semibold transition">Транзакции</button>
                    <button onclick="setHistoryFilter('topups')" id="filter-topups" class="flex-1 py-2 rounded-lg text-gray-400 transition">Пополнения</button>
                    <button onclick="setHistoryFilter('actions')" id="filter-actions" class="flex-1 py-2 rounded-lg text-gray-400 transition">Действия</button>
                </div>
                <div id="history-list-container" class="flex-1 overflow-y-auto space-y-5 pr-1"></div>
            </div>
        </div>

        <div id="add-step1-modal" class="hidden fixed inset-x-0 bottom-0 z-50 p-4 pb-8 flex-col items-center">
            <div class="bg-gray-800 w-full max-w-md rounded-3xl p-6 border border-gray-700 shadow-2xl">
                <div class="flex justify-between items-center mb-4"><h3 class="text-xl font-bold">Новое устройство</h3><button onclick="closeModals()" class="text-gray-400 hover:text-white"><i class="fa-solid fa-xmark text-xl"></i></button></div>
                <input type="text" id="new-device-name" placeholder="Имя (например: Мой iPhone)" class="w-full bg-gray-900 border border-gray-600 text-white text-sm rounded-xl px-4 py-3 mb-6 focus:outline-none focus:border-green-500 transition">
                <p class="text-xs text-gray-400 mb-2">Выберите платформу:</p>
                <div class="grid grid-cols-2 gap-3">
                    <button onclick="generateDevice('iOS')" class="bg-gray-700 hover:bg-gray-600 p-4 rounded-2xl flex flex-col items-center gap-2 transition"><i class="fa-brands fa-apple text-3xl"></i> <span>iOS / Mac</span></button>
                    <button onclick="generateDevice('Android')" class="bg-gray-700 hover:bg-gray-600 p-4 rounded-2xl flex flex-col items-center gap-2 transition"><i class="fa-brands fa-android text-3xl text-green-400"></i> <span>Android</span></button>
                    <button onclick="generateDevice('Windows')" class="bg-gray-700 hover:bg-gray-600 p-4 rounded-2xl flex flex-col items-center gap-2 transition col-span-2"><i class="fa-brands fa-windows text-3xl text-blue-400"></i> <span>Windows</span></button>
                </div>
            </div>
        </div>

        <div id="add-step2-modal" class="hidden fixed inset-x-0 bottom-0 z-50 p-4 pb-8 flex-col items-center">
            <div class="bg-gray-800 w-full max-w-md rounded-3xl p-6 border border-gray-700 shadow-2xl">
                <h3 class="text-xl font-bold mb-2">Ваш ключ готов!</h3>
                <p class="text-sm text-yellow-400 mb-4"><i class="fa-solid fa-triangle-exclamation mr-1"></i>Только для одного устройства.</p>
                <div id="generated-key-display" class="bg-black/80 p-3 rounded-xl border border-gray-700 mb-4 font-mono text-xs text-green-400 break-all select-all max-h-32 overflow-y-auto"></div>
                <div class="grid grid-cols-2 gap-3 mb-4">
                    <button class="bg-gray-700 text-white font-semibold py-3 px-4 rounded-xl"><i class="fa-solid fa-book-open mr-2"></i> Инструкция</button>
                    <button onclick="copyKey()" class="bg-blue-600 text-white font-semibold py-3 px-4 rounded-xl"><i class="fa-regular fa-copy mr-2"></i> Копировать</button>
                </div>
                <button id="btn-done" disabled onclick="window.location.reload()" class="w-full bg-gray-600 text-gray-400 font-bold py-3 px-4 rounded-xl cursor-not-allowed">Сначала скопируйте</button>
            </div>
        </div>

        <div id="manage-device-modal" class="hidden fixed inset-x-0 bottom-0 z-50 p-4 pb-8 flex-col items-center">
            <div class="bg-gray-800 w-full max-w-md rounded-3xl p-6 border border-gray-700 shadow-2xl">
                <div class="flex justify-between items-center mb-6">
                    <h3 class="text-xl font-bold">Настройки устройства</h3>
                    <button onclick="closeModals()" class="text-gray-400 hover:text-white"><i class="fa-solid fa-xmark text-xl"></i></button>
                </div>
                <div class="mb-5 flex gap-2">
                    <input type="text" id="manage-name-input" class="w-full bg-gray-900 border border-gray-600 text-white rounded-xl px-3 py-2">
                    <button onclick="saveDeviceName()" class="bg-blue-600 px-4 py-2 rounded-xl text-white"><i class="fa-solid fa-check"></i></button>
                </div>

                <button onclick="updateDeviceKey()" class="w-full bg-blue-600/20 border border-blue-500/30 text-blue-400 hover:bg-blue-600/30 font-semibold py-3 px-4 rounded-xl transition mb-3 text-left">
                    <i class="fa-solid fa-arrows-rotate mr-2"></i> Обновить ключ <span class="text-xs text-gray-400 block mt-1 font-normal">Старый перестанет работать</span>
                </button>

                <button id="btn-delete-device" onclick="deleteCurrentDevice()" class="w-full bg-red-600/20 text-red-400 font-semibold py-3 px-4 rounded-xl"><i class="fa-solid fa-trash-can mr-2"></i> Удалить устройство</button>
            </div>
        </div>

        <script>
            const CABINET_TOKEN = "{token}";
            const userBalance = {balance};
            const PRICE_PER_DEVICE = {price};
            let devices = {json.dumps(devices_list, ensure_ascii=False)};
            const userHistory = {json.dumps(history_list, ensure_ascii=False)};
            let currentHistoryFilter = 'transactions';
            let currentManageId = null;

            function renderUI() {{
                const list = document.getElementById('device-list');
                const empty = document.getElementById('empty-state');
                document.getElementById('device-count').innerText = devices.length;

                if (devices.length === 0) {{
                    empty.classList.remove('hidden'); list.classList.add('hidden');
                    document.getElementById('days-left').innerHTML = '<span class="text-gray-400 text-xl">Не активна </span> <span class="text-3xl text-gray-300">∞</span>';
                }} else {{
                    empty.classList.add('hidden'); list.classList.remove('hidden');
                    list.innerHTML = devices.map((d, index) => {{
                        let icon = d.os === "Android" ? "fa-android text-green-400" : d.os === "iOS" ? "fa-apple text-gray-200" : "fa-windows text-blue-400";
                        let statusHtml = '';
                        if (userBalance < PRICE_PER_DEVICE) {{
                            statusHtml = '<span class="text-[9px] bg-red-500/20 text-red-400 px-2 py-0.5 rounded border border-red-500/30 ml-2 uppercase font-bold tracking-wider">Нехватка средств</span>';
                            icon = icon.replace('text-green-400', 'text-gray-500').replace('text-blue-400', 'text-gray-500').replace('text-gray-200', 'text-gray-500');
                        }}
                        let shortId = d.short_id || d.id.substring(0, 4).toUpperCase();
                        let titleText = d.name ? `${{shortId}} ${{d.name}}` : `${{shortId}} (${{d.os}})`;

                        return `<div onclick="openManageModal(${{index}})" class="bg-gray-800 hover:bg-gray-700 cursor-pointer rounded-2xl p-4 flex items-center justify-between border border-gray-700 mb-3 transition">
                                    <div class="flex items-center gap-4 truncate">
                                        <div class="w-12 h-12 bg-gray-900 rounded-full flex items-center justify-center text-2xl flex-shrink-0"><i class="fa-brands ${{icon}}"></i></div>
                                        <div class="truncate">
                                            <div class="font-bold text-sm md:text-base flex items-center truncate">${{titleText}} ${{statusHtml}}</div>
                                            <div class="text-xs text-gray-400 mt-0.5">${{d.date}}</div>
                                        </div>
                                    </div>
                                    <i class="fa-solid fa-chevron-right text-gray-500 ml-2"></i>
                                </div>`;
                    }}).join('');
                }}
            }}

            function copyRefFromInput() {{
                const link = document.getElementById('ref-link-input').value;
                try {{ navigator.clipboard.writeText(link); }} catch(e) {{}}
                alert("Реферальная ссылка скопирована!");
            }}

            function openManageModal(index) {{
                currentManageId = index;
                const device = devices[index];
                document.getElementById('manage-name-input').value = device.name;

                const hoursPassed = (Date.now() - device.timestamp) / (1000 * 60 * 60);
                const btnDelete = document.getElementById('btn-delete-device');

                if (hoursPassed < 24) {{
                    btnDelete.disabled = true;
                    btnDelete.classList.add('opacity-50', 'cursor-not-allowed');
                    btnDelete.innerHTML = '<i class="fa-solid fa-trash-can mr-2"></i> Удалить (через ' + Math.ceil(24 - hoursPassed) + ' ч)';
                }} else {{
                    btnDelete.disabled = false;
                    btnDelete.classList.remove('opacity-50', 'cursor-not-allowed');
                    btnDelete.innerHTML = '<i class="fa-solid fa-trash-can mr-2"></i> Удалить устройство';
                }}

                openModal('manage-device-modal');
            }}

            function setHistoryFilter(filter) {{
                currentHistoryFilter = filter;
                ['transactions', 'topups', 'actions'].forEach(f => {{
                    const btn = document.getElementById('filter-' + f);
                    btn.className = (f === filter)
                        ? "flex-1 py-2 rounded-lg bg-gray-700 text-white font-semibold transition"
                        : "flex-1 py-2 rounded-lg text-gray-400 transition";
                }});

                const container = document.getElementById('history-list-container');
                let filtered = userHistory.filter(item => {{
                    if (filter === 'transactions') return item.type === 'income' || item.type === 'expense';
                    if (filter === 'topups') return item.type === 'income';
                    return item.type === 'action';
                }});

                if (filtered.length === 0) {{ container.innerHTML = `<div class="text-center text-gray-500 mt-10">Пусто</div>`; return; }}

                let html = ''; let currentDate = '';
                filtered.forEach(item => {{
                    if (item.date !== currentDate) {{ html += `<div class="text-xs font-bold text-gray-500 uppercase tracking-wider mb-2 mt-4">${{item.date}}</div>`; currentDate = item.date; }}
                    let iconHtml = item.type === 'income' ? '<div class="text-green-400"><i class="fa-solid fa-arrow-down"></i></div>' : item.type === 'expense' ? '<div class="text-red-400"><i class="fa-solid fa-arrow-up"></i></div>' : '<div class="text-blue-400"><i class="fa-solid fa-microchip"></i></div>';
                    html += `<div class="flex items-center justify-between bg-gray-800 p-3 rounded-2xl border border-gray-700 mb-2">
                                <div class="flex items-center gap-3">${{iconHtml}}
                                    <div><div class="font-bold text-sm">${{item.title}}</div>${{item.desc ? `<div class="text-xs text-gray-400">${{item.desc}}</div>` : ''}}<div class="text-[10px] text-gray-500 mt-1">${{item.time}}</div></div>
                                </div><div class="font-bold text-sm">${{item.amount || ''}}</div>
                            </div>`;
                }});
                container.innerHTML = html;
            }}

            async function generateDevice(os) {{
                const name = document.getElementById('new-device-name').value.trim();
                document.getElementById('add-step1-modal').classList.remove('modal-active');

                document.getElementById('add-step2-modal').querySelector('h3').innerText = 'Генерируем ключи... ⏳';
                document.getElementById('generated-key-display').innerText = 'Подключение к серверам...';
                openModal('add-step2-modal');

                try {{
                    const response = await fetch('/api/device/add', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{token: CABINET_TOKEN, os: os, name: name}}) }});
                    const result = await response.json();

                    if (result.status === 'ok') {{
                        document.getElementById('add-step2-modal').querySelector('h3').innerText = 'Ваш ключ готов!';
                        document.getElementById('generated-key-display').innerText = result.key;
                        document.getElementById('btn-done').disabled = true;
                        document.getElementById('btn-done').className = "w-full bg-gray-600 text-gray-400 font-bold py-3 px-4 rounded-xl cursor-not-allowed";
                        document.getElementById('btn-done').innerText = "Сначала скопируйте ключ";
                    }} else {{
                        alert("Внимание: " + result.msg);
                        window.location.reload();
                    }}
                }} catch(e) {{
                    alert("Ошибка сети");
                    window.location.reload();
                }}
            }}

            async function saveDeviceName() {{
                const newName = document.getElementById('manage-name-input').value.trim();
                await fetch('/api/device/rename', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{token: CABINET_TOKEN, device_id: devices[currentManageId].id, new_name: newName}}) }});
                window.location.reload();
            }}

            async function updateDeviceKey() {{
                if (!confirm("Вы уверены? Старый ключ перестанет работать!")) return;

                document.getElementById('manage-device-modal').classList.remove('modal-active');
                document.getElementById('add-step2-modal').querySelector('h3').innerText = 'Обновляем ключи... ⏳';
                document.getElementById('generated-key-display').innerText = 'Подключение к серверам...';
                openModal('add-step2-modal');

                try {{
                    const response = await fetch('/api/device/update_key', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{token: CABINET_TOKEN, device_id: devices[currentManageId].id}}) }});
                    const result = await response.json();

                    if (result.status === 'ok') {{
                        document.getElementById('add-step2-modal').querySelector('h3').innerText = 'Новый ключ готов!';
                        document.getElementById('generated-key-display').innerText = result.new_key;
                        document.getElementById('btn-done').disabled = true;
                        document.getElementById('btn-done').className = "w-full bg-gray-600 text-gray-400 font-bold py-3 px-4 rounded-xl cursor-not-allowed";
                        document.getElementById('btn-done').innerText = "Сначала скопируйте ключ";
                    }} else {{
                        alert("Ошибка: " + result.msg);
                        window.location.reload();
                    }}
                }} catch(e) {{
                    alert("Ошибка сети");
                    window.location.reload();
                }}
            }}

            async function deleteCurrentDevice() {{
                if(confirm("Удалить устройство навсегда?")) {{
                    await fetch('/api/device/delete', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{token: CABINET_TOKEN, device_id: devices[currentManageId].id}}) }});
                    window.location.reload();
                }}
            }}

            function openModal(id) {{ document.getElementById('backdrop').classList.remove('hidden'); document.getElementById(id).classList.add('modal-active'); if(id === 'history-modal') setHistoryFilter(currentHistoryFilter); }}
            function closeModals() {{ document.getElementById('backdrop').classList.add('hidden'); document.querySelectorAll('.modal-active').forEach(el => el.classList.remove('modal-active')); }}

            function copyKey() {{
                const keyText = document.getElementById('generated-key-display').innerText;
                try {{ navigator.clipboard.writeText(keyText); }} catch(e) {{}}

                const btnDone = document.getElementById('btn-done');
                btnDone.disabled = false;
                btnDone.className = "w-full bg-green-600 hover:bg-green-500 text-white font-bold py-3 px-4 rounded-xl transition";
                btnDone.innerHTML = "Готово <i class='fa-solid fa-check ml-1'></i>";
            }}

            window.onload = renderUI;
        </script>
    </body>
    </html>
    """


if __name__ == "__main__":
    uvicorn.run("web:app", host=config.WEB_HOST, port=config.WEB_PORT, reload=True)
