"""本地存储 —— 与原 nonebot 版 storage.py 等价。

index / checkpoint / pending / all（按周 JSONL）/ txt（按行数滚动）/ error。
"""

import json
import re
from datetime import datetime, timedelta, timezone

from . import config

TZ_CN = timezone(timedelta(hours=8))

# ============ index ============
_index_cache: set = set()


def load_index():
    global _index_cache
    if config.INDEX_PATH.exists():
        with open(config.INDEX_PATH, "r", encoding="utf-8") as f:
            _index_cache = set(json.load(f))
    else:
        _index_cache = set()


def get_index() -> set:
    return _index_cache


def add_index(message_id):
    if message_id in _index_cache:
        return
    _index_cache.add(message_id)
    # message_id 可能是 str 或 int（MaiBot 侧不一定统一），排序键统一转字符串
    with open(config.INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(_index_cache, key=lambda x: str(x)), f, ensure_ascii=False)


# ============ checkpoint ============
def read_checkpoint() -> dict | None:
    if not config.CHECKPOINT_PATH.exists():
        return None
    with open(config.CHECKPOINT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def write_checkpoint(user_id: str, message_seq, message_id, time: int):
    data = {
        "user_id": user_id,
        "last_message_seq": message_seq,
        "last_message_id": message_id,
        "last_time": time,
        "updated_at": datetime.now(TZ_CN).isoformat(),
    }
    with open(config.CHECKPOINT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def delete_checkpoint():
    if config.CHECKPOINT_PATH.exists():
        config.CHECKPOINT_PATH.unlink()


# ============ pending ============
def append_pending(message: dict):
    with open(config.PENDING_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(message, ensure_ascii=False) + "\n")


def read_pending() -> list[dict]:
    if not config.PENDING_PATH.exists():
        return []
    items = []
    with open(config.PENDING_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except Exception:
                    pass
    return items


def delete_pending():
    if config.PENDING_PATH.exists():
        config.PENDING_PATH.unlink()


# ============ all（按消息时间分周） ============
# 本进程内已写入 all 的 message_id：实时监听与补抓页可能重叠，防止同一消息写两遍
_all_written: set = set()


def get_week_file(ts=None):
    """ts 为消息时间戳（秒），None/非法值表示用当前时间。"""
    if isinstance(ts, (int, float)) and ts > 0:
        dt = datetime.fromtimestamp(ts, TZ_CN)
    else:
        dt = datetime.now(TZ_CN)
    year, week, _ = dt.isocalendar()
    return config.ALL_DIR / f"{year}-W{week:02d}.jsonl"


def write_all(message: dict):
    if not config.get("save_all"):
        return
    mid = message.get("message_id")
    if mid is not None:
        if mid in _all_written:
            return
        _all_written.add(mid)
    with open(get_week_file(message.get("time")), "a", encoding="utf-8") as f:
        f.write(json.dumps(message, ensure_ascii=False) + "\n")


# ============ txt ============
def _txt_sort_key(p) -> int:
    """按文件名末尾的数字排序（lexicographic 会在 >999 时错乱）。"""
    m = re.search(r"_(\d+)\.txt$", p.name)
    return int(m.group(1)) if m else 0


def append_text(line: str, prefix: str = "all"):
    """prefix="all" 写 all.txt / all_NNN.txt；prefix 传分类名时写 {category}_NNN.txt。

    txt_max_lines <= 0 时不滚动，全部追加到单个 {prefix}.txt。
    """
    max_lines = config.get("txt_max_lines")
    if isinstance(max_lines, int) and max_lines <= 0:
        current = config.TXT_DIR / f"{prefix}.txt"
    else:
        files = sorted(config.TXT_DIR.glob(f"{prefix}_*.txt"), key=_txt_sort_key)
        current = files[-1] if files else config.TXT_DIR / f"{prefix}_001.txt"

        if current.exists():
            with open(current, "r", encoding="utf-8") as f:
                line_count = sum(1 for _ in f)
            if line_count >= max_lines:
                num = _txt_sort_key(current) + 1
                current = config.TXT_DIR / f"{prefix}_{num:03d}.txt"

    with open(current, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ============ error ============
def log_error(message_id, url: str, ftype: str, error: str, reason: str = ""):
    entry = {
        "time": datetime.now(TZ_CN).isoformat(),
        "message_id": message_id,
        "url": url,
        "type": ftype,
        "error": error,
        "reason": reason,
    }
    with open(config.ERROR_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
