import hashlib

from astrbot.api import logger


class VectorDB:
    COLLECTION = "simple_memory"

    def __init__(self, path: str):
        self.path = path
        self.col = None
        self._client = None

    def init(self, dim: int) -> None:
        import chromadb

        self._client = chromadb.PersistentClient(path=self.path)
        self.col = self._client.get_or_create_collection(
            f"{self.COLLECTION}_dim{dim}",
            metadata={"hnsw:space": "cosine"},
        )

    def stale_collections(self, dim: int) -> list[str]:
        current = f"{self.COLLECTION}_dim{dim}"
        result = []
        for c in self._client.list_collections():
            name = getattr(c, "name", None) or str(c)
            if name.startswith(f"{self.COLLECTION}_dim") and name != current:
                result.append(name)
        return result

    @staticmethod
    def chunk_id(file: str, idx: int) -> str:
        return hashlib.md5(f"{file}#{idx}".encode()).hexdigest()

    def delete_file(self, file: str) -> None:
        try:
            self.col.delete(where={"file": file})
        except Exception:
            logger.exception(f"simple_memory 删除文件向量失败: {file}")
            raise

    def get_file_hash(self, file: str) -> str | None:
        try:
            batch = self.col.get(where={"file": file}, limit=1, include=["metadatas"])
        except Exception:
            logger.exception(f"simple_memory 读取文件哈希失败: {file}")
            return None
        metas = batch.get("metadatas") or []
        if metas and metas[0]:
            return metas[0].get("file_hash")
        return None

    def list_files(self) -> set[str]:
        files: set[str] = set()
        limit = 5000
        offset = 0
        while True:
            batch = self.col.get(include=["metadatas"], limit=limit, offset=offset)
            metas = batch.get("metadatas") or []
            if not metas:
                break
            files.update(m["file"] for m in metas if m and m.get("file"))
            if len(metas) < limit:
                break
            offset += limit
        return files

    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        texts: list[str],
        metas: list[dict],
    ) -> None:
        self.col.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metas,
        )

    def query(self, emb: list[float], n: int = 5, where: dict | None = None) -> dict:
        total = self.col.count()
        if total == 0:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        n = min(n, total)
        return self.col.query(
            query_embeddings=[emb],
            n_results=n,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

    def count(self) -> int:
        return self.col.count()
