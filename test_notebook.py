"""notebook.py 纯函数单测（11.1 风格，不依赖 AstrBot）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from notebook import append_text, delete_text, edit_text, next_num, parse_entries

HEADER = "# 长期记忆\n\n<!-- 在这里写需要长期记住的内容 -->\n"


def test_parse_skips_non_entries():
    assert parse_entries(HEADER) == []
    assert parse_entries(HEADER + "1. [2026-08-16] 甲\n") == [
        {"num": 1, "ts": "2026-08-16", "content": "甲"}
    ]
    text = "1. [2026-08-16 05:53] 甲\n2. [2026-08-16] 乙\n"
    entries = parse_entries(text)
    assert [e["num"] for e in entries] == [1, 2]
    assert entries[0]["ts"] == "2026-08-16 05:53"
    assert entries[1]["content"] == "乙"


def test_next_num():
    assert next_num("") == 1
    assert next_num(HEADER) == 1
    assert next_num("1. [t] a\n2. [t] b\n") == 3
    assert next_num("3. [t] c\n") == 4


def test_append():
    new, num = append_text(HEADER, " 甲 ", "2026-08-16 06:15")
    assert num == 1
    assert new.endswith("1. [2026-08-16 06:15] 甲\n")
    new2, num2 = append_text(new, "乙", "2026-08-16 06:16")
    assert num2 == 2
    assert "2. [2026-08-16 06:16] 乙\n" in new2
    assert parse_entries(new2)[0]["content"] == "甲"
    # 无尾换行的旧文本
    new3, num3 = append_text("1. [t] a", "c", "now")
    assert num3 == 2
    assert new3 == "1. [t] a\n2. [now] c\n"


def test_edit_keeps_num_and_ts():
    text = "1. [2026-08-16 05:53] 旧\n2. [t] 乙\n"
    new, hit = edit_text(text, 1, "新")
    assert hit
    assert new.startswith("1. [2026-08-16 05:53] 新\n")
    assert "2. [t] 乙\n" in new
    assert edit_text(text, 9, "x")[1] is False


def test_delete_leaves_gap():
    text = "1. [t] a\n2. [t] b\n3. [t] c\n"
    new, hit = delete_text(text, 2)
    assert hit
    assert new == "1. [t] a\n3. [t] c\n"
    assert next_num(new) == 4
    assert delete_text(text, 9)[1] is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} ok")
    print("notebook 单测全部通过")
