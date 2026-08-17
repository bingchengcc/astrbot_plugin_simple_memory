from __future__ import annotations

import re
from collections.abc import Callable


class Chunker:
    def __init__(self, size: int = 384, overlap: int = 64):
        self.size = size
        self.overlap = overlap

    def split(self, text: str, count_tokens: Callable[[str], int]) -> list[str]:
        paragraphs = self._split_paragraphs(text)
        chunks: list[str] = []
        buf = ""
        for p in paragraphs:
            while count_tokens(p) > self.size:
                if buf:
                    chunks.append(buf)
                    buf = ""
                head = self._fit(p, self.size, count_tokens)
                chunks.append(head)
                rest = p[len(head) :]
                tail = self._overlap_tail(head, count_tokens)
                p = tail + rest if rest else ""
            candidate = f"{buf}\n\n{p}" if buf else p
            if count_tokens(candidate) <= self.size:
                buf = candidate
            else:
                chunks.append(buf)
                buf = p
        if buf.strip():
            chunks.append(buf)
        return [c.strip() for c in chunks if c.strip()]

    def _split_paragraphs(self, text: str) -> list[str]:
        parts = re.split(r"\n(?=#{1,6} )|\n{2,}", text)
        return [p.strip() for p in parts if p.strip()]

    def _fit(self, text: str, max_tokens: int, count_tokens: Callable[[str], int]) -> str:
        lo, hi = 1, len(text)
        best = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if count_tokens(text[:mid]) <= max_tokens:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return text[:best]

    def _overlap_tail(self, text: str, count_tokens: Callable[[str], int]) -> str:
        if not text or count_tokens(text) <= self.overlap:
            return ""
        lo, hi = 0, len(text)
        best = len(text)
        while lo <= hi:
            mid = (lo + hi) // 2
            if count_tokens(text[mid:]) <= self.overlap:
                best = mid
                hi = mid - 1
            else:
                lo = mid + 1
        return text[best:]
