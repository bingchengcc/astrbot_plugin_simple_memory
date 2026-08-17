import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from astrbot.api import logger


class FileWatcher:
    def __init__(
        self,
        watch_dir: Path,
        on_change: Callable[[Path], Awaitable[None]],
        on_delete: Callable[[Path], Awaitable[None]] | None = None,
        debounce_s: float = 2.0,
    ):
        self.watch_dir = watch_dir
        self.on_change = on_change
        self.on_delete = on_delete
        self.debounce_s = debounce_s
        self._observer = None
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        self._loop = asyncio.get_running_loop()
        watcher_self = self
        loop = self._loop

        class _Handler(FileSystemEventHandler):
            def _touch(self, path_str: str, deleted: bool = False) -> None:
                p = Path(path_str)
                if p.suffix.lower() != ".md":
                    return
                if not deleted and not p.is_file():
                    return
                key = str(p)
                if key in watcher_self._timers:
                    watcher_self._timers[key].cancel()
                watcher_self._timers[key] = loop.call_later(
                    watcher_self.debounce_s, watcher_self._fire, p, deleted
                )

            def on_modified(self, event) -> None:
                if not event.is_directory:
                    self._touch(event.src_path)

            def on_created(self, event) -> None:
                if not event.is_directory:
                    self._touch(event.src_path)

            def on_deleted(self, event) -> None:
                if not event.is_directory:
                    self._touch(event.src_path, deleted=True)

        self._observer = Observer()
        self._observer.schedule(_Handler(), str(self.watch_dir), recursive=True)
        self._observer.daemon = True
        self._observer.start()

    def _fire(self, path: Path, deleted: bool) -> None:
        self._timers.pop(str(path), None)
        t = asyncio.ensure_future(self._safe(path, deleted))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _safe(self, path: Path, deleted: bool) -> None:
        cb = self.on_delete if deleted else self.on_change
        if not cb:
            return
        try:
            await cb(path)
        except Exception:
            logger.exception(f"openclaw_memory watcher 处理失败: {path}")

    async def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=5)
        self._tasks.clear()
