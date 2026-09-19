"""Clear the Telegram-derived knowledge base without touching normal chat history.

Run this module from the ai-worker image.  It deliberately has no implicit
write mode: inspecting the database is the default and ``--apply`` is required
for the single cleanup operation.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from app.ai.knowledge import VectorUnavailable, load_sqlite_vec


RELATIONAL_TABLES = (
    "telegram_messages",
    "knowledge_messages",
    "knowledge_chunks",
)
VECTOR_TABLE = "knowledge_chunk_vectors"


@dataclass(frozen=True)
class CleanupCounts:
    knowledge_messages: int
    knowledge_chunks: int
    knowledge_chunk_vectors: int
    source_telegram_messages: int


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove the full Telegram-derived knowledge base while preserving "
            "ordinary Telegram messages, user turns, conversation state, and audit events."
        )
    )
    parser.add_argument(
        "--database-path",
        help="SQLite database path. Defaults to DATABASE_PATH.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the cleanup. Without this flag the command only reports what would be removed.",
    )
    return parser.parse_args(argv)


async def table_exists(db: aiosqlite.Connection, table_name: str) -> bool:
    row = await (
        await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (table_name,),
        )
    ).fetchone()
    return row is not None


async def open_existing_database(path: Path) -> aiosqlite.Connection:
    """Open read/write without creating a database or changing journal mode."""
    db = await aiosqlite.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA busy_timeout = 10000")
    return db


async def require_schema(db: aiosqlite.Connection) -> bool:
    missing = [table for table in RELATIONAL_TABLES if not await table_exists(db, table)]
    if missing:
        raise RuntimeError(
            "database does not contain the required AI knowledge schema: " + ", ".join(missing)
        )
    return await table_exists(db, VECTOR_TABLE)


async def snapshot(db: aiosqlite.Connection, *, has_vector_table: bool) -> CleanupCounts:
    knowledge_messages = int(
        (await (await db.execute("SELECT COUNT(*) FROM knowledge_messages")).fetchone())[0]
    )
    knowledge_chunks = int(
        (await (await db.execute("SELECT COUNT(*) FROM knowledge_chunks")).fetchone())[0]
    )
    source_telegram_messages = int(
        (
            await (
                await db.execute(
                    """SELECT COUNT(DISTINCT telegram_message_row_id)
                       FROM (
                           SELECT telegram_message_row_id FROM knowledge_messages
                           UNION
                           SELECT telegram_message_row_id FROM knowledge_chunks
                       )"""
                )
            ).fetchone()
        )[0]
    )
    knowledge_chunk_vectors = 0
    if has_vector_table:
        knowledge_chunk_vectors = int(
            (await (await db.execute("SELECT COUNT(*) FROM knowledge_chunk_vectors")).fetchone())[0]
        )
    return CleanupCounts(
        knowledge_messages=knowledge_messages,
        knowledge_chunks=knowledge_chunks,
        knowledge_chunk_vectors=knowledge_chunk_vectors,
        source_telegram_messages=source_telegram_messages,
    )


def print_counts(counts: CleanupCounts, *, has_vector_table: bool) -> None:
    vector_suffix = "" if has_vector_table else " (table is absent)"
    print(f"knowledge_messages: {counts.knowledge_messages}")
    print(f"knowledge_chunks: {counts.knowledge_chunks}")
    print(f"knowledge_chunk_vectors: {counts.knowledge_chunk_vectors}{vector_suffix}")
    print(f"source telegram_messages to delete: {counts.source_telegram_messages}")


async def clear_knowledge(db: aiosqlite.Connection, *, has_vector_table: bool) -> CleanupCounts:
    """Execute the cleanup atomically after sqlite-vec has been proven usable."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        # vec0 rows are not governed by relational foreign keys, so they must
        # be removed before the source messages cascade through the schema.
        if has_vector_table:
            await db.execute("DELETE FROM knowledge_chunk_vectors")

        # This includes anomalous chunks without a knowledge_messages row, but
        # never a regular Telegram message without knowledge-derived state.
        await db.execute(
            """DELETE FROM telegram_messages
               WHERE id IN (
                   SELECT telegram_message_row_id FROM knowledge_messages
                   UNION
                   SELECT telegram_message_row_id FROM knowledge_chunks
               )"""
        )
        # Old databases may contain rows created while foreign keys were not
        # enforced. Remove any such orphaned relational state explicitly after
        # the source-id set above has already been used.
        await db.execute("DELETE FROM knowledge_chunks")
        await db.execute("DELETE FROM knowledge_messages")
        remaining = await snapshot(db, has_vector_table=has_vector_table)
        if (
            remaining.knowledge_messages
            or remaining.knowledge_chunks
            or remaining.knowledge_chunk_vectors
        ):
            raise RuntimeError(
                "knowledge cleanup verification failed; transaction was rolled back: "
                f"knowledge_messages={remaining.knowledge_messages}, "
                f"knowledge_chunks={remaining.knowledge_chunks}, "
                f"knowledge_chunk_vectors={remaining.knowledge_chunk_vectors}"
            )
        await db.commit()
        return remaining
    except BaseException:
        await db.rollback()
        raise


async def run(args: argparse.Namespace) -> int:
    database_path = args.database_path or os.environ.get("DATABASE_PATH")
    if not database_path:
        raise ValueError("set DATABASE_PATH or pass --database-path")
    path = Path(database_path)
    if not path.is_file():
        raise ValueError(f"database file does not exist: {path}")

    db = await open_existing_database(path)
    try:
        has_vector_table = await require_schema(db)
        if has_vector_table:
            try:
                await load_sqlite_vec(db)
            except VectorUnavailable as exc:
                # Never begin an apply transaction when vec0 rows cannot be
                # safely addressed by this connection.
                raise RuntimeError(f"sqlite-vec is required to clean {VECTOR_TABLE}: {exc}") from exc

        before = await snapshot(db, has_vector_table=has_vector_table)
        print(f"Database: {path}")
        print_counts(before, has_vector_table=has_vector_table)
        if not args.apply:
            print("Dry run only: no rows were changed. Re-run with --apply to remove this knowledge base.")
            return 0

        await clear_knowledge(db, has_vector_table=has_vector_table)
        print("Cleanup complete: knowledge_messages, knowledge_chunks, and knowledge_chunk_vectors are empty.")
        return 0
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(run(parse_args(argv)))
    except (ValueError, RuntimeError, aiosqlite.Error) as exc:
        print(f"Knowledge cleanup aborted: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
