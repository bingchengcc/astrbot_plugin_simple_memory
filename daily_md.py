from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_DIGEST_TIME = "23:30"


def parse_digest_time(s: str) -> tuple[int, int]:
    h, m = (s or DEFAULT_DIGEST_TIME).strip().split(":")
    return int(h), int(m)


def cycle_file_date(now: datetime, digest_time: str = DEFAULT_DIGEST_TIME) -> str:
    """当前所处总结周期的文件名日期。

    一天 = 上次总结时刻 → 本次总结时刻：
    now 在当日 digest 时刻之前 → 归当日文件（今晚总结）；
    now 在当日 digest 时刻之后 → 归次日文件（明晚总结）。
    """
    h, m = parse_digest_time(digest_time)
    digest_point = now.replace(hour=h, minute=m, second=0, microsecond=0)
    day = now.date() if now < digest_point else now.date() + timedelta(days=1)
    return day.isoformat()


class DailyFile:
    """memory/YYYY-MM-DD.md 的追加写入（纯追加不重写）。"""

    def __init__(self, memory_dir: Path, digest_time: str = DEFAULT_DIGEST_TIME):
        self.memory_dir = memory_dir
        self.digest_time = digest_time

    def path_for(self, now: datetime) -> Path:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        return self.memory_dir / f"{cycle_file_date(now, self.digest_time)}.md"

    def path_for_date(self, day: str) -> Path:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        return self.memory_dir / f"{day}.md"

    def append_to(self, path: Path, block: str) -> Path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(block)
            if not block.endswith("\n"):
                f.write("\n")
        return path

    def append(self, now: datetime, block: str) -> Path:
        return self.append_to(self.path_for(now), block)
