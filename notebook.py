import re

NOTE_ENTRY_RE = re.compile(r"^(\d+)\. \[([^\]]+)\] (.*)$")


def parse_entries(text: str) -> list[dict]:
    entries = []
    for line in text.splitlines():
        m = NOTE_ENTRY_RE.match(line.strip())
        if m:
            entries.append(
                {
                    "num": int(m.group(1)),
                    "ts": m.group(2),
                    "content": m.group(3),
                }
            )
    return entries


def next_num(text: str) -> int:
    entries = parse_entries(text)
    return max((e["num"] for e in entries), default=0) + 1


def find_dup_num(text: str, content: str) -> int:
    target = content.strip()
    if not target:
        return 0
    for e in parse_entries(text):
        if e["content"].strip() == target:
            return e["num"]
    return 0


def _join(lines: list[str], had_trailing_newline: bool) -> str:
    out = "\n".join(lines)
    if had_trailing_newline:
        out += "\n"
    return out


def append_text(text: str, content: str, ts: str) -> tuple[str, int]:
    num = next_num(text)
    line = f"{num}. [{ts}] {content.strip()}"
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n", num


def edit_text(text: str, num: int, content: str) -> tuple[str, bool]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = NOTE_ENTRY_RE.match(line.strip())
        if m and int(m.group(1)) == int(num):
            lines[i] = f"{int(num)}. [{m.group(2)}] {content.strip()}"
            return _join(lines, text.endswith("\n")), True
    return text, False


def renumber_text(text: str) -> str:
    lines = text.splitlines()
    n = 0
    out = []
    for line in lines:
        m = NOTE_ENTRY_RE.match(line.strip())
        if m:
            n += 1
            out.append(f"{n}. [{m.group(2)}] {m.group(3)}")
        else:
            out.append(line)
    return _join(out, text.endswith("\n"))


def delete_text(text: str, num: int) -> tuple[str, bool]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = NOTE_ENTRY_RE.match(line.strip())
        if m and int(m.group(1)) == int(num):
            del lines[i]
            return _join(lines, text.endswith("\n")), True
    return text, False
