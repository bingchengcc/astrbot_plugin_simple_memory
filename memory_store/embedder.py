class Embedder:
    def __init__(
        self, context, provider_id: str, batch_size: int = 16, tasks_limit: int = 3
    ):
        self.context = context
        self.provider_id = provider_id
        self.batch_size = max(1, int(batch_size))
        self.tasks_limit = max(1, int(tasks_limit))
        self.dim = 0

    async def load(self) -> None:
        emb = await self._get_provider().get_embedding("ping")
        self.dim = len(emb)

    def _get_provider(self):
        from astrbot.core.provider.provider import EmbeddingProvider

        if self.context is None:
            raise RuntimeError("context 未注入，无法获取 AstrBot Embedding Provider")
        prov = self.context.get_provider_by_id(self.provider_id)
        if not prov or not isinstance(prov, EmbeddingProvider):
            raise RuntimeError(
                f"未找到 Embedding Provider「{self.provider_id}」，"
                "请在 AstrBot WebUI 提供商管理中配置一个 Embedding 类型提供商并填写其 ID"
            )
        return prov

    def count_tokens(self, text: str) -> int:
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        other = len(text) - cjk
        return cjk + other // 4 + 1

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await self._get_provider().get_embeddings_batch(
            texts,
            batch_size=self.batch_size,
            tasks_limit=self.tasks_limit,
        )

    async def unload(self) -> None:
        pass
