"""Единый асинхронный пул подключений к PostgreSQL.

Используется и веб-сервером (через app.state.db), и фоновыми утилитами
(billing, broadcast, скрипты миграции). Пул создаётся лениво при первом
обращении и переиспользуется в рамках процесса.
"""

import asyncpg
import config

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """Вернуть существующий пул или создать новый."""
    global _pool
    if _pool is None or _pool._closed:
        _pool = await create_pool()
    return _pool


async def create_pool(
    *, min_size: int | None = None, max_size: int | None = None
) -> asyncpg.Pool:
    """Создать новый пул (для случаев, когда нужен изолированный экземпляр)."""
    return await asyncpg.create_pool(
        user=config.DB_USER,
        password=config.DB_PASS,
        database=config.DB_NAME,
        host=config.DB_HOST,
        port=config.DB_PORT,
        min_size=min_size if min_size is not None else config.DB_POOL_MIN,
        max_size=max_size if max_size is not None else config.DB_POOL_MAX,
    )


async def close_pool() -> None:
    """Закрыть глобальный пул (вызывается при остановке процесса)."""
    global _pool
    if _pool is not None and not _pool._closed:
        await _pool.close()
    _pool = None


async def connect() -> asyncpg.Connection:
    """Одиночное подключение — для коротких скриптов (миграции, billing CLI)."""
    return await asyncpg.connect(
        user=config.DB_USER,
        password=config.DB_PASS,
        database=config.DB_NAME,
        host=config.DB_HOST,
        port=config.DB_PORT,
    )
