import asyncio
import hashlib
import json
import re
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

from .digest_worker import DigestWorker
from .daily_hook import ContextDiffer
from .daily_md import DEFAULT_DIGEST_TIME, cycle_file_date
from .notebook import append_text, delete_text, edit_text, find_dup_num
from .memory_store.chunker import Chunker
from .memory_store.embedder import Embedder
from .session_store import SessionStore
from .space import SpaceManager
from .watcher import FileWatcher

INJECT_MARKER = "\n# Memory Context\n\n"


def _dbg(msg: str) -> None:
    try:
        with open(
            StarTools.get_data_dir() / "debug.log", "a", encoding="utf-8"
        ) as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}" + chr(10))
    except Exception:
        pass


def _scan_cmd_handlers() -> None:
    try:
        from astrbot.core.star.star_handler import (
            EventType,
            star_handlers_registry,
        )

        _dbg(f"plugin module name: {__name__}")
        for h in star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent, only_activated=False
        ):
            low = h.handler_name.lower()
            mod = (h.handler_module_path or "").lower()
            if "mem" in low or "simple_memory" in mod:
                _dbg(
                    f"cmd handler: full={h.handler_full_name} "
                    f"module={h.handler_module_path} enabled={h.enabled}"
                )
    except Exception as e:
        _dbg(f"cmd handler scan failed: {e!r}")


def _resolve_data_path(raw: str, fallback: str) -> Path:
    raw = str(raw or "").strip()
    p = Path(raw)
    if p.is_absolute():
        return p
    return StarTools.get_data_dir() / (raw or fallback)



class SimpleMemory(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        cfg = config or {}
        self.cfg = cfg
        self.enabled: bool = bool(cfg.get("enabled", True))
        self.workspace: Path = Path(str(cfg.get("workspace_path", "") or ""))
        chunk_size = int(cfg.get("chunk_size") or 384)
        max_ctx = int(cfg.get("embed_max_ctx") or 0)
        if max_ctx > 0:
            clamped = max(max_ctx - 32, 128)
            if chunk_size > clamped:
                logger.warning(
                    f"[SimpleMemory] chunk_size {chunk_size} 超过 "
                    f"embed_max_ctx {max_ctx}，钳制为 {clamped}"
                )
                chunk_size = clamped
        self.chunker = Chunker(
            size=chunk_size,
            overlap=int(cfg.get("chunk_overlap") or 64),
        )
        self.spaces: SpaceManager | None = None
        self.embedder: Embedder | None = None
        self._embedder_task: asyncio.Task | None = None
        self.watcher: FileWatcher | None = None
        self.inject_files = list(self.cfg.get("inject_files") or ["MEMORY.md"])
        self._index_lock = asyncio.Lock()
        self._daily_lock = asyncio.Lock()
        self._notebook_lock = asyncio.Lock()
        self.notebook_name = str(cfg.get("notebook_name") or "小本子")
        self._inject_cache: dict[str, str] = {}
        self._inited = False
        self._index_state: dict = {}
        self.digest_enabled: bool = bool(cfg.get("digest_enabled", True))
        self.boundary_inject: bool = bool(cfg.get("boundary_inject", True))
        self.capture_think: int = int(cfg.get("capture_think_chars") or 0)
        self.capture_tool: int = int(cfg.get("capture_tool_chars") or 0)
        self.digest_state_budget: int = int(
            cfg.get("digest_state_budget") or 24000
        )
        self.differ: ContextDiffer | None = None
        self.digest_worker: DigestWorker | None = None

    async def initialize(self) -> None:
        _dbg(f"initialize() start enabled={self.enabled}")
        _scan_cmd_handlers()
        if not self.enabled:
            logger.info("simple_memory 未启用")
            _dbg("initialize() early return: disabled")
            return
        self.workspace = _resolve_data_path(
            str(self.cfg.get("workspace_path") or ""), "memory"
        )
        self._index_state = self._load_index_state()
        self.workspace.mkdir(parents=True, exist_ok=True)
        digest_time = str(self.cfg.get("digest_time") or DEFAULT_DIGEST_TIME)
        self.spaces = SpaceManager(
            self.workspace,
            digest_time,
            self.cfg.get("digest_session_whitelist") or [],
        )
        self._inited = True

        if self.digest_enabled:
            self._inject_compress_instruction()

            self.differ = ContextDiffer(
                store=SessionStore(StarTools.get_data_dir() / "session_store.json"),
                daily_file_for=self.spaces.daily_file,
                lock=self._daily_lock,
                think_cap=self.capture_think,
                tool_cap=self.capture_tool,
                context=self.context,
            )
            self.digest_worker = DigestWorker(
                store=self.differ.store,
                daily_file_for=self.spaces.daily_file,
                diary_file_for=self.spaces.diary_file,
                context=self.context,
                lock=self._daily_lock,
                digest_time=str(self.cfg.get("digest_time") or DEFAULT_DIGEST_TIME),
                diary_provider_id=str(self.cfg.get("diary_provider_id") or ""),
                diary_persona_id=str(self.cfg.get("diary_persona_id") or ""),
                tail_summary_threshold=int(
                    self.cfg.get("tail_summary_threshold") or 2000
                ),
                raw_ttl_days=int(self.cfg.get("raw_ttl_days") or 0),
                session_whitelist=self.cfg.get("digest_session_whitelist") or [],
                think_cap=self.capture_think,
                tool_cap=self.capture_tool,
                state_budget=self.digest_state_budget,
            )
            self.digest_worker.start()

        provider_id = str(self.cfg.get("embedding_provider_id") or "")
        if not provider_id:
            logger.warning(
                "simple_memory: 未配置 embedding_provider_id（需在 AstrBot WebUI 提供商管理中配置 Embedding 类型提供商）"
            )
            _dbg("initialize() early return: no embedding_provider_id")
            return
        self.embedder = Embedder(
            context=self.context,
            provider_id=provider_id,
            batch_size=int(self.cfg.get("embed_batch_size") or 16),
            tasks_limit=int(self.cfg.get("embed_concurrency") or 3),
        )
        # M6-9: 核心在加载插件之后才实例化 Provider（plugin_manager.reload() 先于
        # provider_manager.initialize()），在这里内联做必然撞空窗（6 次重试 = 白卡 50 秒启动），
        # 改后台延迟任务：事件循环一让出，provider 已灌满，立即加载成功
        self._embedder_task = asyncio.create_task(self._deferred_embedder_load())

        self.watcher = FileWatcher(
            self.workspace, on_change=self._on_md_changed, on_delete=self._on_md_deleted
        )
        self.watcher.start()

        logger.info(
            f"simple_memory 启动完成，会话空间 {len(self.spaces.existing_dirs())} 个"
        )
        _dbg(f"initialize() done workspace={self.workspace}")

    async def _deferred_embedder_load(self) -> None:
        """M6-9: 后台加载 embedding（原因见 initialize 注释），成功后做初始重建索引"""
        logger.info("simple_memory 正在初始化 embedding（延迟加载）...")
        loaded = False
        for attempt in range(1, 7):
            try:
                await self.embedder.load()
                loaded = True
                break
            except Exception as e:
                logger.warning(
                    f"simple_memory embedding 初始化失败（第 {attempt}/6 次）: {e}"
                )
                if attempt < 6:
                    await asyncio.sleep(10)
        if not loaded:
            logger.warning(
                "simple_memory embedding 6 次重试失败，日记退化为纯文本，语义检索不可用"
            )
            self.embedder = None
            return
        logger.info("simple_memory embedding 就绪（延迟加载）")
        try:
            await self._reindex_all(force=False)
        except Exception as e:
            logger.warning(f"simple_memory 初始重建索引失败: {e}")

    async def terminate(self) -> None:
        _dbg("terminate() called")
        if self.digest_worker:
            await self.digest_worker.stop()
            self.digest_worker = None
        if self.differ:
            await self.differ.store.flush()
        if self.watcher:
            await self.watcher.stop()
            self.watcher = None
        if self._embedder_task and not self._embedder_task.done():
            self._embedder_task.cancel()
        self._embedder_task = None
        if self.embedder:
            await self.embedder.unload()
            self.embedder = None
        # spaces 只是路径计算器不占资源，重载窗口内退场实例的工具调用仍可安全使用
        logger.info("simple_memory 已停止")

    @staticmethod
    def info() -> dict[str, Any]:
        return {
            "name": "astrbot_plugin_simple_memory",
            "author": "tuan",
            "description": "三层记忆：向量检索 + system prompt 注入 + 每日日记 + 共同小本子",
            "version": "0.1.0",
        }

    def _vdb_for(self, session_id: str):
        return self.spaces.vdb(session_id, self.embedder.dim)

    def _state_key(self, dir_name: str, rel: str) -> str:
        return f"{dir_name}/{rel}"

    async def _reindex_all(self, force: bool = True) -> None:
        total_re = 0
        total_unch = 0
        for dir_name in self.spaces.existing_dirs():
            vdb = self._vdb_for(dir_name)
            mf = self.spaces.files(dir_name)
            files = self.spaces.diary_files(dir_name)
            disk = {mf.rel(f) for f in files}
            for stale in vdb.list_files() - disk:
                vdb.delete_file(stale)
                logger.info(
                    f"simple_memory 清理残留索引: {self._state_key(dir_name, stale)}"
                )
            reindexed = 0
            unchanged = 0
            for f in files:
                rel = mf.rel(f)
                key = self._state_key(dir_name, rel)
                try:
                    if not force and self._file_hash_unchanged(vdb, rel, f):
                        unchanged += 1
                        continue
                    await self._index_file(f, dir_name)
                    self._record_indexed(key, f)
                    reindexed += 1
                except Exception:
                    logger.exception(
                        f"simple_memory 索引文件失败（跳过，其余继续）: {key}"
                    )
            total_re += reindexed
            total_unch += unchanged
            logger.info(
                f"simple_memory 索引完成 {dir_name[:24]}：重建 {reindexed} 个文件，"
                f"未变更跳过 {unchanged} 个"
            )
        logger.info(
            f"simple_memory 全空间索引完成：重建 {total_re} 个文件，"
            f"未变更跳过 {total_unch} 个"
        )

    def _file_hash_unchanged(self, vdb, rel: str, path: Path) -> bool:
        stored = vdb.get_file_hash(rel)
        if not stored:
            return False
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        return hashlib.sha256(text.encode("utf-8")).hexdigest() == stored

    async def _index_file(self, path: Path, dir_name: str) -> None:
        async with self._index_lock:
            await self._index_file_locked(path, dir_name)

    async def _index_file_locked(self, path: Path, dir_name: str) -> None:
        vdb = self._vdb_for(dir_name)
        mf = self.spaces.files(dir_name)
        rel = mf.rel(path)
        try:
            text = mf.read(path)
        except OSError:
            vdb.delete_file(rel)
            logger.info(f"simple_memory 索引时文件已消失: {rel}")
            return
        vdb.delete_file(rel)
        if not text.strip():
            return
        chunks = self.chunker.split(text, self.embedder.count_tokens)
        if not chunks:
            return
        embs = await self.embedder.embed(chunks)
        now = int(time.time())
        if not path.is_file():
            logger.info(f"simple_memory embedding 期间文件被删: {rel}")
            return
        file_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        vdb.upsert(
            ids=[vdb.chunk_id(rel, i) for i in range(len(chunks))],
            embeddings=embs,
            texts=chunks,
            metas=[
                {
                    "file": rel,
                    "source": "simple_memory",
                    "timestamp": now,
                    "file_hash": file_hash,
                }
                for _ in chunks
            ],
        )
        logger.info(
            f"simple_memory 已索引 {self._state_key(dir_name, rel)}，{len(chunks)} 块"
        )

    def _pointer_block(self, session_id: str) -> str:
        if not self.digest_enabled:
            return ""
        day = cycle_file_date(datetime.now(), self.spaces.digest_time)
        prev = (
            datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=1)
        ).isoformat()
        dt = self.spaces.digest_time
        diary = self.spaces.diary_dir(session_id) / f"{day}.md"
        raw = self.spaces.memory_dir(session_id) / f"{day}.md"
        lines = ["## 记忆文件"]
        if self.boundary_inject:
            lines.append(
                f"一天边界：一天原文与日记从 {dt} 到次日 {dt}，文件名是结算日日期。"
            )
        lines.append(
            f"最新日记 {diary}（覆盖 {prev[5:]} {dt} → {day[5:]} {dt}）；"
            f"当日原文在 {raw} 可直接读/grep，日记尚未生成"
        )
        return "\n".join(lines)

    def _notebook_block(self) -> str:
        name = self.notebook_name
        return (
            f"## {name}（MEMORY.md）\n"
            "长期共同记忆，条目格式 序号. [日期时间] 内容。\n"
            f"用户确认的稳定事实（偏好、决定、长期信息）可追加进{name}：先问一句，得到点头后用 memory_append 写入；"
            "只问稳定事实，不问即时状态（今天日程、临时心情）。\n"
            "查看用 memory_read，改单条用 memory_edit，删单条用 memory_delete，整篇重写用 memory_write（逃生门，慎用）。"
        )

    def _build_inject(self, session_id: str) -> str:
        parts: list[str] = []
        if self.spaces.is_active(session_id):
            parts.append(self._pointer_block(session_id))
            parts.append(self._notebook_block())
        for name in self.inject_files:
            p = self.spaces.path(session_id) / str(name)
            if not p.is_file():
                p = self.workspace / str(name)
            if p.is_file():
                content = p.read_text(encoding="utf-8", errors="ignore").strip()
                if content:
                    parts.append(f"## {name}\n{content}")
        return "\n\n".join(parts)
    def _inject_compress_instruction(self) -> None:
        """启动时检测压缩提示词，若无结构标记则内存中追加。"""
        try:
            conf = self.context.get_config()
            settings = conf.setdefault("provider_settings", {})
            instr = settings.get("llm_compress_instruction") or ""
            if "[经验 START]" in instr:
                _dbg("compress instruction 已有标记，跳过注入")
                return
            addition = (
                "\n\n摘要末尾追加经验段（固定格式）：\n"
                "[经验 START]\n"
                "- （关键决定/结论/踩坑/偏好，每条一行；无则写\"无\"）\n"
                "[经验 END]"
            )
            settings["llm_compress_instruction"] = instr + addition
            logger.info("simple_memory 已注入压缩摘要结构标记")
            _dbg("compress instruction 注入完成")
        except Exception as e:
            logger.warning(f"simple_memory 压缩提示词注入失败（不影响主功能）: {e}")
            _dbg(f"compress instruction 注入异常: {e}")

    def _load_index_state(self) -> dict:
        p = StarTools.get_data_dir() / "index_state.json"
        try:
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("simple_memory 读取 index_state 失败")
        return {}

    def _save_index_state(self) -> None:
        try:
            p = StarTools.get_data_dir() / "index_state.json"
            p.write_text(
                json.dumps(self._index_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("simple_memory 写入 index_state 失败")

    def _should_reindex_file(self, path: Path, rel: str) -> bool:
        st = self._index_state.get(rel)
        if not st:
            return True
        try:
            raw = path.read_bytes()
            size = int(st["size"])
        except (OSError, KeyError, ValueError):
            return True
        if len(raw) < size:
            return True
        if hashlib.sha256(raw[:size]).hexdigest() != st["prefix_hash"]:
            return True
        delta = raw[size:].decode("utf-8", errors="ignore")
        if not delta.strip():
            return False
        tokens = self.embedder.count_tokens(delta)
        threshold = int(self.cfg.get("reindex_min_delta_tokens") or 2000)
        if tokens < threshold:
            logger.info(
                f"simple_memory watcher 跳过重建: {rel} 增量 {tokens} token < {threshold}"
            )
            return False
        return True

    def _record_indexed(self, rel: str, path: Path) -> None:
        try:
            raw = path.read_bytes()
            self._index_state[rel] = {
                "size": len(raw),
                "prefix_hash": hashlib.sha256(raw).hexdigest(),
            }
            self._save_index_state()
        except Exception:
            logger.exception("simple_memory 记录索引状态失败")

    def _space_dir_of(self, path: Path) -> str | None:
        try:
            parts = path.relative_to(self.workspace).parts
        except ValueError:
            return None
        if len(parts) < 2:
            return None
        return parts[0]

    def _is_diary_path(self, path: Path, dir_name: str) -> bool:
        try:
            parts = path.relative_to(self.workspace / dir_name).parts
        except ValueError:
            return False
        return (
            len(parts) == 3
            and parts[0] == "memory"
            and parts[1] == "diary"
            and parts[2].endswith(".md")
        )

    async def _on_md_changed(self, path: Path) -> None:
        if not self.embedder:
            return
        dir_name = self._space_dir_of(path)
        if dir_name is None:
            if path.name in self.inject_files:
                self._inject_cache.clear()
            return
        rel = path.relative_to(self.workspace / dir_name).as_posix()
        if rel == "MEMORY.md":
            self._inject_cache.pop(dir_name, None)
            return
        if not self._is_diary_path(path, dir_name):
            return  # 原文走 grep，不建索引
        key = self._state_key(dir_name, rel)
        if not self._should_reindex_file(path, key):
            return
        await self._index_file(path, dir_name)
        self._record_indexed(key, path)
        logger.info(f"simple_memory 增量重建索引: {key}")

    async def _on_md_deleted(self, path: Path) -> None:
        if not self.embedder:
            return
        dir_name = self._space_dir_of(path)
        if dir_name is None:
            if path.name in self.inject_files:
                self._inject_cache.clear()
            return
        rel = path.relative_to(self.workspace / dir_name).as_posix()
        if rel == "MEMORY.md":
            self._inject_cache.pop(dir_name, None)
            return
        if not self._is_diary_path(path, dir_name):
            return
        async with self._index_lock:
            vdb = self._vdb_for(dir_name)
            vdb.delete_file(rel)
            if path.is_file():
                await self._index_file_locked(path, dir_name)
            else:
                logger.info(
                    f"simple_memory 已移除索引: {self._state_key(dir_name, rel)}"
                )

    @filter.on_llm_request()
    async def _inject(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self.enabled or not self._inited:
            return
        session_id = req.session_id or str(event.unified_msg_origin)
        text = self._inject_cache.get(session_id)
        if text is None:
            text = self._build_inject(session_id)
            if not text:
                return
            self._inject_cache[session_id] = text
            _dbg(f"_inject built session={session_id[:24]} len={len(text)}")
        sp = req.system_prompt or ""
        if INJECT_MARKER not in sp:
            req.system_prompt = sp + INJECT_MARKER + text

    @filter.after_message_sent()
    async def _capture(self, event: AstrMessageEvent) -> None:
        if not self.enabled or not self.differ:
            _dbg("_capture early: enabled/differ missing")
            return
        try:
            session_id = str(event.unified_msg_origin)
            _dbg(f"_capture fired session={session_id[:30]}")
            if not self.spaces.is_active(session_id):
                _dbg(f"_capture 跳过非活跃会话 {session_id[:24]}")
                return
            await self.differ.process(session_id)
        except Exception:
            import traceback

            _dbg("_capture 异常: " + traceback.format_exc(limit=3))
            logger.exception("simple_memory 上下文捕获失败")

    @filter.llm_tool(name="memory_search")
    async def memory_search(
        self,
        event: AstrMessageEvent,
        query: str,
        source: str = "all",
        time_range: str = "",
    ) -> str:
        """搜索记忆库（日记走向量检索，原文和小本子走 grep）。

        Args:
            query(string): 搜索关键词或自然语言描述
            source(string): 来源过滤，all=日记向量+原文和小本子 grep（默认），diary=只查日记向量，raw=只 grep
            time_range(string): 时间范围，如 7d=最近7天、24h=最近24小时，留空不限
        """
        t = self._embedder_task
        if t is not None and not t.done():
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout=65)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if not self.embedder:
            return "记忆库未初始化"
        session_id = str(event.unified_msg_origin)
        if not self.spaces.is_active(session_id):
            return "本会话未启用记忆（不在白名单）"
        src = (source or "all").strip().lower()
        if src in ("simple_memory", "astrbot"):
            src = "all"
        if src not in ("all", "diary", "raw"):
            src = "all"
        parts: list[str] = []
        if src in ("all", "diary"):
            hits = await self.spaces.searcher(
                session_id, self.embedder.dim, self.embedder
            ).search(query=query, time_range=time_range, top_k=5)
            for h in hits:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(h.timestamp))
                parts.append(
                    f"[日记 | {h.file} | {ts} | score {h.score}]\n{h.text}"
                )
        if src in ("all", "raw"):
            parts.extend(self._grep_search(session_id, query, time_range))
        if not parts:
            return "未找到相关记忆"
        return "\n---\n".join(parts)

    def _grep_search(
        self, session_id: str, query: str, time_range: str = ""
    ) -> list[str]:
        q = (query or "").strip().lower()
        if not q:
            return []
        terms = q.split()
        days = 0.0
        m = re.fullmatch(r"(\d+)([mhdw])", (time_range or "").strip().lower())
        if m:
            unit = {"m": 1 / 1440, "h": 1 / 24, "d": 1, "w": 7}[m.group(2)]
            days = max(1, int(int(m.group(1)) * unit))
        cutoff = date.today() - timedelta(days=days) if days else None
        cands: list[Path] = []
        nb = self.spaces.notebook_path(session_id)
        if nb.is_file():
            cands.append(nb)
        # 7.3D: summary.md 优先（结构化经验，密度高）
        summaries = sorted(
            [
                f
                for f in self.spaces.memory_dir(session_id).glob("*.summary.md")
                if f.is_file()
            ],
            key=lambda p: p.stem.replace(".summary", ""),
            reverse=True,
        )
        if cutoff:
            kept_sum = []
            for f in summaries:
                stem = f.stem.replace(".summary", "")
                dm2 = re.fullmatch(r"(\d{4}-\d{2}-\d{2})", stem)
                if dm2:
                    y2, mo2, d2 = (int(x) for x in dm2.group(1).split("-"))
                    if date(y2, mo2, d2) >= cutoff:
                        kept_sum.append(f)
                else:
                    kept_sum.append(f)
            summaries = kept_sum
        cands.extend(summaries[:10])
        raws = sorted(self.spaces.raw_files(session_id), reverse=True)
        if cutoff:
            kept = []
            for f in raws:
                dm = re.fullmatch(r"(\d{4}-\d{2}-\d{2})", f.stem)
                if dm:
                    y, mo, d = (int(x) for x in dm.group(1).split("-"))
                    if date(y, mo, d) >= cutoff:
                        kept.append(f)
                else:
                    kept.append(f)
            raws = kept
        cands.extend(raws[:20])
        out: list[str] = []
        for f in cands:
            try:
                lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            hits = 0
            for i, line in enumerate(lines):
                low = line.lower()
                if len(terms) > 1:
                    ok = all(t in low for t in terms)
                else:
                    ok = terms[0] in low
                if not ok:
                    continue
                ctx = "\n".join(lines[max(0, i - 1) : i + 2])
                out.append(f"[grep | {f.name}:{i + 1}]\n{ctx}")
                hits += 1
                if hits >= 4:
                    break
            if len(out) >= 8:
                break
        return out[:8]

    def _notebook_path(self, event: AstrMessageEvent) -> Path:
        session_id = str(event.unified_msg_origin)
        self.spaces.ensure(session_id)
        return self.spaces.notebook_path(session_id)

    def _notebook_bak(self, path: Path) -> None:
        try:
            if path.is_file():
                shutil.copy2(path, path.parent / (path.name + ".bak"))
        except Exception:
            logger.exception("simple_memory 小本子备份失败")

    @filter.llm_tool(name="memory_read")
    async def memory_read(self, event: AstrMessageEvent) -> str:
        """读取小本子（MEMORY.md）全文。"""
        session_id = str(event.unified_msg_origin)
        if not self.spaces.is_active(session_id):
            return "本会话未启用记忆（不在白名单）"
        p = self._notebook_path(event)
        if not p.is_file():
            return "小本子还是空的"
        text = p.read_text(encoding="utf-8", errors="ignore").strip()
        return text or "小本子还是空的"

    @filter.llm_tool(name="memory_append")
    async def memory_append(self, event: AstrMessageEvent, content: str) -> str:
        """向小本子（MEMORY.md）追加一条记忆。

        Args:
            content(string): 要记住的内容，一行，多句用分号隔开
        """
        session_id = str(event.unified_msg_origin)
        if not self.spaces.is_active(session_id):
            return "本会话未启用记忆（不在白名单）"
        async with self._notebook_lock:
            p = self._notebook_path(event)
            old = p.read_text(encoding="utf-8", errors="ignore") if p.is_file() else ""
            dup = find_dup_num(old, content)
            if dup:
                return f"小本子已有相同内容（第 {dup} 条），未重复追加"
            new, num = append_text(old, content, time.strftime("%Y-%m-%d %H:%M"))
            self._notebook_bak(p)
            p.write_text(new, encoding="utf-8")
        return f"已记入小本子第 {num} 条：{content.strip()}"

    @filter.llm_tool(name="memory_edit")
    async def memory_edit(self, event: AstrMessageEvent, num: int, content: str) -> str:
        """修改小本子中指定序号条目的内容，序号和时间戳不变。

        Args:
            num(number): 要修改的条目序号
            content(string): 新的内容
        """
        session_id = str(event.unified_msg_origin)
        if not self.spaces.is_active(session_id):
            return "本会话未启用记忆（不在白名单）"
        async with self._notebook_lock:
            p = self._notebook_path(event)
            if not p.is_file():
                return "小本子还是空的"
            old = p.read_text(encoding="utf-8", errors="ignore")
            new, hit = edit_text(old, int(num), content)
            if not hit:
                return f"没找到第 {int(num)} 条，先用 memory_read 看现有条目"
            self._notebook_bak(p)
            p.write_text(new, encoding="utf-8")
        return f"已修改小本子第 {int(num)} 条：{content.strip()}"

    @filter.llm_tool(name="memory_delete")
    async def memory_delete(self, event: AstrMessageEvent, num: int) -> str:
        """删除小本子中指定序号的条目，序号不复用（留洞）。

        Args:
            num(number): 要删除的条目序号
        """
        session_id = str(event.unified_msg_origin)
        if not self.spaces.is_active(session_id):
            return "本会话未启用记忆（不在白名单）"
        async with self._notebook_lock:
            p = self._notebook_path(event)
            if not p.is_file():
                return "小本子还是空的"
            old = p.read_text(encoding="utf-8", errors="ignore")
            new, hit = delete_text(old, int(num))
            if not hit:
                return f"没找到第 {int(num)} 条，先用 memory_read 看现有条目"
            self._notebook_bak(p)
            p.write_text(new, encoding="utf-8")
        return f"已删除小本子第 {int(num)} 条"

    @filter.llm_tool(name="memory_write")
    async def memory_write(self, event: AstrMessageEvent, content: str) -> str:
        """整篇重写小本子（MEMORY.md），逃生门，慎用。

        Args:
            content(string): 新的全文
        """
        session_id = str(event.unified_msg_origin)
        if not self.spaces.is_active(session_id):
            return "本会话未启用记忆（不在白名单）"
        async with self._notebook_lock:
            p = self._notebook_path(event)
            old = p.read_text(encoding="utf-8", errors="ignore") if p.is_file() else ""
            warning = ""
            if old.strip() and len(content.strip()) < len(old.strip()) * 0.5:
                logger.warning(
                    f"simple_memory memory_write 新内容不足旧内容 50%：{len(content.strip())} < {len(old.strip())}"
                )
                warning = "注意：新内容不到旧内容一半，已备份 MEMORY.md.bak"
            self._notebook_bak(p)
            p.write_text(content.strip() + "\n", encoding="utf-8")
        return "已重写小本子。" + warning

    @filter.command_group("mem")
    def mem_group(self) -> None:
        """simple_memory 记忆管理指令组 /mem"""
        pass

    @mem_group.command("status", priority=10)
    async def mem_status(self, event: AstrMessageEvent) -> None:
        """查看索引与注入状态"""
        _dbg(f"mem status hit sender={event.get_sender_id()!r}")
        if not self.embedder:
            yield event.plain_result("记忆插件未启动（检查 enabled / workspace_path）")
            return
        session_id = str(event.unified_msg_origin)
        count = self._vdb_for(session_id).count()
        raws = self.spaces.raw_files(session_id)
        dirs = self.spaces.existing_dirs()
        yield event.plain_result(
            "本会话日记向量: {} 块（原文 {} 个文件走 grep）\nmemory 根: {}\n会话空间: {}\nembedding: {} (dim={})\n注入文件: {}".format(
                count,
                len(raws),
                self.workspace,
                ", ".join(d[:24] for d in dirs) or "暂无",
                self.embedder.provider_id,
                self.embedder.dim,
                ", ".join(map(str, self.inject_files)),
            )
        )

    @mem_group.command("rebuild", priority=10)
    async def mem_rebuild(self, event: AstrMessageEvent) -> None:
        """强制全量重建索引"""
        _dbg(f"mem rebuild hit sender={event.get_sender_id()!r}")
        if not self.embedder:
            yield event.plain_result("记忆插件未启动")
            return
        yield event.plain_result("正在重建索引...")
        await self._reindex_all()
        self._inject_cache.clear()
        yield event.plain_result("重建完成")

    @mem_group.command("digest", priority=10)
    async def mem_digest(self, event: AstrMessageEvent) -> None:
        """手动触发一次日记总结"""
        _dbg(f"mem digest hit sender={event.get_sender_id()!r}")
        if not self.digest_worker:
            yield event.plain_result("digest worker 未启动（检查 digest_enabled）")
            return
        yield event.plain_result("开始手动总结...")
        await self.digest_worker.digest()
        yield event.plain_result("总结完成")

    @mem_group.command("test", priority=10)
    async def mem_test(
        self, event: AstrMessageEvent, query: GreedyStr = GreedyStr
    ) -> None:
        """搜索记忆库"""
        q = " ".join(query.split())
        _dbg(f"mem test hit q={q!r} sender={event.get_sender_id()!r}")
        if not q:
            yield event.plain_result("用法: /mem test <关键词>")
            return
        if not self.embedder:
            yield event.plain_result("记忆插件未启动")
            return
        session_id = str(event.unified_msg_origin)
        parts: list[str] = []
        hits = await self.spaces.searcher(
            session_id, self.embedder.dim, self.embedder
        ).search(query=q, top_k=3)
        for h in hits:
            parts.append(f"[{h.source} | {h.file} | {h.score}]\n{h.text[:200]}")
        parts.extend(self._grep_search(session_id, q, ""))
        if not parts:
            yield event.plain_result(f"「{q}」无结果")
            return
        for p in parts:
            yield event.plain_result(p)
