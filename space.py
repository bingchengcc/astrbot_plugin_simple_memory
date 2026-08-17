from pathlib import Path

from .daily_md import DEFAULT_DIGEST_TIME

NOTEBOOK_NAME = "MEMORY.md"
NOTEBOOK_TEMPLATE = "# 长期记忆\n\n<!-- 在这里写需要长期记住的内容 -->\n"
VDB_DIRNAME = "chroma"


def dir_name_for(session_id: str) -> str:
    """会话统一 ID → 文件夹名（Windows 文件名不能用冒号，换下划线）。"""
    return (session_id or "").replace(":", "_")


class SpaceManager:
    """按会话隔离的记忆空间。

    workspace/<会话 ID 冒号换下划线>/
        MEMORY.md       该会话的小本子（整篇注入）
        memory/         每日原文（grep 检索，不进库）
        memory/diary/   每日日记（向量库检索，每会话一个 chroma）
        chroma/         该会话的向量库（只装日记块）
    """

    def __init__(
        self,
        workspace: Path,
        digest_time: str = DEFAULT_DIGEST_TIME,
        whitelist=None,
    ):
        self.workspace = workspace
        self.digest_time = digest_time
        self.fragments: list[str] = []
        for item in whitelist or []:
            s = str(item or "").strip().replace("：", ":")
            if s:
                self.fragments.append(s)
        self._daily: dict = {}
        self._diary: dict = {}
        self._vdb: dict = {}
        self._files: dict = {}

    def dir_name(self, session_id: str) -> str:
        return dir_name_for(session_id)

    def path(self, session_id: str) -> Path:
        return self.workspace / self.dir_name(session_id)

    def is_active(self, session_id: str) -> bool:
        if not self.fragments:
            return True
        return any(f in session_id for f in self.fragments)

    def ensure(self, session_id: str) -> Path:
        p = self.path(session_id)
        (p / "memory").mkdir(parents=True, exist_ok=True)
        nb = p / NOTEBOOK_NAME
        if not nb.exists():
            nb.write_text(NOTEBOOK_TEMPLATE, encoding="utf-8")
        return p

    def memory_dir(self, session_id: str) -> Path:
        d = self.path(session_id) / "memory"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def daily_file(self, session_id: str):
        from .daily_md import DailyFile

        key = self.dir_name(session_id)
        if key not in self._daily:
            self._daily[key] = DailyFile(
                self.memory_dir(session_id), self.digest_time
            )
        return self._daily[key]

    def diary_dir(self, session_id: str) -> Path:
        d = self.memory_dir(session_id) / "diary"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def diary_file(self, session_id: str):
        from .daily_md import DailyFile

        key = self.dir_name(session_id)
        if key not in self._diary:
            self._diary[key] = DailyFile(
                self.diary_dir(session_id), self.digest_time
            )
        return self._diary[key]

    def raw_files(self, session_id: str) -> list[Path]:
        d = self.path(session_id) / "memory"
        if not d.is_dir():
            return []
        return sorted(f for f in d.glob("*.md") if not f.name.endswith(".summary.md"))

    def diary_files(self, session_id: str) -> list[Path]:
        d = self.path(session_id) / "memory" / "diary"
        if not d.is_dir():
            return []
        return sorted(d.glob("*.md"))

    def notebook_path(self, session_id: str) -> Path:
        return self.path(session_id) / NOTEBOOK_NAME

    def files(self, session_id: str):
        from .memory_store.files import MemoryFiles

        key = self.dir_name(session_id)
        if key not in self._files:
            self._files[key] = MemoryFiles(self.path(session_id))
        return self._files[key]

    def vdb(self, session_id: str, dim: int):
        from .memory_store.vector_db import VectorDB

        key = self.dir_name(session_id)
        if key not in self._vdb:
            d = self.path(session_id) / VDB_DIRNAME
            d.mkdir(parents=True, exist_ok=True)
            v = VectorDB(str(d))
            v.init(dim)
            self._vdb[key] = v
        return self._vdb[key]

    def searcher(self, session_id: str, dim: int, embedder):
        from .memory_store.searcher import Searcher

        return Searcher(self.vdb(session_id, dim), embedder)

    def existing_dirs(self) -> list[str]:
        """磁盘上已存在的会话空间文件夹名。"""
        if not self.workspace.is_dir():
            return []
        return sorted(
            d.name
            for d in self.workspace.iterdir()
            if d.is_dir()
            and ((d / "memory").is_dir() or (d / NOTEBOOK_NAME).is_file())
        )
