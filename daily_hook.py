import asyncio
import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

SUMMARY_PREFIX = "Our previous history conversation summary: "
ACK_TEXT = "Acknowledged the summary of our previous conversation history."
TOOL_RESULT_CAP = 2000
RAW_CONTENT_CAP = 20000
THINK_CAP = 300
FPS_PREFIX = "<" + "!-- fps: "


def _clean_think_tags(text: str) -> str:
    """清洗模型抽风写进正文的 think 标签：完整配对连内容删，孤立标签只剥壳。"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"</(?:parameter|function|invoke|antml:function_calls)>", "", text)
    return text.replace("</think>", "").replace("<think>", "")


def _norm_content(content: Any, think_cap: int = THINK_CAP) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return _clean_think_tags(content)
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(_clean_think_tags(p))
                continue
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t == "text" and p.get("text"):
                parts.append(_clean_think_tags(str(p["text"])))
            elif t == "think" and p.get("think"):
                if think_cap <= 0:
                    continue
                th = str(p["think"])
                if len(th) > think_cap:
                    parts.append(f"〔think〕{th[:think_cap]}…[共{len(th)}字]")
                else:
                    parts.append(f"〔think〕{th}")
            elif t in ("image", "image_url"):
                parts.append("[image]")
        return "\n".join(parts)
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _fp(role: str, content: str, tool_calls: Any) -> str:
    raw = (
        f"{role}\u0001{content}\u0001"
        f"{json.dumps(tool_calls or [], ensure_ascii=False, sort_keys=True)}"
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


_FP_COMMENT_RE = __import__("re").compile(__import__("re").escape(FPS_PREFIX) + r"([0-9a-f ]+) -->")


def _load_seen_fps(path: Path) -> set:
    """读当日文件里已落盘消息的 fp 集合（块尾 fps 注释）。"""
    if not path.is_file():
        return set()
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return set()
    seen: set = set()
    for m in _FP_COMMENT_RE.finditer(text):
        seen.update(m.group(1).split())
    return seen


def normalize(
    contexts: list[dict], think_cap: int = THINK_CAP
) -> list[dict]:
    out = []
    for m in contexts or []:
        role = str(m.get("role") or "")
        content = _norm_content(m.get("content"), think_cap)
        tool_calls = m.get("tool_calls")
        out.append(
            {
                "role": role,
                "content": content,
                "name": m.get("name"),
                "tool_calls": tool_calls,
                "fp": _fp(role, content, tool_calls),
            }
        )
    return out


def _indent(text: str) -> str:
    lines = text.split("\n")
    return lines[0] + "".join("\n  " + l for l in lines[1:])


def render_msg(m: dict, tool_cap: int = TOOL_RESULT_CAP) -> str:
    role = m["role"]
    if role == "tool":
        return ""
    if role == "assistant" and m.get("tool_calls"):
        content = (m.get("content") or "").strip()
        if not content:
            return ""
        return "assistant: " + _indent(content)
    content = m.get("content") or ""
    if not content.strip():
        return ""
    cap = RAW_CONTENT_CAP
    if len(content) > cap:
        content = content[:cap] + f"\n  …[截断 {len(content) - cap} 字符]"
    return f"{role}: " + _indent(content)


class ContextDiffer:
    """on_llm_response：DB 全量读取 + count 推进，新增消息原文落当日 md。"""

    def __init__(
        self,
        store,
        daily_file_for,
        lock: asyncio.Lock,
        think_cap: int = THINK_CAP,
        tool_cap: int = TOOL_RESULT_CAP,
        context=None,
    ):
        self.store = store
        self.daily_file_for = daily_file_for
        self.lock = lock
        self.think_cap = think_cap
        self.tool_cap = tool_cap
        self.context = context

    def _render(self, m: dict) -> str:
        return render_msg(m, tool_cap=self.tool_cap)

    async def _fetch_full(self, session_id: str) -> list[dict]:
        """DB 全量读取（与 digest 的 _db_full_normalized 同路数）。"""
        if self.context is None:
            return []
        try:
            from .digest_worker import session_to_db_user_id

            db = self.context.get_db()
            rows = await db.get_conversations(
                user_id=session_to_db_user_id(session_id)
            )
            if not rows:
                return []
            latest = max(rows, key=lambda r: r.updated_at)
            conv = await db.get_conversation_by_id(latest.conversation_id)
            if conv is None:
                return []
            raw = conv.content or "[]"
            if isinstance(raw, str):
                raw = json.loads(raw)
            return normalize(raw, think_cap=self.think_cap)
        except Exception as e:
            logger.warning(f"simple_memory 读取会话失败 {session_id[:16]}: {e}")
            return []

    async def process(self, session_id: str) -> None:
        msgs = await self._fetch_full(session_id)
        if not msgs:
            from .main import _dbg
            _dbg(f"_fetch_full empty session={session_id[:24]}")
            return
        entry = await self.store.get(session_id)
        snap = entry.get("snapshot") or {}
        count = int(snap.get("count") or 0)

        if count == 0:
            await self.store.update(
                session_id,
                snapshot={"count": len(msgs), "last_fp": msgs[-1]["fp"]},
            )
            return
        if (
            len(msgs) == count
            and msgs[-1]["fp"] == (snap.get("last_fp") or "")
        ):
            return

        if len(msgs) > count:
            new = msgs[count:]
        else:
            seen = _load_seen_fps(
                self.daily_file_for(session_id).path_for(datetime.now())
            )
            new = [m for m in msgs if m["fp"] not in seen]

        summary = None
        raw: list[dict] = []
        for m in new:
            role, content = m["role"], m.get("content") or ""
            if role == "system":
                continue
            if role == "user" and content.startswith(SUMMARY_PREFIX):
                summary = content[len(SUMMARY_PREFIX):].strip()
                continue
            if role == "assistant" and content.strip() == ACK_TEXT:
                continue
            raw.append(m)
        if summary is None and msgs:
            first = msgs[0]
            if first.get("role") == "user":
                fc = first.get("content") or ""
                if isinstance(fc, str) and fc.startswith(SUMMARY_PREFIX):
                    cand = fc[len(SUMMARY_PREFIX):].strip()
                    states = entry.get("summary_states") or []
                    if not states or states[-1]["text"] != cand:
                        summary = cand

        if raw:
            async with self.lock:
                path = self.daily_file_for(session_id).path_for(datetime.now())
                seen = _load_seen_fps(path)
                fresh = raw if not seen else [m for m in raw if m["fp"] not in seen]
                skipped = len(raw) - len(fresh)
                pairs = [(m["fp"], self._render(m)) for m in fresh]
                pairs = [(fp, s) for fp, s in pairs if s]
                if pairs:
                    lines = [f"## [{session_id[:24]}]"]
                    lines.extend(s for _, s in pairs)
                    lines.append(FPS_PREFIX + " ".join(fp for fp, _ in pairs) + " -->")
                    self.daily_file_for(session_id).append_to(path, "\n".join(lines))
                    logger.info(
                        f"simple_memory 原文落盘 {session_id[:16]}: "
                        f"new={len(pairs)} dedup={skipped}"
                    )
                elif skipped:
                    logger.info(
                        f"simple_memory {session_id[:16]} "
                        f"fp 全部命中去重 {skipped} 条，不落盘"
                    )
        if summary and (
            not entry.get("summary_states")
            or entry["summary_states"][-1]["text"] != summary
        ):
            states = entry.get("summary_states") or []
            states.append({"ts": int(time.time()), "text": summary})
            await self.store.update(
                session_id,
                summary=summary,
                summary_states=states,
                last_compress_ts=int(time.time()),
            )
            logger.info(f"simple_memory 捕获新摘要 {session_id[:16]}: {len(summary)} 字")
            async with self.lock:
                now = datetime.now()
                tag = now.strftime("%H:%M")
                sum_path = self.daily_file_for(session_id).summary_path_for(now)
                self.daily_file_for(session_id).append_to(
                    sum_path,
                    f"## [压缩 {tag}]\n{summary}\n",
                )
        await self.store.update(
            session_id,
            snapshot={"count": len(msgs), "last_fp": msgs[-1]["fp"]},
        )
