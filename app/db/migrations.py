import asyncio
import sqlite3

from app.config import get_settings
from app.db.connection import open_database
from app.utils.datetime import datetime_to_iso, iso_to_datetime, utc_now


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


SCHEMA_STATEMENTS = tuple(statement.strip() for statement in SCHEMA_SQL.split(";") if statement.strip())
MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""
MIGRATION_LOCK_RETRIES = 5
MIGRATION_LOCK_RETRY_SECONDS = 0.25


async def _table_columns(db, table_name: str) -> set[str]:
    rows = await db.execute_fetchall(f"PRAGMA table_info({table_name})")
    return {row["name"] for row in rows}


async def _ensure_columns(db, table_name: str, columns: dict[str, str]) -> None:
    existing = await _table_columns(db, table_name)
    for name, definition in columns.items():
        if name not in existing:
            await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {definition}")


async def _create_schema(db) -> None:
    for statement in SCHEMA_STATEMENTS:
        await db.execute(statement)


async def _migrate_additive_columns(db) -> None:
    """Add columns that SQLite can add without rewriting or discarding old rows."""
    await _ensure_columns(
        db,
        "users",
        {
            "access_until": "TEXT",
            "is_in_group": "INTEGER NOT NULL DEFAULT 0",
            "warned_access_until": "TEXT",
        },
    )
    await _ensure_columns(
        db,
        "tariffs",
        {
            "description": "TEXT NOT NULL DEFAULT ''",
            "currency": "TEXT NOT NULL DEFAULT 'RUB'",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
            "sort_order": "INTEGER NOT NULL DEFAULT 100",
        },
    )
    await _ensure_columns(
        db,
        "payments",
        {
            "provider": "TEXT NOT NULL DEFAULT 'lava'",
            "payment_url": "TEXT",
            "paid_at": "TEXT",
            "expires_at": "TEXT",
            "raw_payload": "TEXT NOT NULL DEFAULT '{}'",
            "applied_at": "TEXT",
        },
    )
    await _ensure_columns(
        db,
        "invite_links",
        {
            "payment_id": "INTEGER",
            "telegram_invite_link_id": "TEXT",
            "used_at": "TEXT",
            "revoked_at": "TEXT",
        },
    )
    await _ensure_columns(db, "access_events", {"details": "TEXT NOT NULL DEFAULT '{}'"})


async def _migrate_payment_column_names(db) -> None:
    """Adopt payment names introduced after the first production schema.

    A database may have only the legacy columns or both legacy and current names
    after an interrupted/manual upgrade. When both exist, copy values into the
    current column and keep the legacy column untouched so no historic payment
    data is discarded.
    """
    columns = await _table_columns(db, "payments")

    if "internal_invoice_id" in columns and "order_id" not in columns:
        await db.execute("ALTER TABLE payments RENAME COLUMN internal_invoice_id TO order_id")
        columns = await _table_columns(db, "payments")
    elif "order_id" not in columns:
        raise RuntimeError("payments table has neither order_id nor internal_invoice_id")

    if "provider_invoice_id" in columns and "invoice_id" not in columns:
        await db.execute("ALTER TABLE payments RENAME COLUMN provider_invoice_id TO invoice_id")
        columns = await _table_columns(db, "payments")
    elif "invoice_id" not in columns:
        await db.execute("ALTER TABLE payments ADD COLUMN invoice_id TEXT")
        columns = await _table_columns(db, "payments")

    if "internal_invoice_id" in columns:
        await db.execute(
            "UPDATE payments SET order_id = COALESCE(order_id, internal_invoice_id) "
            "WHERE internal_invoice_id IS NOT NULL"
        )
    if "provider_invoice_id" in columns:
        await db.execute(
            "UPDATE payments SET invoice_id = COALESCE(invoice_id, provider_invoice_id) "
            "WHERE provider_invoice_id IS NOT NULL"
        )

    await db.execute("DROP INDEX IF EXISTS idx_payments_provider_invoice_id")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_payments_invoice_id ON payments(invoice_id)")


TIMESTAMP_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": ("access_until", "warned_access_until", "created_at", "updated_at"),
    "tariffs": ("created_at", "updated_at"),
    "payments": ("created_at", "paid_at", "expires_at", "applied_at"),
    "invite_links": ("expires_at", "created_at", "used_at", "revoked_at"),
    "access_events": ("created_at",),
    "schema_migrations": ("applied_at",),
}


def _timestamp_in_moscow(value: str) -> str:
    """Convert a legacy timestamp representation without changing its instant.

    Older releases wrote UTC values and treated timezone-naive values as UTC.
    Keeping that interpretation makes the one-time conversion deterministic.
    Malformed historic text is left untouched rather than discarded.
    """
    try:
        parsed = iso_to_datetime(value)
    except ValueError:
        return value
    return datetime_to_iso(parsed) or value


async def _migrate_timestamps_to_moscow(db) -> None:
    """Normalize every application timestamp to an ISO value with Moscow offset."""
    for table_name, candidate_columns in TIMESTAMP_COLUMNS.items():
        existing_columns = await _table_columns(db, table_name)
        primary_key = "version" if table_name == "schema_migrations" else "id"
        for column_name in candidate_columns:
            if column_name not in existing_columns:
                continue
            rows = await db.execute_fetchall(
                f"SELECT {primary_key}, {column_name} FROM {table_name} "
                f"WHERE {column_name} IS NOT NULL"
            )
            for row in rows:
                original = row[column_name]
                normalized = _timestamp_in_moscow(original)
                if normalized != original:
                    await db.execute(
                        f"UPDATE {table_name} SET {column_name} = ? WHERE {primary_key} = ?",
                        (normalized, row[primary_key]),
                    )


async def _applied_versions(db) -> set[int]:
    rows = await db.execute_fetchall("SELECT version FROM schema_migrations")
    return {row["version"] for row in rows}


async def _apply_migrations(db) -> None:
    await db.execute(MIGRATIONS_TABLE_SQL)
    applied_versions = await _applied_versions(db)

    if 1 not in applied_versions:
        await _create_schema(db)
        await db.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)",
            (datetime_to_iso(utc_now()),),
        )

    if 2 not in applied_versions:
        await _migrate_additive_columns(db)
        await db.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (2, ?)",
            (datetime_to_iso(utc_now()),),
        )

    if 3 not in applied_versions:
        await _migrate_payment_column_names(db)
        await db.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (3, ?)",
            (datetime_to_iso(utc_now()),),
        )

    if 4 not in applied_versions:
        await _migrate_timestamps_to_moscow(db)
        await db.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (4, ?)",
            (datetime_to_iso(utc_now()),),
        )


async def run_migrations(database_path: str) -> None:
    """Run production-safe migrations under SQLite's single-writer lock."""
    for attempt in range(MIGRATION_LOCK_RETRIES):
        try:
            async with open_database(database_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await _apply_migrations(db)
                except BaseException:
                    await db.rollback()
                    raise
                else:
                    await db.commit()
            return
        except sqlite3.OperationalError as error:
            error_message = str(error).lower()
            if (
                not any(marker in error_message for marker in ("locked", "busy"))
                or attempt == MIGRATION_LOCK_RETRIES - 1
            ):
                raise
            await asyncio.sleep(MIGRATION_LOCK_RETRY_SECONDS * (attempt + 1))


async def _main() -> None:
    settings = get_settings()
    await run_migrations(str(settings.database_path))


if __name__ == "__main__":
    asyncio.run(_main())
