import asyncpg
import asyncio

# Настройки подключения
DB_USER = "siha_user"
DB_PASS = "SihaPass123!"
DB_NAME = "sihavpn"
DB_HOST = "127.0.0.1"

async def init_db():
    print("🔄 Подключаемся к PostgreSQL...")
    conn = await asyncpg.connect(user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST)
    
    # Создаем таблицу пользователей
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Создаем таблицу подписок
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            sub_id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id),
            uuid TEXT UNIQUE,
            status TEXT DEFAULT 'active',
            expire_at TIMESTAMP,
            traffic_used BIGINT DEFAULT 0,
            traffic_limit BIGINT
        )
    ''')
    
    print("✅ Таблицы в базе данных успешно созданы!")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(init_db())
