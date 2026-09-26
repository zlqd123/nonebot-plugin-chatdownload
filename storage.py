import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .config import (
    TXT_DIR, ALL_DIR, ERROR_PATH,
    CHECKPOINT_PATH, PENDING_PATH, INDEX_PATH, META,
)

TZ_CN = timezone(timedelta(hours=8))

# ============ index ============
_index_cache: set = set()


def load_index():
    global _index_cache
    if INDEX_PATH.exists():
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            _index_cache = set(json.load(f))
    else:
        _index_cache = set()


def get_index() -> set:
    return _index_cache


def add_index(message_id: int):
    if message_id not in _index_cache:
        _index_cache.add(message_id)
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(_index_cache), f)


# ============ checkpoint ============
def read_checkpoint() -> dict | None:
    if not CHECKPOINT_PATH.exists():
        return None
    with open(CHECKPOINT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def write_checkpoint(user_id: str, message_seq: int, message_id: int, time: int):
    data = {
        "user_id": user_id,
        "last_message_seq": message_seq,
        "last_message_id": message_id,
        "last_time": time,
        "updated_at": datetime.now(TZ_CN).isoformat(),
    }
    with open(CHECKPOINT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def delete_checkpoint():
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()


# ============ pending ============
def append_pending(message: dict):
    with open(PENDING_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(message, ensure_ascii=False) + "\n")


def read_pending() -> list[dict]:
    if not PENDING_PATH.exists():
        return []
    items = []
    with open(PENDING_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except Exception:
                    pass
    return items


def delete_pending():
    if PENDING_PATH.exists():
        PENDING_PATH.unlink()


# ============ all（按消息时间分周） ============
def get_week_file(ts: float | int | None = None) -> Path:
    """ts 为消息时间戳（秒），None/非法值表示用当前时间。"""
    if isinstance(ts, (int, float)) and ts > 0:
        dt = datetime.fromtimestamp(ts, TZ_CN)
    else:
        dt = datetime.now(TZ_CN)
    year, week, _ = dt.isocalendar()
    return ALL_DIR / f"{year}-W{week:02d}.jsonl"


def write_all(message: dict):
    if not META["save_all"]:
        return
    with open(get_week_file(message.get("time")), "a", encoding="utf-8") as f:
        f.write(json.dumps(message, ensure_ascii=False) + "\n")


# ============ txt ============
def _txt_sort_key(p: Path) -> int:
    """按文件名末尾的数字排序（lexicographic 会在 >999 时错乱）。"""
    m = re.search(r"_(\d+)\.txt$", p.name)
    return int(m.group(1)) if m else 0


def append_text(category: str, line: str):
    max_bytes = META["txt_max_size_kb"] * 1024
    files = sorted(TXT_DIR.glob(f"{category}_*.txt"), key=_txt_sort_key)
    current = files[-1] if files else TXT_DIR / f"{category}_001.txt"

    if current.exists() and current.stat().st_size + len(line.encode("utf-8")) > max_bytes:
        num = _txt_sort_key(current) + 1
        current = TXT_DIR / f"{category}_{num:03d}.txt"

    with open(current, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ============ error ============
def log_error(message_id: int, url: str, ftype: str, error: str, reason: str = ""):
    entry = {
        "time": datetime.now(TZ_CN).isoformat(),
        "message_id": message_id,
        "url": url,
        "type": ftype,
        "error": error,
        "reason": reason,
    }
    with open(ERROR_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
