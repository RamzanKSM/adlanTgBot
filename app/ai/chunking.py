from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from app.utils.json import dumps_compact


TOKEN_RE = re.compile(r"\S+", re.UNICODE)
BOUNDARY_RE = re.compile(r"\n\s*\n|\n|[.!?…](?:\s|$)|[;:—](?:\s|$)|\s", re.UNICODE)


class Tokenizer(Protocol):
    profile: str

    def spans(self, text: str) -> list[tuple[int, int]]: ...


class RegexTokenizer:
    """Deterministic fallback when FastEmbed does not expose its tokenizer API."""

    profile = "regex-v1"

    def spans(self, text: str) -> list[tuple[int, int]]:
        return [(match.start(), match.end()) for match in TOKEN_RE.finditer(text)]


def utf16_offset(text: str, char_offset: int) -> int:
    return len(text[:char_offset].encode("utf-16-le")) // 2


def normalized_embedding_text(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True, slots=True)
class ChunkingProfile:
    target_tokens: int = 96
    max_tokens: int = 112
    overlap_tokens: int = 16
    min_tail_tokens: int = 32
    tokenizer_profile: str = RegexTokenizer.profile

    def encoded(self) -> str:
        return dumps_compact({
            "target_tokens": self.target_tokens,
            "max_tokens": self.max_tokens,
            "overlap_tokens": self.overlap_tokens,
            "min_tail_tokens": self.min_tail_tokens,
            "tokenizer": self.tokenizer_profile,
            "normalization": "collapse-whitespace-v1",
        })


class RecursiveChunker:
    def __init__(self, tokenizer: Tokenizer | None = None, profile: ChunkingProfile | None = None):
        self.tokenizer = tokenizer or RegexTokenizer()
        self.profile = profile or ChunkingProfile(tokenizer_profile=self.tokenizer.profile)

    def _best_end(self, text: str, start: int, desired_end: int, max_end: int) -> int:
        # Prefer the last strongest available natural boundary; a regex match at
        # the end represents the end of its delimiter, which preserves raw spans.
        candidates = [match.end() for match in BOUNDARY_RE.finditer(text, start, max_end)]
        target_candidates = [end for end in candidates if end <= desired_end]
        if target_candidates:
            return target_candidates[-1]
        if candidates:
            return candidates[-1]
        return max_end

    def chunk(self, text: str) -> list[dict[str, int | str]]:
        tokens = self.tokenizer.spans(text)
        return self.chunk_from_spans(text, tokens)

    def chunk_from_spans(self, text: str, tokens: list[tuple[int, int]]) -> list[dict[str, int | str]]:
        if not tokens:
            return []
        profile = self.profile.encoded()
        if len(tokens) <= self.profile.max_tokens:
            return [self._item(text, tokens[0][0], tokens[-1][1], len(tokens), profile)]

        result: list[dict[str, int | str]] = []
        token_start = 0
        while token_start < len(tokens):
            remaining = len(tokens) - token_start
            take = min(self.profile.target_tokens, self.profile.max_tokens, remaining)
            # Avoid a tiny final chunk when it can fit in the current chunk.
            if remaining > take and remaining - take < self.profile.min_tail_tokens:
                take = min(self.profile.max_tokens, remaining)
            proposed_end_index = token_start + take - 1
            max_end_index = min(token_start + self.profile.max_tokens - 1, len(tokens) - 1)
            start_char = tokens[token_start][0]
            end_char = self._best_end(text, start_char, tokens[proposed_end_index][1], tokens[max_end_index][1])
            included = [span for span in tokens[token_start:max_end_index + 1] if span[1] <= end_char]
            if not included:
                included = [tokens[token_start]]
                end_char = included[-1][1]
            result.append(self._item(text, start_char, end_char, len(included), profile))
            if included[-1][1] >= tokens[-1][1]:
                break
            next_token = token_start + len(included) - self.profile.overlap_tokens
            token_start = max(token_start + 1, next_token)
        return result

    @staticmethod
    def _item(text: str, start: int, end: int, token_count: int, profile: str) -> dict[str, int | str]:
        raw = text[start:end]
        return {
            "start_char": start,
            "end_char": end,
            "start_utf16": utf16_offset(text, start),
            "end_utf16": utf16_offset(text, end),
            "embedding_text": normalized_embedding_text(raw),
            "token_count": token_count,
            "profile": profile,
        }
