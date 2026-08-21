import time

from astrbot.api.star import StarTools


def _dbg(msg: str, tag: str = "") -> None:
    try:
        prefix = f"[{tag}] " if tag else ""
        with open(
            StarTools.get_data_dir("astrbot_plugin_lite_memory")
            / "debug.log",
            "a",
            encoding="utf-8",
        ) as f:
            f.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {prefix}{msg}" + chr(10)
            )
    except Exception:
        pass
