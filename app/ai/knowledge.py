from __future__ import annotations

import asyncio
import logging
import struct
import time
import importlib.metadata
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

import aiosqlite

from app.ai.chunking import RecursiveChunker
from app.ai.repositories import AiRepository, KnowledgeChunk
from app.utils.json import dumps_compact
from app.utils.datetime import datetime_to_iso, utc_now


logger = logging.getLogger(__name__)
FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIMENSIONS = 384


class VectorUnavailable(RuntimeError):
    pass


class Embedder(Protocol):
    model_name: str

    def embed(self, texts: Iterable[str]) -> Iterable[Iterable[float]]: ...


class FastEmbedder:
    model_name = FASTEMBED_MODEL

    def __init__(self, cache_dir: Path | None = None) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # installation is intentionally explicit
            raise VectorUnavailable("fastembed is not installed") from exc
        self._model = TextEmbedding(model_name=self.model_name, cache_dir=str(cache_dir) if cache_dir else None)
        source = getattr(getattr(self._model, "model", None), "tokenizer", None) or getattr(self._model, "tokenizer", None)
        try:
            from tokenizers import Tokenizer
            self._offset_tokenizer = Tokenizer.from_str(source.to_str())
            self._offset_tokenizer.no_truncation()
            self._offset_tokenizer.no_padding()
        except Exception as exc:
            raise VectorUnavailable("unable to create untruncated offset tokenizer") from exc
        self._offset_lock = threading.Lock()

    def embed(self, texts: Iterable[str]) -> Iterable[Iterable[float]]:
        return self._model.embed(list(texts))

    def token_spans(self, text: str) -> list[tuple[int, int]]:
        # This clone deliberately has no truncation/padding; the shared
        # FastEmbed inference tokenizer retains its 128-token configuration.
        with self._offset_lock:
            offsets = self._offset_tokenizer.encode(text).offsets
        spans: list[tuple[int, int]] = []
        for start, end in offsets:
            span = (int(start), int(end))
            if span[1] > span[0] and (not spans or span != spans[-1]):
                spans.append(span)
        if not spans or any(start < 0 or end > len(text) or start >= end for start, end in spans):
            raise VectorUnavailable("offset tokenizer returned invalid character offsets")
        return spans

    @property
    def tokenizer_profile(self) -> str:
        return "fastembed-tokenizers-offsets-v1"


_runtimes: dict[str, FastEmbedder] = {}
_runtime_lock = asyncio.Lock()
_embedding_semaphore = asyncio.Semaphore(1)


async def get_embedder(cache_dir: Path | None) -> FastEmbedder:
    key = str(cache_dir or "default")
    async with _runtime_lock:
        if key not in _runtimes:
            _runtimes[key] = await asyncio.to_thread(FastEmbedder, cache_dir)
        return _runtimes[key]


async def load_sqlite_vec(db: aiosqlite.Connection) -> None:
    """Load sqlite-vec on this connection only, never during generic migration."""
    try:
        import sqlite_vec
    except ImportError as exc:
        raise VectorUnavailable("sqlite-vec is not installed") from exc
    try:
        # sqlite-vec calls sqlite3 APIs and must run on aiosqlite's owning thread.
        await db.enable_load_extension(True)
        await db._execute(sqlite_vec.load, db._conn)  # type: ignore[attr-defined]
        await db.enable_load_extension(False)
        await db.execute("SELECT vec_version()")
    except Exception as exc:
        raise VectorUnavailable(f"sqlite-vec could not be loaded: {type(exc).__name__}") from exc


def pack_embedding(values: Iterable[float]) -> bytes:
    vector = [float(value) for value in values]
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise ValueError(f"expected {EMBEDDING_DIMENSIONS} embedding dimensions, got {len(vector)}")
    return struct.pack(f"<{EMBEDDING_DIMENSIONS}f", *vector)


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: int
    source_message_id: int
    source_telegram_message_id: int
    source_chat_id: int
    source_sender_telegram_user_id: int | None
    text: str
    distance: float


class KnowledgeIndex:
    def __init__(self, db: aiosqlite.Connection, embedder: Embedder | None = None, cache_dir: Path | None = None):
        self.db = db
        self.embedder = embedder
        self.cache_dir = cache_dir
        self._ready = False
        self._pending_profile: str | None = None

    async def initialize(self, *, allow_profile_mismatch: bool = False) -> None:
        if self._ready:
            return
        # Resolve/download the runtime before opening any SQLite write work.
        embedder = await self.get_embedder()
        profile = dumps_compact({"fastembed_version": importlib.metadata.version("fastembed") if self.embedder is None else "test", "model": embedder.model_name, "model_revision": "faf4aa4225822f3bc6376869cb1164e8e3feedd0", "dimension": EMBEDDING_DIMENSIONS, "pooling": "mean", "distance_metric": "cosine", "tokenizer": getattr(embedder, "tokenizer_profile", "test"), "chunk": {"target": 96, "max": 112, "overlap": 16, "min_tail": 32}})
        await load_sqlite_vec(self.db)
        await self.db.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunk_vectors USING vec0(
                embedding float[384] distance_metric=cosine,
                +chunk_id INTEGER
            )"""
        )
        await self.db.execute("DELETE FROM knowledge_chunk_vectors WHERE chunk_id NOT IN (SELECT id FROM knowledge_chunks)")
        existing = await (await self.db.execute("SELECT profile_json FROM embedding_profiles WHERE id = 1")).fetchone()
        if existing is None:
            await self.db.execute("INSERT INTO embedding_profiles (id, profile_json, updated_at) VALUES (1, ?, ?)", (profile, datetime_to_iso(utc_now())))
        elif existing["profile_json"] != profile:
            if not allow_profile_mismatch:
                raise VectorUnavailable("embedding profile mismatch; run explicit rebuild")
            self._pending_profile = profile
        # setup is a short transaction; callers must never carry its write lock
        # into query embedding/model inference.
        await self.db.commit()
        self._ready = True

    async def _embed(self, texts: Iterable[str]) -> list[Iterable[float]]:
        embedder = await self.get_embedder()
        async with _embedding_semaphore:
            return await asyncio.to_thread(lambda: list(embedder.embed(texts)))

    async def get_embedder(self) -> Embedder:
        return self.embedder or await get_embedder(self.cache_dir)

    async def index_chunks(self, chunks: list[KnowledgeChunk]) -> int:
        if not chunks:
            return 0
        await self.initialize()
        vectors = await self._embed(chunk.embedding_text for chunk in chunks)
        return await self.index_precomputed(chunks, vectors)

    async def index_precomputed(self, chunks: list[KnowledgeChunk], vectors: list[Iterable[float]]) -> int:
        if not chunks:
            return 0
        await self.initialize()
        if len(vectors) != len(chunks):
            raise RuntimeError("embedder returned a different number of vectors")
        for chunk, vector in zip(chunks, vectors, strict=True):
            await self.db.execute("DELETE FROM knowledge_chunk_vectors WHERE chunk_id = ?", (chunk.id,))
            await self.db.execute(
                "INSERT INTO knowledge_chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
                (chunk.id, pack_embedding(vector)),
            )
        return len(chunks)

    async def remove_message(self, message_row_id: int) -> None:
        await self.initialize()
        await self.db.execute(
            """DELETE FROM knowledge_chunk_vectors WHERE chunk_id IN
                (SELECT id FROM knowledge_chunks WHERE telegram_message_row_id = ?)""", (message_row_id,)
        )

    async def search(self, query: str, *, top_k: int, context_char_budget: int) -> list[RetrievedChunk]:
        await self.initialize()
        started = time.monotonic()
        vectors = await self._embed([query])
        if len(vectors) != 1:
            raise RuntimeError("embedder did not return one query vector")
        vector_rows = await self.db.execute_fetchall(
            "SELECT chunk_id, distance FROM knowledge_chunk_vectors WHERE embedding MATCH ? AND k = ?",
            (pack_embedding(vectors[0]), max(top_k * 3, top_k)),
        )
        distances = {int(row["chunk_id"]): float(row["distance"]) for row in vector_rows}
        if not distances:
            return []
        placeholders = ",".join("?" for _ in distances)
        rows = await self.db.execute_fetchall(
            f"""SELECT kc.id AS chunk_id, kc.telegram_message_row_id, tm.telegram_message_id, tm.chat_id,
                      tm.sender_telegram_user_id, tm.text AS raw_text, kc.raw_start_char, kc.raw_end_char
               FROM knowledge_chunks kc JOIN knowledge_messages km ON km.telegram_message_row_id = kc.telegram_message_row_id
               JOIN telegram_messages tm ON tm.id = kc.telegram_message_row_id
               WHERE km.candidate_state = 'include' AND kc.id IN ({placeholders})""",
            tuple(distances),
        )
        rows = sorted(rows, key=lambda row: distances[int(row["chunk_id"])])
        grouped: dict[int, list[aiosqlite.Row]] = {}
        for row in rows:
            grouped.setdefault(int(row["telegram_message_row_id"]), []).append(row)
        result: list[RetrievedChunk] = []
        used_chars = 0
        for source_id, group in sorted(grouped.items(), key=lambda item: min(distances[int(row["chunk_id"])] for row in item[1])):
            raw = group[0]["raw_text"]
            spans = sorted((int(row["raw_start_char"]), int(row["raw_end_char"])) for row in group)
            merged: list[list[int]] = []
            for start, end in spans:
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            text = "\n[…]\n".join(raw[start:end] for start, end in merged)
            if result and used_chars + len(text) > context_char_budget:
                continue
            row = min(group, key=lambda item: distances[int(item["chunk_id"])])
            used_chars += len(text)
            result.append(RetrievedChunk(chunk_id=int(row["chunk_id"]), source_message_id=source_id,
                source_telegram_message_id=row["telegram_message_id"], source_chat_id=row["chat_id"],
                source_sender_telegram_user_id=row["sender_telegram_user_id"], text=text, distance=distances[int(row["chunk_id"])]))
            if len(result) >= top_k:
                break
        logger.info("ai.retrieve status=ok query_chars=%s returned=%s duration_ms=%s", len(query), len(result), int((time.monotonic() - started) * 1000))
        return result


class KnowledgeIngestionService:
    """Classify first; only included messages are chunked and embedded."""
    def __init__(self, repo: AiRepository, index: KnowledgeIndex, chunker: RecursiveChunker | None = None):
        self.repo = repo
        self.index = index
        self.chunker = chunker or RecursiveChunker()

    async def include(self, message_row_id: int, text: str, reason: str, classifier_profile: str) -> int:
        started = time.monotonic()
        embedder = await self.index.get_embedder()
        if not isinstance(embedder, FastEmbedder):
            # Test adapters explicitly use their own deterministic chunker.
            chunks = self.chunker.chunk(text)
        else:
            spans = await asyncio.to_thread(embedder.token_spans, text)
            profile = self.chunker.profile.__class__(tokenizer_profile=embedder.tokenizer_profile)
            chunks = RecursiveChunker(self.chunker.tokenizer, profile).chunk_from_spans(text, spans)
        vectors = await self.index._embed(chunk["embedding_text"] for chunk in chunks)
        # caller may commit before this short relational/vector write section.
        stored = await self.repo.replace_chunks(message_row_id, chunks)
        count = await self.index.index_precomputed(stored, vectors)
        await self.repo.set_candidate_result(message_row_id, state="include", reason=reason, profile=classifier_profile)
        await self.repo.audit("embed", "ok", message_row_id=message_row_id, counts={"chunks": count}, duration_ms=int((time.monotonic() - started) * 1000))
        return count

    async def rebuild(self) -> int:
        """Re-chunk raw included messages, then rebuild vectors for a new profile."""
        rows = await self.index.db.execute_fetchall(
            """SELECT tm.id, tm.text FROM telegram_messages tm JOIN knowledge_messages km
               ON km.telegram_message_row_id = tm.id WHERE km.candidate_state = 'include' ORDER BY tm.id"""
        )
        # Full preparation (including model inference) is read-only; no old
        # index/profile is touched until every replacement is ready.
        prepared: list[tuple[int, list[dict[str, int | str]], list[Iterable[float]]]] = []
        for row in rows:
            embedder = await self.index.get_embedder()
            if isinstance(embedder, FastEmbedder):
                spans = await asyncio.to_thread(embedder.token_spans, row["text"])
                profile = self.chunker.profile.__class__(tokenizer_profile=embedder.tokenizer_profile)
                chunks = RecursiveChunker(self.chunker.tokenizer, profile).chunk_from_spans(row["text"], spans)
            else:
                chunks = self.chunker.chunk(row["text"])
            prepared.append((row["id"], chunks, await self.index._embed(chunk["embedding_text"] for chunk in chunks)))
        await self.index.initialize(allow_profile_mismatch=True)
        await self.index.db.execute("BEGIN IMMEDIATE")
        try:
            await self.index.db.execute("DELETE FROM knowledge_chunk_vectors")
            total = 0
            for message_id, chunks, vectors in prepared:
                stored = await self.repo.replace_chunks(message_id, chunks)
                total += await self.index.index_precomputed(stored, vectors)
            if self.index._pending_profile:
                await self.index.db.execute("UPDATE embedding_profiles SET profile_json = ?, updated_at = ? WHERE id = 1", (self.index._pending_profile, datetime_to_iso(utc_now())))
        except BaseException:
            await self.index.db.rollback()
            raise
        else:
            await self.index.db.commit()
        return total
