# billing.py — ЕЖЕДНЕВНОЕ СПИСАНИЕ БАЛАНСА
#
# Как работает:
#   1. Берём всех пользователей у которых есть устройства
#   2. Считаем стоимость: количество_устройств × 3.33₽
#   3. Если баланс >= стоимость → списываем
#   4. Если баланс < стоимость → баланс обнуляем, устройства НЕ удаляем
#      (пользователь видит статус "Остановлен" в кабинете)
#   5. Всё записываем в таблицу transactions
#
# Запуск: python billing.py
# Автозапуск: cron каждый день в 00:00

import asyncio
import asyncpg
import config
from datetime import datetime

PRICE_PER_DEVICE = 3.33  # рублей в день за одно устройство


async def run_billing():
    print(f"\n{'='*50}")
    print(f"💸 ЗАПУСК СПИСАНИЯ: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    print(f"{'='*50}")

    conn = await asyncpg.connect(
        user=config.DB_USER, password=config.DB_PASS,
        database=config.DB_NAME, host=config.DB_HOST
    )

    # Берём всех пользователей у которых есть хотя бы одно устройство
    users = await conn.fetch('''
        SELECT u.user_id, u.balance, COUNT(d.id) as device_count
        FROM users u
        JOIN devices d ON d.user_id = u.user_id
        GROUP BY u.user_id, u.balance
        HAVING COUNT(d.id) > 0
    ''')

    print(f"👥 Пользователей с устройствами: {len(users)}")

    charged = 0      # Успешно списано
    insufficient = 0 # Недостаточно средств
    skipped = 0      # Пропущено (баланс уже 0)

    for user in users:
        user_id = user['user_id']
        balance = float(user['balance'])
        device_count = user['device_count']
        daily_cost = round(device_count * PRICE_PER_DEVICE, 2)

        # Если баланс уже 0 — пропускаем (уже остановлен)
        if balance <= 0:
            skipped += 1
            continue

        if balance >= daily_cost:
            # Списываем полную стоимость
            new_balance = round(balance - daily_cost, 2)
            await conn.execute(
                'UPDATE users SET balance = $1 WHERE user_id = $2',
                new_balance, user_id
            )
            await conn.execute(
                '''INSERT INTO transactions (user_id, type, title, description, amount)
                   VALUES ($1, 'expense', 'Списание за день', $2, $3)''',
                user_id,
                f"{device_count} устр. × {PRICE_PER_DEVICE}₽",
                f"-{daily_cost}₽"
            )
            print(f"   ✅ user {user_id}: -{daily_cost}₽ (было {balance}₽, стало {new_balance}₽, {device_count} устр.)")
            charged += 1

        else:
            # Баланса не хватает — обнуляем
            await conn.execute(
                'UPDATE users SET balance = 0 WHERE user_id = $1', user_id
            )
            await conn.execute(
                '''INSERT INTO transactions (user_id, type, title, description, amount)
                   VALUES ($1, 'expense', 'Подписка остановлена', $2, $3)''',
                user_id,
                f"Недостаточно средств (нужно {daily_cost}₽, было {balance}₽)",
                f"-{balance}₽"
            )
            print(f"   ⚠️  user {user_id}: баланс {balance}₽ < {daily_cost}₽ — обнулён, подписка остановлена")
            insufficient += 1

    print(f"\n📊 ИТОГО:")
    print(f"   ✅ Списано успешно: {charged}")
    print(f"   ⚠️  Недостаточно средств: {insufficient}")
    print(f"   ⏭️  Пропущено (баланс 0): {skipped}")
    print(f"{'='*50}\n")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(run_billing())
