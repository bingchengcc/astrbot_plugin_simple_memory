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
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

from .digest_worker import DigestWorker
from .daily_md import DEFAULT_DIGEST_TIME, cycle_file_date
from .notebook import append_text, delete_text, edit_text, entry_content_at, find_dup_num, find_num_by_content, parse_entries, renumber_text
from .memory_store.chunker import Chunker
from .memory_store.embedder import Embedder
from .session_store import SessionStore
from .space import SpaceManager
from .watcher import FileWatcher



from .debug_logger import _dbg

INJECT_START = "\n# Memory Context\n\n"


import logging


class _ResetLogHandler(logging.Handler):
    """抓日志 /new 和 /reset，把所有 session 的 pending 落地到 MEMORY.md 和 INDEX.md。"""

    def __init__(self, plugin: "SimpleMemory"):
        super().__init__(level=logging.INFO)
        self.plugin = plugin

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if "Switched to new conversation" in msg or "Conversation reset successfully" in msg:
                for d in self.plugin.spaces.existing_dirs():
                    self.plugin._flush_pending(d)
                _dbg("log handler: 落地完成")
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
        self.workspace: Path = Path("memory")
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
        self.embedder_state: str = "STARTING"
        self._embedder_task: asyncio.Task | None = None
        self.watcher: FileWatcher | None = None
        self.inject_files = list(self.cfg.get("inject_files") or ["MEMORY.md"])
        self._index_lock = asyncio.Lock()
        self._daily_lock = asyncio.Lock()
        self._notebook_lock = asyncio.Lock()
        self.notebook_name = str(cfg.get("notebook_name") or "小本子")
        self._memory_topics: list[str] = self.cfg.get("notebook_memory_topics") or ["身份", "关系", "偏好"]
        self._index_topics: list[str] = self.cfg.get("notebook_index_topics") or ["配置", "密钥", "项目"]
        self._inject_cache: dict[str, str] = {}
        self._last_summary: dict[int, str] = {}
        self._inited = False
        self._index_state: dict = {}
        self.boundary_inject: bool = bool(cfg.get("boundary_inject", True))
        self.capture_think: int = int(cfg.get("capture_think_chars") or 0)
        self.store: SessionStore | None = None
        self.digest_worker: DigestWorker | None = None

    async def initialize(self) -> None:
        _dbg("initialize() start")
        _scan_cmd_handlers()
        self.workspace = _resolve_data_path(
            "", "memory"
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

        self._inject_compress_instruction()

        self.store = SessionStore(StarTools.get_data_dir() / "session_store.json")
        self.digest_worker = DigestWorker(
            store=self.store,
            daily_file_for=self.spaces.daily_file,
            diary_file_for=self.spaces.diary_file,
            context=self.context,
            lock=self._daily_lock,
            digest_time=str(self.cfg.get("digest_time") or DEFAULT_DIGEST_TIME),
            diary_provider_id=str(self.cfg.get("diary_provider_id") or ""),
            diary_persona_id=str(self.cfg.get("diary_persona_id") or ""),
            
            raw_ttl_days=int(self.cfg.get("raw_ttl_days") or 0),
            session_whitelist=self.cfg.get("digest_session_whitelist") or [],
            diary_max_ctx=max(4096, int(self.cfg.get("diary_max_ctx") or 32768)),
        )
        self.digest_worker.start()

        use_embedding = bool(self.cfg.get("use_embedding", True))
        provider_id = str(self.cfg.get("embedding_provider_id") or "")

        # 开启 AstrBot：把所有 session 的 pending 落地到 MEMORY.md 和 INDEX.md
        # 不依赖 embedding，放 early return 前保证纯 grep 模式也执行
        for d in self.spaces.existing_dirs():
            self._flush_pending(d)

        # 日志监听：抓 /new 和 /reset 触发注入
        self._log_handler = _ResetLogHandler(self)
        logger.addHandler(self._log_handler)

        if not use_embedding:
            logger.info("simple_memory: use_embedding=false，跳过向量检索（纯 grep 模式）")
            _dbg("initialize() done (no embedding)")
            return
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

        n_dirs = len(self.spaces.existing_dirs())
        if self.embedder:
            logger.info(
                f"simple_memory 启动完成，会话空间 {n_dirs} 个，向量检索已启用"
            )
        else:
            logger.info(
                f"simple_memory 启动完成，会话空间 {n_dirs} 个，纯 grep 模式（未启用向量检索）"
            )
        _dbg(f"initialize() done workspace={self.workspace}")

    async def _deferred_embedder_load(self) -> None:
        """M6-9: 后台加载 embedding（原因见 initialize 注释），成功后做初始重建索引"""
        self.embedder_state = "STARTING"
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
            self.embedder_state = "DEGRADED"
            logger.warning(
                "simple_memory embedding 6 次重试失败，语义检索不可用（状态: DEGRADED）"
            )
            self.embedder = None
            return
        self.embedder_state = "READY"
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
        if self.store:
            await self.store.flush()
        if self.watcher:
            await self.watcher.stop()
            self.watcher = None
        if hasattr(self, '_log_handler') and self._log_handler:
            logger.removeHandler(self._log_handler)
            self._log_handler = None
        if self._embedder_task and not self._embedder_task.done():
            self._embedder_task.cancel()
        self._embedder_task = None
        if self.embedder:
            await self.embedder.unload()
            self.embedder = None
        self.embedder_state = "FAILED"
        # spaces 只是路径计算器不占资源，重载窗口内退场实例的工具调用仍可安全使用
        logger.info("simple_memory 已停止")

    @staticmethod
    def info() -> dict[str, Any]:
        return {
            "name": "astrbot_plugin_lite_memory",
            "author": "冰城cc",
            "description": "三层记忆：向量检索 + system prompt 注入 + 每日日记 + 共同小本子",
            "version": "0.4.1",
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
                    if await self._index_file(f, dir_name):
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

    async def _index_file(self, path: Path, dir_name: str) -> bool:
        async with self._index_lock:
            return await self._index_file_locked(path, dir_name)

    async def _index_file_locked(self, path: Path, dir_name: str) -> bool:
        vdb = self._vdb_for(dir_name)
        mf = self.spaces.files(dir_name)
        rel = mf.rel(path)
        try:
            text = mf.read(path)
        except OSError:
            vdb.delete_file(rel)
            logger.info(f"simple_memory 索引时文件已消失: {rel}")
            return False
        vdb.delete_file(rel)
        if not text.strip():
            return False
        chunks = self.chunker.split(text, self.embedder.count_tokens)
        if not chunks:
            return False
        embs = await self.embedder.embed(chunks)
        if not path.is_file():
            logger.info(f"simple_memory embedding 期间文件被删: {rel}")
            return False
        try:
            recheck = mf.read(path)
        except OSError:
            logger.info(f"simple_memory embedding 后文件被删: {rel}")
            return False
        if hashlib.sha256(recheck.encode("utf-8")).hexdigest() != hashlib.sha256(text.encode("utf-8")).hexdigest():
            logger.info(f"simple_memory embedding 期间文件已变，丢弃本次索引: {rel}")
            return False
        file_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        # 从文件路径提取日记日期作为 timestamp（而非索引时间）
        ts_m = re.search(r"(\d{4}-\d{2}-\d{2})", rel)
        if ts_m:
            diary_ts = int(datetime.strptime(ts_m.group(1), "%Y-%m-%d").replace(hour=21, minute=45).timestamp())
        else:
            diary_ts = int(time.time())
        vdb.upsert(
            ids=[vdb.chunk_id(rel, i) for i in range(len(chunks))],
            embeddings=embs,
            texts=chunks,
            metas=[
                {
                    "file": rel,
                    "source": "simple_memory",
                    "timestamp": diary_ts,
                    "file_hash": file_hash,
                }
                for _ in chunks
            ],
        )
        logger.info(
            f"simple_memory 已索引 {self._state_key(dir_name, rel)}，{len(chunks)} 块"
        )
        return True

    def _pointer_block(self, session_id: str) -> str:
        day = cycle_file_date(datetime.now(), self.spaces.digest_time)
        prev = (
            datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=1)
        ).isoformat()
        dt = self.spaces.digest_time
        dir_name = self.spaces.dir_name(session_id)
        diary_rel = f"{dir_name}/memory/diary/{day}.md"
        raw_rel = f"{dir_name}/memory/{day}.md"
        lines = [f"## 记忆文件（根: {self.workspace}）"]
        if self.boundary_inject:
            lines.append(
                f"一天边界：一天原文与日记从 {dt} 到次日 {dt}，文件名是结算日日期。"
            )
        lines.append(
            f"最新日记: {diary_rel}（覆盖 {prev[5:]} {dt} → {day[5:]} {dt}）"
        )
        lines.append(
            f"当日原文: {raw_rel}"
        )
        return "\n".join(lines)

    def _notebook_block(self) -> str:
        name = self.notebook_name
        return (
            f"## {name}（MEMORY.md）\n"
            f"长期共同记忆，每次对话自动注入。条目格式：序号. [日期时间] [话题] 内容。\n"
            f"追加时 topic 只能选这些（选错不记录）：小本子 {'、'.join(self._memory_topics)}；索引 {'、'.join(self._index_topics)}（只存摘要）\n"
            f"写入规则：\n"
            f"- 用户说了一句稳定事实（身份、偏好、决定）→ 立即 memory_edit 追加，不必等用户说'记住'\n"
            f"- 用户纠正了旧信息 → memory_edit 改对应条目\n"
            f"- 纯提问、即时状态（今天心情、临时日程）→ 不写\n"
            f"- 你的搜索/推理/建议 → 不写（可重新推导的不存）\n"
            f"- 代码/日志/配置示例中出现的值（key/token/密码/端口）→ 不主动记，除非用户明确说'记一下'\n"
            f"- 代码结构、函数名、文件路径（代码库里有）、git 历史（git log 能查）→ 不写\n"
            f"- 存疑就不写\n"
            f"- 写入后不要在回复中提'已记住''已记录'，直接回答用户的问题\n"
            f"读取规则：\n"
            f"- 回答涉及用户过去说的事之前，先 memory_edit 读取小本子确认\n"
            f"- 说'我不记得'之前必须先查过\n"
            f"读/改/删小本子用 memory_edit（content 留空即删除），"
            f"整篇重写用 memory_edit（不传 num + 多行 content，逃生门慎用）。"
            f"INDEX 条目用 memory_edit 传 name 参数操作。\n"
            f"当对话变长时，部分上下文会被摘要压缩，你不需要提前收尾或总结当前进度，继续做事或聊天即可。"
        )

    async def _maybe_compress_notebook(self, session_id: str) -> None:
        """S10: 小本子超限时自动压缩最旧5条为1条摘要（后台执行）"""
        if not self.cfg.get("auto_compress_notebook", False):
            return
        p = self.spaces.notebook_path(session_id)
        if not p.is_file():
            return
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            from .digest_worker import count_tokens
            threshold = int(self.cfg.get("auto_compress_threshold") or 2000)
            if count_tokens(text) < threshold:
                return
            entries = parse_entries(text)
            if len(entries) < 8:
                return
            # 取最旧5条
            oldest = entries[:5]
            oldest_text = "\n".join(f"{e['num']}. [{e['ts']}] {e['content']}" for e in oldest)
            # 用LLM压缩
            provider_id = str(self.cfg.get("diary_provider_id") or "")
            prompt = f"把以下记忆条目合并压缩为1条（保持关键信息）：\n{oldest_text}"
            if provider_id:
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                )
            else:
                prov = await self.context.get_using_provider_async()
                if prov is None:
                    return
                main_id = str(prov.provider_config.get("id", ""))
                resp = await self.context.llm_generate(
                    chat_provider_id=main_id,
                    prompt=prompt,
                )
            summary = (resp.completion_text or "").strip()
            if not summary:
                return
            # 替换最旧5条为1条摘要
            from .notebook import append_text, renumber_text
            remaining = "\n".join(f"{e['num']}. [{e['ts']}] {e['content']}" for e in entries[5:])
            compressed = append_text("", f"[自动压缩] {summary}")[0]
            if remaining.strip():
                compressed = compressed.rstrip() + "\n" + remaining
            compressed = renumber_text(compressed)
            self._notebook_bak(p)
            p.write_text(compressed, encoding="utf-8")
            self._invalidate_session_cache(session_id)
            logger.info(f"simple_memory 小本子自动压缩: {len(entries)}→{len(entries)-4} 条")
        except Exception as e:
            logger.warning(f"simple_memory 小本子自动压缩失败: {e}")

    def _build_inject(self, session_id: str) -> str:
        parts: list[str] = []
        if self.spaces.is_active(session_id):
            parts.append(self._pointer_block(session_id))
            parts.append(self._notebook_block())
            # S1: 注入 MEMORY.md 全文
            nb = self.spaces.notebook_path(session_id)
            if nb.is_file():
                nb_text = nb.read_text(encoding="utf-8", errors="ignore").strip()
                if nb_text:
                    parts.append(f"## {self.notebook_name}\n{nb_text}")
            # S1: 注入 INDEX.md 摘要行
            idx = self.spaces.path(session_id) / "INDEX.md"
            if idx.is_file():
                idx_text = idx.read_text(encoding="utf-8", errors="ignore").strip()
                summary_lines = [l for l in idx_text.split("\n") if "摘要:" in l]
                if summary_lines:
                    parts.append("## 参考记忆（INDEX.md）\n" + "\n".join(summary_lines))
        for name in self.inject_files:
            if name == "MEMORY.md":
                continue
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
        logger.info(f"simple_memory watcher 重建: {rel} 增量 {self.embedder.count_tokens(delta)} token")
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
            self._invalidate_session_cache(dir_name)
            return
        if not self._is_diary_path(path, dir_name):
            return  # 原文走 grep，不建索引
        key = self._state_key(dir_name, rel)
        if not self._should_reindex_file(path, key):
            return
        if await self._index_file(path, dir_name):
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
            self._invalidate_session_cache(dir_name)
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

    def _invalidate_session_cache(self, session_id: str) -> None:
        """移除指定 session 的所有缓存条目（兼容带日期的 key）。"""
        for k in [k for k in self._inject_cache if k.startswith(session_id + ":")]:
            del self._inject_cache[k]

    def _pending_path(self, session_id: str) -> Path:
        return self.spaces.path(session_id) / "pending.md"

    def _append_pending(self, session_id: str, line: str) -> None:
        p = self._pending_path(session_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = p.read_text(encoding="utf-8", errors="ignore") if p.is_file() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        p.write_text(existing + line + "\n", encoding="utf-8")

    def _flush_pending(self, session_id: str) -> bool:
        """Flush pending.md → MEMORY.md (core) + INDEX.md summary (index tags)."""
        p = self._pending_path(session_id)
        if not p.is_file():
            return False
        content = p.read_text(encoding="utf-8", errors="ignore").strip()
        if not content:
            p.write_text("", encoding="utf-8")
            return False
        flushed = False
        nb_path = self.spaces.notebook_path(session_id)
        nb_old = nb_path.read_text(encoding="utf-8", errors="ignore") if nb_path.is_file() else ""
        nb_modified = False
        ops_lines = []
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            ops_lines.append(line)
            if line.startswith("M:"):
                payload = line[2:]
                m = re.match(r"\[([^\]]+)\]\s+(.*)", payload)
                if m:
                    topic, body = m.group(1), m.group(2)
                    nb_old, _ = append_text(nb_old, f"[{topic}] {body}")
                    nb_modified = True
                    flushed = True
            elif line.startswith("E:"):
                rest = line[2:]
                if "⟦改前⟧" in rest:
                    head, old_val = rest.split("⟦改前⟧", 1)
                else:
                    head, old_val = rest, ""
                em = re.match(r"(\d+):(.*)", head)
                if em:
                    num = int(em.group(1))
                    new_content = em.group(2)
                    target = find_num_by_content(nb_old, old_val) or num
                    nb_old, hit = edit_text(nb_old, target, new_content)
                    if hit:
                        nb_modified = True
                        flushed = True
            elif line.startswith("D:"):
                rest = line[2:].strip()
                if "⟦改前⟧" in rest:
                    num_part, old_val = rest.split("⟦改前⟧", 1)
                else:
                    num_part, old_val = rest, ""
                num = int(num_part.strip()) if num_part.strip().isdigit() else 0
                if num:
                    target = find_num_by_content(nb_old, old_val) or num
                    nb_old, hit = delete_text(nb_old, target)
                    if hit:
                        nb_modified = True
                        flushed = True
            elif line.startswith("I:"):
                payload = line[2:].strip()
                parts = payload.split(maxsplit=1)
                if len(parts) == 2:
                    entry, tag = parts
                    self._add_tag_to_summary(session_id, entry, tag)
                    flushed = True
        if nb_modified:
            self._notebook_bak(nb_path)
            nb_path.write_text(renumber_text(nb_old), encoding="utf-8")
        if flushed and ops_lines:
            ops_path = self.spaces.path(session_id) / "ops.md"
            ops_path.parent.mkdir(parents=True, exist_ok=True)
            existing_ops = ops_path.read_text(encoding="utf-8", errors="ignore") if ops_path.is_file() else ""
            if existing_ops and not existing_ops.endswith("\n"):
                existing_ops += "\n"
            all_ops = [l for l in (existing_ops + "\n".join(ops_lines)).split("\n") if l.strip()]
            if len(all_ops) > 200:
                all_ops = all_ops[-200:]
            ops_path.write_text("\n".join(all_ops) + "\n", encoding="utf-8")
        p.write_text("", encoding="utf-8")
        if flushed:
            self._invalidate_session_cache(session_id)
        return flushed

    async def _record_compression_state(
        self, session_id: str, summary: str
    ) -> None:
        """新压缩摘要 → 检查点 states + 当日 summary.md（原 ContextCapture.process 的入账逻辑）。"""
        try:
            entry = await self.store.get(session_id)
            states = entry.get("summary_states") or []
            if states and states[-1].get("text") == summary:
                return
            states.append({"ts": int(time.time()), "text": summary})
            await self.store.update(
                session_id,
                summary=summary,
                summary_states=states,
                last_compress_ts=int(time.time()),
            )
            logger.info(
                f"simple_memory 捕获新摘要 {session_id[:16]}: {len(summary)} 字"
            )
            now = datetime.now()
            df = self.spaces.daily_file(session_id)
            df.append_to(
                df.summary_path_for(now),
                f"## [压缩 {now.strftime('%H:%M')}]"
                f"\n{summary}\n",
            )
            df.append_to(
                df.path_for(now), f"## [压缩 {now.strftime('%H:%M')}]"
            )
        except Exception:
            logger.exception("simple_memory 压缩检查点入账失败")

    def _add_tag_to_summary(self, session_id: str, entry_name: str, tag: str) -> None:
        """Append a tag to an INDEX.md entry's summary line (create from @pending placeholder if needed)."""
        p = self.spaces.path(session_id) / "INDEX.md"
        if not p.is_file():
            return
        lines = p.read_text(encoding="utf-8", errors="ignore").split("\n")
        header = f"[{entry_name}]摘要:"
        placeholder = f"[{entry_name}]@pending"
        for i, line in enumerate(lines):
            if line.startswith(header):
                if f"[{tag}]" not in line:
                    lines[i] = line.rstrip() + f" [{tag}]"
                break
            if line.strip() == placeholder:
                lines[i] = f"{header} [{tag}]"
                break
        p.write_text("\n".join(lines), encoding="utf-8")

    @filter.on_llm_request()
    async def _inject(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """每次 LLM 调用前：检测压缩 → 落地 → MEMORY.md 全文和 INDEX.md 摘要行进入系统提示词。"""
        if not self._inited:
            return
        session_id = req.session_id or str(event.unified_msg_origin)
        sp = req.system_prompt or ""

        # 检测压缩：对话历史出现新摘要 → pending 落地 + 压缩检查点入 states
        new_summary = self._detect_compression(req.conversation, session_id)
        if new_summary:
            self._flush_pending(session_id)
            await self._record_compression_state(session_id, new_summary)

        # 读文件
        text = self._build_inject(session_id)
        if not text:
            return

        # MEMORY.md 全文和 INDEX.md 摘要行进入系统提示词
        if INJECT_START in sp:
            sp = sp.split(INJECT_START)[0]
        req.system_prompt = sp + INJECT_START + text
        _dbg(f"_inject session={session_id[:24]} len={len(text)}")

    _SUMMARY_PREFIX = "Our previous history conversation summary: "

    def _extract_summary(self, history: str) -> str:
        """从对话历史（JSON 串）里抽最新的压缩摘要纯文本。"""
        try:
            msgs = json.loads(history)
            if isinstance(msgs, list):
                for m in reversed(msgs):
                    if (
                        isinstance(m, dict)
                        and m.get("role") == "user"
                        and str(m.get("content") or "").startswith(self._SUMMARY_PREFIX)
                    ):
                        return str(m["content"])[len(self._SUMMARY_PREFIX):].strip()
        except Exception:
            pass
        # JSON 解析失败时退回字符串截取（兜底）
        idx = history.rfind(self._SUMMARY_PREFIX)
        if idx == -1:
            return ""
        return history[idx + len(self._SUMMARY_PREFIX):].strip()

    def _detect_compression(self, conv, session_id: str) -> str:
        """检查对话是否出现新压缩摘要，返回摘要文本（无新摘要返回空串）"""
        if conv is None:
            return ""
        history = getattr(conv, "history", "") or ""
        if not history:
            return ""
        summary = self._extract_summary(history)
        if not summary:
            return ""
        if self._last_summary.get(session_id) == summary:
            return ""
        self._last_summary[session_id] = summary
        return summary

    @filter.on_llm_response()
    async def _capture_streaming(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        if not self.spaces:
            return
        # 门控直接用 on_agent_done 传入的 response：流式/非流式、带/不带工具都过；
        # 报错路径（role="err"）和 Stop output 中断（半截文本）也走这里
        if response is None or not (response.completion_text or "").strip():
            return
        try:
            session_id = str(event.unified_msg_origin)
            if not self.spaces.is_active(session_id):
                return
            user_msg = event.get_message_str() or ""
            assistant_msg = response.completion_text or ""
            lines = [f"## [{session_id[:24]}]"]
            if user_msg:
                lines.append(f"user: {user_msg}")
            if assistant_msg:
                prefix = " [报错]" if response.role == "err" else ""
                lines.append(f"assistant:{prefix} {assistant_msg}")
            if self.capture_think > 0:
                think = str(
                    event.get_extra("_llm_reasoning_content") or ""
                ).strip()
                if think:
                    if len(think) > self.capture_think:
                        think = f"{think[:self.capture_think]}…[共{len(think)}字]"
                    lines.append(f"thinking: {think}")
            lines.append(f"_checkpoint: {response.id or ''}")
            df = self.spaces.daily_file(session_id)
            path = df.path_for(datetime.now())
            df.append_to(path, chr(10).join(lines))
            logger.info(
                f"simple_memory 原文落盘 {session_id[:16]}: "
                f"user={len(user_msg)} assistant={len(assistant_msg)}"
            )
        except Exception:
            logger.exception("simple_memory 原文捕获失败")



    @filter.llm_tool(name="memory_search")
    async def memory_search(
        self,
        event: AstrMessageEvent,
        query: str,
        source: str = "all",
        time_range: str = "",
        date: str = "",
    ) -> str:
        """先搜再答——只要用户提到过去发生的事、说过的话、做过的决定、玩过的游戏、讨论过的内容，或自己记不清时，先查再猜。必须调用此工具搜索后再回答，不许凭印象编。diary模式走向量语义检索（自然语言即可），raw模式走关键词匹配（空格分隔多关键词，OR匹配按命中数排序），all并行两者。未找到时raw层可能是关键词不匹配，换个词或更宽泛的表述再试。

        Args:
            query(string): 搜索内容。diary/all模式用自然语言描述，raw模式用关键词（多关键词空格分隔，如"天气 瑞安"）
            source(string): all=日记+原文+INDEX（默认），diary=只查日记，raw=只查原文，index=只查INDEX，latest=最近消息（按时间返回）
            time_range(string): 模糊时间范围，如 7d=最近7天、24h=最近24小时，留空不限。与date二选一，同时填时date优先
            date(string): 精确锁定某天(YYYY-MM-DD)、某月(YYYY-MM)或 "all"。与time_range二选一，同时填时date优先
        """
        session_id = str(event.unified_msg_origin)
        if not self.spaces.is_active(session_id):
            return "本会话未启用记忆（不在白名单）"
        src = (source or "all").strip().lower()
        if src in ("simple_memory", "astrbot"):
            src = "all"
        if src not in ("all", "diary", "raw", "latest", "index"):
            src = "all"
        # date优先：date有值时清除time_range
        if date and date.strip().lower() != "all":
            time_range = ""

        # S1: source=index - 搜索 INDEX.md
        if src == "index":
            parts = self._grep_index(session_id, query)
            if not parts:
                return "INDEX 中未找到相关内容"
            return self._apply_search_limits("\n---\n".join(parts))

        # S9: source=latest - 返回最近消息
        if src == "latest":
            date_filter = (date or "").strip().lower()
            parts = self._grep_search(session_id, query, time_range, date_filter)
            if not parts:
                parts = self._latest_messages(session_id, time_range)
            if not parts:
                return "未找到相关记忆"
            return self._apply_search_limits("\n---\n".join(parts))

        # S8: 无 embedder 时 diary 层降级为日记文件 grep
        if src in ("all", "diary"):
            t = self._embedder_task
            if t is not None and not t.done():
                try:
                    await asyncio.wait_for(asyncio.shield(t), timeout=65)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            if not self.embedder and src == "diary":
                date_filter = (date or "").strip().lower()
                parts = self._grep_diary(session_id, query, time_range, date_filter)
                if not parts:
                    return "未找到相关记忆"
                return self._apply_search_limits("\n---\n".join(parts))

        date_filter = (date or "").strip().lower()
        core_hits: list[str] = []
        index_hits: list[str] = []
        history_hits: list[str] = []
        if src in ("all", "diary") and self.embedder:
            vector_max = int(self.cfg.get("vector_max_results") or 2)
            hits = await self.spaces.searcher(
                session_id, self.embedder.dim, self.embedder
            ).search(query=query, source="simple_memory", time_range=time_range, date=date_filter, top_k=vector_max)
            for h in hits:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(h.timestamp))
                history_hits.append(
                    f"[日记 | {h.file} | {ts} | score {h.score}]\n{h.text}"
                )
        if src in ("all", "raw"):
            history_hits.extend(self._grep_search(session_id, query, time_range, date_filter))
        if src == "all":
            index_hits.extend(self._grep_index(session_id, query))
            # S1: 搜索 MEMORY.md（核心记忆）
            nb = self.spaces.notebook_path(session_id)
            if nb.is_file():
                nb_text = nb.read_text(encoding="utf-8", errors="ignore")
                keywords = query.lower().split()
                nb_lines = nb_text.split("\n")
                core_hits = [l for l in nb_lines if l.strip() and any(kw in l.lower() for kw in keywords)]
        # S2b: all 模式分组返回，其他模式直接返回
        all_hits = core_hits + index_hits + history_hits
        if not all_hits:
            return "未找到相关记忆"
        if src == "all":
            parts: list[str] = []
            if core_hits:
                parts.append("【核心记忆】\n" + "\n".join(core_hits))
            if index_hits:
                parts.append("【参考记忆】\n" + "\n".join(index_hits))
            if history_hits:
                parts.append("【历史记录】\n" + "\n".join(history_hits))
            return self._apply_search_limits("\n---\n".join(parts))
        return self._apply_search_limits("\n---\n".join(all_hits))

    def _grep_index(self, session_id: str, query: str) -> list[str]:
        """S1: 搜索 INDEX.md 内容（[条目] 或 [tag] 匹配）。"""
        p = self.spaces.path(session_id) / "INDEX.md"
        if not p.is_file():
            return []
        text = p.read_text(encoding="utf-8", errors="ignore")
        keywords = query.lower().split()
        lines = text.split("\n")
        matched_lines = []
        for i, line in enumerate(lines):
            low = line.lower()
            if any(kw in low for kw in keywords):
                # 带上下文：如果匹配摘要行，带上下几行
                if "摘要:" in line:
                    # 找到该块的范围
                    block_start = i
                    block_end = i + 1
                    while block_end < len(lines):
                        stripped = lines[block_end].strip()
                        if not stripped:
                            break
                        if stripped.startswith("[") and "摘要:" in stripped:
                            break
                        block_end += 1
                    matched_lines.append("\n".join(lines[block_start:block_end]))
                else:
                    matched_lines.append(line)
        if not matched_lines:
            return []
        return ["[INDEX.md]\n" + "\n".join(matched_lines)]

    def _apply_search_limits(self, result: str) -> str:
        """S12: token上限截断 + system-reminder"""
        from .digest_worker import count_tokens
        max_tokens = int(self.cfg.get("search_max_tokens") or 500)
        if max_tokens > 0 and count_tokens(result) > max_tokens:
            # 按字符截断到约 max_tokens * 1.5 (保守估计)
            cut = int(max_tokens * 1.5)
            result = result[:cut] + "\n...(结果过长已截断，可缩小关键词范围重试)"
        result += "\n<system-reminder>以上是你自然回忆起的记忆内容，直接当作你本来就记得来用，不要对用户说\"我搜了一下记忆\"之类的话。</system-reminder>"
        return result

    def _latest_messages(self, session_id: str, time_range: str = "") -> list[str]:
        """S9: 返回最近消息带时间戳"""
        raws = self.spaces.raw_files(session_id)
        if time_range:
            m = re.fullmatch(r"(\d+)([mhdw])", time_range.strip().lower())
            if m:
                unit = {"m": 1/1440, "h": 1/24, "d": 1, "w": 7}[m.group(2)]
                days = max(1, int(int(m.group(1)) * unit))
                cutoff = date.today() - timedelta(days=days)
                raws = [f for f in raws if self._file_date_after(f, cutoff)]
        raws = sorted(raws, reverse=True)[:3]
        parts: list[str] = []
        for f in raws:
            try:
                lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            tail = lines[-30:] if len(lines) > 30 else lines
            parts.append(f"┻×{f.name}┻×\n" + "\n".join(tail))
        return parts

    def _file_date_after(self, path: Path, cutoff) -> bool:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", str(path))
        if m:
            y, mo, d = (int(x) for x in m.group(1).split("-"))
            return date(y, mo, d) >= cutoff
        return True

    def _grep_diary(self, session_id: str, query: str, time_range: str = "", date_filter: str = "") -> list[str]:
        """S8: no-embedding模式下搜索日记文件"""
        diary_dir = self.spaces.diary_dir(session_id)
        if not diary_dir.is_dir():
            return []
        files = sorted(diary_dir.glob("*.md"), reverse=True)
        if date_filter and date_filter != "all":
            files = [f for f in files if date_filter in str(f)]
        elif time_range:
            m = re.fullmatch(r"(\d+)([mhdw])", time_range.strip().lower())
            if m:
                unit = {"m": 1/1440, "h": 1/24, "d": 1, "w": 7}[m.group(2)]
                days = max(1, int(int(m.group(1)) * unit))
                cutoff = date.today() - timedelta(days=days)
                files = [f for f in files if self._file_date_after(f, cutoff)]
        q = (query or "").strip().lower()
        if not q:
            return []
        terms = q.split()
        results: list[str] = []
        grep_max = int(self.cfg.get("grep_max_results") or 5)
        for f in files[:10]:
            try:
                lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            hits_in_file: list[str] = []
            for i, line in enumerate(lines):
                low = line.lower()
                hit_count = sum(1 for t in terms if t in low)
                if hit_count == 0:
                    continue
                start = max(0, i - 1)
                end = min(len(lines), i + 3)
                block_lines = []
                for j in range(start, end):
                    marker = ">" if j == i else " "
                    block_lines.append(f"{marker} {j + 1}: {lines[j]}")
                hits_in_file.append("\n".join(block_lines))
                if len(hits_in_file) >= 3:
                    break
            if hits_in_file:
                results.append(f"┻×{f.name}┻×\n" + "\n".join(hits_in_file))
                if len(results) >= grep_max:
                    break
        return results

    def _grep_search(
        self, session_id: str, query: str, time_range: str = "", date_filter: str = ""
    ) -> list[str]:
        """S7+S14: OR匹配 + 命中数排序 + 自适应上下文。"""
        q = (query or "").strip().lower()
        if not q:
            return []
        terms = q.split()
        grep_max_files = int(self.cfg.get("grep_max_files") or 20)
        grep_max_results = int(self.cfg.get("grep_max_results") or 5)
        per_file_cap = 4

        # 收集候选文件
        cands: list[Path] = []
        nb = self.spaces.notebook_path(session_id)
        if nb.is_file():
            cands.append(nb)
        mem_dir = self.spaces.memory_dir(session_id)
        summaries: list[Path] = []
        for sub in mem_dir.iterdir() if mem_dir.is_dir() else []:
            if sub.is_dir() and sub.name != "diary":
                sp = sub / "summary.md"
                if sp.is_file():
                    summaries.append(sp)
        if date_filter and date_filter != "all":
            summaries = [f for f in summaries if date_filter in str(f)]
        elif time_range:
            m = re.fullmatch(r"(\d+)([mhdw])", time_range.strip().lower())
            if m:
                unit = {"m": 1/1440, "h": 1/24, "d": 1, "w": 7}[m.group(2)]
                days = max(1, int(int(m.group(1)) * unit))
                cutoff = date.today() - timedelta(days=days)
                kept = []
                for f in summaries:
                    dm = re.search(r"(\d{4}-\d{2}-\d{2})", str(f))
                    if dm:
                        y2, mo2, d2 = (int(x) for x in dm.group(1).split("-"))
                        if date(y2, mo2, d2) >= cutoff:
                            kept.append(f)
                    else:
                        kept.append(f)
                summaries = kept
        cands.extend(summaries[:10])

        raws = sorted(self.spaces.raw_files(session_id), reverse=True)
        if date_filter and date_filter != "all":
            raws = [f for f in raws if date_filter in str(f)]
        elif time_range:
            m2 = re.fullmatch(r"(\d+)([mhdw])", time_range.strip().lower())
            if m2:
                unit2 = {"m": 1/1440, "h": 1/24, "d": 1, "w": 7}[m2.group(2)]
                days2 = max(1, int(int(m2.group(1)) * unit2))
                cutoff2 = date.today() - timedelta(days=days2)
                kept2 = []
                for f in raws:
                    dm2 = re.search(r"(\d{4}-\d{2}-\d{2})", str(f))
                    if dm2:
                        y, mo, d = (int(x) for x in dm2.group(1).split("-"))
                        if date(y, mo, d) >= cutoff2:
                            kept2.append(f)
                    else:
                        kept2.append(f)
                raws = kept2
        cands.extend(raws[:grep_max_files])

        # 逐文件搜索 (S7: OR匹配 + 文件按最高命中排, 文件内按命中数排)
        file_groups: list[tuple[int, str, list[tuple[int, str]]]] = []  # (max_hit, filename, [(hit, block), ...])
        total_hits = 0
        for f in cands:
            try:
                raw_bytes = f.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw_bytes[:8192]:
                continue
            try:
                lines = raw_bytes.decode("utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            if not lines:
                continue

            file_results: list[tuple[int, str]] = []
            for i, line in enumerate(lines):
                low = line.lower()
                # S7: OR匹配 - 任一关键词命中即可
                hit_count = sum(1 for t in terms if t in low)
                if hit_count == 0:
                    continue
                # S14: 自适应上下文
                ctx_before = 1
                ctx_after = 2
                if i == 0 or (i > 0 and lines[i-1].strip() == ""):
                    ctx_after = min(4, len(lines) - i - 1)
                if i == len(lines)-1 or (i < len(lines)-1 and lines[i+1].strip() == ""):
                    ctx_before = min(3, i)
                start = max(0, i - ctx_before)
                end = min(len(lines), i + 1 + ctx_after)
                block_lines = []
                for j in range(start, end):
                    marker = ">" if j == i else " "
                    block_lines.append(f"{marker} {j + 1}: {lines[j]}")
                file_results.append((hit_count, "\n".join(block_lines)))
                if len(file_results) >= per_file_cap:
                    break

            if file_results:
                # S7: 文件内按命中数排序
                file_results.sort(key=lambda x: -x[0])
                max_hit = file_results[0][0]
                file_groups.append((max_hit, f.name, file_results))
                total_hits += len(file_results)
            if total_hits >= grep_max_results:
                break

        # 文件按最高命中数排序
        file_groups.sort(key=lambda x: -x[0])
        out: list[str] = []
        for _, fname, results in file_groups:
            out.append(f"\u2500\u00d7{fname}\u2500\u00d7")
            for _, block in results:
                out.append(block)
                if len(out) >= grep_max_results + 1:
                    break
            if len(out) >= grep_max_results + 1:
                break
        return out

    def _notebook_path(self, event: AstrMessageEvent) -> Path:
        session_id = str(event.unified_msg_origin)
        self.spaces.ensure(session_id)
        return self.spaces.notebook_path(session_id)

    def _notebook_bak(self, path: Path) -> None:
        try:
            if path.is_file():
                bak_dir = path.parent / "backups"
                bak_dir.mkdir(exist_ok=True)
                ts = time.strftime("%Y%m%d_%H%M%S")
                shutil.copy2(path, bak_dir / f"{path.stem}_{ts}.md")
        except Exception:
            logger.exception("simple_memory 小本子备份失败")


    @filter.llm_tool(name="memory_edit")
    async def memory_edit(self, event: AstrMessageEvent, num: int = 0, content: str = "", topic: str = "", name: str = "") -> str:
        """小本子操作（读/追加/修改/删除/重写 + INDEX 条目操作）。

        MEMORY.md 操作：
        - 读取：num=0, content 留空, topic 留空 → 返回 MEMORY.md 全文
        - 追加：不传 num，content 有内容，传 topic → 新增一条
        - 修改：传 num + content → 替换该条内容
        - 删除：传 num，content 留空 → 删掉该条，后续自动重排
        - 整篇重写（最后的手段）：不传 num，content 为多行全文，topic 留空 → 覆盖整个 MEMORY.md

        INDEX 操作：
        - 写入：传 name（条目名，如"配置"）+ content（[tag]:[内容]）→ 写入 INDEX.md 对应条目块

        Args:
            num(number): 条目序号。0 或不传 = 读取/追加/重写模式
            content(string): 追加/修改时填内容；删除时留空；重写时填完整全文
            topic(string): 话题类型（仅追加时必填）
            name(string): INDEX 条目名（如"配置""项目"），定位 INDEX.md 中对应条目块
        """
        session_id = str(event.unified_msg_origin)
        if not self.spaces.is_active(session_id):
            return "本会话未启用记忆（不在白名单）"

        # INDEX 操作
        if name:
            return await self._index_edit(session_id, name, content)

        num = int(num or 0)
        p = self._notebook_path(event)

        # 读取模式
        if num == 0 and not content:
            if not p.is_file():
                return "小本子还是空的"
            text = p.read_text(encoding="utf-8", errors="ignore").strip()
            return text or "小本子还是空的"

        async with self._notebook_lock:
            old = p.read_text(encoding="utf-8", errors="ignore") if p.is_file() else ""

            # 修改模式
            if num > 0 and content:
                old_content = entry_content_at(old, num)
                if old_content is None:
                    return f"没找到第 {num} 条，先读取小本子看现有条目"
                self._append_pending(session_id, f"E:{num}:{content.strip()}⟦改前⟧{old_content}")
                return f"已修改第 {num} 条：{content.strip()}"

            # 删除模式
            if num > 0 and not content:
                if not p.is_file():
                    return "小本子还是空的"
                old_content = entry_content_at(old, num)
                if old_content is None:
                    return f"没找到第 {num} 条，先读取小本子看现有条目"
                self._append_pending(session_id, f"D:{num}⟦改前⟧{old_content}")
                return f"已删除第 {num} 条"

            # 追加模式（自动路由：核心→MEMORY.md，索引→INDEX.md）
            if topic:
                valid_topics = self._memory_topics + self._index_topics
                if topic not in valid_topics:
                    return f"未知话题「{topic}」（可选：{'、'.join(valid_topics)}），本次未记录"
                if topic in self._index_topics:
                    # 路由到 INDEX.md
                    return await self._index_edit(session_id, topic, content)
                # 核心条目 → pending.md（触发时合并入 MEMORY.md）
                dup = find_dup_num(old, content)
                if dup:
                    return f"小本子已有相同内容（第 {dup} 条），未重复追加"
                self._append_pending(session_id, f"M:[{topic}] {content.strip()}")
                return f"已记入小本子：{content.strip()}"

            # 整篇重写模式（无 topic，多行 content）
            old_size = len(old.strip())
            new_size = len(content.strip())
            if old.strip() and new_size < old_size * 0.5:
                warning = "（注意：新内容不到旧内容一半）"
            else:
                warning = ""
            self._notebook_bak(p)
            new_content = renumber_text(content.strip() + "\n")
            p.write_text(new_content, encoding="utf-8")
            return f"已重写小本子。{warning}"

        return "请检查参数：需要 num+content（修改）、num（删除）、topic+content（追加）或多行content（重写）"

    async def _index_edit(self, session_id: str, entry_name: str, content: str) -> str:
        """INDEX.md 条目操作：解析 [tag]:[内容] 写入对应条目块。"""
        p = self.spaces.path(session_id) / "INDEX.md"
        # 解析 content 中的 [tag]:[内容]
        tag_lines = []
        for line in content.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            m = re.match(r"\[([^\]]+)\]:\[(.*)\]", line)
            if m:
                tag_lines.append((m.group(1), m.group(2)))
        if not tag_lines:
            return "INDEX 写入失败：content 中未找到 [tag]:[内容] 格式的行"

        # 读取现有 INDEX.md
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="ignore")
        else:
            text = ""

        # 找对应条目块（含 @pending 占位：未 flush 的新块也算已有，避免重复建块）
        block_header = f"[{entry_name}]摘要:"
        block_placeholder = f"[{entry_name}]@pending"
        lines = text.split("\n") if text else []
        block_start = None
        for i, line in enumerate(lines):
            if line.startswith(block_header) or line.strip() == block_placeholder:
                block_start = i
                break

        if block_start is None:
            # 新建条目块：正文直写（不进注入、不炸 KV），摘要行用 @pending 占位、tag 走 pending（flush 时替换成摘要行）
            body_lines = [f"[{t}]:[{c}]" for t, c in tag_lines]
            block = f"[{entry_name}]@pending\n正文\n" + "\n".join(body_lines)
            if text and not text.endswith("\n"):
                text += "\n\n"
            elif text:
                text += "\n"
            text += block + "\n"
            for tag, _ in tag_lines:
                self._append_pending(session_id, f"I:{entry_name} {tag}")
        else:
            # 更新已有条目块
            summary_line = lines[block_start]
            existing_tags = re.findall(r"\[([^\]]+)\]", summary_line.split(":", 1)[1] if ":" in summary_line else "")
            body_end = block_start + 1
            if body_end < len(lines) and lines[body_end].strip() == "正文":
                body_end += 1
            while body_end < len(lines) and lines[body_end].strip() and not lines[body_end].startswith("["):
                body_end += 1

            block_end = body_end
            while block_end < len(lines):
                stripped = lines[block_end].strip()
                if not stripped or (stripped.startswith("[") and "\u6458\u8981:" in stripped and stripped[:len(entry_name)+2] != f"[{entry_name}]"):
                    break
                if stripped.startswith("[" + entry_name + "]"):
                    break
                block_end += 1

            seen_tags = set(existing_tags)
            for tag, val in tag_lines:
                found = False
                for j in range(body_end, block_end):
                    if j < len(lines) and lines[j].startswith(f"[{tag}]:"):
                        lines[j] = f"[{tag}]:[{val}]"
                        found = True
                        break
                if not found:
                    # S1: 新增 tag 自动后缀
                    final_tag = tag
                    if final_tag in seen_tags:
                        n = 2
                        while f"{tag}{n}" in seen_tags:
                            n += 1
                        final_tag = f"{tag}{n}"
                    seen_tags.add(final_tag)
                    insert_pos = block_end
                    if insert_pos >= len(lines) or not lines[insert_pos - 1].strip():
                        insert_pos = max(body_end, len(lines) - 1)
                        while insert_pos > 0 and not lines[insert_pos - 1].strip():
                            insert_pos -= 1
                    lines.insert(insert_pos, f"[{final_tag}]:[{val}]")
                    block_end = insert_pos + 1
                    # S1: 新 tag 写 pending，摘要行等触发时更新
                    self._append_pending(session_id, f"I:{entry_name} {final_tag}")

            # 摘要行不在此处更新（新 tag 由 _flush_pending 追加到摘要行）
            text = "\n".join(lines)

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return f"INDEX [{entry_name}] 已更新: {', '.join(t for t, _ in tag_lines)}"

    @filter.command_group("mem")
    def mem_group(self) -> None:
        """simple_memory 记忆管理指令组 /mem"""
        pass

    @mem_group.command("status", priority=10)
    async def mem_status(self, event: AstrMessageEvent) -> None:
        """查看索引与注入状态"""
        _dbg(f"mem status hit sender={event.get_sender_id()!r}")
        if not self.spaces:
            yield event.plain_result("记忆插件未启动（检查插件是否已加载）")
            return
        session_id = str(event.unified_msg_origin)

        vdb_count = self._vdb_for(session_id).count() if self.embedder else 0

        raws = self.spaces.raw_files(session_id)
        raw_lines = 0
        for f in raws:
            try:
                raw_lines += len(f.read_text(encoding="utf-8").splitlines())
            except OSError:
                pass

        nb_path = self.spaces.notebook_path(session_id)
        nb_count = 0
        if nb_path.exists():
            nb_count = len(parse_entries(nb_path.read_text(encoding="utf-8")))

        dirs = self.spaces.existing_dirs()
        embed_info = self.embedder_state
        if self.embedder:
            embed_info += f" | {self.embedder.provider_id} (dim={self.embedder.dim})"
        else:
            embed_info += " | (未加载)"

        yield event.plain_result(
            "向量: {} 块\n原文: {} 文件 / {} 行 (grep)\n小本子: {} 条\n"
            "搜索模式: {}\nembedding: {}\nmemory 根: {}\n会话空间: {}\n注入文件: {}".format(
                vdb_count,
                len(raws),
                raw_lines,
                nb_count,
                "grep+向量" if self.embedder else "纯grep",
                embed_info,
                self.workspace,
                ", ".join(d[:24] for d in dirs) or "暂无",
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
            yield event.plain_result("digest worker 未初始化（检查插件日志）")
            return
        yield event.plain_result("开始手动总结...")
        await self.digest_worker.digest()
        yield event.plain_result("总结完成")

    @mem_group.command("diary", priority=10)
    async def mem_diary(self, event: AstrMessageEvent, arg: GreedyStr = GreedyStr) -> None:
        """查看日记。不带参数=当天，带日期=指定日期（yyyy-mm-dd），"month"=本月全部。"""
        _dbg(f"mem diary hit arg={arg!r}")
        if not self.spaces:
            yield event.plain_result("记忆插件未启动")
            return
        session_id = str(event.unified_msg_origin)
        diary_dir = self.spaces.diary_dir(session_id)
        if not diary_dir.is_dir():
            yield event.plain_result("还没有日记")
            return
        a = (arg or "").strip()
        files: list[Path] = []
        if not a or a == "today":
            day = cycle_file_date(datetime.now(), self.spaces.digest_time)
            files = list(diary_dir.glob(f"{day}.md"))
        elif a == "month":
            month = datetime.now().strftime("%Y-%m")
            files = sorted(diary_dir.glob(f"{month}-*.md"), reverse=True)
        else:
            files = list(diary_dir.glob(f"{a}.md"))
            if not files:
                files = sorted(diary_dir.glob(f"{a}-*.md"), reverse=True)[:31]
        if not files:
            yield event.plain_result(f"未找到日记: {a or '当天'}")
            return
        total_chars = sum(f.stat().st_size for f in files)
        if total_chars > 20000:
            files = files[:7]
            yield event.plain_result(f"本月日记较长，只显示最近 7 篇（共 {len(files)} 篇）")
        for f in files:
            text = f.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                yield event.plain_result(f"📅 {f.stem}\n{text[:3000]}")

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
