#!/usr/bin/env python3
"""Clear the Telegram-derived knowledge rows directly in the SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DATABASE_PATH = Path("/root/adlanbot/data/bot.sqlite3")
REQUIRED_TABLES = (
    "telegram_messages",
    "knowledge_messages",
    "knowledge_chunks",
)


@dataclass(frozen=True)
class CleanupCounts:
    knowledge_messages: int
    knowledge_chunks: int
    source_telegram_messages: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить или очистить Telegram-базу знаний в SQLite."
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help=f"путь к SQLite-базе (по умолчанию {DEFAULT_DATABASE_PATH})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="действительно удалить данные; без флага выполняется только dry-run",
    )
    return parser.parse_args()


def open_database(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"Файл базы данных не найден: {path}")

    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=rw",
        uri=True,
        timeout=10,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def require_schema(connection: sqlite3.Connection) -> None:
    existing_tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing_tables = [table for table in REQUIRED_TABLES if table not in existing_tables]
    if missing_tables:
        raise ValueError(
            "В базе нет нужной схемы knowledge: " + ", ".join(missing_tables)
        )


def snapshot(connection: sqlite3.Connection) -> CleanupCounts:
    knowledge_messages = connection.execute(
        "SELECT COUNT(*) FROM knowledge_messages"
    ).fetchone()[0]
    knowledge_chunks = connection.execute(
        "SELECT COUNT(*) FROM knowledge_chunks"
    ).fetchone()[0]
    source_telegram_messages = connection.execute(
        """SELECT COUNT(DISTINCT telegram_message_row_id)
           FROM (
               SELECT telegram_message_row_id FROM knowledge_messages
               UNION
               SELECT telegram_message_row_id FROM knowledge_chunks
           )"""
    ).fetchone()[0]
    return CleanupCounts(
        knowledge_messages=knowledge_messages,
        knowledge_chunks=knowledge_chunks,
        source_telegram_messages=source_telegram_messages,
    )


def print_counts(counts: CleanupCounts) -> None:
    print(f"knowledge_messages: {counts.knowledge_messages}")
    print(f"knowledge_chunks: {counts.knowledge_chunks}")
    print(f"Исходных telegram_messages к удалению: {counts.source_telegram_messages}")


def clear_knowledge(connection: sqlite3.Connection) -> CleanupCounts:
    connection.execute("BEGIN IMMEDIATE")
    try:
        before = snapshot(connection)
        # Only rows referenced by knowledge state are source messages. Foreign
        # keys cascade the normal dependent rows; explicit deletes below also
        # remove historical orphan rows made while FK checks were disabled.
        connection.execute(
            """DELETE FROM telegram_messages
               WHERE id IN (
                   SELECT telegram_message_row_id FROM knowledge_messages
                   UNION
                   SELECT telegram_message_row_id FROM knowledge_chunks
               )"""
        )
        connection.execute("DELETE FROM knowledge_chunks")
        connection.execute("DELETE FROM knowledge_messages")

        remaining = snapshot(connection)
        if remaining.knowledge_messages or remaining.knowledge_chunks:
            raise RuntimeError(
                "Проверка очистки не пройдена: остались "
                f"knowledge_messages={remaining.knowledge_messages}, "
                f"knowledge_chunks={remaining.knowledge_chunks}."
            )
        connection.commit()
        return before
    except BaseException:
        connection.rollback()
        raise


def print_vector_notice() -> None:
    print(
        "Таблица knowledge_chunk_vectors не открывалась и не изменялась: "
        "для неё нужен sqlite-vec. После удаления knowledge_chunks оставшиеся "
        "векторные строки не участвуют в поиске; KnowledgeIndex.initialize() "
        "удалит их при следующем перезапуске ai-worker."
    )


def main() -> int:
    args = parse_args()
    try:
        connection = open_database(args.database_path)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"Очистка не запущена: {error}", file=sys.stderr)
        return 2

    try:
        require_schema(connection)
        print(f"База данных: {args.database_path}")
        if not args.apply:
            print_counts(snapshot(connection))
            print("Dry-run: данные не изменялись. Для удаления повторите с --apply.")
            print_vector_notice()
            return 0

        before = clear_knowledge(connection)
        print_counts(before)
        print("Очистка завершена: knowledge_messages и knowledge_chunks пусты.")
        print_vector_notice()
        return 0
    except (sqlite3.Error, RuntimeError, ValueError) as error:
        print(f"Очистка прервана: {error}", file=sys.stderr)
        return 2
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
