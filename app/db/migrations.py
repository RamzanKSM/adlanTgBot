import asyncio

from app.config import get_settings
from app.db.connection import open_database


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    access_until TEXT,
    is_in_group INTEGER NOT NULL DEFAULT 0,
    warned_access_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tariffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price_amount INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'RUB',
    duration_days INTEGER NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 100,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tariff_id INTEGER NOT NULL REFERENCES tariffs(id) ON DELETE RESTRICT,
    provider TEXT NOT NULL DEFAULT 'lava',
    invoice_id TEXT,
    order_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    payment_url TEXT,
    created_at TEXT NOT NULL,
    paid_at TEXT,
    expires_at TEXT,
    raw_payload TEXT NOT NULL DEFAULT '{}',
    applied_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_payments_status_created_at ON payments(status, created_at);

CREATE TABLE IF NOT EXISTS invite_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
    invite_link TEXT NOT NULL UNIQUE,
    telegram_invite_link_id TEXT,
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    used_at TEXT,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_invite_links_user_status ON invite_links(user_id, status);

CREATE TABLE IF NOT EXISTS access_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    telegram_user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_access_events_user_created_at ON access_events(telegram_user_id, created_at);
"""


async def _payments_columns(db) -> set[str]:
    rows = await db.execute_fetchall("PRAGMA table_info(payments)")
    return {row["name"] for row in rows}


async def _migrate_payment_column_names(db) -> None:
    columns = await _payments_columns(db)
    if "internal_invoice_id" in columns and "order_id" not in columns:
        await db.execute("ALTER TABLE payments RENAME COLUMN internal_invoice_id TO order_id")
        columns = await _payments_columns(db)
    if "provider_invoice_id" in columns and "invoice_id" not in columns:
        await db.execute("ALTER TABLE payments RENAME COLUMN provider_invoice_id TO invoice_id")

    await db.execute("DROP INDEX IF EXISTS idx_payments_provider_invoice_id")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_payments_invoice_id ON payments(invoice_id)")


async def run_migrations(database_path: str) -> None:
    async with open_database(database_path) as db:
        await db.executescript(SCHEMA_SQL)
        await _migrate_payment_column_names(db)
        await db.commit()


async def _main() -> None:
    settings = get_settings()
    await run_migrations(str(settings.database_path))


if __name__ == "__main__":
    asyncio.run(_main())
