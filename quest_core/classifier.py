"""文本分类 — 逻辑与原 nonebot 版 classifier 一致（从 __pycache__ 恢复）。

优先级：links > japanese > numbers > other
"""

import re

# 日文假名（平假名/片假名）
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
# 链接：协议头 或 裸域名（xxx.xxx）
_LINK_RE = re.compile(r"(https?://|ftp://|mqqapi://|[a-zA-Z0-9\-]+\.[a-zA-Z]{2,})")
# 字母 / 汉字 / 假名 —— 出现任意一个即不算纯数字
_WORD_RE = re.compile(r"[a-zA-Z\u4e00-\u9fff\u3040-\u30ff]")
_DIGIT_RE = re.compile(r"\d")

DEFAULT_JAPANESE_RATIO = 0.3


def is_japanese(text: str, ratio: float = DEFAULT_JAPANESE_RATIO) -> bool:
    """假名占比 >= ratio 判定为日文。"""
    if not text:
        return False
    return len(_KANA_RE.findall(text)) / len(text) >= ratio


def is_number(text: str) -> bool:
    """至少含一个数字，且不含字母/汉字/假名。符号、空格、换行不限。"""
    if not _DIGIT_RE.search(text):
        return False
    return not _WORD_RE.search(text)


def is_link(text: str) -> bool:
    return bool(_LINK_RE.search(text))


def get_category(text: str, japanese_ratio: float = DEFAULT_JAPANESE_RATIO) -> str:
    if is_link(text):
        return "links"
    if is_japanese(text, japanese_ratio):
        return "japanese"
    if is_number(text):
        return "numbers"
    return "other"
