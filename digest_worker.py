import asyncio
import json
import re
import time
from datetime import datetime, timedelta

from astrbot.api import logger

from .daily_hook import (
    FPS_PREFIX,
    THINK_CAP,
    TOOL_RESULT_CAP,
    normalize,
    render_msg,
)
from .daily_md import DEFAULT_DIGEST_TIME, cycle_file_date, parse_digest_time

TAIL_RAW_CAP = 4000
CATCHUP_MAX_HOURS = 12


def _dbg(msg: str) -> None:
    try:
        from astrbot.api.star import StarTools
        with open(
            StarTools.get_data_dir("astrbot_plugin_openclaw_memory") / "debug.log", "a", encoding="utf-8"
        ) as f:
            f.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [worker] {msg}"
                + chr(10)
            )
    except Exception:
        pass

DIARY_RULES = """日记死规矩：
- 只记决定/结论/踩的坑/偏好，不抄代码
- 代码改动只记文件名 + 改了什么
- 第一人称、口语化日记体，不要正式报告腔
- 300 到 800 字，别注水"""

TAIL_SUMMARY_RULES = """总结以下对话原文，用作当日日记的补充输入。
- 保留决定/结论/踩的坑/偏好，不抄代码
- 300 字以内"""


def count_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + other // 4 + 1


def seconds_until_next(digest_time: str, now: datetime) -> float:
    h, m = parse_digest_time(digest_time)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def most_recent_past_target(digest_time: str, now: datetime) -> datetime:
    h, m = parse_digest_time(digest_time)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target >= now:
        target -= timedelta(days=1)
    return target


def session_to_db_user_id(session_id: str) -> str:
    """统一会话 ID（全冒号）→ conversations.user_id 格式（平台:类型!umo…）。"""
    parts = session_id.split(":")
    if len(parts) < 3:
        return session_id
    return f"{parts[0]}:{parts[1]}:{'!'.join(parts[2:])}"


class DigestWorker:
    """每日总结 worker：补尾 + 日记（摘要用完即焚）。

    每天 digest_time 对每个会话：
    1. 补尾——DB 会话里超出快照条数的消息（钩子一轮延迟没落盘的最后一段）
       落当日 md，快照推进到 DB 全量，防止下一轮钩子重灌；
    2. 日记——有存储摘要才写：（摘要 + 尾巴/补尾摘要）→ 人设模型出日记
       追加同文件；失败/空返回则摘要原文直接充当当天记录；
    3. 水位——记录窗口（>36h 告警疑似补跑），只清已消费摘要，
       长会话快照与水位跨天保留。
    """

    def __init__(
        self,
        *,
        store,
        daily_file_for,
        diary_file_for,
        context,
        lock: asyncio.Lock,
        digest_time: str = DEFAULT_DIGEST_TIME,
        diary_provider_id: str = "",
        diary_persona_id: str = "",
        tail_summary_threshold: int = 2000,
        raw_ttl_days: int = 0,
        session_whitelist=None,
        think_cap: int = THINK_CAP,
        tool_cap: int = TOOL_RESULT_CAP,
        state_budget: int = 24000,
    ):
        self.store = store
        self.daily_file_for = daily_file_for
        self.diary_file_for = diary_file_for
        self.context = context
        self.lock = lock
        self.digest_time = digest_time
        self.diary_provider_id = diary_provider_id
        self.diary_persona_id = diary_persona_id
        self.tail_summary_threshold = tail_summary_threshold
        self.raw_ttl_days = raw_ttl_days
        self.think_cap = think_cap
        self.tool_cap = tool_cap
        self.state_budget = state_budget
        self.session_whitelist: list[str] = []
        for item in session_whitelist or []:
            s = str(item or "").strip().replace("：", ":")
            if not s:
                continue
            if s not in self.session_whitelist:
                self.session_whitelist.append(s)
        self._task: asyncio.Task | None = None
        self._persona_card: str | None = None

    def start(self) -> None:
        _dbg(f"start() called digest_time={self.digest_time}")
        if self._task is None:
            self._task = asyncio.create_task(
                self._loop(), name="openclaw_memory_digest"
            )
            logger.info(
                f"openclaw_memory digest worker 启动（每日 {self.digest_time}）"
            )

    async def stop(self) -> None:
        tasks = [self._task] if self._task is not None else []
        self._task = None
        if not tasks:
            return
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except BaseException:
                pass
        logger.info("openclaw_memory digest worker 停止")

    async def _loop(self) -> None:
        _dbg("_loop 任务开始运行")
        first = True
        while True:
            if first:
                first = False
                now = datetime.now()
                target = most_recent_past_target(self.digest_time, now)
                gap_h = (now - target).total_seconds() / 3600
                _dbg(
                    f"首迭代 target={target:%m-%d %H:%M} gap={gap_h:.2f}h"
                )
                if 0 < gap_h <= CATCHUP_MAX_HOURS:
                    _dbg("进入补跑闸门")
                    try:
                        sessions = await self.store.keys()
                        if self.session_whitelist:
                            sessions = [
                                s
                                for s in sessions
                                if any(f in s for f in self.session_whitelist)
                            ]
                        wms = [
                            int((await self.store.get(s)).get("watermark_ts") or 0)
                            for s in sessions
                        ]
                        max_wm = max(wms, default=0)
                        _dbg(f"闸门判定 max_wm={max_wm} target_ts={int(target.timestamp())}")
                        if max_wm < int(target.timestamp()):
                            logger.info(
                                f"openclaw_memory 启动补跑：{target:%m-%d %H:%M} 目标已过 "
                                f"{gap_h:.1f}h，立即结算 {target.date().isoformat()}.md"
                            )
                            _dbg("启动补跑触发")
                            try:
                                await self.digest(now=target)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                logger.exception("openclaw_memory 启动补跑失败")
                                _dbg("启动补跑失败")
                        else:
                            _dbg("闸门未过，无需补跑")
                    except Exception as e:
                        _dbg(f"首迭代闸门异常: {e!r}")
                        logger.exception("openclaw_memory 首迭代补跑检查异常，继续运行")
            delay = seconds_until_next(self.digest_time, datetime.now())
            await asyncio.sleep(delay)
            try:
                await self.digest()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("openclaw_memory digest 运行失败")

    async def digest(self, now: datetime | None = None) -> None:
        """执行一次总结。now 默认当前时刻（测试可注入）。

        文件口径：digest 时刻落在当天（now.date()）的文件上——
        该文件收集的是 (前一日 digest → 本次 digest) 窗口，
        与钩子 cycle_file_date 的内容分桶一致。
        每个会话写入自己空间内的文件：日记进 memory/diary/（向量库），
        补尾原文进 memory/ 当日原文（grep）。
        """
        now = now or datetime.now()
        day = now.date().isoformat()
        sessions = await self.store.keys()
        if self.session_whitelist:
            sessions = [
                s for s in sessions if any(f in s for f in self.session_whitelist)
            ]
        logger.info(
            f"openclaw_memory digest 开始：{len(sessions)} 个会话，目标文件 {day}.md"
        )
        _dbg(f"digest 开始 {len(sessions)} 会话 day={day}")

        for sid in sessions:
            raw_target = self.daily_file_for(sid).path_for_date(day)
            target = self.diary_file_for(sid).path_for_date(day)
            try:
                await self._digest_session(sid, now, raw_target, target)
            except Exception:
                logger.exception(f"openclaw_memory digest 处理 {sid[:16]} 失败")
        try:
            await self._expire_raw(now)
        except Exception:
            logger.exception("openclaw_memory 原文过期清理失败")
        logger.info("openclaw_memory digest 完成")

    async def _digest_session(
        self, sid: str, now: datetime, raw_target, target
    ) -> None:
        entry = await self.store.get(sid)
        summary = (entry.get("summary") or "").strip()
        wm = int(entry.get("watermark_ts") or 0)
        if wm:
            window_h = (now.timestamp() - wm) / 3600
            if window_h > 36:
                logger.warning(
                    f"openclaw_memory {sid[:16]} digest 窗口 {window_h:.1f}h"
                    "（>36h，疑似停机后补跑）"
                )

        tail, full = await self._fetch_tail(sid, entry)
        tail_lines = [
            s
            for s in (render_msg(m, tool_cap=self.tool_cap) for m in tail)
            if s
        ]
        if full:
            await self.store.update(
                sid,
                snapshot={"count": len(full), "last_fp": full[-1]["fp"]},
            )
        _dbg(f"补尾 {sid[:24]} tail={len(tail_lines)} 条 full={bool(full)}")
        if tail_lines:
            block = f"## [tail] {sid[:24]}\n"
            block += "\n".join(tail_lines) + "\n"
            block += FPS_PREFIX + " ".join(m["fp"] for m in tail) + " -->\n"
            async with self.lock:
                self.daily_file_for(sid).append_to(raw_target, block)
            logger.info(f"openclaw_memory {sid[:16]} 补尾 {len(tail)} 条")

        states = entry.get("summary_states") or []
        if not states and summary:
            states = [
                {
                    "ts": int(entry.get("last_compress_ts") or 0),
                    "text": summary,
                }
            ]

        if states:
            tail_text = "\n".join(tail_lines)
            input_parts = [
                "[摘要检查点（滚动摘要在不同时刻的快照，按时间顺序）]\n"
                + self._render_states(states)
            ]
            if tail_text:
                if count_tokens(tail_text) > self.tail_summary_threshold:
                    tail_sum = await self._llm(
                        TAIL_SUMMARY_RULES,
                        tail_text[: self.tail_summary_threshold * 4],
                    )
                    if tail_sum:
                        input_parts.append(f"[补尾摘要]\n{tail_sum}")
                    else:
                        input_parts.append(
                            f"[今日尾部原文]\n{tail_text[:TAIL_RAW_CAP]}"
                        )
                else:
                    input_parts.append(f"[今日尾部原文]\n{tail_text[:TAIL_RAW_CAP]}")
            _t0 = time.time()
            _dbg(
                f"llm 开始 {sid[:24]} states={len(states)} "
                f"input={len(chr(10).join(input_parts))} 字"
            )
            diary = await self._llm(
                await self._diary_system(now), "\n\n".join(input_parts)
            )
            _dl = len(diary or "")
            _dbg(f"llm 完成 {sid[:24]} {_dl} 字 {time.time() - _t0:.0f}s")

            if diary:
                body = diary
                logger.info(
                    f"openclaw_memory {sid[:16]} 日记生成 {len(diary)} 字"
                )
            else:
                body = (
                    "[日记生成失败/空返回，以最新摘要充当当天记录]\n"
                    + states[-1]["text"]
                )
                logger.warning(f"openclaw_memory {sid[:16]} 日记为空，摘要直接落盘")
            async with self.lock:
                self.diary_file_for(sid).append_to(
                    target, f"## [diary] {sid[:24]}\n{body}\n"
                )
            await self.store.update(
                sid, summary="", summary_states=[], summary_consumed=True
            )

        _dbg(f"store 更新 {sid[:24]} wm={int(now.timestamp())}")
        await self.store.update(sid, watermark_ts=int(now.timestamp()))

    # ---------- 数据获取 ----------

    async def _db_full_normalized(self, sid: str) -> list[dict]:
        try:
            db = self.context.get_db()
            rows = await db.get_conversations(
                user_id=session_to_db_user_id(sid)
            )
            if not rows:
                return []
            row = max(rows, key=lambda r: r.updated_at)
            full = await db.get_conversation_by_id(row.conversation_id)
            if full is None:
                return []
            raw = full.content or "[]"
            if isinstance(raw, str):
                raw = json.loads(raw)
            return normalize(raw, think_cap=self.think_cap)
        except Exception as e:
            logger.warning(f"openclaw_memory 读取会话失败 {sid[:16]}: {e}")
            return []

    async def _fetch_tail(
        self, sid: str, entry: dict
    ) -> tuple[list[dict], list[dict]]:
        """返回 (尾部消息, DB 全量规范化消息)。"""
        snap = entry.get("snapshot") or {}
        n = int(snap.get("count") or 0)
        if not n or self.context is None:
            return [], []
        full = await self._db_full_normalized(sid)
        if len(full) <= n:
            return [], []
        return full[n:], full

    def _render_states(self, states: list[dict]) -> str:
        """检查点拼文：超 state_budget 时保首条 + 自最新往前取。"""
        total = sum(count_tokens(s.get("text") or "") for s in states)
        if total > self.state_budget:
            head = states[0]
            picked: list[dict] = []
            used = count_tokens(head.get("text") or "")
            for s in reversed(states[1:]):
                c = count_tokens(s.get("text") or "")
                if used + c > self.state_budget:
                    break
                picked.append(s)
                used += c
            states = [head] + picked[::-1]
        lines = []
        for s in states:
            ts = int(s.get("ts") or 0)
            tag = (
                datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "???"
            )
            lines.append(f"[{tag}] {s.get('text') or ''}")
        return "\n\n".join(lines)

    # ---------- LLM ----------

    async def _llm(self, system_prompt: str, prompt: str) -> str:
        if self.context is None:
            return ""
        provider_id = self.diary_provider_id
        if not provider_id:
            try:
                prov = self.context.get_using_provider()
                provider_id = str(
                    (getattr(prov, "provider_config", None) or {}).get("id", "")
                )
            except Exception:
                provider_id = ""
        if not provider_id:
            logger.warning(
                "openclaw_memory 日记 LLM 提供商未解析"
                "（配置 diary_provider_id 或 AstrBot 主 LLM）"
            )
            return ""
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=system_prompt,
                prompt=prompt,
            )
        except Exception as e:
            logger.warning(f"openclaw_memory 日记 LLM 调用失败: {e}")
            return ""
        return (getattr(resp, "completion_text", "") or "").strip()

    async def _diary_system(self, now: datetime) -> str:
        parts = []
        card = await self._persona_card_now()
        if card:
            parts.append(card)
        parts.append(
            f"今天是 {now.strftime('%Y-%m-%d')}。"
            "下面是当天会话的摘要检查点（不同时刻的滚动摘要快照）与尾部内容，"
            "请以角色风格写成一篇日记：\n"
            + DIARY_RULES
        )
        return "\n\n".join(parts)

    async def _persona_card_now(self) -> str:
        if self._persona_card is not None:
            return self._persona_card
        card = ""
        if self.diary_persona_id and self.context is not None:
            try:
                p = await self.context.get_db().get_persona_by_id(
                    self.diary_persona_id
                )
                if p is not None:
                    card = (p.system_prompt or "").strip()
            except Exception as e:
                logger.warning(f"openclaw_memory 人设卡获取失败: {e}")
        self._persona_card = card
        return card

    # ---------- 原文过期 ----------

    async def _expire_raw(self, now: datetime) -> None:
        if not self.raw_ttl_days:
            return
        cutoff = (now - timedelta(days=self.raw_ttl_days)).date().isoformat()
        for sid in await self.store.keys():
            d = self.daily_file_for(sid).memory_dir
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.md", f.name)
                if not m or m.group(1) >= cutoff:
                    continue
                text = f.read_text(encoding="utf-8", errors="ignore")
                lines = text.splitlines(keepends=True)
                i = next(
                    (
                        k
                        for k, l in enumerate(lines)
                        if l.startswith("## [") and "[diary]" in l
                    ),
                    None,
                )
                if i is None:
                    f.unlink()
                    logger.info(
                        f"openclaw_memory 原文过期无日记，删除 {sid[:16]}/{f.name}"
                    )
                else:
                    f.write_text("".join(lines[i:]), encoding="utf-8")
                    logger.info(
                        f"openclaw_memory 原文过期留日记 {sid[:16]}/{f.name}（删原文 {i} 行）"
                    )
