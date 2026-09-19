import asyncio

from app.ai.knowledge import KnowledgeIndex, KnowledgeIngestionService
from app.ai.repositories import AiRepository
from app.config import get_settings
from app.db.connection import open_database
from app.db.migrations import run_migrations


async def main() -> None:
    settings = get_settings()
    await run_migrations(str(settings.database_path))
    async with open_database(settings.database_path) as db:
        count = await KnowledgeIngestionService(AiRepository(db), KnowledgeIndex(db, cache_dir=settings.ai_embedding_cache_dir)).rebuild()
        await db.commit()
        print(f"rebuilt_chunks={count}")


if __name__ == "__main__":
    asyncio.run(main())
