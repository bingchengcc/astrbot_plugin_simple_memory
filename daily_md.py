from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_DIGEST_TIME = "23:30"
RAW_ROTATE_BYTES = 32 * 1024  # 32KB


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
    """memory/YYYY-MM-DD/raw.md 的追加写入（纯追加不重写，超 32KB 自动开 raw_N.md）。"""

    def __init__(self, memory_dir: Path, digest_time: str = DEFAULT_DIGEST_TIME):
        self.memory_dir = memory_dir
        self.digest_time = digest_time

    def day_dir_for(self, now: datetime) -> Path:
        d = self.memory_dir / cycle_file_date(now, self.digest_time)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def day_dir_for_date(self, day: str) -> Path:
        d = self.memory_dir / day
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path_for(self, now: datetime) -> Path:
        d = self.day_dir_for(now)
        return self._active_raw(d)

    def path_for_date(self, day: str) -> Path:
        d = self.day_dir_for_date(day)
        return self._active_raw(d)

    def _active_raw(self, d: Path) -> Path:
        """找到当前应写入的 raw 文件（log rotation）。"""
        idx = 1
        while True:
            p = d / f"raw{'' if idx == 1 else '_' + str(idx)}.md"
            if not p.exists():
                return p
            if p.stat().st_size < RAW_ROTATE_BYTES:
                return p
            idx += 1

    def summary_path_for(self, now: datetime) -> Path:
        return self.day_dir_for(now) / "summary.md"

    def summary_path_for_date(self, day: str) -> Path:
        return self.day_dir_for_date(day) / "summary.md"

    def raw_files_for_date(self, day: str) -> list[Path]:
        """某天所有 raw 文件（raw.md, raw_2.md, ...）。"""
        d = self.memory_dir / day
        if not d.is_dir():
            return []
        return sorted(d.glob("raw*.md"))

    def append_to(self, path: Path, block: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(block)
            if not block.endswith("\n"):
                f.write("\n")
        return path

    def append(self, now: datetime, block: str) -> Path:
        return self.append_to(self.path_for(now), block)
