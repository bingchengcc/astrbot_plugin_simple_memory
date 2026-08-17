import asyncio
import json
from pathlib import Path
from typing import Any

from astrbot.api import logger


def empty_entry() -> dict[str, Any]:
    return {
        "summary": "",
        "summary_states": [],
        "last_compress_ts": 0,
        "watermark_ts": 0,
        "summary_consumed": False,
        "snapshot": {"count": 0, "last_fp": ""},
    }


class SessionStore:
    """会话级中转存储：最新压缩摘要 + 上次请求快照 + 水位线。

    插件 data 目录 JSON 持久化，单锁串行写，原子替换落盘。
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self._data: dict[str, dict] = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            try:
                if self.path.is_file():
                    self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"session_store 读取失败，重置: {e}")
                self._data = {}
            self._loaded = True

    async def get(self, session_id: str) -> dict:
        await self._ensure_loaded()
        async with self._lock:
            return self._data.get(session_id) or empty_entry()

    async def update(self, session_id: str, **fields: Any) -> None:
        await self._ensure_loaded()
        async with self._lock:
            entry = self._data.setdefault(session_id, empty_entry())
            entry.update(fields)
            self._write_unlocked()

    async def clear(self, session_id: str, **fields: Any) -> None:
        await self._ensure_loaded()
        async with self._lock:
            self._data.pop(session_id, None)
            self._data[session_id] = {**empty_entry(), **fields}
            self._write_unlocked()

    async def keys(self) -> list[str]:
        await self._ensure_loaded()
        async with self._lock:
            return list(self._data.keys())

    async def flush(self) -> None:
        await self._ensure_loaded()
        async with self._lock:
            self._write_unlocked()

    def _write_unlocked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self.path)
        except Exception as e:
            logger.warning(f"session_store 写盘失败: {e}")
