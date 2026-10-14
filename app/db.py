"""asyncpg pool + thin query helpers.

Raw SQL rather than an ORM: this box has one vCPU and no swap, and SQLAlchemy's
import footprint buys us nothing for ~15 tables of straightforward queries.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

import asyncpg

from app.config import settings

log = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        # min_size=1 deliberately: idle connections are wasted RSS on this box.
        _pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=1,
            max_size=6,
            command_timeout=60,
        )
        log.info("postgres pool ready")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db pool not initialised; call init_pool() first")
    return _pool


async def fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    async with pool().acquire() as con:
        return await con.fetch(sql, *args)


async def fetchrow(sql: str, *args: Any) -> Optional[asyncpg.Record]:
    async with pool().acquire() as con:
        return await con.fetchrow(sql, *args)


async def fetchval(sql: str, *args: Any) -> Any:
    async with pool().acquire() as con:
        return await con.fetchval(sql, *args)


async def execute(sql: str, *args: Any) -> str:
    async with pool().acquire() as con:
        return await con.execute(sql, *args)


async def executemany(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    async with pool().acquire() as con:
        await con.executemany(sql, rows)
