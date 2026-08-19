import re
import time
from dataclasses import dataclass


@dataclass
class SearchHit:
    text: str
    source: str
    file: str
    timestamp: int
    score: float


class Searcher:
    def __init__(self, vdb, embedder):
        self.vdb = vdb
        self.embedder = embedder

    async def search(
        self,
        query: str,
        source: str = "all",
        time_range: str = "",
        date: str = "",
        top_k: int = 5,
    ) -> list[SearchHit]:
        if not query.strip():
            return []
        q_emb = await self.embedder.embed([query])
        # Build where clause: source + time_range + date as metadata filter (not post-filter)
        where_clauses: list[dict] = []
        if source in ("simple_memory", "astrbot"):
            where_clauses.append({"source": {"$eq": source}})
        cutoff = self._parse_range(time_range)
        if cutoff:
            where_clauses.append({"timestamp": {"$gte": cutoff}})
        if date and date != "all":
            where_clauses.append({"file": {"$contains": date}})
        where = {"$and": where_clauses} if len(where_clauses) > 1 else (where_clauses[0] if where_clauses else None)
        raw = self.vdb.query(q_emb[0], n=top_k * 3, where=where)

        hits: list[SearchHit] = []
        seen: set[str] = set()
        docs = raw.get("documents", [[]])[0]
        metas = raw.get("metadatas", [[]])[0]
        dists = raw.get("distances", [[]])[0]
        for i, doc in enumerate(docs):
            meta = (metas[i] or {}) if i < len(metas) else {}
            ts = int(meta.get("timestamp", 0) or 0)
            file_key = str(meta.get("file", ""))
            dedup_key = f"{file_key}#{doc[:64]}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            score = round(1.0 - float(dists[i]), 4) if i < len(dists) else 0.0
            hits.append(
                SearchHit(
                    text=doc,
                    source=str(meta.get("source", "unknown")),
                    file=file_key,
                    timestamp=ts,
                    score=score,
                )
            )
            if len(hits) >= top_k:
                break
        return hits

    @staticmethod
    def _parse_range(time_range: str) -> int:
        m = re.fullmatch(r"(\d+)([mhdw])", (time_range or "").strip().lower())
        if not m:
            return 0
        n, unit = int(m.group(1)), m.group(2)
        factor = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
        return int(time.time()) - n * factor
