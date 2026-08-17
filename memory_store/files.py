from pathlib import Path


class MemoryFiles:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def list_memory_files(self) -> list[Path]:
        result: list[Path] = []
        if self.workspace.is_dir():
            result.extend(sorted(self.workspace.glob("*.md")))
            mem_dir = self.workspace / "memory"
            if mem_dir.is_dir():
                result.extend(sorted(mem_dir.rglob("*.md")))
        return result

    def read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="ignore")

    def rel(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()
