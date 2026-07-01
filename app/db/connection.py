from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite


async def connect_database(database_path: str | Path) -> aiosqlite.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA busy_timeout = 5000")
    return db


@asynccontextmanager
async def open_database(database_path: str | Path) -> AsyncIterator[aiosqlite.Connection]:
    db = await connect_database(database_path)
    try:
        yield db
    finally:
        await db.close()
