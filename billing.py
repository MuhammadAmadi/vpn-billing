"""Ежедневное списание баланса.

Логика:
  1. Берём всех пользователей с устройствами.
  2. Считаем стоимость: количество_устройств × PRICE_PER_DEVICE.
  3. Если баланс ≥ стоимость → списываем.
  4. Если баланс < стоимость → баланс обнуляем, устройства не удаляем
     (пользователь видит «Остановлен» в кабинете).
  5. Каждое изменение пишется в transactions в одной транзакции с UPDATE.

Запуск: `python billing.py`. Автозапуск: cron каждый день в 00:00.
"""

import asyncio
import logging
from datetime import datetime

import config
import db

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("billing")


async def _charge_user(conn, user_id: int, balance: float, device_count: int):
    """Списать день у одного пользователя в одной транзакции. Возвращает 'charged'|'stopped'|'skip'."""
    if balance <= 0:
        return "skip"

    daily_cost = round(device_count * config.PRICE_PER_DEVICE, 2)
    async with conn.transaction():
        if balance >= daily_cost:
            new_balance = round(balance - daily_cost, 2)
            await conn.execute(
                "UPDATE users SET balance = $1 WHERE user_id = $2",
                new_balance, user_id,
            )
            await conn.execute(
                """INSERT INTO transactions (user_id, type, title, description, amount)
                   VALUES ($1, 'expense', 'Списание за день', $2, $3)""",
                user_id,
                f"{device_count} устр. × {config.PRICE_PER_DEVICE}₽",
                f"-{daily_cost}₽",
            )
            log.info("user %s: -%s₽ (%s→%s, %s устр.)",
                     user_id, daily_cost, balance, new_balance, device_count)
            return "charged"

        await conn.execute("UPDATE users SET balance = 0 WHERE user_id = $1", user_id)
        await conn.execute(
            """INSERT INTO transactions (user_id, type, title, description, amount)
               VALUES ($1, 'expense', 'Подписка остановлена', $2, $3)""",
            user_id,
            f"Недостаточно средств (нужно {daily_cost}₽, было {balance}₽)",
            f"-{balance}₽",
        )
        log.warning("user %s: баланс %s₽ < %s₽ — обнулён, подписка остановлена",
                    user_id, balance, daily_cost)
        return "stopped"


async def run_billing():
    log.info("Запуск списания: %s", datetime.now().strftime("%d.%m.%Y %H:%M:%S"))

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        users = await conn.fetch("""
            SELECT u.user_id, u.balance, COUNT(d.id) AS device_count
            FROM users u
            JOIN devices d ON d.user_id = u.user_id
            GROUP BY u.user_id, u.balance
            HAVING COUNT(d.id) > 0
        """)

        log.info("Пользователей с устройствами: %s", len(users))

        counts = {"charged": 0, "stopped": 0, "skip": 0}
        for u in users:
            result = await _charge_user(
                conn, u["user_id"], float(u["balance"]), int(u["device_count"])
            )
            counts[result] += 1

    log.info("Итого: списано %s, остановлено %s, пропущено %s",
             counts["charged"], counts["stopped"], counts["skip"])


async def main():
    try:
        await run_billing()
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
