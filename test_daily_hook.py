"""daily_hook.py 单测：count 推进 / 压缩 fallback / 孤儿闭标签清洗（fake store/context，不依赖 AstrBot 运行时）"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astrbot_plugin_openclaw_memory.daily_hook import ContextDiffer
from astrbot_plugin_openclaw_memory.daily_md import DailyFile

SID = "webchat:FriendMessage:test"
S = "Our previous history conversation summary: 摘要"
S2 = "Our previous history conversation summary: 摘要2"


class FakeStore:
    def __init__(self):
        self.data = {}

    def _empty(self):
        return {
            "summary": "",
            "summary_states": [],
            "snapshot": {"count": 0},
        }

    async def get(self, sid):
        return self.data.get(sid) or self._empty()

    async def update(self, sid, **fields):
        self.data.setdefault(sid, self._empty()).update(fields)


class Row:
    def __init__(self, updated_at):
        self.updated_at = updated_at
        self.conversation_id = 1


class Conv:
    def __init__(self, content):
        self.conversation_id = 1
        self.content = content


class FakeDB:
    def __init__(self):
        self.msgs: list = []

    async def get_conversations(self, user_id):
        return [Row("2026-08-17T10:00:00")]

    async def get_conversation_by_id(self, conversation_id):
        return Conv(json.dumps(self.msgs, ensure_ascii=False))


def _differ(td):
    df = DailyFile(Path(td), "21:45")
    db = FakeDB()
    ctx = type("FakeContext", (), {"get_db": lambda self: db})()
    differ = ContextDiffer(
        FakeStore(), lambda s: df, asyncio.Lock(), context=ctx
    )
    return differ, db


def _set(db, *pairs):
    db.msgs = [{"role": r, "content": t} for r, t in pairs]


def test_count_advancement():
    """基线不落盘；后续增量只落一次，重复触发无副作用。"""
    async def run():
        with tempfile.TemporaryDirectory() as td:
            d, db = _differ(td)
            _set(db, ("user", "消息甲"), ("assistant", "回复乙"))
            await d.process(SID)
            assert not list(Path(td).iterdir()), "基线不应落盘"
            _set(
                db,
                ("user", "消息甲"),
                ("assistant", "回复乙"),
                ("user", "消息丙"),
                ("assistant", "回复丁"),
            )
            await d.process(SID)
            await d.process(SID)
            f = Path(td) / "2026-08-17.md"
            text = f.read_text(encoding="utf-8")
            assert "消息甲" not in text
            assert text.count("消息丙") == 1
            assert text.count("回复丁") == 1
    asyncio.run(run())


def test_fp_dedup_storm():
    """压缩 fallback：已写消息不重写，新消息只落一次，摘要进 summary_states。"""
    async def run():
        with tempfile.TemporaryDirectory() as td:
            d, db = _differ(td)
            _set(db, ("user", "消息甲"), ("assistant", "回复乙"))
            await d.process(SID)
            _set(
                db,
                ("user", "消息甲"),
                ("assistant", "回复乙"),
                ("assistant", "回复丙"),
            )
            await d.process(SID)
            _set(db, ("user", S), ("assistant", "回复丙"))
            await d.process(SID)
            _set(
                db,
                ("user", S),
                ("assistant", "回复丙"),
                ("user", "消息丁"),
                ("assistant", "回复戊"),
            )
            await d.process(SID)
            _set(db, ("user", S2), ("assistant", "回复戊"))
            await d.process(SID)
            f = Path(td) / "2026-08-17.md"
            text = f.read_text(encoding="utf-8")
            assert text.count("回复丙") == 1, text
            assert text.count("消息丁") == 1, text
            assert "回复戊" in text
            assert "摘要2" not in text
            store = d.store.data[SID]
            assert store["summary_states"][-1]["text"] == "摘要2"
            assert store["snapshot"]["count"] == 2
    asyncio.run(run())


def test_close_tags_stripped():
    """孤儿闭标签不进原文。"""
    async def run():
        with tempfile.TemporaryDirectory() as td:
            d, db = _differ(td)
            _set(db, ("user", "hi"))
            await d.process(SID)
            _set(
                db,
                ("user", "hi"),
                ("assistant", "hi2"),
                ("user", "u3"),
                ("assistant", "回答</parameter></function></invoke> 结尾"),
            )
            await d.process(SID)
            text = (Path(td) / "2026-08-17.md").read_text(encoding="utf-8")
            assert "</parameter>" not in text
            assert "</function>" not in text
            assert "</invoke>" not in text
            assert "回答" in text
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} ok")
    print("daily_hook 单测全部通过")
